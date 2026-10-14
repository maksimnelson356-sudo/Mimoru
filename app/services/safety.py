from datetime import datetime, timedelta, timezone


def should_force_verification(join_count: int, limit: int, enabled: bool) -> bool:
    return enabled and limit > 0 and join_count > limit


def warning_expiry_label(hours: int) -> str:
    """Human-readable warning window. 0 means "never expire"."""
    if not hours:
        return "не удалять"
    if hours % 24 == 0:
        return f"{hours // 24} дн."
    return f"{hours} ч."


def warning_expiry_cutoff(hours: int, now: datetime | None = None) -> datetime | None:
    """Threshold below which a warning stops counting.

    The window is stored in hours so 24 means exactly a day, not "somewhere
    inside the next calendar day".
    """
    if hours <= 0:
        return None
    current = now or datetime.now(timezone.utc)
    return current - timedelta(hours=hours)
