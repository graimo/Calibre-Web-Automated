# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import sqlite3
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cps import constants, ub
from cps.shared_books import get_shared_books_page


@pytest.fixture
def app_session(tmp_path):
    engine = create_engine("sqlite:///{}".format(tmp_path / "shared-app.db"))
    ub.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def add_user(session, name, role=constants.ROLE_USER):
    user = ub.User(
        name=name,
        email="{}@example.test".format(name),
        role=role,
        password="test",
    )
    session.add(user)
    session.commit()
    return user


def create_calibre_library(path, books):
    path.mkdir()
    with sqlite3.connect(path / "metadata.db") as database:
        database.executescript("""
            CREATE TABLE books (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                author_sort TEXT,
                path TEXT NOT NULL DEFAULT '',
                has_cover INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE authors (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE books_authors_link (
                book INTEGER NOT NULL,
                author INTEGER NOT NULL
            );
        """)
        for book_id, title, author in books:
            database.execute(
                "INSERT INTO books (id, title, author_sort) VALUES (?, ?, ?)",
                (book_id, title, author),
            )
            database.execute(
                "INSERT INTO authors (id, name) VALUES (?, ?)",
                (book_id, author),
            )
            database.execute(
                "INSERT INTO books_authors_link (book, author) VALUES (?, ?)",
                (book_id, book_id),
            )


def add_library(session, path, slug, name):
    library = ub.Library(
        public_id=str(uuid.uuid4()),
        slug=slug,
        name=name,
        kind="shared",
        root_path=str(path),
        status="active",
    )
    session.add(library)
    session.commit()
    return library


@pytest.mark.unit
def test_shared_books_page_enforces_memberships_and_reads_correct_database(
    app_session, tmp_path
):
    sharer = add_user(app_session, "sharer")
    recipient = add_user(app_session, "recipient")
    outsider = add_user(app_session, "outsider")
    admin = add_user(app_session, "admin-shares", constants.ROLE_ADMIN)

    source_path = tmp_path / "source"
    first_target_path = tmp_path / "target-one"
    second_target_path = tmp_path / "target-two"
    create_calibre_library(source_path, [(1, "Original title", "Ada Author")])
    create_calibre_library(first_target_path, [(9, "Recipient copy", "Ada Author")])
    create_calibre_library(second_target_path, [(10, "Private target copy", "Bob Writer")])
    source = add_library(app_session, source_path, "source", "Source Library")
    first_target = add_library(
        app_session, first_target_path, "target-one", "Recipient Library"
    )
    second_target = add_library(
        app_session, second_target_path, "target-two", "Private Library"
    )
    app_session.add_all((
        ub.LibraryMembership(
            library_id=source.id,
            user_id=sharer.id,
            role="manager",
            is_default=True,
        ),
        ub.LibraryMembership(
            library_id=first_target.id,
            user_id=recipient.id,
            role="viewer",
            is_default=True,
        ),
        ub.BookShare(
            source_library_id=source.id,
            source_book_id=1,
            target_library_id=first_target.id,
            target_book_id=9,
            shared_by_user_id=sharer.id,
        ),
        ub.BookShare(
            source_library_id=source.id,
            source_book_id=1,
            target_library_id=second_target.id,
            target_book_id=10,
            shared_by_user_id=sharer.id,
        ),
    ))
    app_session.commit()

    recipient_page = get_shared_books_page(app_session, recipient)
    assert recipient_page.total == 1
    assert len(recipient_page.items) == 1
    assert recipient_page.items[0].title == "Recipient copy"
    assert recipient_page.items[0].authors == "Ada Author"
    assert recipient_page.items[0].direction == "incoming"
    assert recipient_page.items[0].displayed_library == "Recipient Library"

    sharer_page = get_shared_books_page(app_session, sharer)
    assert sharer_page.total == 2
    assert {item.title for item in sharer_page.items} == {"Original title"}
    assert all(item.direction == "outgoing" for item in sharer_page.items)

    assert get_shared_books_page(app_session, outsider).items == []

    admin_page = get_shared_books_page(app_session, admin)
    assert admin_page.total == 0
    assert admin_page.items == []

    app_session.add(ub.LibraryMembership(
        library_id=first_target.id,
        user_id=admin.id,
        role="manager",
        is_default=True,
    ))
    app_session.commit()
    admin_page = get_shared_books_page(app_session, admin)
    assert admin_page.total == 1
    assert [item.title for item in admin_page.items] == ["Recipient copy"]


@pytest.mark.unit
def test_shared_books_page_keeps_deleted_sharer_attribution(app_session, tmp_path):
    sharer = add_user(app_session, "former-sharer")
    recipient = add_user(app_session, "history-reader")
    source_path = tmp_path / "history-source"
    target_path = tmp_path / "history-target"
    create_calibre_library(source_path, [(1, "Source", "Author")])
    create_calibre_library(target_path, [(2, "Retained copy", "Author")])
    source = add_library(app_session, source_path, "history-source", "History Source")
    target = add_library(app_session, target_path, "history-target", "History Target")
    app_session.add(ub.LibraryMembership(
        library_id=target.id,
        user_id=recipient.id,
        role="viewer",
        is_default=True,
    ))
    share = ub.BookShare(
        source_library_id=source.id,
        source_book_id=1,
        target_library_id=target.id,
        target_book_id=2,
        shared_by_user_id=sharer.id,
    )
    app_session.add(share)
    app_session.commit()
    share.shared_by_user_id = None
    app_session.delete(sharer)
    app_session.commit()

    card = get_shared_books_page(app_session, recipient).items[0]
    assert card.title == "Retained copy"
    assert card.shared_by == "former-sharer"
    assert card.direction == "incoming"


@pytest.mark.unit
def test_shared_books_route_template_and_sidebar_are_wired():
    project_root = Path(__file__).resolve().parents[2]
    web_source = (project_root / "cps" / "web.py").read_text(encoding="utf-8")
    sidebar_source = (project_root / "cps" / "render_template.py").read_text(
        encoding="utf-8"
    )
    template = project_root / "cps" / "templates" / "shared_books.html"

    assert '@web.route("/shared-books")' in web_source
    assert '"web.shared_books"' in sidebar_source
    assert "Shared Books" in template.read_text(encoding="utf-8")


@pytest.mark.unit
def test_shared_books_rejects_symlink_library_root(app_session, tmp_path):
    sharer = add_user(app_session, "symlink-sharer")
    recipient = add_user(app_session, "symlink-recipient")
    source_path = tmp_path / "safe-source"
    external_path = tmp_path / "external-private"
    linked_path = tmp_path / "linked-target"
    create_calibre_library(source_path, [(1, "Safe source", "Author")])
    create_calibre_library(external_path, [(2, "Must not be exposed", "Private")])
    linked_path.symlink_to(external_path, target_is_directory=True)
    source = add_library(app_session, source_path, "safe-source", "Safe Source")
    target = add_library(app_session, linked_path, "linked-target", "Linked Target")
    app_session.add_all((
        ub.LibraryMembership(
            library_id=target.id,
            user_id=recipient.id,
            role="viewer",
            is_default=True,
        ),
        ub.BookShare(
            source_library_id=source.id,
            source_book_id=1,
            target_library_id=target.id,
            target_book_id=2,
            shared_by_user_id=sharer.id,
        ),
    ))
    app_session.commit()

    card = get_shared_books_page(app_session, recipient).items[0]
    assert card.available is False
    assert card.title == "Unavailable shared book"
    assert "Must not be exposed" not in card.title
