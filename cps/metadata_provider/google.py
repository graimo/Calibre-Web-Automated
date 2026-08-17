# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2025 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

# Google Books api document: https://developers.google.com/books/docs/v1/using
# Query building mirrors Calibre's google source (isbn: qualifier + title tokens).
import os
import re
from typing import Dict, List, Optional
from datetime import datetime

import requests

from cps import logger
from cps.isoLanguages import get_lang3, get_language_name
from cps.services.Metadata import MetaRecord, MetaSourceInfo, Metadata

log = logger.create()

ISBN_RE = re.compile(r"^(97[89][0-9]{10}|[0-9]{9}[0-9Xx])$")


def as_isbn(query: str) -> Optional[str]:
    """Return the normalised ISBN if the query is (just) an ISBN-10/13."""
    digits = re.sub(r"[\s-]", "", query or "")
    return digits if ISBN_RE.match(digits) else None


class Google(Metadata):
    __name__ = "Google"
    __id__ = "google"
    DESCRIPTION = "Google Books"
    META_URL = "https://books.google.com/"
    BOOK_URL = "https://books.google.com/books?id="
    SEARCH_URL = "https://www.googleapis.com/books/v1/volumes"
    MAX_RESULTS = 20

    @staticmethod
    def _api_key() -> Optional[str]:
        key = os.environ.get("GOOGLE_BOOKS_API_KEY")
        if key:
            return key.strip()
        try:
            import sys
            sys.path.insert(1, "/app/calibre-web-automated/scripts/")
            from cwa_db import CWA_DB
            value = CWA_DB().cwa_settings.get("google_books_api_key")
            if value:
                return str(value).strip()
        except Exception:
            pass
        return None

    def _build_query(self, query: str) -> str:
        isbn = as_isbn(query)
        if isbn:
            return "isbn:" + isbn
        # Same approach as Calibre: build the query from cleaned title tokens.
        title_tokens = list(self.get_title_tokens(query, strip_joiners=False))
        if title_tokens:
            return " ".join(title_tokens)
        return query

    def search(
        self, query: str, generic_cover: str = "", locale: str = "en"
    ) -> Optional[List[MetaRecord]]:
        if not self.active:
            return []
        query = (query or "").strip()
        if not query:
            return []

        params = {
            "q": self._build_query(query),
            "maxResults": self.MAX_RESULTS,
        }
        api_key = self._api_key()
        if api_key:
            params["key"] = api_key
        # Google Books can require a country for some queries; allow overriding.
        country = os.environ.get("GOOGLE_BOOKS_COUNTRY")
        if country:
            params["country"] = country.strip()

        try:
            response = requests.get(self.SEARCH_URL, params=params, timeout=15)
            response.raise_for_status()
        except Exception as error:
            log.warning("Google Books search failed: %s", error)
            return []

        results = []
        for item in response.json().get("items", []):
            record = self._parse_search_result(
                result=item, generic_cover=generic_cover, locale=locale
            )
            if record:
                results.append(record)
        return results

    def _parse_search_result(
        self, result: Dict, generic_cover: str, locale: str
    ) -> Optional[MetaRecord]:
        volume_info = result.get("volumeInfo", {})
        if "title" not in volume_info:
            return None

        match = MetaRecord(
            id=result["id"],
            title=volume_info["title"],
            authors=volume_info.get("authors", []),
            url=Google.BOOK_URL + result["id"],
            source=MetaSourceInfo(
                id=self.__id__,
                description=Google.DESCRIPTION,
                link=Google.META_URL,
            ),
        )

        subtitle = volume_info.get("subtitle")
        if subtitle:
            match.title = "{}: {}".format(match.title, subtitle)
        match.cover = self._parse_cover(result=result, generic_cover=generic_cover)
        match.description = volume_info.get("description", "")
        match.languages = self._parse_languages(result=result, locale=locale)
        match.publisher = volume_info.get("publisher", "")
        match.publishedDate = self._parse_date(volume_info.get("publishedDate", ""))
        match.rating = volume_info.get("averageRating", 0) or 0
        match.series, match.series_index = "", 1
        match.tags = volume_info.get("categories", [])

        match.identifiers = {"google": match.id}
        match = self._parse_isbn(result=result, match=match)
        return match

    @staticmethod
    def _parse_date(published: str) -> str:
        """Google returns YYYY, YYYY-MM or YYYY-MM-DD. Normalise to a full date."""
        published = (published or "").strip()
        for fmt, norm in (("%Y-%m-%d", "%Y-%m-%d"), ("%Y-%m", "%Y-%m-01"), ("%Y", "%Y-01-01")):
            try:
                return datetime.strptime(published, fmt).strftime(norm)
            except ValueError:
                continue
        return ""

    @staticmethod
    def _parse_isbn(result: Dict, match: MetaRecord) -> MetaRecord:
        identifiers = result["volumeInfo"].get("industryIdentifiers", [])
        isbn13 = isbn10 = None
        for identifier in identifiers:
            if identifier.get("type") == "ISBN_13":
                isbn13 = identifier.get("identifier")
            elif identifier.get("type") == "ISBN_10":
                isbn10 = identifier.get("identifier")
        if isbn13 or isbn10:
            match.identifiers["isbn"] = isbn13 or isbn10
        return match

    @staticmethod
    def _parse_cover(result: Dict, generic_cover: str) -> str:
        image_links = result["volumeInfo"].get("imageLinks")
        if image_links:
            cover_url = image_links.get("thumbnail") or image_links.get("smallThumbnail")
            if cover_url:
                cover_url = cover_url.replace("&edge=curl", "")
                cover_url += "&fife=w800-h900"
                return cover_url.replace("http://", "https://")
        return generic_cover

    @staticmethod
    def _parse_languages(result: Dict, locale: str) -> List[str]:
        language_iso2 = result["volumeInfo"].get("language", "")
        return (
            [get_language_name(locale, get_lang3(language_iso2))]
            if language_iso2
            else []
        )
