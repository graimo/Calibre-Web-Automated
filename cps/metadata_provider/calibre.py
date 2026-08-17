# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Metadata provider backed by Calibre's configured metadata sources."""

import base64
import threading
from typing import Optional

from cps import logger
from cps.services.Metadata import MetaRecord, MetaSourceInfo, Metadata
from cps.services.calibre_metadata import CalibreMetadataError, CalibreMetadataService

log = logger.create()
_CALIBRE_SEARCH_SLOTS = threading.BoundedSemaphore(value=2)


class Calibre(Metadata):
    """Expose ``fetch-ebook-metadata`` through CWA's provider interface."""

    __name__ = "Calibre"
    __id__ = "calibre"

    DESCRIPTION = "Calibre metadata sources"
    META_URL = "https://calibre-ebook.com/"
    # Outer wall-clock budget for the whole fetch-ebook-metadata run. Must leave
    # room above Calibre's inner per-source timeout (see CalibreMetadataService.fetch,
    # which reserves ~5s of headroom) for Calibre startup and cover download.
    FETCH_TIMEOUT = 30.0

    def __init__(self) -> None:
        super().__init__()
        self._service = CalibreMetadataService()

    def search(
        self, query: str, generic_cover: str = "", locale: str = "en"
    ) -> Optional[list[MetaRecord]]:
        del locale  # Calibre applies its own configured source preferences.

        query = (query or "").strip()
        if not self.active or not query:
            return []

        if not _CALIBRE_SEARCH_SLOTS.acquire(blocking=False):
            log.warning("Calibre metadata search skipped because the concurrency limit is busy")
            return []
        try:
            try:
                result = self._service.fetch(
                    title=query,
                    timeout=self.FETCH_TIMEOUT,
                    fetch_cover=True,
                )
            except CalibreMetadataError as error:
                log.warning("Calibre metadata search failed: %s", error)
                return []
        finally:
            _CALIBRE_SEARCH_SLOTS.release()

        metadata = result.metadata
        if not metadata.title:
            return []

        cover = self._cover_data_uri(result.cover) or generic_cover
        record_id = (
            metadata.identifiers.get("isbn")
            or next(iter(metadata.identifiers.values()), None)
            or metadata.title
        )

        return [
            MetaRecord(
                id=record_id,
                title=metadata.title,
                authors=metadata.authors,
                url=self.META_URL,
                source=MetaSourceInfo(
                    id=self.__id__,
                    description=self.DESCRIPTION,
                    link=self.META_URL,
                ),
                cover=cover,
                description=metadata.description,
                series=metadata.series,
                series_index=metadata.series_index,
                identifiers=metadata.identifiers,
                publisher=metadata.publisher,
                publishedDate=metadata.published_date,
                rating=(metadata.rating / 2 if metadata.rating is not None else 0),
                languages=metadata.languages,
                tags=metadata.tags,
            )
        ]

    @staticmethod
    def _cover_data_uri(cover: Optional[bytes]) -> str:
        if not cover:
            return ""

        if cover.startswith(b"\xff\xd8\xff"):
            media_type = "image/jpeg"
        elif cover.startswith(b"\x89PNG\r\n\x1a\n"):
            media_type = "image/png"
        elif len(cover) >= 12 and cover[:4] == b"RIFF" and cover[8:12] == b"WEBP":
            media_type = "image/webp"
        elif cover.startswith(b"BM"):
            media_type = "image/bmp"
        else:
            log.warning("Calibre returned a cover in an unsupported image format")
            return ""

        encoded = base64.b64encode(cover).decode("ascii")
        return f"data:{media_type};base64,{encoded}"
