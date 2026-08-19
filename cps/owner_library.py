# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Per-user multi-owner book visibility (approach B).

A single physical Calibre library is shared by all users. Ownership is stored in
a multi-value TEXT custom column ``#owner`` (label ``owner``) whose values are the
integer IDs of the owning ``ub.User`` rows. Visibility is enforced by the existing
``cps.db.CalibreDB.common_filters`` restricted-column mechanism:

* ``config.config_restricted_column`` is set to the ``#owner`` column id (globally).
* each non-admin user's ``allowed_column_value`` is their own ``str(user.id)``.
* admins keep an empty ``allowed_column_value`` and therefore see every book.

Because ``common_filters`` uses ``custom_column_N.any(value.in_(pos_cc_list))`` a book
is visible to a user iff its ``#owner`` list contains that user's id — so a book may
have several owners (sharing), and each owner keeps their own per-user reading state
(all keyed on ``book_id`` in app.db, which stays globally unique under a single
physical library).

The ``#owner`` column itself is created at boot by ``scripts/auto_library.py`` (raw
SQL, no Calibre binaries). This module only reads/writes ownership and drives the
isolation toggle; it is used from the Flask process only. The ingest worker tags new
books with their owner directly via sqlite3 (see ``scripts/ingest_processor.py``).
"""

import os

from . import logger

log = logger.create()

OWNER_LABEL = "owner"


# --------------------------------------------------------------------------- #
# Column lookup
# --------------------------------------------------------------------------- #
def get_owner_column(session=None):
    """Return the CustomColumns row for the ``#owner`` column, or None."""
    from . import calibre_db, db
    session = session or calibre_db.session
    if session is None:
        return None
    try:
        return session.query(db.CustomColumns).filter(
            db.CustomColumns.label == OWNER_LABEL
        ).first()
    except Exception as error:  # pragma: no cover - defensive
        log.debug("get_owner_column failed: %s", error)
        return None


def owner_column_id(session=None):
    col = get_owner_column(session)
    return col.id if col is not None else None


def _owner_attr(col_id):
    return "custom_column_" + str(col_id)


# --------------------------------------------------------------------------- #
# Isolation state
# --------------------------------------------------------------------------- #
def is_isolation_active(config=None):
    """True when per-user owner filtering is actually engaged.

    Filtering is engaged when ``config_restricted_column`` points at the ``#owner``
    column. This is the single source of truth used by ``common_filters``; the
    ``owner_isolation`` CWA setting is just the admin-facing switch that sets it.
    """
    if config is None:
        from . import config as _config
        config = _config
    restricted = getattr(config, "config_restricted_column", 0)
    if not restricted:
        return False
    return restricted == owner_column_id()


def is_admin(user):
    try:
        return bool(user.role_admin())
    except Exception:
        return False


def allowed_value_for_user(user, isolation_active=True):
    """The ``allowed_column_value`` a user should carry.

    Admins (and anyone, when isolation is off) get an empty value → they see every
    book. A normal user is restricted to books they own (their own id).
    """
    if not isolation_active or is_admin(user):
        return ""
    return str(user.id)


# --------------------------------------------------------------------------- #
# Reading/writing ownership (ORM, Flask side)
# --------------------------------------------------------------------------- #
def get_book_owner_ids(book, col=None):
    """Return the list of owner user-ids (ints) currently linked to ``book``."""
    from . import calibre_db
    col = col or get_owner_column()
    if col is None or book is None:
        return []
    values = getattr(book, _owner_attr(col.id), None) or []
    owners = []
    for entry in values:
        raw = getattr(entry, "value", None)
        try:
            owners.append(int(str(raw).strip()))
        except (TypeError, ValueError):
            continue
    return owners


def set_book_owners(book, owner_ids, col=None, commit=True):
    """Replace ``book``'s ``#owner`` list with exactly ``owner_ids`` (list of ints).

    Returns True if anything changed. Reuses the same value-diffing helper the
    edit-book UI uses for multi-value custom columns.
    """
    from . import calibre_db, db
    from . import editbooks  # lazy: editbooks imports many things
    col = col or get_owner_column()
    if col is None or book is None:
        return False
    # Deduplicate and stringify; drop blanks.
    wanted = []
    seen = set()
    for uid in owner_ids:
        s = str(uid).strip()
        if s and s not in seen:
            seen.add(s)
            wanted.append(s)
    changed = editbooks.modify_database_object(
        wanted,
        getattr(book, _owner_attr(col.id)),
        db.cc_classes[col.id],
        calibre_db.session,
        'custom',
    )
    if changed and commit:
        try:
            calibre_db.set_metadata_dirty(book.id)
        except Exception:
            pass
        calibre_db.session.commit()
    return changed


def add_book_owners(book, new_owner_ids, col=None, commit=True):
    """Add owner ids to a book without removing existing owners."""
    col = col or get_owner_column()
    if col is None or book is None:
        return False
    current = get_book_owner_ids(book, col)
    merged = current + [int(x) for x in new_owner_ids if str(x).strip()]
    return set_book_owners(book, merged, col, commit)


def remove_book_owner(book, owner_id, col=None, commit=True):
    """Remove one owner id. Returns (changed, remaining_owner_ids)."""
    col = col or get_owner_column()
    if col is None or book is None:
        return False, []
    current = get_book_owner_ids(book, col)
    remaining = [uid for uid in current if uid != int(owner_id)]
    if remaining == current:
        return False, current
    set_book_owners(book, remaining, col, commit)
    return True, remaining


def purge_user_from_all_books(user_id):
    """Remove a (deleted) user's id from every book's ``#owner`` list.

    Books left with no owners survive but become admin-only until reassigned.
    Best-effort; commits on the calibre session.
    """
    from . import calibre_db, db
    col = get_owner_column()
    if col is None:
        return 0
    value_cls = db.cc_classes.get(col.id)
    if value_cls is None:
        return 0
    try:
        value_row = calibre_db.session.query(value_cls).filter(
            value_cls.value == str(user_id)
        ).first()
        if value_row is None:
            return 0
        books = list(getattr(value_row, "books", []) or [])
        for book in books:
            links = getattr(book, _owner_attr(col.id))
            if value_row in links:
                links.remove(value_row)
        # Drop the now-orphaned value row so it doesn't linger.
        calibre_db.session.delete(value_row)
        calibre_db.session.commit()
        return len(books)
    except Exception as error:
        calibre_db.session.rollback()
        log.warning("purge_user_from_all_books(%s) failed: %s", user_id, error)
        return 0


# --------------------------------------------------------------------------- #
# User resolution / ingest path
# --------------------------------------------------------------------------- #
def resolve_username_to_user_id(username, session=None):
    from . import ub
    if not username:
        return None
    session = session or ub.session
    try:
        user = session.query(ub.User).filter(ub.User.name == username).one_or_none()
        return user.id if user is not None else None
    except Exception as error:  # pragma: no cover - defensive
        log.debug("resolve_username_to_user_id(%r) failed: %s", username, error)
        return None


def username_from_ingest_path(filepath, ingest_folder):
    """First path component under the ingest root (the per-user dropzone), or None."""
    if not filepath or not ingest_folder:
        return None
    try:
        rel = os.path.relpath(os.path.normpath(filepath), os.path.normpath(ingest_folder))
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    parts = rel.split(os.sep)
    if len(parts) < 2:
        return None  # file sat directly in the ingest root, no user subfolder
    first = parts[0].strip()
    if not first or first in (".", ".."):
        return None
    return first


# --------------------------------------------------------------------------- #
# Per-user state cleanup (used when a user unshares a still-owned book)
# --------------------------------------------------------------------------- #
def cleanup_user_book_state(user_id, book_id):
    """Delete one user's per-user state for a book they no longer own.

    Covers read status/progress, bookmarks, archived flag, Kobo synced/reading
    state and shelf membership on the user's own shelves. Best-effort; each step is
    guarded so one failure does not abort the rest. app.db only.
    """
    from . import ub
    session = ub.session
    if session is None:
        return
    steps = (
        lambda: session.query(ub.ReadBook).filter(
            ub.ReadBook.user_id == user_id, ub.ReadBook.book_id == book_id).delete(),
        lambda: session.query(ub.Bookmark).filter(
            ub.Bookmark.user_id == user_id, ub.Bookmark.book_id == book_id).delete(),
        lambda: session.query(ub.ArchivedBook).filter(
            ub.ArchivedBook.user_id == user_id, ub.ArchivedBook.book_id == book_id).delete(),
        lambda: session.query(ub.KoboSyncedBooks).filter(
            ub.KoboSyncedBooks.user_id == user_id, ub.KoboSyncedBooks.book_id == book_id).delete(),
        lambda: session.query(ub.KoboReadingState).filter(
            ub.KoboReadingState.user_id == user_id, ub.KoboReadingState.book_id == book_id).delete(),
        _cleanup_shelf_membership(session, user_id, book_id),
    )
    for step in steps:
        try:
            step()
        except Exception as error:
            session.rollback()
            log.debug("cleanup_user_book_state step failed (user=%s book=%s): %s",
                      user_id, book_id, error)
    try:
        session.commit()
    except Exception as error:
        session.rollback()
        log.warning("cleanup_user_book_state commit failed (user=%s book=%s): %s",
                    user_id, book_id, error)


def _cleanup_shelf_membership(session, user_id, book_id):
    from . import ub

    def _run():
        shelf_ids = [row.id for row in session.query(ub.Shelf.id).filter(
            ub.Shelf.user_id == user_id).all()]
        if shelf_ids:
            session.query(ub.BookShelf).filter(
                ub.BookShelf.book_id == book_id,
                ub.BookShelf.shelf.in_(shelf_ids)).delete(synchronize_session=False)
    return _run


# --------------------------------------------------------------------------- #
# Admin isolation toggle
# --------------------------------------------------------------------------- #
def apply_isolation(config, enable):
    """Engage or release per-user owner isolation.

    On enable: point ``config_restricted_column`` at the ``#owner`` column and give
    every non-admin user their own id as ``allowed_column_value`` (admins cleared so
    they keep seeing everything). On disable: clear the restricted column and every
    user's ``allowed_column_value`` so all books are visible to all users again.

    Returns (ok: bool, message: str).
    """
    from . import ub

    if enable:
        col_id = owner_column_id()
        if not col_id:
            return False, ("The #owner column does not exist yet. Restart the "
                           "container once so it can be created, then try again.")
        config.config_restricted_column = col_id
        config.save()
        _backfill_allowed_values(ub, isolation_active=True)
        return True, "Per-user ownership isolation enabled."
    else:
        config.config_restricted_column = 0
        config.save()
        _backfill_allowed_values(ub, isolation_active=False)
        return True, "Per-user ownership isolation disabled."


def _backfill_allowed_values(ub, isolation_active):
    """Set every non-anonymous user's allowed_column_value to match the mode."""
    try:
        users = ub.session.query(ub.User).all()
    except Exception as error:
        log.warning("Could not load users to backfill owner restriction: %s", error)
        return
    changed = False
    for user in users:
        try:
            if user.role_anonymous():
                continue
        except Exception:
            continue
        desired = allowed_value_for_user(user, isolation_active)
        if (user.allowed_column_value or "") != desired:
            user.allowed_column_value = desired
            changed = True
    if changed:
        try:
            ub.session_commit("Updated per-user owner restriction values")
        except Exception as error:
            log.warning("Could not commit owner restriction backfill: %s", error)


def owner_isolation_setting():
    """Read the admin-facing ``owner_isolation`` switch from cwa.db.

    Returns True/False, or None when it can't be read (treated as 'unknown' by
    callers, which then leave state untouched)."""
    try:
        import sys
        sys.path.insert(1, '/app/calibre-web-automated/scripts/')
        from cwa_db import CWA_DB
        return bool(CWA_DB().cwa_settings.get('owner_isolation', 0))
    except Exception as error:
        log.debug("Could not read owner_isolation setting: %s", error)
        return None


def reconcile_at_startup(config):
    """Keep per-user ownership self-consistent on boot.

    1. Ensure every non-anonymous user has an ingest dropzone (independent of the
       isolation toggle, so external automation can drop files immediately).
    2. If the admin switch ``owner_isolation`` is on, re-apply isolation so
       ``config_restricted_column`` tracks the current #owner column id (it may have
       changed if the column was recreated) and every user carries the right
       ``allowed_column_value``. If the column isn't ready yet the persisted config
       from the previous boot still applies, so filtering keeps working.
    """
    from . import ub
    # 1) Ingest dropzones for everyone.
    try:
        from . import library_control
        for user in ub.session.query(ub.User).all():
            try:
                if not user.role_anonymous():
                    library_control.ensure_user_ingest_dir(user)
            except Exception:
                continue
    except Exception as error:  # pragma: no cover - best effort
        log.debug("owner ingest-dir reconcile failed: %s", error)
    # 2) Re-apply the intended isolation state.
    try:
        if owner_isolation_setting() is True:
            ok, message = apply_isolation(config, True)
            if not ok:
                log.warning("Owner isolation is enabled but could not be applied at startup: %s", message)
    except Exception as error:  # pragma: no cover - best effort
        log.debug("owner isolation reconcile_at_startup failed: %s", error)
