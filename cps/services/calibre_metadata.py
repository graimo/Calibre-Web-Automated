# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Access Calibre's configured metadata sources through its command line tool."""

from __future__ import annotations

import math
import os
import re
import signal
import subprocess
import tempfile
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence, Union

DEFAULT_EXECUTABLE = "fetch-ebook-metadata"
DEFAULT_DEBUG_EXECUTABLE = "calibre-debug"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_OUTPUT_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_COVER_BYTES = 16 * 1024 * 1024
_IDENTIFIER_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")

# Delimiter used to separate the individual OPF documents printed by the
# identify helper script (see _IDENTIFY_SCRIPT). Must not appear in OPF output.
_CANDIDATE_DELIM = "<<<CWA-OPF-DELIM>>>"

# Script executed via `calibre-debug -c`. Unlike `fetch-ebook-metadata` (which
# merges every source into one OPF), Calibre's identify() returns a ranked list
# of candidates. Parameters are passed via environment variables (CWA_ID_*) to
# avoid any shell quoting/injection. Calibre's own logging is muted / redirected
# to stderr so stdout carries only the delimited OPF documents.
_IDENTIFY_SCRIPT = r"""
import os, sys
from threading import Event
try:
    from calibre.ebooks.metadata.sources.identify import identify
    from calibre.ebooks.metadata.opf2 import metadata_to_opf
    from calibre.utils.logging import Log
except Exception as e:
    sys.stderr.write("cwa-import-error: %r\n" % (e,))
    sys.exit(3)

title = os.environ.get("CWA_ID_TITLE") or None
authors = [a for a in (os.environ.get("CWA_ID_AUTHORS") or "").split("\n") if a] or None
isbn = os.environ.get("CWA_ID_ISBN") or None
timeout = int(os.environ.get("CWA_ID_TIMEOUT") or "30")
max_results = int(os.environ.get("CWA_ID_MAX") or "5")
identifiers = {}
if isbn:
    identifiers["isbn"] = isbn

log = Log()
try:
    log.outputs = []
except Exception:
    pass
abort = Event()
real_stdout = sys.stdout
sys.stdout = sys.stderr
try:
    results = identify(log, abort, title=title, authors=authors,
                       identifiers=identifiers, timeout=timeout)
except Exception as e:
    sys.stdout = real_stdout
    sys.stderr.write("cwa-identify-error: %r\n" % (e,))
    sys.exit(4)
sys.stdout = real_stdout

parts = []
for mi in (results or [])[:max_results]:
    try:
        opf = metadata_to_opf(mi)
        if isinstance(opf, bytes):
            opf = opf.decode("utf-8", "replace")
        parts.append(opf)
    except Exception as e:
        sys.stderr.write("cwa-opf-error: %r\n" % (e,))
real_stdout.write(("\n<<<CWA-OPF-DELIM>>>\n").join(parts))
real_stdout.flush()
"""


class CalibreMetadataError(RuntimeError):
    """Base error raised by the Calibre metadata service."""


class CalibreMetadataExecutableNotFound(CalibreMetadataError):
    """Raised when ``fetch-ebook-metadata`` cannot be started."""


class CalibreMetadataProcessError(CalibreMetadataError):
    """Raised when the Calibre command exits unsuccessfully."""

    def __init__(self, returncode: int, detail: str = ""):
        self.returncode = returncode
        super().__init__(f"fetch-ebook-metadata exited with status {returncode}{detail}")


class CalibreMetadataTimeout(CalibreMetadataError):
    """Raised when the Calibre command exceeds the overall timeout."""


class CalibreMetadataOutputTooLarge(CalibreMetadataError):
    """Raised when metadata or cover output exceeds its configured limit."""


@dataclass
class CalibreMetadata:
    """Metadata fields represented by an OPF document emitted by Calibre."""

    title: str = ""
    authors: list[str] = field(default_factory=list)
    publisher: str = ""
    published_date: str = ""
    description: str = ""
    series: Optional[str] = None
    series_index: Optional[float] = None
    rating: Optional[float] = None
    languages: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    identifiers: dict[str, str] = field(default_factory=dict)


@dataclass
class CalibreMetadataFetchResult:
    """Parsed metadata and the optional cover downloaded by Calibre."""

    metadata: CalibreMetadata
    cover: Optional[bytes] = None


def _local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _element_text(element: Optional[ET.Element]) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def _first(elements: Sequence[ET.Element]) -> Optional[ET.Element]:
    return elements[0] if elements else None


def _unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _optional_float(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_opf(opf: Union[str, bytes]) -> CalibreMetadata:
    """Parse metadata from a Calibre OPF document.

    Namespace prefixes are intentionally ignored because OPF producers may use
    different prefixes for the same Dublin Core and OPF namespaces.
    """

    try:
        root = ET.fromstring(opf)
    except (ET.ParseError, TypeError, ValueError) as error:
        raise CalibreMetadataError("Calibre returned invalid OPF metadata") from error

    metadata_elements = [
        element for element in root.iter() if _local_name(element.tag) == "metadata"
    ]
    metadata_root = metadata_elements[0] if metadata_elements else root

    by_name: dict[str, list[ET.Element]] = {}
    calibre_meta: dict[str, str] = {}
    for element in metadata_root.iter():
        local_name = _local_name(element.tag)
        by_name.setdefault(local_name, []).append(element)
        if local_name != "meta":
            continue
        attributes = {_local_name(key): value for key, value in element.attrib.items()}
        name = attributes.get("name", "").lower()
        if name.startswith("calibre:"):
            calibre_meta[name] = attributes.get(
                "content", _element_text(element)
            ).strip()

    authors = []
    for creator in by_name.get("creator", []):
        attributes = {_local_name(key): value for key, value in creator.attrib.items()}
        role = attributes.get("role", "").lower()
        if role in ("", "aut"):
            authors.append(_element_text(creator))

    identifiers: dict[str, str] = {}
    for identifier in by_name.get("identifier", []):
        value = _element_text(identifier)
        if not value:
            continue
        attributes = {_local_name(key): item for key, item in identifier.attrib.items()}
        scheme = attributes.get("scheme", "").strip().lower()
        if not scheme and value.lower().startswith("urn:isbn:"):
            scheme, value = "isbn", value[9:]
        identifiers[scheme or "identifier"] = value

    for name, value in calibre_meta.items():
        if name.startswith("calibre:identifier:") and value:
            identifiers[name.removeprefix("calibre:identifier:")] = value

    return CalibreMetadata(
        title=_element_text(_first(by_name.get("title", []))),
        authors=_unique(authors),
        publisher=_element_text(_first(by_name.get("publisher", []))),
        published_date=_element_text(_first(by_name.get("date", []))),
        description=_element_text(_first(by_name.get("description", []))),
        series=calibre_meta.get("calibre:series") or None,
        series_index=_optional_float(calibre_meta.get("calibre:series_index")),
        rating=_optional_float(calibre_meta.get("calibre:rating")),
        languages=_unique(
            [_element_text(element) for element in by_name.get("language", [])]
        ),
        tags=_unique(
            [_element_text(element) for element in by_name.get("subject", [])]
        ),
        identifiers=identifiers,
    )


def _validate_text(name: str, value: Optional[str], max_length: int) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    value = value.strip()
    if len(value) > max_length:
        raise ValueError(f"{name} exceeds {max_length} characters")
    return value or None


def _normalize_authors(authors: Union[str, Sequence[str], None]) -> list[str]:
    if authors is None:
        return []
    if isinstance(authors, str):
        authors = [authors]
    if len(authors) > 32:
        raise ValueError("authors exceeds 32 entries")
    return [
        value
        for author in authors
        if (value := _validate_text("author", author, 500)) is not None
    ]


def _normalize_identifiers(
    identifiers: Optional[Mapping[str, str]],
) -> dict[str, str]:
    if identifiers is None:
        return {}
    if len(identifiers) > 32:
        raise ValueError("identifiers exceeds 32 entries")
    normalized = {}
    for name, raw_value in identifiers.items():
        if not isinstance(name, str) or not _IDENTIFIER_NAME.fullmatch(name):
            raise ValueError(f"invalid identifier name: {name!r}")
        value = _validate_text(f"identifier {name}", raw_value, 1024)
        if value is not None:
            normalized[name.lower()] = value
    return normalized


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:  # pragma: no cover - exercised on Windows
            process.kill()
    except ProcessLookupError:
        pass


class CalibreMetadataService:
    """Safe wrapper around Calibre's ``fetch-ebook-metadata`` executable."""

    def __init__(
        self,
        executable: Union[str, os.PathLike[str]] = DEFAULT_EXECUTABLE,
        *,
        debug_executable: Union[str, os.PathLike[str]] = DEFAULT_DEBUG_EXECUTABLE,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        max_cover_bytes: int = DEFAULT_MAX_COVER_BYTES,
    ) -> None:
        self.executable = os.fspath(executable)
        self.debug_executable = os.fspath(debug_executable)
        if not self.executable or not self.debug_executable:
            raise ValueError("executable must not be empty")
        if max_output_bytes <= 0 or max_cover_bytes <= 0:
            raise ValueError("output limits must be positive")
        self.max_output_bytes = max_output_bytes
        self.max_cover_bytes = max_cover_bytes

    def _run_capture(self, command, timeout, env=None):
        """Run a command with a wall-clock timeout, capturing stdout and a
        bounded stderr tail. Returns (returncode, stdout_bytes, stderr_snippet,
        timed_out). The process group is killed on timeout / oversized output."""
        try:
            process = subprocess.Popen(  # nosec B603
                command,
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=os.name == "posix",
                env=env,
            )
        except FileNotFoundError as error:
            raise CalibreMetadataExecutableNotFound(
                "%s executable was not found" % command[0]
            ) from error
        except OSError as error:
            raise CalibreMetadataError("%s could not be started" % command[0]) from error

        stdout = bytearray()
        stderr_tail = bytearray()
        output_too_large = threading.Event()
        output_read_error: list[OSError] = []

        def read_stdout() -> None:
            try:
                if process.stdout is None:
                    raise OSError("output pipe is unavailable")
                read_size = min(64 * 1024, self.max_output_bytes + 1)
                while chunk := process.stdout.read(read_size):
                    if len(stdout) + len(chunk) > self.max_output_bytes:
                        output_too_large.set()
                        _terminate_process_group(process)
                        return
                    stdout.extend(chunk)
            except OSError as error:
                output_read_error.append(error)
                _terminate_process_group(process)

        def read_stderr() -> None:
            try:
                if process.stderr is None:
                    return
                while chunk := process.stderr.read(4096):
                    stderr_tail.extend(chunk)
                    if len(stderr_tail) > 8192:
                        del stderr_tail[:-8192]
            except OSError:
                pass

        t_out = threading.Thread(target=read_stdout, daemon=True)
        t_err = threading.Thread(target=read_stderr, daemon=True)
        t_out.start()
        t_err.start()

        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_process_group(process)
            process.wait()
            timed_out = True

        t_out.join()
        t_err.join(timeout=1)
        snippet = bytes(stderr_tail).decode("utf-8", "replace").strip()
        snippet = (" | calibre stderr: " + snippet[-500:]) if snippet else ""

        if output_too_large.is_set():
            raise CalibreMetadataOutputTooLarge(
                "Calibre metadata output exceeds the configured limit"
            )
        if output_read_error:
            raise CalibreMetadataError(
                "Calibre metadata output could not be read"
            ) from output_read_error[0]
        return process.returncode, bytes(stdout), snippet, timed_out

    def fetch_candidates(
        self,
        *,
        title: Optional[str] = None,
        authors: Union[str, Sequence[str], None] = None,
        isbn: Optional[str] = None,
        identifiers: Optional[Mapping[str, str]] = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_results: int = 8,
    ) -> "list[CalibreMetadata]":
        """Return multiple ranked metadata candidates via Calibre's identify()."""
        if (
            not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a positive finite number")

        title = _validate_text("title", title, 1000)
        author_list = _normalize_authors(authors)
        isbn = _validate_text("isbn", isbn, 64)
        identifier_map = _normalize_identifiers(identifiers)
        if isbn is None:
            isbn = identifier_map.pop("isbn", None)
        if not any((title, author_list, isbn)):
            raise ValueError("at least one title, author or ISBN is required")

        # Give Calibre a shorter inner timeout than the outer wall-clock wait so
        # it can finish (startup included) before the wrapper kills it.
        inner_timeout = max(1, math.ceil(timeout) - 5)
        try:
            max_results = max(1, min(int(max_results), 20))
        except (TypeError, ValueError):
            max_results = 8

        env = dict(os.environ)
        env["CWA_ID_TITLE"] = title or ""
        env["CWA_ID_AUTHORS"] = "\n".join(author_list)
        env["CWA_ID_ISBN"] = isbn or ""
        env["CWA_ID_TIMEOUT"] = str(inner_timeout)
        env["CWA_ID_MAX"] = str(max_results)

        command = [self.debug_executable, "-c", _IDENTIFY_SCRIPT]
        returncode, out, snippet, timed_out = self._run_capture(command, timeout, env)
        if timed_out:
            raise CalibreMetadataTimeout(
                f"calibre-debug identify exceeded {timeout:g} seconds{snippet}"
            )
        if returncode:
            raise CalibreMetadataProcessError(returncode, snippet)

        text = out.decode("utf-8", "replace")
        candidates: list[CalibreMetadata] = []
        for chunk in text.split(_CANDIDATE_DELIM):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                candidates.append(parse_opf(chunk))
            except CalibreMetadataError:
                continue
        return candidates

    def fetch(
        self,
        *,
        title: Optional[str] = None,
        authors: Union[str, Sequence[str], None] = None,
        isbn: Optional[str] = None,
        identifiers: Optional[Mapping[str, str]] = None,
        allowed_plugins: Optional[Sequence[str]] = None,
        timeout: float = DEFAULT_TIMEOUT,
        fetch_cover: bool = True,
    ) -> CalibreMetadataFetchResult:
        """Fetch one best metadata candidate using Calibre's configured plugins."""

        if (
            not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a positive finite number")

        title = _validate_text("title", title, 1000)
        author_list = _normalize_authors(authors)
        isbn = _validate_text("isbn", isbn, 64)
        identifier_map = _normalize_identifiers(identifiers)
        if isbn is None:
            isbn = identifier_map.pop("isbn", None)

        plugin_list = []
        if allowed_plugins is not None:
            if len(allowed_plugins) > 64:
                raise ValueError("allowed_plugins exceeds 64 entries")
            plugin_list = [
                value
                for plugin in allowed_plugins
                if (value := _validate_text("plugin name", plugin, 200)) is not None
            ]

        if not any((title, author_list, isbn, identifier_map)):
            raise ValueError(
                "at least one title, author, ISBN, or identifier is required"
            )

        # fetch-ebook-metadata needs its own per-source timeout PLUS time to start
        # Calibre and download the cover. If the inner --timeout equals the outer
        # wall-clock wait, the wrapper kills the process exactly when Calibre would
        # be finishing, so it never returns results. Give Calibre a shorter inner
        # timeout and reserve headroom for the outer wait.
        inner_timeout = max(1, math.ceil(timeout) - 5)

        with tempfile.TemporaryDirectory(prefix="cwa-calibre-metadata-") as temp_dir:
            cover_path = Path(temp_dir, "cover")
            command = [
                self.executable,
                "--opf",
                "--timeout",
                str(inner_timeout),
            ]
            if title:
                command.extend(("--title", title))
            if author_list:
                command.extend(("--authors", " & ".join(author_list)))
            if isbn:
                command.extend(("--isbn", isbn))
            for name, value in identifier_map.items():
                command.extend(("--identifier", f"{name}:{value}"))
            for plugin in plugin_list:
                command.extend(("--allowed-plugin", plugin))
            if fetch_cover:
                command.extend(("--cover", os.fspath(cover_path)))

            try:
                process = subprocess.Popen(  # nosec B603
                    command,
                    shell=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=os.name == "posix",
                )
            except FileNotFoundError as error:
                raise CalibreMetadataExecutableNotFound(
                    "fetch-ebook-metadata executable was not found"
                ) from error
            except OSError as error:
                raise CalibreMetadataError(
                    "fetch-ebook-metadata could not be started"
                ) from error

            stdout = bytearray()
            output_too_large = threading.Event()
            output_read_error: list[OSError] = []

            def read_stdout() -> None:
                try:
                    if process.stdout is None:  # pragma: no cover - Popen contract
                        raise OSError("Calibre metadata output pipe is unavailable")
                    read_size = min(64 * 1024, self.max_output_bytes + 1)
                    while chunk := process.stdout.read(read_size):
                        if len(stdout) + len(chunk) > self.max_output_bytes:
                            output_too_large.set()
                            _terminate_process_group(process)
                            return
                        stdout.extend(chunk)
                except OSError as error:
                    output_read_error.append(error)
                    _terminate_process_group(process)

            stdout_reader = threading.Thread(
                target=read_stdout,
                name="calibre-metadata-stdout",
                daemon=True,
            )
            stdout_reader.start()

            # Capture stderr so failures/timeouts are diagnosable (Calibre logs
            # which sources it tried and why they failed there). Bounded to avoid
            # unbounded growth; the pipe must be drained or the child can block.
            stderr_tail = bytearray()

            def read_stderr() -> None:
                try:
                    if process.stderr is None:
                        return
                    while chunk := process.stderr.read(4096):
                        stderr_tail.extend(chunk)
                        if len(stderr_tail) > 8192:
                            del stderr_tail[:-8192]
                except OSError:
                    pass

            stderr_reader = threading.Thread(
                target=read_stderr,
                name="calibre-metadata-stderr",
                daemon=True,
            )
            stderr_reader.start()

            def _stderr_snippet() -> str:
                stderr_reader.join(timeout=1)
                text = bytes(stderr_tail).decode("utf-8", "replace").strip()
                return (" | calibre stderr: " + text[-500:]) if text else ""

            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                _terminate_process_group(process)
                process.wait()
                stdout_reader.join()
                raise CalibreMetadataTimeout(
                    f"fetch-ebook-metadata exceeded {timeout:g} seconds{_stderr_snippet()}"
                ) from error

            stdout_reader.join()
            if output_too_large.is_set():
                raise CalibreMetadataOutputTooLarge(
                    "Calibre metadata output exceeds the configured limit"
                )
            if output_read_error:
                raise CalibreMetadataError(
                    "Calibre metadata output could not be read"
                ) from output_read_error[0]
            if process.returncode:
                raise CalibreMetadataProcessError(process.returncode, _stderr_snippet())

            metadata = parse_opf(bytes(stdout))
            cover = None
            if fetch_cover and cover_path.is_file():
                cover_size = cover_path.stat().st_size
                if cover_size > self.max_cover_bytes:
                    raise CalibreMetadataOutputTooLarge(
                        "Calibre cover output exceeds the configured limit"
                    )
                cover = cover_path.read_bytes()

            return CalibreMetadataFetchResult(metadata=metadata, cover=cover)


__all__ = [
    "CalibreMetadata",
    "CalibreMetadataError",
    "CalibreMetadataExecutableNotFound",
    "CalibreMetadataFetchResult",
    "CalibreMetadataOutputTooLarge",
    "CalibreMetadataProcessError",
    "CalibreMetadataService",
    "CalibreMetadataTimeout",
    "parse_opf",
]
