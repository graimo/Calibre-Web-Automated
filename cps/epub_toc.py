# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Native (Calibre-free) inspection of an EPUB's table of contents.

Determines whether an EPUB carries a usable chapter structure by counting the
entries of its navigation document:
  * EPUB 3: the nav document (manifest item with properties="nav"),
    <nav epub:type="toc"> ... <a> entries.
  * EPUB 2: the NCX (spine @toc or media-type application/x-dtbncx+xml),
    <navPoint> entries.

Only standard-library + lxml are used, so there is no dependency on the Calibre
runtime. A companion generator (build a nav/NCX from heading structure) can be
layered on top of these primitives.
"""

import posixpath
import zipfile

from lxml import etree

from cps import logger

log = logger.create()

CONTAINER_PATH = "META-INF/container.xml"
NCX_MEDIA_TYPE = "application/x-dtbncx+xml"


def _local(tag):
    """Local name of a possibly-namespaced lxml tag."""
    if isinstance(tag, str):
        return tag.rsplit("}", 1)[-1]
    return ""


def _find_opf_path(zf):
    try:
        root = etree.fromstring(zf.read(CONTAINER_PATH))
    except (KeyError, etree.XMLSyntaxError):
        return None
    for element in root.iter():
        if _local(element.tag) == "rootfile" and element.get("full-path"):
            return element.get("full-path")
    return None


def _resolve(base_dir, href):
    href = (href or "").split("#", 1)[0]
    if not href:
        return None
    return posixpath.normpath(posixpath.join(base_dir, href)) if base_dir else href


def _count_nav_entries(zf, nav_path):
    try:
        root = etree.fromstring(zf.read(nav_path))
    except (KeyError, etree.XMLSyntaxError):
        return 0
    navs = [e for e in root.iter() if _local(e.tag) == "nav"]
    # Prefer the nav explicitly typed as the TOC (epub:type="toc").
    toc_nav = None
    for nav in navs:
        for key, value in nav.attrib.items():
            if _local(key) == "type" and "toc" in (value or "").lower():
                toc_nav = nav
                break
        if toc_nav is not None:
            break
    target = toc_nav if toc_nav is not None else (navs[0] if navs else None)
    if target is None:
        return 0
    return sum(1 for e in target.iter() if _local(e.tag) == "a")


def _count_ncx_entries(zf, ncx_path):
    try:
        root = etree.fromstring(zf.read(ncx_path))
    except (KeyError, etree.XMLSyntaxError):
        return 0
    return sum(1 for e in root.iter() if _local(e.tag) == "navPoint")


def toc_entry_count(epub_path):
    """Return the number of TOC entries found (0 if none / on error)."""
    try:
        with zipfile.ZipFile(epub_path) as zf:
            opf_path = _find_opf_path(zf)
            if not opf_path:
                return 0
            opf_dir = posixpath.dirname(opf_path)
            opf = etree.fromstring(zf.read(opf_path))

            manifest = {}       # id -> (href, media_type, properties)
            for element in opf.iter():
                if _local(element.tag) == "item":
                    manifest[element.get("id")] = (
                        element.get("href"),
                        (element.get("media-type") or "").strip().lower(),
                        (element.get("properties") or ""),
                    )

            counts = [0]

            # EPUB 3 nav document.
            for href, _media, properties in manifest.values():
                if "nav" in properties.split():
                    nav_path = _resolve(opf_dir, href)
                    if nav_path:
                        counts.append(_count_nav_entries(zf, nav_path))

            # EPUB 2 NCX: spine @toc first, then any ncx media type.
            ncx_href = None
            for element in opf.iter():
                if _local(element.tag) == "spine" and element.get("toc"):
                    entry = manifest.get(element.get("toc"))
                    if entry:
                        ncx_href = entry[0]
                    break
            if not ncx_href:
                for href, media, _properties in manifest.values():
                    if media == NCX_MEDIA_TYPE:
                        ncx_href = href
                        break
            if ncx_href:
                ncx_path = _resolve(opf_dir, ncx_href)
                if ncx_path:
                    counts.append(_count_ncx_entries(zf, ncx_path))

            return max(counts)
    except (zipfile.BadZipFile, OSError, etree.XMLSyntaxError) as error:
        log.debug("toc_entry_count failed for %s: %s", epub_path, error)
        return 0


def has_valid_toc(epub_path, min_entries=2):
    """True if the EPUB has a navigation document with at least min_entries."""
    return toc_entry_count(epub_path) >= max(1, min_entries)
