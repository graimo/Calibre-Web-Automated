# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Characterization and unit tests for the Phase 0/1 library control plane."""

import os
import sqlite3
import threading
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from cps import constants, library_control, ub


@pytest.fixture
def app_session(tmp_path):
    engine = create_engine("sqlite:///{}".format(tmp_path / "app.db"))
    ub.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def add_user(session, name, role=constants.ROLE_USER):
    user = ub.User(name=name, email="{}@example.test".format(name), role=role, password="test")
    session.add(user)
    session.commit()
    return user


def fake_calibre_initializer(path):
    database = sqlite3.connect(os.path.join(path, "metadata.db"))
    database.execute("CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT NOT NULL)")
    database.commit()
    database.close()


def create_collision_library(path, title, custom_label):
    path.mkdir()
    database = sqlite3.connect(path / "metadata.db")
    database.execute("CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT NOT NULL)")
    database.execute("CREATE TABLE custom_columns (id INTEGER PRIMARY KEY, label TEXT NOT NULL)")
    database.execute("INSERT INTO books (id, title) VALUES (1, ?)", (title,))
    database.execute("INSERT INTO custom_columns (id, label) VALUES (1, ?)", (custom_label,))
    database.commit()
    database.close()


@pytest.mark.unit
class TestLibraryControlPlaneSchema:
    def test_subprocess_metadata_provider_is_opt_in(self):
        assert constants.metadata_provider_enabled_by_default("calibre") is False
        assert constants.metadata_provider_enabled_by_default("google") is True

    def test_additive_migration_is_idempotent(self, app_session):
        engine = app_session.bind
        ub.BookShare.__table__.drop(engine)
        ub.LibraryMembership.__table__.drop(engine)
        ub.Library.__table__.drop(engine)

        ub.migrate_multi_library_control_plane(engine)
        ub.migrate_multi_library_control_plane(engine)

        tables = set(inspect(engine).get_table_names())
        assert {"library", "library_membership", "book_share"} <= tables

    def test_feature_flag_disabled_is_a_noop(self, app_session, monkeypatch, tmp_path):
        monkeypatch.setattr(constants, "MULTI_LIBRARY_ENABLED", False)
        result = library_control.bootstrap_control_plane(
            app_session,
            str(tmp_path / "legacy"),
            managed_root=str(tmp_path / "managed"),
            initializer=fake_calibre_initializer,
        )
        assert result == {"legacy": None, "personal": []}
        assert app_session.query(ub.Library).count() == 0


@pytest.mark.unit
class TestPersonalLibraryProvisioning:
    def test_is_idempotent_and_uses_uuid_path(self, app_session, tmp_path):
        user = add_user(app_session, "reader")
        managed_root = tmp_path / "managed"

        first = library_control.provision_personal_library(
            user,
            app_session,
            str(managed_root),
            fake_calibre_initializer,
        )
        second = library_control.provision_personal_library(
            user,
            app_session,
            str(managed_root),
            fake_calibre_initializer,
        )

        assert first.id == second.id
        assert first.status == "active"
        assert app_session.query(ub.Library).filter_by(owner_user_id=user.id).count() == 1
        assert os.path.dirname(first.root_path) == os.path.realpath(managed_root)
        assert os.path.basename(first.root_path) == str(uuid.UUID(first.public_id))
        assert user.name not in first.root_path
        membership = app_session.query(ub.LibraryMembership).filter_by(
            library_id=first.id,
            user_id=user.id,
        ).one()
        assert membership.role == "manager"
        assert membership.is_default is True

    def test_anonymous_user_is_never_provisioned(self, app_session, tmp_path):
        guest = add_user(app_session, "Guest", constants.ROLE_ANONYMOUS)
        result = library_control.provision_personal_library(
            guest,
            app_session,
            str(tmp_path / "managed"),
            fake_calibre_initializer,
        )
        assert result is None
        assert app_session.query(ub.Library).count() == 0

    def test_failed_provisioning_is_retryable(self, app_session, tmp_path):
        user = add_user(app_session, "retry")
        managed_root = str(tmp_path / "managed")

        def fail(_path):
            raise RuntimeError("expected failure")

        failed = library_control.provision_personal_library(
            user, app_session, managed_root, fail
        )
        assert failed.status == "error"

        retried = library_control.provision_personal_library(
            user, app_session, managed_root, fake_calibre_initializer
        )
        assert retried.id == failed.id
        assert retried.status == "active"

    def test_symlink_child_is_rejected(self, tmp_path):
        managed_root = tmp_path / "managed"
        managed_root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        public_id = str(uuid.uuid4())
        (managed_root / public_id).symlink_to(outside, target_is_directory=True)

        with pytest.raises(library_control.LibraryPolicyError):
            library_control.managed_library_path(public_id, str(managed_root))

    def test_metadata_symlink_is_never_marked_active(self, app_session, tmp_path):
        user = add_user(app_session, "unsafe")
        outside_database = tmp_path / "outside.db"
        outside_database.write_bytes(b"not a calibre database")

        def symlink_initializer(path):
            os.symlink(outside_database, os.path.join(path, "metadata.db"))

        library = library_control.provision_personal_library(
            user,
            app_session,
            str(tmp_path / "managed"),
            symlink_initializer,
        )
        assert library.status == "error"


@pytest.mark.unit
class TestLegacyAndMembershipPolicy:
    def test_legacy_registration_is_conservative_and_idempotent(self, app_session, tmp_path):
        admin = add_user(app_session, "admin2", constants.ROLE_ADMIN)
        reader = add_user(app_session, "reader2")
        add_user(app_session, "Guest2", constants.ROLE_ANONYMOUS)
        legacy_root = tmp_path / "legacy"
        legacy_root.mkdir()

        first = library_control.ensure_legacy_library_registration(
            app_session, str(legacy_root), "legacy-uuid"
        )
        second = library_control.ensure_legacy_library_registration(
            app_session, str(legacy_root), "legacy-uuid"
        )
        moved_root = tmp_path / "legacy-moved"
        moved_root.mkdir()
        updated = library_control.ensure_legacy_library_registration(
            app_session, str(moved_root), "legacy-uuid-updated"
        )

        assert first.id == second.id == updated.id
        assert updated.root_path == os.path.realpath(moved_root)
        assert updated.calibre_uuid == "legacy-uuid-updated"
        assert app_session.query(ub.Library).filter_by(slug="legacy").count() == 1
        memberships = app_session.query(ub.LibraryMembership).filter_by(library_id=first.id).all()
        assert {item.user_id: item.role for item in memberships} == {
            admin.id: "manager",
            reader.id: "viewer",
        }
        assert all(item.is_default for item in memberships)

        personal = library_control.provision_personal_library(
            reader,
            app_session,
            str(tmp_path / "managed"),
            fake_calibre_initializer,
        )
        personal_membership = app_session.query(ub.LibraryMembership).filter_by(
            library_id=personal.id,
            user_id=reader.id,
        ).one()
        assert personal_membership.role == "manager"
        assert personal_membership.is_default is False

    def test_removing_default_repairs_default_even_if_personal_is_in_error(self, app_session, tmp_path):
        user = add_user(app_session, "defaultrepair")
        legacy_root = tmp_path / "legacy-repair"
        legacy_root.mkdir()
        legacy = library_control.ensure_legacy_library_registration(
            app_session,
            str(legacy_root),
        )
        personal = library_control.provision_personal_library(
            user,
            app_session,
            str(tmp_path / "managed-repair"),
            fake_calibre_initializer,
        )
        personal.status = "error"
        app_session.commit()

        assert library_control.remove_membership(app_session, legacy, user) is True
        remaining = app_session.query(ub.LibraryMembership).filter_by(
            library_id=personal.id,
            user_id=user.id,
        ).one()
        assert remaining.is_default is True

    def test_personal_owner_membership_cannot_be_removed(self, app_session, tmp_path):
        owner = add_user(app_session, "owner")
        personal = library_control.provision_personal_library(
            owner,
            app_session,
            str(tmp_path / "managed"),
            fake_calibre_initializer,
        )
        with pytest.raises(library_control.LibraryPolicyError):
            library_control.remove_membership(app_session, personal, owner)


@pytest.mark.unit
def test_two_physical_libraries_can_both_contain_book_id_one(tmp_path):
    first = tmp_path / "first-library"
    second = tmp_path / "second-library"
    create_collision_library(first, "First tenant book", "first_custom")
    create_collision_library(second, "Second tenant book", "second_custom")

    with sqlite3.connect(first / "metadata.db") as first_db:
        first_book = first_db.execute("SELECT id, title FROM books WHERE id = 1").fetchone()
        first_column = first_db.execute("SELECT label FROM custom_columns WHERE id = 1").fetchone()[0]
    with sqlite3.connect(second / "metadata.db") as second_db:
        second_book = second_db.execute("SELECT id, title FROM books WHERE id = 1").fetchone()
        second_column = second_db.execute("SELECT label FROM custom_columns WHERE id = 1").fetchone()[0]

    assert first_book == (1, "First tenant book")
    assert second_book == (1, "Second tenant book")
    assert first_column == "first_custom"
    assert second_column == "second_custom"


@pytest.mark.unit
class TestPersonalLibraryLifecycle:
    def test_disabled_library_is_not_reprovisioned(self, app_session, tmp_path):
        user = add_user(app_session, "disabled")
        managed_root = str(tmp_path / "managed")
        library = library_control.provision_personal_library(
            user, app_session, managed_root, fake_calibre_initializer
        )
        library_control.disable_personal_library(app_session, library)

        calls = []
        result = library_control.provision_personal_library(
            user,
            app_session,
            managed_root,
            lambda path: calls.append(path),
        )

        assert result.status == "disabled"
        assert calls == []

    def test_disabled_library_can_be_deleted_with_its_files(self, app_session, tmp_path):
        user = add_user(app_session, "delete-library")
        managed_root = str(tmp_path / "managed-delete")
        library = library_control.provision_personal_library(
            user, app_session, managed_root, fake_calibre_initializer
        )
        library_id = library.id
        root_path = library.root_path
        library_control.disable_personal_library(app_session, library)

        assert library_control.delete_personal_library(
            app_session, library, managed_root
        ) is True
        assert not os.path.exists(root_path)
        assert app_session.query(ub.Library).filter_by(id=library_id).count() == 0
        assert app_session.query(ub.LibraryMembership).filter_by(
            library_id=library_id
        ).count() == 0

    def test_active_library_must_be_disabled_before_delete(self, app_session, tmp_path):
        user = add_user(app_session, "active-delete")
        managed_root = str(tmp_path / "managed-active")
        library = library_control.provision_personal_library(
            user, app_session, managed_root, fake_calibre_initializer
        )

        with pytest.raises(library_control.LibraryPolicyError, match="Disable"):
            library_control.delete_personal_library(app_session, library, managed_root)
        assert os.path.isdir(library.root_path)

    def test_init_app_queues_instead_of_provisioning(self, app_session, monkeypatch):
        calls = []

        class FakeApp:
            def __init__(self):
                self.extensions = {}
                self.before_request_callback = None

            def before_request(self, callback):
                self.before_request_callback = callback
                return callback

        class FakeConfig:
            config_calibre_dir = ""
            config_calibre_uuid = None

        monkeypatch.setattr(constants, "MULTI_LIBRARY_ENABLED", True)
        monkeypatch.setattr(
            library_control,
            "ensure_legacy_library_registration",
            lambda *args, **kwargs: calls.append("legacy"),
        )
        monkeypatch.setattr(
            library_control,
            "queue_personal_library_provisioning",
            lambda user_id=None: calls.append(("queued", user_id)),
        )
        app = FakeApp()

        library_control.init_app(app, app_session, FakeConfig())

        assert calls == ["legacy", ("queued", None)]
        assert app.extensions["multi_library_control_plane"] is True
        assert app.before_request_callback is not None

    def test_queue_deduplicates_pending_user(self, monkeypatch):
        from cps.services.worker import WorkerThread

        queued = []
        monkeypatch.setattr(constants, "MULTI_LIBRARY_ENABLED", True)
        monkeypatch.setattr(
            WorkerThread,
            "add",
            classmethod(lambda cls, user, task, hidden=False: queued.append(task.user_id)),
        )
        library_control._QUEUED_PROVISIONING.clear()
        try:
            assert library_control.queue_personal_library_provisioning(42) is True
            assert library_control.queue_personal_library_provisioning(42) is False
            assert queued == [42]
        finally:
            library_control._release_provisioning_queue_key(42)

    def test_background_task_provisions_requested_user(
        self, app_session, monkeypatch, tmp_path
    ):
        from cps.tasks.library import TaskProvisionPersonalLibraries

        user = add_user(app_session, "background-user")
        worker_session = sessionmaker(bind=app_session.bind)()
        monkeypatch.setattr(ub, "init_db_thread", lambda: worker_session)
        monkeypatch.setattr(
            constants, "CALIBRE_LIBRARIES_ROOT", str(tmp_path / "managed-task")
        )
        monkeypatch.setattr(
            library_control,
            "create_empty_calibre_library",
            fake_calibre_initializer,
        )

        task = TaskProvisionPersonalLibraries(user.id)
        task.run(None)

        app_session.expire_all()
        library = app_session.query(ub.Library).filter_by(owner_user_id=user.id).one()
        assert library.status == "active"
        assert task.progress == 1

    def test_disabled_personal_library_allows_user_deletion(
        self, app_session, monkeypatch, tmp_path
    ):
        from cps import admin

        add_user(app_session, "remaining-admin", constants.ROLE_ADMIN)
        user = add_user(app_session, "removable-user")
        managed_root = str(tmp_path / "managed-user-delete")
        monkeypatch.setattr(constants, "CALIBRE_LIBRARIES_ROOT", managed_root)
        monkeypatch.setattr(ub, "session", app_session)
        library = library_control.provision_personal_library(
            user, app_session, managed_root, fake_calibre_initializer
        )
        library_control.disable_personal_library(app_session, library)

        message = admin._delete_user(user)

        assert "deleted" in str(message).lower()
        assert app_session.query(ub.User).filter_by(id=user.id).count() == 0
        assert app_session.query(ub.Library).filter_by(owner_user_id=user.id).count() == 0


@pytest.mark.unit
def test_shipped_deployments_persist_managed_library_root():
    project_root = Path(__file__).resolve().parents[2]
    compose = (project_root / "docker-compose.yml").read_text(encoding="utf-8")
    dockerfile = (project_root / "Dockerfile").read_text(encoding="utf-8")
    deployment = (project_root / "kubernetes" / "deployment.yaml").read_text(encoding="utf-8")
    pvc = (project_root / "kubernetes" / "pvc-library.yml").read_text(encoding="utf-8")

    assert ":/calibre-libraries" in compose
    assert "CALIBRE_LIBRARIES_ROOT=/calibre-libraries" in compose
    assert "VOLUME /calibre-libraries" in dockerfile
    assert "mountPath: /calibre-libraries" in deployment
    assert "claimName: calibre-libraries-pvc" in deployment
    assert "name: calibre-libraries-pvc" in pvc


@pytest.mark.unit
def test_global_queue_does_not_drop_later_user_request(monkeypatch):
    from cps.services.worker import WorkerThread

    queued = []
    monkeypatch.setattr(constants, "MULTI_LIBRARY_ENABLED", True)
    monkeypatch.setattr(
        WorkerThread,
        "add",
        classmethod(lambda cls, user, task, hidden=False: queued.append(task.user_id)),
    )
    library_control._QUEUED_PROVISIONING.clear()
    try:
        assert library_control.queue_personal_library_provisioning() is True
        assert library_control.queue_personal_library_provisioning(42) is True
        assert library_control.queue_personal_library_provisioning(42) is False
        assert queued == [None, 42]
    finally:
        library_control._release_provisioning_queue_key()
        library_control._release_provisioning_queue_key(42)


@pytest.mark.unit
def test_disable_waits_for_provisioning_and_wins_final_state(app_session, tmp_path):
    user = add_user(app_session, "lifecycle-race")
    user_id = user.id
    managed_root = str(tmp_path / "managed-race")
    initializer_started = threading.Event()
    allow_initializer_to_finish = threading.Event()
    disable_finished = threading.Event()
    errors = []

    def blocking_initializer(path):
        initializer_started.set()
        if not allow_initializer_to_finish.wait(5):
            raise RuntimeError("test initializer timed out")
        fake_calibre_initializer(path)

    def provision_worker():
        session = sessionmaker(bind=app_session.bind)()
        try:
            worker_user = session.query(ub.User).filter_by(id=user_id).one()
            library_control.provision_personal_library(
                worker_user, session, managed_root, blocking_initializer
            )
        except Exception as error:  # pragma: no cover - assertion reports details
            errors.append(error)
        finally:
            session.close()

    def disable_worker():
        session = sessionmaker(bind=app_session.bind)()
        try:
            library = session.query(ub.Library).filter_by(owner_user_id=user_id).one()
            library_control.disable_personal_library(session, library)
            disable_finished.set()
        except Exception as error:  # pragma: no cover - assertion reports details
            errors.append(error)
        finally:
            session.close()

    provision_thread = threading.Thread(target=provision_worker)
    provision_thread.start()
    assert initializer_started.wait(5)

    disable_thread = threading.Thread(target=disable_worker)
    disable_thread.start()
    assert not disable_finished.wait(0.1)
    allow_initializer_to_finish.set()
    provision_thread.join(5)
    disable_thread.join(5)

    assert not provision_thread.is_alive()
    assert not disable_thread.is_alive()
    assert errors == []
    app_session.expire_all()
    library = app_session.query(ub.Library).filter_by(owner_user_id=user_id).one()
    assert library.status == "disabled"


@pytest.mark.unit
def test_user_delete_commit_failure_restores_personal_library(
    app_session, monkeypatch, tmp_path
):
    from cps import admin

    add_user(app_session, "rollback-admin", constants.ROLE_ADMIN)
    user = add_user(app_session, "rollback-user")
    managed_root = str(tmp_path / "managed-user-rollback")
    monkeypatch.setattr(constants, "CALIBRE_LIBRARIES_ROOT", managed_root)
    monkeypatch.setattr(ub, "session", app_session)
    library = library_control.provision_personal_library(
        user, app_session, managed_root, fake_calibre_initializer
    )
    library_control.disable_personal_library(app_session, library)
    library_id = library.id
    root_path = library.root_path

    def fail_commit():
        raise RuntimeError("forced user deletion commit failure")

    monkeypatch.setattr(app_session, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="forced user deletion"):
        admin._delete_user(user)

    assert os.path.isdir(root_path)
    assert app_session.query(ub.User).filter_by(id=user.id).count() == 1
    assert app_session.query(ub.Library).filter_by(id=library_id).count() == 1
    assert not list(Path(managed_root).glob("*.deleting-*"))


@pytest.mark.unit
def test_app_db_engine_enforces_foreign_keys(tmp_path):
    engine = ub._create_app_db_engine(str(tmp_path / "foreign-keys.db"))
    ub.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        assert session.execute(text("PRAGMA foreign_keys")).scalar() == 1
        sharer = add_user(session, "fk-sharer")
        source = ub.Library(
            public_id=str(uuid.uuid4()),
            slug="fk-source",
            name="FK source",
            kind="shared",
            root_path=str(tmp_path / "fk-source"),
            status="active",
        )
        target = ub.Library(
            public_id=str(uuid.uuid4()),
            slug="fk-target",
            name="FK target",
            kind="shared",
            root_path=str(tmp_path / "fk-target"),
            status="active",
        )
        session.add_all((source, target))
        session.flush()
        share = ub.BookShare(
            source_library_id=source.id,
            source_book_id=1,
            target_library_id=target.id,
            target_book_id=1,
            shared_by_user_id=sharer.id,
        )
        session.add(share)
        session.commit()
        share_id = share.id
        source_id = source.id
        target_id = target.id
        sharer_id = sharer.id

        session.delete(source)
        session.commit()
        preserved = session.query(ub.BookShare).filter_by(id=share_id).one()
        assert preserved.source_library_id is None
        assert preserved.source_library_name == "FK source"

        session.delete(sharer)
        session.commit()
        preserved = session.query(ub.BookShare).filter_by(id=share_id).one()
        assert preserved.shared_by_user_id is None
        assert preserved.shared_by_name == "fk-sharer"
        assert session.query(ub.Library).filter_by(id=source_id).count() == 0

        target = session.query(ub.Library).filter_by(id=target_id).one()
        session.delete(target)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        assert session.query(ub.Library).filter_by(id=target_id).count() == 1
        assert session.query(ub.User).filter_by(id=sharer_id).count() == 0
    finally:
        session.close()
        engine.dispose()


@pytest.mark.unit
def test_provisioning_follow_up_keeps_key_until_every_task_finishes(monkeypatch):
    from cps.services.worker import WorkerThread

    queued = []
    monkeypatch.setattr(constants, "MULTI_LIBRARY_ENABLED", True)
    monkeypatch.setattr(
        WorkerThread,
        "add",
        classmethod(lambda cls, user, task, hidden=False: queued.append(task.user_id)),
    )
    library_control._QUEUED_PROVISIONING.clear()
    try:
        assert library_control.queue_personal_library_provisioning(42) is True
        assert library_control.queue_personal_library_provisioning(
            42, ensure_follow_up=True
        ) is True
        assert queued == [42, 42]
        library_control._release_provisioning_queue_key(42)
        assert library_control.queue_personal_library_provisioning(42) is False
        library_control._release_provisioning_queue_key(42)
        assert library_control.queue_personal_library_provisioning(42) is True
        assert queued == [42, 42, 42]
    finally:
        while library_control._QUEUED_PROVISIONING.get(42):
            library_control._release_provisioning_queue_key(42)


@pytest.mark.unit
def test_user_delete_with_production_foreign_keys_enabled(monkeypatch, tmp_path):
    from cps import admin

    engine = ub._create_app_db_engine(str(tmp_path / "user-delete-fk.db"))
    ub.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    managed_root = str(tmp_path / "managed-user-delete-fk")
    monkeypatch.setattr(constants, "CALIBRE_LIBRARIES_ROOT", managed_root)
    monkeypatch.setattr(ub, "session", session)
    try:
        remaining_admin = add_user(session, "fk-admin", constants.ROLE_ADMIN)
        user = add_user(session, "fk-removable")
        user_id = user.id
        library = library_control.provision_personal_library(
            user, session, managed_root, fake_calibre_initializer
        )
        library_control.disable_personal_library(session, library)

        shelf = ub.Shelf(name="Owned shelf", user_id=user_id)
        magic_shelf = ub.MagicShelf(name="Owned magic shelf", user_id=user_id)
        session.add_all((shelf, magic_shelf))
        session.flush()
        shelf_id = shelf.id
        magic_shelf_id = magic_shelf.id
        book_shelf = ub.BookShelf(book_id=1, shelf=shelf_id)
        book_shelf.ub_shelf = shelf
        session.add_all((
            book_shelf,
            ub.OpdsShelfExposure(user_id=remaining_admin.id, shelf_id=shelf_id),
            ub.MagicShelfCache(
                shelf_id=magic_shelf_id,
                user_id=user_id,
                sort_param="stored",
                book_ids=[1],
                total_count=1,
            ),
            ub.OpdsMagicShelfExposure(
                user_id=remaining_admin.id,
                shelf_id=magic_shelf_id,
            ),
            ub.HiddenMagicShelfTemplate(
                user_id=user_id,
                shelf_id=magic_shelf_id,
            ),
            ub.DismissedDuplicateGroup(
                user_id=user_id,
                group_hash="a" * 32,
            ),
            ub.ShelfArchive(uuid="deleted-shelf", user_id=user_id),
            ub.KoboAnnotationSync(
                user_id=user_id,
                annotation_id="annotation",
                book_id=1,
            ),
        ))
        session.commit()

        message = admin._delete_user(user)

        assert "deleted" in str(message).lower()
        assert session.query(ub.User).filter_by(id=user_id).count() == 0
        assert session.query(ub.Library).filter_by(owner_user_id=user_id).count() == 0
        assert session.query(ub.Shelf).filter_by(user_id=user_id).count() == 0
        assert session.query(ub.MagicShelf).filter_by(user_id=user_id).count() == 0
        assert session.query(ub.MagicShelfCache).filter_by(user_id=user_id).count() == 0
        assert session.query(ub.OpdsShelfExposure).filter_by(shelf_id=shelf_id).count() == 0
        assert session.query(ub.OpdsMagicShelfExposure).filter_by(
            shelf_id=magic_shelf_id
        ).count() == 0
        assert session.query(ub.User).filter_by(id=remaining_admin.id).count() == 1
    finally:
        session.close()
        engine.dispose()


@pytest.mark.unit
def test_background_task_releases_queue_key_when_session_init_fails(monkeypatch):
    from cps.tasks.library import TaskProvisionPersonalLibraries

    monkeypatch.setattr(
        ub,
        "init_db_thread",
        lambda: (_ for _ in ()).throw(RuntimeError("session init failed")),
    )
    library_control._QUEUED_PROVISIONING.clear()
    library_control._QUEUED_PROVISIONING[73] = 1

    with pytest.raises(RuntimeError, match="session init failed"):
        TaskProvisionPersonalLibraries(73).run(None)

    assert 73 not in library_control._QUEUED_PROVISIONING


@pytest.mark.unit
def test_background_task_releases_queue_key_when_session_close_fails(
    app_session, monkeypatch
):
    from cps.tasks.library import TaskProvisionPersonalLibraries

    monkeypatch.setattr(ub, "init_db_thread", lambda: app_session)
    original_close = app_session.close
    monkeypatch.setattr(
        app_session,
        "close",
        lambda: (_ for _ in ()).throw(RuntimeError("session close failed")),
    )
    library_control._QUEUED_PROVISIONING.clear()
    library_control._QUEUED_PROVISIONING[74] = 1

    try:
        with pytest.raises(RuntimeError, match="session close failed"):
            TaskProvisionPersonalLibraries(74).run(None)
    finally:
        monkeypatch.setattr(app_session, "close", original_close)

    assert 74 not in library_control._QUEUED_PROVISIONING


@pytest.mark.unit
def test_legacy_book_share_migration_preserves_rows_and_adds_snapshots(tmp_path):
    database_path = tmp_path / "legacy-share.db"
    engine = ub._create_app_db_engine(str(database_path))
    ub.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        sharer = add_user(session, "legacy-sharer")
        source = ub.Library(
            public_id=str(uuid.uuid4()),
            slug="legacy-share-source",
            name="Original library",
            kind="shared",
            root_path=str(tmp_path / "legacy-source"),
            status="active",
        )
        target = ub.Library(
            public_id=str(uuid.uuid4()),
            slug="legacy-share-target",
            name="Destination library",
            kind="shared",
            root_path=str(tmp_path / "legacy-target"),
            status="active",
        )
        session.add_all((source, target))
        session.commit()
        source_id, target_id, sharer_id = source.id, target.id, sharer.id

        ub.BookShare.__table__.drop(engine)
        with engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE book_share (
                    id INTEGER NOT NULL PRIMARY KEY,
                    source_library_id INTEGER NOT NULL,
                    source_book_id INTEGER NOT NULL,
                    target_library_id INTEGER NOT NULL,
                    target_book_id INTEGER NOT NULL,
                    shared_by_user_id INTEGER NOT NULL,
                    created_at DATETIME NOT NULL,
                    FOREIGN KEY(source_library_id) REFERENCES library(id) ON DELETE RESTRICT,
                    FOREIGN KEY(target_library_id) REFERENCES library(id) ON DELETE RESTRICT,
                    FOREIGN KEY(shared_by_user_id) REFERENCES user(id) ON DELETE RESTRICT
                )
            """))
            connection.execute(text("""
                INSERT INTO book_share (
                    id, source_library_id, source_book_id,
                    target_library_id, target_book_id,
                    shared_by_user_id, created_at
                ) VALUES (
                    1, :source_id, 7, :target_id, 9, :sharer_id, CURRENT_TIMESTAMP
                )
            """), {
                "source_id": source_id,
                "target_id": target_id,
                "sharer_id": sharer_id,
            })

        # Legacy app.db connections did not enable foreign_keys, so orphaned
        # source/user/target IDs may exist in real upgrades.
        with sqlite3.connect(database_path) as legacy_connection:
            legacy_connection.execute("PRAGMA foreign_keys=OFF")
            legacy_connection.execute("""
                INSERT INTO book_share (
                    id, source_library_id, source_book_id,
                    target_library_id, target_book_id,
                    shared_by_user_id, created_at
                ) VALUES (2, 999, 8, ?, 10, 999, CURRENT_TIMESTAMP)
            """, (target_id,))
            legacy_connection.execute("""
                INSERT INTO book_share (
                    id, source_library_id, source_book_id,
                    target_library_id, target_book_id,
                    shared_by_user_id, created_at
                ) VALUES (3, ?, 9, 999, 11, ?, CURRENT_TIMESTAMP)
            """, (source_id, sharer_id))
            legacy_connection.commit()

        ub.migrate_multi_library_control_plane(engine)
        ub.migrate_multi_library_control_plane(engine)

        with engine.connect() as connection:
            columns = {
                row[1]: row for row in connection.execute(
                    text("PRAGMA table_info(book_share)")
                ).fetchall()
            }
            foreign_keys = connection.execute(
                text("PRAGMA foreign_key_list(book_share)")
            ).fetchall()
            foreign_key_violations = connection.execute(
                text("PRAGMA foreign_key_check(book_share)")
            ).fetchall()
        assert foreign_key_violations == []
        assert columns["source_library_id"][3] == 0
        assert columns["shared_by_user_id"][3] == 0
        assert {row[3]: str(row[6]).upper() for row in foreign_keys}[
            "source_library_id"
        ] == "SET NULL"
        assert {row[3]: str(row[6]).upper() for row in foreign_keys}[
            "shared_by_user_id"
        ] == "SET NULL"

        session.expire_all()
        migrated = session.query(ub.BookShare).filter_by(id=1).one()
        assert migrated.source_library_name == "Original library"
        assert migrated.shared_by_name == "legacy-sharer"
        assert migrated.target_book_id == 9

        orphaned_source = session.query(ub.BookShare).filter_by(id=2).one()
        assert orphaned_source.source_library_id is None
        assert orphaned_source.source_library_name == "Deleted library"
        assert orphaned_source.shared_by_user_id is None
        assert orphaned_source.shared_by_name == "Deleted user"
        assert orphaned_source.target_library_id == target_id
        assert session.query(ub.BookShare).filter_by(id=3).count() == 0
    finally:
        session.close()
        engine.dispose()


@pytest.mark.unit
def test_deleting_sharer_preserves_target_copy_and_provenance(monkeypatch, tmp_path):
    from cps import admin

    engine = ub._create_app_db_engine(str(tmp_path / "delete-sharer.db"))
    ub.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    managed_root = str(tmp_path / "managed-sharer")
    monkeypatch.setattr(constants, "CALIBRE_LIBRARIES_ROOT", managed_root)
    monkeypatch.setattr(ub, "session", session)
    try:
        add_user(session, "share-admin", constants.ROLE_ADMIN)
        sharer = add_user(session, "alice")
        sharer_id = sharer.id
        source = library_control.provision_personal_library(
            sharer, session, managed_root, fake_calibre_initializer
        )
        source_id = source.id

        target_root = tmp_path / "recipient-library"
        target_root.mkdir()
        fake_calibre_initializer(str(target_root))
        with sqlite3.connect(target_root / "metadata.db") as metadata:
            metadata.execute(
                "INSERT INTO books (id, title) VALUES (?, ?)",
                (9, "A retained shared book"),
            )
            metadata.commit()
        target = ub.Library(
            public_id=str(uuid.uuid4()),
            slug="recipient-library",
            name="Bob's Library",
            kind="shared",
            root_path=str(target_root),
            status="active",
        )
        session.add(target)
        session.flush()
        target_id = target.id
        share = ub.BookShare(
            source_library_id=source_id,
            source_book_id=1,
            target_library_id=target_id,
            target_book_id=9,
            shared_by_user_id=sharer_id,
        )
        session.add(share)
        session.commit()
        share_id = share.id
        library_control.disable_personal_library(session, source)

        message = admin._delete_user(sharer)

        assert "deleted" in str(message).lower()
        assert session.query(ub.User).filter_by(id=sharer_id).count() == 0
        assert session.query(ub.Library).filter_by(id=source_id).count() == 0
        assert session.query(ub.Library).filter_by(id=target_id).count() == 1
        assert (target_root / "metadata.db").is_file()
        with sqlite3.connect(target_root / "metadata.db") as metadata:
            assert metadata.execute(
                "SELECT title FROM books WHERE id = 9"
            ).fetchone()[0] == "A retained shared book"

        preserved = session.query(ub.BookShare).filter_by(id=share_id).one()
        assert preserved.source_library_id is None
        assert preserved.source_library_name == "alice's Library"
        assert preserved.shared_by_user_id is None
        assert preserved.shared_by_name == "alice"
        assert preserved.target_library_id == target_id
        assert preserved.target_book_id == 9
    finally:
        session.close()
        engine.dispose()


@pytest.mark.unit
def test_standalone_library_delete_preserves_downstream_copy(app_session, tmp_path):
    owner = add_user(app_session, "standalone-owner")
    managed_root = str(tmp_path / "managed-standalone")
    source = library_control.provision_personal_library(
        owner, app_session, managed_root, fake_calibre_initializer
    )
    target_root = tmp_path / "standalone-target"
    target_root.mkdir()
    fake_calibre_initializer(str(target_root))
    target = ub.Library(
        public_id=str(uuid.uuid4()),
        slug="standalone-target",
        name="Standalone Target",
        kind="shared",
        root_path=str(target_root),
        status="active",
    )
    app_session.add(target)
    app_session.flush()
    share = ub.BookShare(
        source_library_id=source.id,
        source_book_id=1,
        target_library_id=target.id,
        target_book_id=5,
        shared_by_user_id=owner.id,
    )
    app_session.add(share)
    app_session.commit()
    share_id = share.id
    source_id = source.id
    target_id = target.id
    library_control.disable_personal_library(app_session, source)

    assert library_control.delete_personal_library(
        app_session, source, managed_root
    ) is True

    preserved = app_session.query(ub.BookShare).filter_by(id=share_id).one()
    assert preserved.source_library_id is None
    assert preserved.source_library_name == "standalone-owner's Library"
    assert preserved.target_library_id == target_id
    assert app_session.query(ub.Library).filter_by(id=source_id).count() == 0
    assert app_session.query(ub.Library).filter_by(id=target_id).count() == 1
    assert (target_root / "metadata.db").is_file()


@pytest.mark.unit
def test_library_delete_rejects_registered_path_outside_configured_root(
    app_session, monkeypatch, tmp_path
):
    owner = add_user(app_session, "unsafe-delete-owner")
    public_id = str(uuid.uuid4())
    external_root = tmp_path / "external" / public_id
    external_root.mkdir(parents=True)
    marker = external_root / "must-survive.txt"
    marker.write_text("retained", encoding="utf-8")
    configured_root = tmp_path / "configured-managed"
    monkeypatch.setattr(
        constants,
        "CALIBRE_LIBRARIES_ROOT",
        str(configured_root),
    )
    library = ub.Library(
        public_id=public_id,
        slug="unsafe-delete",
        name="Unsafe delete",
        kind="personal",
        owner_user_id=owner.id,
        root_path=str(external_root),
        status="disabled",
    )
    app_session.add(library)
    app_session.commit()
    library_id = library.id
    downstream = ub.Library(
        public_id=str(uuid.uuid4()),
        slug="unsafe-downstream",
        name="Unsafe Downstream",
        kind="shared",
        root_path=str(tmp_path / "unsafe-downstream"),
        status="active",
    )
    upstream = ub.Library(
        public_id=str(uuid.uuid4()),
        slug="unsafe-upstream",
        name="Unsafe Upstream",
        kind="shared",
        root_path=str(tmp_path / "unsafe-upstream"),
        status="active",
    )
    app_session.add_all((downstream, upstream))
    app_session.flush()
    outgoing = ub.BookShare(
        source_library_id=library_id,
        source_book_id=1,
        target_library_id=downstream.id,
        target_book_id=2,
        shared_by_user_id=owner.id,
    )
    incoming = ub.BookShare(
        source_library_id=upstream.id,
        source_book_id=3,
        target_library_id=library_id,
        target_book_id=4,
        shared_by_user_id=owner.id,
    )
    app_session.add_all((outgoing, incoming))
    app_session.commit()
    outgoing_id, incoming_id = outgoing.id, incoming.id

    with pytest.raises(library_control.LibraryPolicyError, match="managed root"):
        library_control.delete_personal_library(app_session, library)

    # A later caller commit must not persist the pre-validation detach/delete.
    app_session.commit()
    app_session.expire_all()
    assert marker.read_text(encoding="utf-8") == "retained"
    assert app_session.query(ub.Library).filter_by(id=library_id).count() == 1
    assert app_session.query(ub.BookShare).filter_by(
        id=outgoing_id,
        source_library_id=library_id,
    ).count() == 1
    assert app_session.query(ub.BookShare).filter_by(
        id=incoming_id,
        target_library_id=library_id,
    ).count() == 1
