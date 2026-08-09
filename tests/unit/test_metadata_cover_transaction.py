# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from types import SimpleNamespace

import pytest

from cps import helper, metadata_helper


class FakeSettingsDB:
    def get_cwa_settings(self):
        return {
            "auto_metadata_smart_application": False,
            "auto_metadata_update_title": False,
            "auto_metadata_update_authors": False,
            "auto_metadata_update_description": False,
            "auto_metadata_update_publisher": False,
            "auto_metadata_update_tags": False,
            "auto_metadata_update_series": False,
            "auto_metadata_update_published_date": False,
            "auto_metadata_update_rating": False,
            "auto_metadata_update_identifiers": False,
            "auto_metadata_update_cover": True,
        }


class FakeSession:
    def __init__(self, commit_error=None):
        self.commit_error = commit_error
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1
        if self.commit_error:
            raise self.commit_error

    def rollback(self):
        self.rollbacks += 1


@pytest.fixture
def cover_metadata_context(monkeypatch):
    monkeypatch.setattr(metadata_helper, "CWA_DB", FakeSettingsDB)
    book = SimpleNamespace(id=7, path="Author/Book", has_cover=1)
    metadata = SimpleNamespace(cover="data:image/jpeg;base64,/9j/")
    return book, metadata


@pytest.mark.unit
def test_cover_is_restored_when_metadata_commit_fails(
    monkeypatch, cover_metadata_context
):
    book, metadata = cover_metadata_context
    session = FakeSession(RuntimeError("commit failed"))
    calibre_db = SimpleNamespace(session=session)
    token = {"snapshot": True}
    restored = []
    discarded = []
    thumbnails = []

    monkeypatch.setattr(helper, "create_cover_backup", lambda path: token)
    monkeypatch.setattr(helper, "save_cover_from_url", lambda url, path: (True, None))
    monkeypatch.setattr(helper, "restore_cover_backup", restored.append)
    monkeypatch.setattr(helper, "discard_cover_backup", discarded.append)
    monkeypatch.setattr(
        helper,
        "replace_cover_thumbnail_cache",
        lambda *args, **kwargs: thumbnails.append(args),
    )

    assert metadata_helper._apply_metadata_to_book(book, metadata, calibre_db) is False
    assert session.commits == 1
    assert session.rollbacks == 1
    assert restored == [token]
    assert discarded == []
    assert thumbnails == []


@pytest.mark.unit
def test_cover_backup_is_discarded_after_successful_commit(
    monkeypatch, cover_metadata_context
):
    book, metadata = cover_metadata_context
    session = FakeSession()
    calibre_db = SimpleNamespace(session=session)
    token = {"snapshot": True}
    restored = []
    discarded = []
    thumbnails = []

    monkeypatch.setattr(helper, "create_cover_backup", lambda path: token)
    monkeypatch.setattr(helper, "save_cover_from_url", lambda url, path: (True, None))
    monkeypatch.setattr(helper, "restore_cover_backup", restored.append)
    monkeypatch.setattr(helper, "discard_cover_backup", discarded.append)
    monkeypatch.setattr(
        helper,
        "replace_cover_thumbnail_cache",
        lambda *args, **kwargs: thumbnails.append((args, kwargs)),
    )

    assert metadata_helper._apply_metadata_to_book(book, metadata, calibre_db) is True
    assert session.commits == 1
    assert session.rollbacks == 0
    assert restored == []
    assert discarded == [token]
    assert thumbnails == [((7,), {"book_path": "Author/Book"})]


@pytest.mark.unit
def test_local_cover_backup_restores_previous_file(monkeypatch, tmp_path):
    monkeypatch.setattr(helper.config, "config_use_google_drive", False, raising=False)
    monkeypatch.setattr(helper.config, "get_book_path", lambda: str(tmp_path), raising=False)
    cover_dir = tmp_path / "Author" / "Book"
    cover_dir.mkdir(parents=True)
    cover_path = cover_dir / "cover.jpg"
    cover_path.write_bytes(b"old-cover")

    token = helper.create_cover_backup("Author/Book")
    cover_path.write_bytes(b"new-cover")
    helper.restore_cover_backup(token)

    assert cover_path.read_bytes() == b"old-cover"


@pytest.mark.unit
def test_local_cover_backup_removes_new_file_when_none_existed(monkeypatch, tmp_path):
    monkeypatch.setattr(helper.config, "config_use_google_drive", False, raising=False)
    monkeypatch.setattr(helper.config, "get_book_path", lambda: str(tmp_path), raising=False)
    cover_dir = tmp_path / "Author" / "Book"
    cover_dir.mkdir(parents=True)
    cover_path = cover_dir / "cover.jpg"

    token = helper.create_cover_backup("Author/Book")
    cover_path.write_bytes(b"new-cover")
    helper.restore_cover_backup(token)

    assert not cover_path.exists()


@pytest.mark.unit
def test_atomic_cover_write_preserves_existing_file_on_replace_failure(
    monkeypatch, tmp_path
):
    cover_path = tmp_path / "cover.jpg"
    cover_path.write_bytes(b"old-cover")
    response = helper.requests.Response()
    response.status_code = 200
    response.headers["content-type"] = "image/jpeg"
    response._content = b"new-cover"

    def fail_replace(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr(helper.os, "replace", fail_replace)
    saved, _message = helper.save_cover_from_filestorage(
        str(tmp_path), "cover.jpg", response
    )

    assert saved is False
    assert cover_path.read_bytes() == b"old-cover"
    assert not list(tmp_path.glob(".cwa-cover-*"))


@pytest.mark.unit
def test_gdrive_cover_backup_uses_private_download(monkeypatch, tmp_path):
    monkeypatch.setattr(helper.config, "config_use_google_drive", True, raising=False)
    calls = []

    def private_download(book_path, destination_path):
        calls.append(book_path)
        with open(destination_path, "wb") as destination:
            destination.write(b"private-cover")
        return True

    monkeypatch.setattr(helper.gd, "download_cover_to_file", private_download)
    monkeypatch.setattr(
        helper.gd,
        "get_cover_via_gdrive",
        lambda _path: pytest.fail("ACL-changing cover API must not be used for backup"),
    )

    token = helper.create_cover_backup("Author/Private Book")
    try:
        assert calls == ["Author/Private Book"]
        assert token["had_cover"] is True
        with open(token["backup_path"], "rb") as backup:
            assert backup.read() == b"private-cover"
    finally:
        helper.discard_cover_backup(token)


@pytest.mark.unit
def test_failed_gdrive_restore_retains_backup(monkeypatch, tmp_path):
    backup_dir = tmp_path / "cover-backup"
    backup_dir.mkdir()
    backup_path = backup_dir / "cover.jpg"
    backup_path.write_bytes(b"original-cover")
    token = {
        "book_path": "Author/Book",
        "backup_path": str(backup_path),
        "temp_dir": str(backup_dir),
        "had_cover": True,
        "gdrive": True,
    }

    def fail_upload(*_args, **_kwargs):
        raise OSError("temporary Drive failure")

    monkeypatch.setattr(helper.gd, "uploadFileToEbooksFolder", fail_upload)
    with pytest.raises(OSError, match="Drive failure"):
        helper.restore_cover_backup(token)

    assert backup_path.read_bytes() == b"original-cover"
