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
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_OUTPUT_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_COVER_BYTES = 16 * 1024 * 1024
_IDENTIFIER_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


class CalibreMetadataError(RuntimeError):
    """Base error raised by the Calibre metadata service."""


class CalibreMetadataExecutableNotFound(CalibreMetadataError):
    """Raised when ``fetch-ebook-metadata`` cannot be started."""


class CalibreMetadataProcessError(CalibreMetadataError):
    """Raised when the Calibre command exits unsuccessfully."""

    def __init__(self, returncode: int):
        self.returncode = returncode
        super().__init__(f"fetch-ebook-metadata exited with status {returncode}")


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
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        max_cover_bytes: int = DEFAULT_MAX_COVER_BYTES,
    ) -> None:
        self.executable = os.fspath(executable)
        if not self.executable:
            raise ValueError("executable must not be empty")
        if max_output_bytes <= 0 or max_cover_bytes <= 0:
            raise ValueError("output limits must be positive")
        self.max_output_bytes = max_output_bytes
        self.max_cover_bytes = max_cover_bytes

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
                    stderr=subprocess.DEVNULL,
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

            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                _terminate_process_group(process)
                process.wait()
                stdout_reader.join()
                raise CalibreMetadataTimeout(
                    f"fetch-ebook-metadata exceeded {timeout:g} seconds"
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
                raise CalibreMetadataProcessError(process.returncode)

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
