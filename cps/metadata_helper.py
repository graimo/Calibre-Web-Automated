# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2025 Calibre-Web contributors
# Copyright (C) 2024-2025 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

import json

from cps import constants, logger, db, helper
from cps.search_metadata import cl as metadata_providers
from cps.services.Metadata import Metadata
import sys
sys.path.insert(1, '/app/calibre-web-automated/scripts/')
from cwa_db import CWA_DB

log = logger.create()

# Auto-metadata result selection thresholds (0..1). A candidate must clear
# MIN_CONFIDENCE to be applied; reaching HIGH_CONFIDENCE stops querying more
# providers. This replaces "take the first provider's first result" with a
# scored choice that also prefers results carrying a cover and a description.
MIN_CONFIDENCE = 0.45
HIGH_CONFIDENCE = 0.85


def _token_set(text):
    return {t.lower() for t in Metadata.get_title_tokens(text or "", strip_joiners=True)}


def _overlap(a_tokens, b_tokens):
    if not a_tokens or not b_tokens:
        return 0.0
    return len(a_tokens & b_tokens) / max(len(a_tokens), len(b_tokens))


def _score_candidate(metadata, query_title, query_authors):
    """Score a metadata candidate against the ingested book (0..1)."""
    title_score = _overlap(_token_set(metadata.title), _token_set(query_title))

    author_tokens = set()
    for name in (query_authors or []):
        author_tokens |= _token_set(name)
    cand_author_tokens = set()
    for name in (getattr(metadata, "authors", None) or []):
        cand_author_tokens |= _token_set(name)
    author_score = _overlap(cand_author_tokens, author_tokens) if author_tokens else 0.0

    score = 0.6 * title_score + 0.25 * author_score
    # Tie-breakers: prefer candidates that actually carry a cover / description.
    if metadata.cover and str(metadata.cover).startswith("http"):
        score += 0.10
    if getattr(metadata, "description", None) and metadata.description.strip():
        score += 0.05
    return min(score, 1.0)

def fetch_and_apply_metadata(book_id: int, user_enabled: bool = False) -> bool:
    """
    Fetch metadata for a newly ingested book and apply it if settings allow.
    
    Args:
        book_id: The ID of the book to fetch metadata for
        user_enabled: Deprecated parameter - metadata fetching is now admin-controlled only
        
    Returns:
        bool: True if metadata was successfully fetched and applied, False otherwise
    """
    try:
        if not db.CalibreDB.session_factory:
            log.error("CalibreDB not initialized; skipping metadata fetch")
            return False

        # Check global settings (admin-controlled only)
        cwa_db = CWA_DB()
        cwa_settings = cwa_db.get_cwa_settings()
        
        if not cwa_settings.get('auto_metadata_fetch_enabled', False):
            log.debug("Auto metadata fetch disabled by administrator")
            return False
            
        # Get the book
        calibre_db_instance = db.CalibreDB(expire_on_commit=False, init=True)
        book = calibre_db_instance.get_book(book_id)
        if not book:
            log.error(f"Book with ID {book_id} not found")
            return False
            
        # Create search query from book title and author
        search_query = book.title
        if book.authors:
            author_names = [author.name for author in book.authors]
            search_query += " " + " ".join(author_names)
            
        log.info(f"Fetching metadata for: {search_query}")
        
        # Get provider hierarchy
        try:
            provider_hierarchy = json.loads(cwa_settings.get('metadata_provider_hierarchy', '["google","douban","dnb","ibdb","comicvine"]'))
        except (json.JSONDecodeError, TypeError):
            provider_hierarchy = ["google", "douban", "dnb", "ibdb", "comicvine"]

        # Global provider enablement map
        enabled_map = _parse_metadata_providers_enabled(
            cwa_settings.get('metadata_providers_enabled', '{}')
        )
            
        # Query providers in hierarchy order and keep the best-scoring candidate
        # (title/author match, preferring results that carry a cover + description).
        query_title = book.title or ""
        query_authors = [author.name for author in book.authors] if book.authors else []

        best_metadata = None
        best_score = 0.0
        best_provider = None
        for provider_id in provider_hierarchy:
            # Respect each provider's centralized safe default.
            is_enabled = enabled_map.get(
                provider_id,
                constants.metadata_provider_enabled_by_default(provider_id),
            )
            if not is_enabled:
                log.debug(f"Provider {provider_id} is globally disabled")
                continue
            provider = next(
                (p for p in metadata_providers if p.__id__ == provider_id), None
            )
            if not provider or not provider.active:
                continue
            try:
                log.debug(f"Trying metadata provider: {provider.__name__}")
                results = provider.search(search_query, "", "en") or []
            except Exception as e:
                log.warning(f"Error fetching metadata from provider {provider_id}: {e}")
                continue

            for candidate in results:
                score = _score_candidate(candidate, query_title, query_authors)
                candidate.confidence_score = score
                if score > best_score:
                    best_score, best_metadata, best_provider = score, candidate, provider

            if best_score >= HIGH_CONFIDENCE:
                break  # already a strong match; no need to query more providers

        metadata_found = False
        if best_metadata is not None and best_score >= MIN_CONFIDENCE:
            if _apply_metadata_to_book(book, best_metadata, calibre_db_instance):
                log.info(
                    "Applied metadata from %s (score %.2f) for book: %s",
                    best_provider.__name__, best_score, book.title,
                )
                metadata_found = True
        else:
            log.info(
                "No confident metadata match for '%s' (best score %.2f < %.2f); leaving as-is",
                book.title, best_score, MIN_CONFIDENCE,
            )

        calibre_db_instance.session.close()
        return metadata_found
        
    except Exception as e:
        log.error(f"Error in fetch_and_apply_metadata: {e}", exc_info=True)
        return False


def _apply_metadata_to_book(book, metadata, calibre_db_instance) -> bool:
    """
    Apply fetched metadata to a book record.
    
    Args:
        book: The book database record
        metadata: The metadata record from provider
        calibre_db_instance: Database instance
        
    Returns:
        bool: True if metadata was successfully applied
    """
    cover_backup = None
    try:
        # Get CWA settings to check smart application preference and field selections
        cwa_db = CWA_DB()
        cwa_settings = cwa_db.get_cwa_settings()
        use_smart_application = cwa_settings.get('auto_metadata_smart_application', False)
        
        updated = False
        cover_updated = False
        
        # Update title - only if enabled in settings
        if (cwa_settings.get('auto_metadata_update_title', True) and 
            metadata.title and metadata.title.strip()):
            if use_smart_application:
                if len(metadata.title.strip()) > len(book.title.strip()):
                    book.title = metadata.title.strip()
                    updated = True
            else:
                book.title = metadata.title.strip()
                updated = True
            
        # Update authors - only if enabled in settings
        if (cwa_settings.get('auto_metadata_update_authors', True) and 
            metadata.authors and len(metadata.authors) > 0):
            # Clear existing authors
            book.authors.clear()
            for author_name in metadata.authors:
                if author_name and author_name.strip():
                    author = calibre_db_instance.get_author_by_name(author_name.strip())
                    if not author:
                        author = db.Authors(author_name.strip(), author_name.strip())
                        calibre_db_instance.session.add(author)
                    book.authors.append(author)
            updated = True
            
        # Update description - only if enabled in settings
        if (cwa_settings.get('auto_metadata_update_description', True) and 
            metadata.description and metadata.description.strip()):
            current_description = book.comments[0].text if book.comments else ""
            if use_smart_application:
                if len(metadata.description.strip()) > len(current_description):
                    if book.comments:
                        book.comments[0].text = metadata.description.strip()
                    else:
                        comment = db.Comments(metadata.description.strip(), book.id)
                        calibre_db_instance.session.add(comment)
                    updated = True
            else:
                if book.comments:
                    book.comments[0].text = metadata.description.strip()
                else:
                    comment = db.Comments(metadata.description.strip(), book.id)
                    calibre_db_instance.session.add(comment)
                updated = True
            
        # Update publisher - only if enabled in settings
        if (cwa_settings.get('auto_metadata_update_publisher', True) and 
            metadata.publisher and metadata.publisher.strip()):
            if use_smart_application:
                if not book.publishers or len(book.publishers) == 0:
                    publisher = calibre_db_instance.get_publisher_by_name(metadata.publisher.strip())
                    if not publisher:
                        publisher = db.Publishers(metadata.publisher.strip(), metadata.publisher.strip())
                        calibre_db_instance.session.add(publisher)
                    book.publishers = [publisher]
                    updated = True
            else:
                # Clear existing publishers and add new one
                book.publishers.clear()
                publisher = calibre_db_instance.get_publisher_by_name(metadata.publisher.strip())
                if not publisher:
                    publisher = db.Publishers(metadata.publisher.strip(), metadata.publisher.strip())
                    calibre_db_instance.session.add(publisher)
                book.publishers = [publisher]
                updated = True
                
        # Update tags if available and enabled in settings
        if (cwa_settings.get('auto_metadata_update_tags', True) and 
            hasattr(metadata, 'tags') and metadata.tags):
            for tag_name in metadata.tags:
                if tag_name and tag_name.strip():
                    tag = calibre_db_instance.get_tag_by_name(tag_name.strip())
                    if not tag:
                        tag = db.Tags(name=tag_name.strip())
                        calibre_db_instance.session.add(tag)
                    if tag not in book.tags:
                        book.tags.append(tag)
            updated = True
            
        # Update series if available and enabled in settings
        if (cwa_settings.get('auto_metadata_update_series', True) and 
            hasattr(metadata, 'series') and metadata.series and metadata.series.strip()):
            series = calibre_db_instance.get_series_by_name(metadata.series.strip())
            if not series:
                series = db.Series(metadata.series.strip(), metadata.series.strip())
                calibre_db_instance.session.add(series)
            book.series.clear()
            book.series.append(series)
            
            # Set series index if available
            if hasattr(metadata, 'series_index') and metadata.series_index:
                try:
                    # Convert to float first to validate, then store as string (DB column is String)
                    float_value = float(metadata.series_index)
                    book.series_index = str(float_value)
                except (ValueError, TypeError):
                    book.series_index = '1.0'
            updated = True
            
        # Update published date if available and enabled in settings
        if (cwa_settings.get('auto_metadata_update_published_date', True) and 
            hasattr(metadata, 'publishedDate') and metadata.publishedDate):
            try:
                from datetime import datetime
                if isinstance(metadata.publishedDate, str):
                    # Try to parse various date formats
                    for fmt in ['%Y-%m-%d', '%Y-%m', '%Y']:
                        try:
                            book.pubdate = datetime.strptime(metadata.publishedDate, fmt).date()
                            updated = True
                            break
                        except ValueError:
                            continue
                elif hasattr(metadata.publishedDate, 'date'):
                    book.pubdate = metadata.publishedDate.date()
                    updated = True
            except Exception as e:
                log.warning(f"Error parsing published date: {e}")
                
        # Update rating if available and enabled in settings.
        # 'ratings' is a SHARED, UNIQUE-valued lookup table (like tags/authors):
        #  - reuse the existing row for a value instead of inserting a duplicate
        #    (a blind INSERT raised "UNIQUE constraint failed: ratings.rating");
        #  - never mutate an existing row's value (that would re-rate every book
        #    linked to it) — re-point the book's link instead.
        if (cwa_settings.get('auto_metadata_update_rating', True) and
            hasattr(metadata, 'rating') and metadata.rating):
            try:
                rating_value = float(metadata.rating)
                # Providers report a 0-5 score; Calibre stores it on a 0-10 scale.
                rating_int = max(0, min(10, int(round(rating_value * 2))))
                if rating_int <= 0:
                    if book.ratings:
                        book.ratings = []
                        updated = True
                else:
                    existing_rating = (calibre_db_instance.session.query(db.Ratings)
                                       .filter(db.Ratings.rating == rating_int)
                                       .first())
                    if existing_rating is None:
                        existing_rating = db.Ratings(rating=rating_int)
                        calibre_db_instance.session.add(existing_rating)
                    if not book.ratings or book.ratings[0].rating != rating_int:
                        book.ratings = [existing_rating]
                        updated = True
            except (ValueError, TypeError):
                pass
                
        # Update identifiers if available and enabled in settings
        if (cwa_settings.get('auto_metadata_update_identifiers', True) and 
            hasattr(metadata, 'identifiers') and metadata.identifiers):
            for identifier_type, identifier_value in metadata.identifiers.items():
                if identifier_type and identifier_value:
                    # Check if identifier already exists
                    existing = False
                    for identifier in book.identifiers:
                        if identifier.type == identifier_type:
                            identifier.val = identifier_value
                            existing = True
                            break
                    if not existing:
                        new_identifier = db.Identifiers(identifier_value, identifier_type, book.id)
                        calibre_db_instance.session.add(new_identifier)
                        book.identifiers.append(new_identifier)
                    updated = True
        
        # Handle cover image - only if enabled in settings
        if (
            cwa_settings.get('auto_metadata_update_cover', True)
            and hasattr(metadata, 'cover')
            and metadata.cover
        ):
            # Smart mode preserves an existing cover until resolution comparison is
            # available; direct mode always applies the provider cover.
            should_apply_cover = not use_smart_application or not bool(book.has_cover)
            if should_apply_cover:
                cover_backup = helper.create_cover_backup(book.path)
                cover_saved, cover_error = helper.save_cover_from_url(
                    metadata.cover, book.path
                )
                if cover_saved:
                    book.has_cover = 1
                    cover_updated = True
                    updated = True
                else:
                    helper.discard_cover_backup(cover_backup)
                    cover_backup = None
                    log.warning(
                        "Could not apply metadata cover for book %s: %s",
                        book.id,
                        cover_error,
                    )

        if updated:
            calibre_db_instance.session.commit()

        helper.discard_cover_backup(cover_backup)
        cover_backup = None
        if cover_updated:
            try:
                helper.replace_cover_thumbnail_cache(book.id, book_path=book.path)
            except Exception as error:
                log.warning(
                    "Metadata committed but thumbnail refresh failed for book %s: %s",
                    book.id,
                    error,
                )

        return updated

    except Exception as e:
        calibre_db_instance.session.rollback()
        if cover_backup is not None:
            try:
                helper.restore_cover_backup(cover_backup)
                cover_backup = None
            except Exception as restore_error:
                log.error(
                    "Could not restore cover for book %s after metadata rollback: %s; "
                    "backup retained at %s",
                    getattr(book, 'id', 'unknown'),
                    restore_error,
                    cover_backup.get("backup_path", "unknown"),
                )
        log.error(f"Error applying metadata to book {getattr(book, 'id', 'unknown')}: {e}")
        return False


def _parse_metadata_providers_enabled(raw_value):
    """Lightweight parser for metadata_providers_enabled without importing cwa_functions."""
    try:
        if raw_value is None:
            return {}
        if isinstance(raw_value, bytes):
            raw_value = raw_value.decode('utf-8', errors='ignore')
        if isinstance(raw_value, str):
            s = raw_value.strip()
            if not s:
                return {}
            if s.startswith("'") and s.endswith("'"):
                s = s[1:-1]
            if not s:
                return {}
            data = json.loads(s)
            return data if isinstance(data, dict) else {}
        if isinstance(raw_value, dict):
            return raw_value
        return {}
    except (json.JSONDecodeError, ValueError, TypeError, AttributeError):
        return {}
