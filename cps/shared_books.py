# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Read-only shared-book catalogue across registered Calibre libraries."""

from dataclasses import dataclass
from pathlib import Path
import os
import sqlite3
import stat
from urllib.parse import quote

from sqlalchemy import and_, false, or_
from sqlalchemy.orm import joinedload

from . import constants, ub


@dataclass(frozen=True)
class SharedBookCard:
    share_id: int
    title: str
    authors: str
    available: bool
    direction: str
    source_library: str
    target_library: str
    displayed_library: str
    shared_by: str
    created_at: object


@dataclass(frozen=True)
class SharedBooksPage:
    items: list
    total: int
    page: int
    per_page: int


def _accessible_library_ids(app_session, user):
    return {
        library_id
        for (library_id,) in app_session.query(ub.LibraryMembership.library_id)
        .filter(ub.LibraryMembership.user_id == user.id)
        .all()
    }


def _visible_shares_query(app_session, user, accessible_library_ids):
    query = app_session.query(ub.BookShare).options(
        joinedload(ub.BookShare.source_library),
        joinedload(ub.BookShare.target_library),
    )
    if not accessible_library_ids:
        return query.filter(false())
    return query.filter(or_(
        ub.BookShare.target_library_id.in_(accessible_library_ids),
        and_(
            ub.BookShare.shared_by_user_id == user.id,
            ub.BookShare.source_library_id.in_(accessible_library_ids),
        ),
    ))


def _path_identity(path):
    try:
        item = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    return item.st_dev, item.st_ino, item.st_mode


def _safe_metadata_path(library):
    if library is None or library.status != "active":
        return None
    registered_root = os.path.abspath(library.root_path)
    root_identity = _path_identity(registered_root)
    if root_identity is None or not stat.S_ISDIR(root_identity[2]):
        return None
    if os.path.realpath(registered_root) != registered_root:
        return None

    if library.kind == "personal":
        try:
            from .library_control import managed_library_path

            expected_root = managed_library_path(
                library.public_id,
                constants.CALIBRE_LIBRARIES_ROOT,
            )
        except (OSError, ValueError):
            return None
        if registered_root != expected_root:
            return None

    candidate = os.path.join(registered_root, "metadata.db")
    metadata_identity = _path_identity(candidate)
    if metadata_identity is None or not stat.S_ISREG(metadata_identity[2]):
        return None
    resolved = os.path.realpath(candidate)
    try:
        contained = os.path.commonpath((registered_root, resolved)) == registered_root
    except ValueError:
        return None
    if not contained or resolved != candidate:
        return None
    return candidate, root_identity, metadata_identity


def _read_book_metadata(library, book_id):
    safe_path = _safe_metadata_path(library)
    if safe_path is None:
        return None
    metadata_path, root_identity, metadata_identity = safe_path

    uri = "file:{}?mode=ro&immutable=1".format(
        quote(str(Path(metadata_path)), safe="/")
    )
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            if (
                _path_identity(os.path.abspath(library.root_path)) != root_identity
                or _path_identity(metadata_path) != metadata_identity
            ):
                return None
            book_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(books)")
            }
            if not {"id", "title"}.issubset(book_columns):
                return None
            optional = [
                column
                for column in ("author_sort", "path", "has_cover")
                if column in book_columns
            ]
            selected = ", ".join(["id", "title"] + optional)
            row = connection.execute(
                "SELECT {} FROM books WHERE id = ?".format(selected),
                (int(book_id),),
            ).fetchone()
            if row is None:
                return None

            authors = row["author_sort"] if "author_sort" in row.keys() else ""
            tables = {
                item[0]
                for item in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if {"authors", "books_authors_link"}.issubset(tables):
                author_rows = connection.execute("""
                    SELECT authors.name
                    FROM authors
                    JOIN books_authors_link
                        ON books_authors_link.author = authors.id
                    WHERE books_authors_link.book = ?
                    ORDER BY authors.name
                """, (int(book_id),)).fetchall()
                if author_rows:
                    authors = " & ".join(item[0] for item in author_rows if item[0])
            result = {
                "title": row["title"] or "Unknown title",
                "authors": authors or "Unknown author",
            }
            if (
                _path_identity(os.path.abspath(library.root_path)) != root_identity
                or _path_identity(metadata_path) != metadata_identity
            ):
                return None
            return result
        finally:
            connection.close()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None


def get_shared_books_page(app_session, user, page=1, per_page=24):
    """Return only shares the user may inspect, with cross-library metadata."""
    page = max(1, int(page))
    per_page = max(1, min(100, int(per_page)))
    accessible = _accessible_library_ids(app_session, user)
    query = _visible_shares_query(app_session, user, accessible)
    total = query.count()
    shares = query.order_by(ub.BookShare.created_at.desc(), ub.BookShare.id.desc()).offset(
        (page - 1) * per_page
    ).limit(per_page).all()

    cards = []
    for share in shares:
        target_access = share.target_library_id in accessible
        source_access = (
            share.source_library_id is not None
            and share.source_library_id in accessible
        )
        if target_access:
            displayed_library = share.target_library
            displayed_book_id = share.target_book_id
        elif share.shared_by_user_id == user.id and source_access:
            displayed_library = share.source_library
            displayed_book_id = share.source_book_id
        else:  # Defensive: the SQL visibility policy should make this unreachable.
            continue

        metadata = _read_book_metadata(displayed_library, displayed_book_id)
        source_name = (
            share.source_library.name
            if share.source_library is not None
            else share.source_library_name
        )
        target_name = (
            share.target_library.name
            if share.target_library is not None
            else "Unavailable library"
        )
        cards.append(SharedBookCard(
            share_id=share.id,
            title=metadata["title"] if metadata else "Unavailable shared book",
            authors=metadata["authors"] if metadata else "Metadata is not available",
            available=metadata is not None,
            direction=(
                "outgoing"
                if share.shared_by_user_id == user.id
                else "incoming"
            ),
            source_library=source_name or "Unknown library",
            target_library=target_name,
            displayed_library=(
                displayed_library.name
                if displayed_library is not None
                else "Unavailable library"
            ),
            shared_by=share.shared_by_name or "Deleted user",
            created_at=share.created_at,
        ))
    return SharedBooksPage(cards, total, page, per_page)


__all__ = ["SharedBookCard", "SharedBooksPage", "get_shared_books_page"]
