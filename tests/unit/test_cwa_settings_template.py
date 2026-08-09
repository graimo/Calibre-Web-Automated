import re
from pathlib import Path


TEMPLATE_PATH = Path(__file__).parents[2] / "cps" / "templates" / "cwa_settings.html"

EXPECTED_CONTROL_NAMES = {
    "archived_cleanup_enabled",
    "archived_cleanup_schedule",
    "archived_cleanup_schedule_day",
    "archived_cleanup_schedule_hour",
    "auto_backup_conversions",
    "auto_backup_epub_fixes",
    "auto_backup_imports",
    "auto_convert",
    "auto_convert_target_format",
    "auto_ingest_automerge",
    "auto_metadata_enforcement",
    "auto_metadata_fetch_enabled",
    "auto_metadata_smart_application",
    "auto_metadata_update_authors",
    "auto_metadata_update_cover",
    "auto_metadata_update_description",
    "auto_metadata_update_identifiers",
    "auto_metadata_update_published_date",
    "auto_metadata_update_publisher",
    "auto_metadata_update_rating",
    "auto_metadata_update_series",
    "auto_metadata_update_tags",
    "auto_metadata_update_title",
    "auto_send_delay_minutes",
    "auto_zip_backups",
    "config_kobo_sync_magic_shelves",
    "contribute_translations_notifications",
    "convert_retained_{{ format }}",
    "cover_download_max_mb",
    "cwa_update_notifications",
    "duplicate_auto_resolve_cooldown_minutes",
    "duplicate_auto_resolve_enabled",
    "duplicate_auto_resolve_strategy",
    "duplicate_detection_author",
    "duplicate_detection_enabled",
    "duplicate_detection_format",
    "duplicate_detection_language",
    "duplicate_detection_publisher",
    "duplicate_detection_series",
    "duplicate_detection_title",
    "duplicate_detection_use_sql",
    "duplicate_format_priority",
    "duplicate_notifications_enabled",
    "duplicate_scan_cron",
    "duplicate_scan_debounce_seconds",
    "duplicate_scan_enabled",
    "duplicate_scan_frequency",
    "duplicate_scan_method",
    "enable_mobile_blur",
    "hardcover_auto_fetch_batch_size",
    "hardcover_auto_fetch_enabled",
    "hardcover_auto_fetch_min_confidence",
    "hardcover_auto_fetch_rate_limit",
    "hardcover_auto_fetch_schedule",
    "hardcover_auto_fetch_schedule_day",
    "hardcover_auto_fetch_schedule_hour",
    "ignore_convert_{{ format }}",
    "ignore_ingest_{{ format }}",
    "ingest_stale_temp_interval",
    "ingest_stale_temp_minutes",
    "ingest_timeout_minutes",
    "kindle_epub_fixer",
    "kindle_epub_fixer_aggressive",
    "koreader_sync_enabled",
    "metadata_provider_hierarchy",
    "metadata_providers_enabled",
    "submit_button",
}

EXPECTED_SECTION_IDS = {
    "settings-services",
    "settings-processing",
    "settings-providers",
    "settings-hardcover",
    "settings-interface",
    "settings-backups",
    "settings-conversion-target",
    "settings-conversion-ignored",
    "settings-conversion-retained",
    "settings-ingest-automerge",
    "settings-ingest-ignored",
    "settings-duplicate-detection",
    "settings-duplicate-scanning",
    "settings-duplicate-criteria",
    "settings-duplicate-priority",
    "settings-duplicate-resolution",
}


def _template_source():
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def test_cwa_settings_preserves_complete_form_contract():
    source = _template_source()
    control_names = set(re.findall(r'\bname=["\']([^"\']+)', source))

    assert control_names == EXPECTED_CONTROL_NAMES
    assert 'action="{{ url_for(\'cwa_settings.set_cwa_settings\')}}"' in source
    assert 'method="post"' in source
    assert 'value="Apply Default Settings"' in source
    assert source.count('value="Submit"') >= 2


def test_cwa_settings_preserves_operational_javascript_hooks():
    source = _template_source()

    for hook in (
        'url_for("metadata.metadata_provider")',
        "toggleArchivedCleanupScheduleOptions",
        "toggleScheduleOptions",
        "metadata_provider_hierarchy_hidden",
        "metadata_providers_enabled_hidden",
        "populateProviderList",
        "populateEnabledToggles",
        "setupDragAndDrop",
        "initializeFormatPriorityList",
        "updateFormatPriorityHiddenInput",
        "duplicate_format_priority",
        "{id: 'calibre', name: 'Calibre', active: true}",
        "p.hasOwnProperty('globally_enabled')",
    ):
        assert hook in source


def test_cwa_settings_control_room_has_all_navigable_sections():
    source = _template_source()
    section_ids = set(
        re.findall(
            r'<div class="settings-container"[^>]*\bid="([^"]+)"[^>]*\bdata-settings-section',
            source,
        )
    )

    assert section_ids == EXPECTED_SECTION_IDS
    assert source.count('<div class="settings-container"') == len(EXPECTED_SECTION_IDS)
    for section_id in EXPECTED_SECTION_IDS:
        assert source.count(f'id="{section_id}"') == 1
        assert source.count(f'href="#{section_id}"') == 1


def test_cwa_settings_control_room_is_searchable_responsive_and_accessible():
    source = _template_source()

    for marker in (
        'id="cwa-settings-page"',
        'id="settings-search"',
        'aria-controls="settings-content"',
        'aria-label="{{_(\'Settings sections\')}}"',
        'id="settings-save-state" role="status" aria-live="polite"',
        'class="cwa-toolbar-meta" role="status" aria-live="polite"',
        'id="settings-no-results" role="status"',
        "settingsPage.classList.add('is-enhanced')",
        "filterSettings",
        "IntersectionObserver",
        "prefers-reduced-motion: reduce",
        "@media (max-width: 980px)",
        "@media (max-width: 640px)",
    ):
        assert marker in source
