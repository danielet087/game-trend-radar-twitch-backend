"""The Twitch mapping executes the installed shared admission policy."""
from radar_core.domain import twitch_admission as core
from collectors import steam_twitch_mapping, twitch_steam_admission as compatibility


def test_legacy_api_exports_the_shared_rules():
    expected = {
        "EVIDENCE_SOURCES", "METHOD", "TAIPEI", "TW_STORE_DATE_AUTHORITY",
        "TW_STORE_DATE_PROVIDER", "aware_time", "decimal_id",
        "has_taiwan_store_date_authority", "has_twitch_admission",
        "is_twitch_qualified", "normalize_twitch_admission",
        "preserve_twitch_admission", "resolve_store_release_day",
        "valid_enrollment", "validate_twitch_snapshot",
    }
    assert set(compatibility.__all__) == expected
    for name in expected:
        assert getattr(compatibility, name) is getattr(core, name)


def test_mapping_uses_shared_qualification_and_date_authority():
    assert steam_twitch_mapping.is_twitch_qualified is core.is_twitch_qualified
    assert steam_twitch_mapping.has_taiwan_store_date_authority is core.has_taiwan_store_date_authority
