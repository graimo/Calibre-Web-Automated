# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

# Open Library search API: https://openlibrary.org/dev/docs/api/search
# Free JSON API, no anti-bot / no API key required. Complements Google Books.
from typing import Dict, List, Optional

import requests

from cps import logger
from cps.isoLanguages import get_language_name
from cps.services.Metadata import MetaRecord, MetaSourceInfo, Metadata
from cps.metadata_provider.google import as_isbn

log = logger.create()


class OpenLibrary(Metadata):
    __name__ = "Open Library"
    __id__ = "openlibrary"
    DESCRIPTION = "Open Library"
    META_URL = "https://openlibrary.org/"
    SEARCH_URL = "https://openlibrary.org/search.json"
    COVER_URL = "https://covers.openlibrary.org/b"
    MAX_RESULTS = 20
    FIELDS = "key,title,author_name,first_publish_year,isbn,cover_i,language,publisher,subject,ratings_average"

    def search(
        self, query: str, generic_cover: str = "", locale: str = "en"
    ) -> Optional[List[MetaRecord]]:
        if not self.active:
            return []
        query = (query or "").strip()
        if not query:
            return []

        isbn = as_isbn(query)
        params = {
            "fields": self.FIELDS,
            "limit": self.MAX_RESULTS,
        }
        if isbn:
            params["q"] = "isbn:" + isbn
        else:
            params["q"] = query

        try:
            response = requests.get(
                self.SEARCH_URL,
                params=params,
                timeout=15,
                headers={"User-Agent": "Calibre-Web-Automated/metadata"},
            )
            response.raise_for_status()
        except Exception as error:
            log.warning("Open Library search failed: %s", error)
            return []

        results = []
        for doc in response.json().get("docs", []):
            record = self._parse_doc(doc=doc, generic_cover=generic_cover, locale=locale)
            if record:
                results.append(record)
        return results

    def _parse_doc(self, doc: Dict, generic_cover: str, locale: str) -> Optional[MetaRecord]:
        if not doc.get("title"):
            return None

        key = (doc.get("key") or "").strip("/")
        isbn_list = doc.get("isbn") or []
        isbn = isbn_list[0] if isbn_list else None

        identifiers: Dict[str, str] = {}
        if key:
            identifiers["openlibrary"] = key
        if isbn:
            identifiers["isbn"] = isbn

        year = doc.get("first_publish_year")
        published_date = "{}-01-01".format(year) if year else ""

        record = MetaRecord(
            id=key or (isbn or doc["title"]),
            title=doc["title"],
            authors=doc.get("author_name", []) or [],
            url="{}{}".format(self.META_URL.rstrip("/"), doc.get("key", "")),
            source=MetaSourceInfo(
                id=self.__id__,
                description=self.DESCRIPTION,
                link=self.META_URL,
            ),
            cover=self._parse_cover(doc, isbn, generic_cover),
            description="",
            series=None,
            series_index=1,
            identifiers=identifiers,
            publisher=(doc.get("publisher") or [None])[0],
            publishedDate=published_date,
            rating=int(round(doc.get("ratings_average") or 0)),
            languages=self._parse_languages(doc, locale),
            tags=(doc.get("subject") or [])[:8],
        )
        return record

    def _parse_cover(self, doc: Dict, isbn: Optional[str], generic_cover: str) -> str:
        cover_i = doc.get("cover_i")
        if cover_i:
            return "{}/id/{}-L.jpg".format(self.COVER_URL, cover_i)
        if isbn:
            return "{}/isbn/{}-L.jpg".format(self.COVER_URL, isbn)
        return generic_cover

    @staticmethod
    def _parse_languages(doc: Dict, locale: str) -> List[str]:
        languages = []
        for code in (doc.get("language") or [])[:1]:
            try:
                languages.append(get_language_name(locale, code))
            except Exception:
                languages.append(code)
        return languages
