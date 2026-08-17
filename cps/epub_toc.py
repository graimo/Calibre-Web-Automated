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


# --------------------------------------------------------------------------
# Native TOC generation (no Calibre). Builds a chapter-per-spine-document TOC
# without modifying the content documents themselves: it only adds a nav.xhtml
# (EPUB3) and a toc.ncx (EPUB2) and references them from the OPF. Runs only when
# the EPUB has no usable TOC. Writes atomically (temp file + os.replace).
# --------------------------------------------------------------------------
import html
import os
import re

OPF_NS = "http://www.idpf.org/2007/opf"

# Titles that are actually source file paths/names (converter artefacts) or file
# extensions are rejected so they don't end up as TOC entries.
_JUNK_TITLE_RE = re.compile(r"\.(pdf|epub|mobi|azw3?|kepub|txt|html?)\b", re.IGNORECASE)


def _clean_title(text):
    if not text:
        return None
    text = text.strip()
    if not text or "/" in text or "\\" in text or _JUNK_TITLE_RE.search(text):
        return None
    return text

_NAV_TEMPLATE = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">\n'
    "<head><title>Table of Contents</title></head>\n"
    '<body>\n<nav epub:type="toc" id="toc"><h1>Table of Contents</h1>\n<ol>\n'
    "{items}"
    "</ol>\n</nav>\n</body>\n</html>\n"
)

_NCX_TEMPLATE = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
    '<head><meta name="dtb:uid" content="{uid}"/></head>\n'
    "<docTitle><text>Table of Contents</text></docTitle>\n"
    "<navMap>\n{points}</navMap>\n</ncx>\n"
)


def _manifest_map(opf_root):
    manifest = {}
    for element in opf_root.iter():
        if _local(element.tag) == "item":
            manifest[element.get("id")] = (
                element.get("href"),
                (element.get("media-type") or "").strip().lower(),
                (element.get("properties") or ""),
            )
    return manifest


def _spine_doc_hrefs(opf_root, manifest):
    hrefs = []
    spine = next((e for e in opf_root.iter() if _local(e.tag) == "spine"), None)
    if spine is None:
        return hrefs
    for itemref in spine:
        if _local(itemref.tag) != "itemref":
            continue
        item = manifest.get(itemref.get("idref"))
        if item and "html" in (item[1] or ""):
            hrefs.append(item[0])
    return hrefs


def _first_heading_text(zf, path):
    try:
        root = etree.fromstring(zf.read(path))
    except (KeyError, etree.XMLSyntaxError, OSError):
        return None
    for wanted in ("h1", "h2", "h3", "title"):
        for element in root.iter():
            if _local(element.tag) == wanted:
                text = " ".join("".join(element.itertext()).split()).strip()
                if text:
                    return text[:200]
    return None


def _book_uid(opf_root):
    for element in opf_root.iter():
        if _local(element.tag) == "identifier":
            text = (element.text or "").strip()
            if text:
                return text
    return "cwa-toc"


def _augment_opf(opf_root, nav_href, nav_id, ncx_href, ncx_id):
    ns = opf_root.tag.split("}", 1)[0][1:] if opf_root.tag.startswith("{") else None
    qn = (lambda name: "{%s}%s" % (ns, name)) if ns else (lambda name: name)

    manifest = next((e for e in opf_root.iter() if _local(e.tag) == "manifest"), None)
    spine = next((e for e in opf_root.iter() if _local(e.tag) == "spine"), None)
    if manifest is None or spine is None:
        return None

    nav_item = etree.SubElement(manifest, qn("item"))
    nav_item.set("id", nav_id)
    nav_item.set("href", nav_href)
    nav_item.set("media-type", "application/xhtml+xml")
    nav_item.set("properties", "nav")

    ncx_item = etree.SubElement(manifest, qn("item"))
    ncx_item.set("id", ncx_id)
    ncx_item.set("href", ncx_href)
    ncx_item.set("media-type", NCX_MEDIA_TYPE)

    spine.set("toc", ncx_id)
    return etree.tostring(opf_root, xml_declaration=True, encoding="utf-8")


def generate_toc(epub_path, min_entries=2):
    """Generate a nav/NCX TOC for an EPUB that lacks one.

    Returns True if a TOC was generated and written, False if it was not needed
    or could not be built. The original file is only replaced on full success.
    """
    try:
        if has_valid_toc(epub_path, min_entries):
            return False

        with zipfile.ZipFile(epub_path) as zf:
            names = zf.namelist()
            opf_path = _find_opf_path(zf)
            if not opf_path:
                return False
            opf_dir = posixpath.dirname(opf_path)
            opf_root = etree.fromstring(zf.read(opf_path))
            manifest = _manifest_map(opf_root)

            entries = []  # (title, href-relative-to-opf-dir)
            for href in _spine_doc_hrefs(opf_root, manifest):
                full = _resolve(opf_dir, href)
                raw = _first_heading_text(zf, full) if full else None
                title = _clean_title(raw) \
                    or posixpath.splitext(posixpath.basename(href or "section"))[0]
                entries.append((title, href))

            if len(entries) < max(1, min_entries):
                return False

            uid = _book_uid(opf_root)
            contents = {name: zf.read(name) for name in names}

        # Build the nav and NCX documents (placed next to the OPF).
        nav_items = "".join(
            '<li><a href="%s">%s</a></li>\n' % (html.escape(href, quote=True), html.escape(title))
            for title, href in entries
        )
        points = "".join(
            '<navPoint id="np%d" playOrder="%d"><navLabel><text>%s</text></navLabel>'
            '<content src="%s"/></navPoint>\n'
            % (i, i, html.escape(title), html.escape(href, quote=True))
            for i, (title, href) in enumerate(entries, start=1)
        )
        nav_doc = _NAV_TEMPLATE.format(items=nav_items).encode("utf-8")
        ncx_doc = _NCX_TEMPLATE.format(uid=html.escape(uid, quote=True), points=points).encode("utf-8")

        new_opf = _augment_opf(opf_root, "cwa_nav.xhtml", "cwa-nav", "cwa_toc.ncx", "cwa-ncx")
        if not new_opf:
            return False

        nav_name = posixpath.join(opf_dir, "cwa_nav.xhtml") if opf_dir else "cwa_nav.xhtml"
        ncx_name = posixpath.join(opf_dir, "cwa_toc.ncx") if opf_dir else "cwa_toc.ncx"
        contents[opf_path] = new_opf
        contents[nav_name] = nav_doc
        contents[ncx_name] = ncx_doc

        tmp_path = epub_path + ".cwatoc.tmp"
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as out:
            # OCF requires 'mimetype' first and stored uncompressed.
            if "mimetype" in contents:
                out.writestr(zipfile.ZipInfo("mimetype"), contents.pop("mimetype"),
                             compress_type=zipfile.ZIP_STORED)
            for name, data in contents.items():
                out.writestr(name, data)
        os.replace(tmp_path, epub_path)
        log.info("Generated TOC (%d entries) for %s", len(entries), epub_path)
        return True
    except (zipfile.BadZipFile, OSError, etree.XMLSyntaxError, ValueError) as error:
        log.warning("generate_toc failed for %s: %s", epub_path, error)
        try:
            if os.path.exists(epub_path + ".cwatoc.tmp"):
                os.remove(epub_path + ".cwatoc.tmp")
        except OSError:
            pass
        return False
