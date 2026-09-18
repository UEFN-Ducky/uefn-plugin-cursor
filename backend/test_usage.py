from __future__ import annotations

from usage import session_cookie_from_token, windows_from_cursor, windows_from_headers


def test_session_cookie_prefixes_jwt_sub() -> None:
    import base64
    import json

    payload = base64.urlsafe_b64encode(json.dumps({"sub": "user_abc"}).encode()).decode().rstrip("=")
    token = f"eyJhbGciOiJub25lIn0.{payload}.sig"
    assert session_cookie_from_token(token) == f"user_abc::{token}"
    assert session_cookie_from_token("already::token") == "already::token"


def test_five_hour_and_weekly_from_vendor_json() -> None:
    rows = windows_from_cursor(
        {
            "five_hour": {"used_percent": 100, "reset_after_seconds": 9660},
            "weekly": {"used_percent": 34, "reset_at": "2026-09-23T16:00:00Z"},
        }
    )
    assert rows[0]["label"] == "5-hour limit"
    assert rows[0]["readout"] == "100%"
    assert "2 hr" in rows[0]["reset"] or "2hr" in rows[0]["reset"].replace(" ", "")
    assert rows[1]["label"] == "Weekly · all models"
    assert rows[1]["used"] == 34


def test_plan_percents_fallback() -> None:
    rows = windows_from_cursor(
        {
            "billingCycleEnd": "2026-10-01T00:00:00Z",
            "individualUsage": {"plan": {"autoPercentUsed": 10, "apiPercentUsed": 50}},
        }
    )
    assert [w["id"] for w in rows] == ["included", "api"]


def test_empty_without_limits() -> None:
    assert windows_from_headers({}) == []
    assert windows_from_cursor({}) == []
