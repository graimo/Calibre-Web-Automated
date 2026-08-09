# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Phase 1 control-plane provisioning for physical Calibre libraries."""

import os
import shutil
import subprocess
import threading
import uuid
from contextlib import contextmanager

from sqlalchemy.exc import IntegrityError

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback uses the process lock
    fcntl = None

from . import constants, logger, ub

log = logger.create()

LIBRARY_ROLES = frozenset(("viewer", "editor", "manager"))
_PROVISION_LOCK = threading.RLock()
_PROVISION_QUEUE_LOCK = threading.Lock()
_QUEUED_PROVISIONING = {}


class LibraryPolicyError(ValueError):
    """Raised when a requested control-plane change violates library policy."""


class LibraryCleanupError(OSError):
    """Raised after a DB deletion committed but quarantined files remain."""


def feature_enabled():
    return bool(constants.MULTI_LIBRARY_ENABLED)


def managed_library_path(public_id, managed_root=None):
    """Return a UUID-only child path contained beneath the managed root."""
    try:
        canonical_id = str(uuid.UUID(str(public_id)))
    except (TypeError, ValueError, AttributeError) as error:
        raise LibraryPolicyError("Invalid library public ID") from error

    root = os.path.realpath(managed_root or constants.CALIBRE_LIBRARIES_ROOT)
    candidate = os.path.abspath(os.path.join(root, canonical_id))
    resolved_candidate = os.path.realpath(candidate)
    expected_candidate = os.path.join(root, canonical_id)

    try:
        contained = os.path.commonpath((root, resolved_candidate)) == root
    except ValueError as error:
        raise LibraryPolicyError("Managed library path is not contained by its root") from error

    # Reject a pre-existing UUID child that is itself a symlink, even when its
    # target happens to remain beneath the root.
    if not contained or resolved_candidate != expected_candidate or os.path.islink(candidate):
        raise LibraryPolicyError("Managed library path escapes its configured root")
    return candidate


@contextmanager
def _library_operation_lock(public_id, managed_root=None):
    """Serialize lifecycle and Calibre writes across threads and processes."""
    canonical_id = str(uuid.UUID(str(public_id)))
    root = os.path.realpath(managed_root or constants.CALIBRE_LIBRARIES_ROOT)
    lock_dir = os.path.join(root, ".locks")
    os.makedirs(lock_dir, mode=0o750, exist_ok=True)
    if os.path.islink(lock_dir):
        raise LibraryPolicyError("Managed library lock directory must not be a symlink")
    lock_path = os.path.join(lock_dir, "{}.lock".format(canonical_id))

    with _PROVISION_LOCK:
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _calibre_debug_binary():
    configured = os.environ.get("CALIBRE_DEBUG_BINARY", "").strip()
    candidates = [configured]
    try:
        from . import config

        if config.config_binariesdir:
            candidates.append(os.path.join(config.config_binariesdir, "calibre-debug"))
    except (AttributeError, ImportError):
        pass
    candidates.extend(("/app/calibre/calibre-debug", shutil.which("calibre-debug") or ""))
    return next((path for path in candidates if path and os.path.isfile(path) and os.access(path, os.X_OK)), "")


def _metadata_path(root_path):
    root = os.path.realpath(root_path)
    metadata_path = os.path.join(root_path, "metadata.db")
    resolved_metadata = os.path.realpath(metadata_path)
    expected_metadata = os.path.join(root, "metadata.db")
    try:
        contained = os.path.commonpath((root, resolved_metadata)) == root
    except ValueError as error:
        raise LibraryPolicyError("Calibre metadata path is not contained by its library") from error
    if not contained or resolved_metadata != expected_metadata or os.path.islink(metadata_path):
        raise LibraryPolicyError("Calibre metadata path escapes its library")
    return metadata_path


def create_empty_calibre_library(root_path):
    """Initialize or validate metadata.db using Calibre's own Python runtime."""
    metadata_path = _metadata_path(root_path)

    binary = _calibre_debug_binary()
    if not binary:
        raise RuntimeError("calibre-debug is required to provision a physical Calibre library")

    os.makedirs(root_path, mode=0o750, exist_ok=True)
    environment = os.environ.copy()
    environment["CWA_PROVISION_LIBRARY_PATH"] = root_path
    command = [
        binary,
        "-c",
        "import os; from calibre.db.legacy import LibraryDatabase; "
        "LibraryDatabase(os.environ['CWA_PROVISION_LIBRARY_PATH'])",
    ]
    result = subprocess.run(
        command,
        check=False,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
    )
    if result.returncode:
        detail = (result.stderr or "Calibre initialization failed").strip()[-500:]
        raise RuntimeError(detail)
    metadata_path = _metadata_path(root_path)
    if not os.path.isfile(metadata_path):
        raise RuntimeError("Calibre did not create metadata.db")


def _is_anonymous(user):
    return not user or not getattr(user, "id", None) or bool(user.role_anonymous())


def _default_membership_exists(app_session, user_id):
    return app_session.query(ub.LibraryMembership).filter_by(
        user_id=user_id,
        is_default=True,
    ).first() is not None


def _ensure_default_membership(app_session, user_id):
    if _default_membership_exists(app_session, user_id):
        return
    replacement = app_session.query(ub.LibraryMembership).join(ub.Library).filter(
        ub.LibraryMembership.user_id == user_id,
    ).order_by(
        (ub.Library.status == "active").desc(),
        (ub.Library.kind == "personal").desc(),
        ub.LibraryMembership.created_at.asc(),
    ).first()
    if replacement:
        replacement.is_default = True


def _ensure_owner_membership(app_session, library, user_id):
    membership = app_session.query(ub.LibraryMembership).filter_by(
        library_id=library.id,
        user_id=user_id,
    ).one_or_none()
    if membership is None:
        membership = ub.LibraryMembership(
            library_id=library.id,
            user_id=user_id,
            role="manager",
            is_default=not _default_membership_exists(app_session, user_id),
        )
        app_session.add(membership)
    else:
        membership.role = "manager"
    return membership


def _provision_personal_library_unlocked(user, app_session=None, managed_root=None, initializer=None):
    """Create or retry one personal library for a non-anonymous user."""
    if _is_anonymous(user):
        return None

    app_session = app_session or ub.session
    initializer = initializer or create_empty_calibre_library
    library = app_session.query(ub.Library).filter_by(
        kind="personal",
        owner_user_id=user.id,
    ).one_or_none()

    if library is None:
        public_id = str(uuid.uuid4())
        library = ub.Library(
            public_id=public_id,
            slug="personal-{}".format(public_id),
            name="{}'s Library".format(user.name),
            kind="personal",
            owner_user_id=user.id,
            root_path=managed_library_path(public_id, managed_root),
            status="provisioning",
        )
        app_session.add(library)
        try:
            app_session.flush()
            _ensure_owner_membership(app_session, library, user.id)
            app_session.commit()
        except IntegrityError:
            app_session.rollback()
            library = app_session.query(ub.Library).filter_by(
                kind="personal",
                owner_user_id=user.id,
            ).one()
    else:
        expected_path = managed_library_path(library.public_id, managed_root)
        if os.path.realpath(library.root_path) != os.path.realpath(expected_path):
            raise LibraryPolicyError("Registered personal library is outside the managed root")
        _ensure_owner_membership(app_session, library, user.id)
        app_session.commit()

    operation_root = os.path.dirname(os.path.realpath(library.root_path))
    with _library_operation_lock(library.public_id, operation_root):
        app_session.refresh(library)
        if library.status == "disabled":
            return library

        if library.status == "active":
            try:
                if os.path.isfile(_metadata_path(library.root_path)):
                    return library
            except LibraryPolicyError as error:
                library.status = "error"
                log.error(
                    "Unsafe metadata path for personal library %s: %s",
                    library.public_id,
                    error,
                )
                app_session.commit()
                return library

        library.status = "provisioning"
        app_session.commit()
        try:
            # Revalidate immediately before touching the filesystem to catch a
            # UUID directory replaced by a symlink after registration.
            path = managed_library_path(library.public_id, managed_root)
            if os.path.realpath(library.root_path) != os.path.realpath(path):
                raise LibraryPolicyError("Registered personal library path changed")
            os.makedirs(path, mode=0o750, exist_ok=True)
            initializer(path)
            metadata_path = _metadata_path(path)
            if not os.path.isfile(metadata_path):
                raise RuntimeError("Library initializer did not create metadata.db")
            library.status = "active"
        except Exception as error:
            library.status = "error"
            log.error("Failed to provision personal library %s: %s", library.public_id, error)
        app_session.commit()
        return library


def provision_personal_library(user, app_session=None, managed_root=None, initializer=None):
    """Serialize process-local provisioning to avoid duplicate Calibre writers."""
    with _PROVISION_LOCK:
        return _provision_personal_library_unlocked(
            user,
            app_session,
            managed_root,
            initializer,
        )


def ensure_legacy_library_registration(app_session, legacy_root, calibre_uuid=None):
    """Register the current singleton library without moving or modifying it."""
    if not legacy_root or not os.path.isdir(legacy_root):
        return None

    root_path = os.path.realpath(legacy_root)
    library = app_session.query(ub.Library).filter_by(slug="legacy").one_or_none()
    path_owner = app_session.query(ub.Library).filter_by(root_path=root_path).one_or_none()
    if path_owner is not None and (library is None or path_owner.id != library.id):
        raise LibraryPolicyError("Configured legacy path belongs to another registered library")

    normalized_uuid = calibre_uuid or None
    if normalized_uuid:
        uuid_owner = app_session.query(ub.Library).filter_by(calibre_uuid=normalized_uuid).one_or_none()
        if uuid_owner is not None and (library is None or uuid_owner.id != library.id):
            raise LibraryPolicyError("Configured legacy UUID belongs to another registered library")

    if library is None:
        library = ub.Library(
            public_id=str(uuid.uuid4()),
            slug="legacy",
            name="Legacy Library",
            kind="shared",
            owner_user_id=None,
            root_path=root_path,
            calibre_uuid=normalized_uuid,
            status="active",
        )
        app_session.add(library)
    else:
        # config_calibre_dir remains authoritative during Phase 1 because the
        # singleton CalibreDB still serves all book traffic.
        library.root_path = root_path
        library.calibre_uuid = normalized_uuid
        library.status = "active"
    try:
        app_session.commit()
    except IntegrityError as error:
        app_session.rollback()
        raise LibraryPolicyError("Legacy library registration conflicts with existing state") from error

    users = app_session.query(ub.User).all()
    for user in users:
        if _is_anonymous(user):
            continue
        membership = app_session.query(ub.LibraryMembership).filter_by(
            library_id=library.id,
            user_id=user.id,
        ).one_or_none()
        if membership is None:
            app_session.add(ub.LibraryMembership(
                library_id=library.id,
                user_id=user.id,
                role="manager" if user.role_admin() else "viewer",
                is_default=not _default_membership_exists(app_session, user.id),
            ))
        elif user.role_admin():
            membership.role = "manager"
    app_session.commit()
    return library


def set_membership(app_session, library, user, role, is_default=False):
    if role not in LIBRARY_ROLES:
        raise LibraryPolicyError("Invalid library membership role")
    if _is_anonymous(user):
        raise LibraryPolicyError("Anonymous users cannot receive library memberships")
    if library.kind == "personal" and library.owner_user_id != user.id:
        raise LibraryPolicyError("Personal libraries only allow their owner")
    if library.kind == "personal":
        role = "manager"

    membership = app_session.query(ub.LibraryMembership).filter_by(
        library_id=library.id,
        user_id=user.id,
    ).one_or_none()
    if is_default:
        app_session.query(ub.LibraryMembership).filter_by(user_id=user.id).update(
            {ub.LibraryMembership.is_default: False},
            synchronize_session="fetch",
        )
    if membership is None:
        membership = ub.LibraryMembership(
            library_id=library.id,
            user_id=user.id,
            role=role,
            is_default=is_default or not _default_membership_exists(app_session, user.id),
        )
        app_session.add(membership)
    else:
        membership.role = role
        membership.is_default = (
            is_default
            or membership.is_default
            or not _default_membership_exists(app_session, user.id)
        )
    _ensure_default_membership(app_session, user.id)
    app_session.commit()
    return membership


def remove_membership(app_session, library, user):
    if library.kind == "personal" and library.owner_user_id == user.id:
        raise LibraryPolicyError("A personal-library owner membership cannot be removed")
    membership = app_session.query(ub.LibraryMembership).filter_by(
        library_id=library.id,
        user_id=user.id,
    ).one_or_none()
    if membership is None:
        return False

    app_session.delete(membership)
    app_session.flush()
    _ensure_default_membership(app_session, user.id)
    app_session.commit()
    return True



def _library_managed_root(library, managed_root=None):
    return os.path.realpath(
        managed_root or os.path.dirname(os.path.realpath(library.root_path))
    )


def disable_personal_library(app_session, library):
    if library.kind != "personal":
        raise LibraryPolicyError("Only personal libraries can be disabled")
    operation_root = _library_managed_root(library)
    with _library_operation_lock(library.public_id, operation_root):
        app_session.refresh(library)
        library.status = "disabled"
        app_session.commit()
    return library


def enable_personal_library(app_session, library):
    if library.kind != "personal":
        raise LibraryPolicyError("Only personal libraries can be enabled")
    operation_root = _library_managed_root(library)
    with _library_operation_lock(library.public_id, operation_root):
        app_session.refresh(library)
        library.status = "provisioning"
        app_session.commit()
    try:
        queue_personal_library_provisioning(
            library.owner_user_id, ensure_follow_up=True
        )
    except Exception:
        with _library_operation_lock(library.public_id, operation_root):
            app_session.refresh(library)
            library.status = "error"
            app_session.commit()
        raise
    return library



def _detach_shares_for_owner_library_deletion(app_session, library):
    """Preserve outgoing copies while removing provenance for this target."""
    outgoing = app_session.query(ub.BookShare).filter(
        ub.BookShare.source_library_id == library.id
    ).all()
    for share in outgoing:
        if not share.source_library_name or share.source_library_name == "Unknown library":
            share.source_library_name = library.name
        share.source_library_id = None

    # These records describe copies inside the library being deleted. Removing
    # them never touches their source or any downstream target copies.
    app_session.query(ub.BookShare).filter(
        ub.BookShare.target_library_id == library.id
    ).delete(synchronize_session=False)
    app_session.flush()

def _validate_personal_library_deletion(app_session, library, managed_root=None):
    if library.kind != "personal":
        raise LibraryPolicyError("Only personal libraries can be deleted")
    if library.status != "disabled":
        raise LibraryPolicyError("Disable the personal library before deleting it")

    shared = app_session.query(ub.BookShare.id).filter(
        (ub.BookShare.source_library_id == library.id)
        | (ub.BookShare.target_library_id == library.id)
    ).first()
    if shared is not None:
        raise LibraryPolicyError("A library referenced by book-sharing history cannot be deleted")

    expected_path = managed_library_path(library.public_id, managed_root)
    registered_path = os.path.abspath(library.root_path)
    if (
        os.path.islink(registered_path)
        or registered_path != expected_path
        or os.path.realpath(registered_path) != expected_path
    ):
        raise LibraryPolicyError("Registered personal library is outside the managed root")
    return expected_path


def _restore_quarantined_library(quarantine_path, expected_path):
    if not quarantine_path or not os.path.exists(quarantine_path):
        return
    if os.path.lexists(expected_path):
        raise RuntimeError(
            "Could not restore quarantined library because its managed path was recreated"
        )
    os.replace(quarantine_path, expected_path)


@contextmanager
def personal_library_deletion_transaction(
    app_session,
    library,
    managed_root=None,
    preserve_outgoing_shares=False,
):
    """Stage files and commit the library with all caller DB changes atomically."""
    operation_root = os.path.realpath(
        managed_root or constants.CALIBRE_LIBRARIES_ROOT
    )
    state = {"cleanup_error": None, "quarantine_path": None}
    with _library_operation_lock(library.public_id, operation_root):
        expected_path = None
        try:
            app_session.refresh(library)
            if preserve_outgoing_shares:
                _detach_shares_for_owner_library_deletion(app_session, library)
            expected_path = _validate_personal_library_deletion(
                app_session, library, operation_root
            )
            if os.path.exists(expected_path):
                revalidated_path = managed_library_path(
                    library.public_id, operation_root
                )
                if revalidated_path != expected_path or os.path.islink(expected_path):
                    raise LibraryPolicyError(
                        "Managed library path changed before deletion"
                    )
                state["quarantine_path"] = "{}.deleting-{}".format(
                    expected_path, uuid.uuid4()
                )
                os.replace(expected_path, state["quarantine_path"])

            app_session.delete(library)
            yield state
            app_session.commit()
        except Exception:
            app_session.rollback()
            try:
                if expected_path is not None:
                    _restore_quarantined_library(
                        state["quarantine_path"], expected_path
                    )
            except Exception as restore_error:
                log.critical(
                    "Database rollback succeeded but library restore failed from %s to %s: %s",
                    state["quarantine_path"],
                    expected_path,
                    restore_error,
                )
            raise

        if state["quarantine_path"]:
            try:
                shutil.rmtree(state["quarantine_path"])
            except OSError as error:
                state["cleanup_error"] = error
                log.error(
                    "Library deletion committed but cleanup remains pending at %s: %s",
                    state["quarantine_path"],
                    error,
                )


def delete_personal_library(app_session, library, managed_root=None):
    """Delete a disabled personal library with rollback-safe file staging."""
    with personal_library_deletion_transaction(
        app_session,
        library,
        managed_root,
        preserve_outgoing_shares=True,
    ) as state:
        pass
    if state["cleanup_error"] is not None:
        raise LibraryCleanupError(
            "Library record was deleted, but quarantined files could not be removed: {}".format(
                state["quarantine_path"]
            )
        ) from state["cleanup_error"]
    return True


def _release_provisioning_queue_key(user_id=None):
    key = "all" if user_id is None else int(user_id)
    with _PROVISION_QUEUE_LOCK:
        count = _QUEUED_PROVISIONING.get(key, 0)
        if count <= 1:
            _QUEUED_PROVISIONING.pop(key, None)
        else:
            _QUEUED_PROVISIONING[key] = count - 1


def queue_personal_library_provisioning(user_id=None, ensure_follow_up=False):
    """Queue physical provisioning without blocking startup or a request."""
    if not feature_enabled():
        return False
    key = "all" if user_id is None else int(user_id)
    with _PROVISION_QUEUE_LOCK:
        count = _QUEUED_PROVISIONING.get(key, 0)
        if count and not ensure_follow_up:
            return False
        _QUEUED_PROVISIONING[key] = count + 1
    try:
        from .services.worker import WorkerThread
        from .tasks.library import TaskProvisionPersonalLibraries

        WorkerThread.add(None, TaskProvisionPersonalLibraries(user_id=user_id), hidden=True)
        return True
    except Exception:
        _release_provisioning_queue_key(user_id)
        raise

def bootstrap_control_plane(app_session, legacy_root, calibre_uuid=None, managed_root=None, initializer=None):
    """Idempotently register legacy state and provision all current users."""
    if not feature_enabled():
        return {"legacy": None, "personal": []}

    legacy = ensure_legacy_library_registration(app_session, legacy_root, calibre_uuid)
    personal = []
    for user in app_session.query(ub.User).all():
        library = provision_personal_library(user, app_session, managed_root, initializer)
        if library is not None:
            personal.append(library)
            _ensure_default_membership(app_session, user.id)
    app_session.commit()
    return {"legacy": legacy, "personal": personal}


def init_app(app, app_session, config):
    """Register legacy state and queue physical provisioning in the worker."""
    if not feature_enabled():
        return

    ensure_legacy_library_registration(
        app_session,
        getattr(config, "config_calibre_dir", ""),
        getattr(config, "config_calibre_uuid", None),
    )
    queue_personal_library_provisioning()

    @app.before_request
    def _queue_missing_current_user_personal_library():
        from .cw_login import current_user

        if current_user.is_authenticated and not current_user.is_anonymous:
            personal_status = app_session.query(ub.Library.status).filter_by(
                kind="personal",
                owner_user_id=current_user.id,
            ).scalar()
            if personal_status in (None, "error"):
                queue_personal_library_provisioning(current_user.id)

    app.extensions["multi_library_control_plane"] = True
