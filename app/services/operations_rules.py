from __future__ import annotations

from typing import Any

SAFE_SETTING_FIELDS = {
    "antiflood_enabled","links_enabled","captcha_enabled","welcome_enabled","welcome_text",
    "warnings_limit","default_mute_seconds","antiflood_limit","antiflood_window_seconds",
    "antiflood_mute_seconds","repeats_enabled","repeats_limit","caps_enabled","caps_percent",
    "caps_min_length","voices_allowed","stickers_allowed","forwards_allowed","rules_text",
    "reports_enabled","report_hour_utc","timezone_name","anti_raid_enabled","anti_raid_limit",
    "anti_raid_window_seconds","warning_expire_hours","night_mode_enabled","night_mode_start",
    "night_mode_end","join_requests_enabled","join_requests_auto_approve",
    "newcomer_quarantine_enabled","newcomer_quarantine_seconds",
    "newcomer_quarantine_block_links","newcomer_quarantine_block_media",
    "newcomer_quarantine_block_forwards","slow_mode_enabled","slow_mode_seconds",
    "campaign_spam_enabled","campaign_spam_limit","campaign_spam_window_seconds",
    "campaign_spam_mute_seconds","edit_protection_enabled","edit_protection_window_seconds",
    "mention_filter_enabled","mention_limit","hashtag_limit","mention_mute_seconds",
    "sender_chat_filter_enabled","allow_group_sender_identity","automation_enabled",
    "deleted_cleanup_schedule",
}

def diagnostics_score(d: dict[str, Any]) -> int:
    weights={"reachable":20,"is_admin":20,"delete":20,"restrict":20,"invite":10,"manage_chat":10}
    return sum(v for k,v in weights.items() if d.get(k))
