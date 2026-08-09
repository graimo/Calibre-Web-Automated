# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

import importlib.util
import json
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest


def _load_module():
    module_path = (
        Path(__file__).resolve().parents[2] / "cps" / "services" / "calibre_metadata.py"
    )
    spec = importlib.util.spec_from_file_location("calibre_metadata", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


calibre_metadata = _load_module()

OPF = b"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf"
         xmlns:dc="http://purl.org/dc/elements/1.1/"
         unique-identifier="uuid_id">
  <metadata>
    <dc:title>The Example Book</dc:title>
    <dc:creator role="aut">Ada Example</dc:creator>
    <dc:creator role="edt">Eve Editor</dc:creator>
    <dc:creator role="aut">Bob Writer</dc:creator>
    <dc:publisher>Example Press</dc:publisher>
    <dc:date>2026-08-07</dc:date>
    <dc:description>First <em>edition</em>.</dc:description>
    <dc:language>eng</dc:language>
    <dc:language>ita</dc:language>
    <dc:subject>Fiction</dc:subject>
    <dc:subject>Testing</dc:subject>
    <dc:identifier scheme="ISBN">9781234567890</dc:identifier>
    <dc:identifier scheme="AMAZON">B012345678</dc:identifier>
    <meta name="calibre:series" content="Examples" />
    <meta name="calibre:series_index" content="2.5" />
    <meta name="calibre:rating" content="8" />
    <meta name="calibre:identifier:goodreads" content="12345" />
  </metadata>
</package>
"""


@pytest.fixture
def fake_executable(tmp_path):
    executable = tmp_path / "fetch-ebook-metadata"
    executable.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json
            import os
            import sys
            import time

            mode = os.environ.get("CWA_FAKE_METADATA_MODE", "success")
            capture_path = os.environ.get("CWA_FAKE_METADATA_CAPTURE")
            if capture_path:
                with open(capture_path, "w", encoding="utf-8") as capture:
                    json.dump(sys.argv[1:], capture)
            if mode == "timeout":
                time.sleep(10)
            if mode == "error":
                sys.stderr.write("private plugin diagnostic")
                raise SystemExit(7)
            if "--cover" in sys.argv:
                cover_path = sys.argv[sys.argv.index("--cover") + 1]
                with open(cover_path, "wb") as cover:
                    cover.write(b"fake-cover")
            sys.stdout.buffer.write({OPF!r})
            """
        ),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


def test_parse_opf_extracts_calibre_and_dublin_core_metadata():
    result = calibre_metadata.parse_opf(OPF)

    assert result.title == "The Example Book"
    assert result.authors == ["Ada Example", "Bob Writer"]
    assert result.publisher == "Example Press"
    assert result.published_date == "2026-08-07"
    assert result.description == "First edition."
    assert result.languages == ["eng", "ita"]
    assert result.tags == ["Fiction", "Testing"]
    assert result.identifiers == {
        "isbn": "9781234567890",
        "amazon": "B012345678",
        "goodreads": "12345",
    }
    assert result.series == "Examples"
    assert result.series_index == 2.5
    assert result.rating == 8.0


def test_parse_opf_rejects_invalid_xml():
    with pytest.raises(
        calibre_metadata.CalibreMetadataError,
        match="invalid OPF",
    ):
        calibre_metadata.parse_opf(b"not XML")


def test_fetch_uses_argument_list_and_returns_cover(
    fake_executable, monkeypatch, tmp_path
):
    capture_path = tmp_path / "arguments.json"
    injection_marker = tmp_path / "should-not-exist"
    title = f"A title; touch {injection_marker}"
    monkeypatch.setenv("CWA_FAKE_METADATA_CAPTURE", os.fspath(capture_path))
    service = calibre_metadata.CalibreMetadataService(fake_executable)

    result = service.fetch(
        title=title,
        authors=["Ada Example", "Bob Writer"],
        identifiers={"isbn": "9781234567890", "goodreads": "12345"},
        allowed_plugins=["Google", "Open Library"],
        timeout=2,
    )

    arguments = json.loads(capture_path.read_text(encoding="utf-8"))
    assert arguments[:4] == ["--opf", "--timeout", "2", "--title"]
    assert arguments[4] == title
    assert arguments[arguments.index("--authors") + 1] == "Ada Example & Bob Writer"
    assert arguments[arguments.index("--isbn") + 1] == "9781234567890"
    assert "goodreads:12345" in arguments
    assert arguments.count("--allowed-plugin") == 2
    assert "--cover" in arguments
    assert not injection_marker.exists()
    assert result.metadata.title == "The Example Book"
    assert result.cover == b"fake-cover"


def test_fetch_requires_a_query(fake_executable):
    service = calibre_metadata.CalibreMetadataService(fake_executable)

    with pytest.raises(ValueError, match="at least one"):
        service.fetch()


def test_fetch_reports_nonzero_exit_without_exposing_stderr(
    fake_executable, monkeypatch
):
    monkeypatch.setenv("CWA_FAKE_METADATA_MODE", "error")
    service = calibre_metadata.CalibreMetadataService(fake_executable)

    with pytest.raises(
        calibre_metadata.CalibreMetadataProcessError,
        match="status 7",
    ) as error:
        service.fetch(title="Example", timeout=2)

    assert "private plugin diagnostic" not in str(error.value)
    assert error.value.returncode == 7


def test_fetch_terminates_process_after_timeout(fake_executable, monkeypatch):
    monkeypatch.setenv("CWA_FAKE_METADATA_MODE", "timeout")
    service = calibre_metadata.CalibreMetadataService(fake_executable)
    started = time.monotonic()

    with pytest.raises(calibre_metadata.CalibreMetadataTimeout):
        service.fetch(title="Example", timeout=0.2)

    assert time.monotonic() - started < 3


def test_fetch_enforces_metadata_output_limit(fake_executable):
    service = calibre_metadata.CalibreMetadataService(
        fake_executable,
        max_output_bytes=32,
    )

    with pytest.raises(calibre_metadata.CalibreMetadataOutputTooLarge):
        service.fetch(title="Example", timeout=2)
