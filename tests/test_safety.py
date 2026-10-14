from datetime import datetime, timezone

from app.services.safety import should_force_verification, warning_expiry_cutoff


def test_antiraid_threshold_is_strictly_above_limit():
    assert not should_force_verification(10, 10, True)
    assert should_force_verification(11, 10, True)
    assert not should_force_verification(100, 10, False)


def test_warning_expiry_cutoff():
    now = datetime(2026, 8, 2, tzinfo=timezone.utc)
    assert warning_expiry_cutoff(0, now) is None
    assert warning_expiry_cutoff(24, now).date().isoformat() == "2026-08-01"
    assert warning_expiry_cutoff(168, now).date().isoformat() == "2026-07-26"
