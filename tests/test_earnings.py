"""실적 DB·비교 로직 단위 테스트."""
from datetime import date, timedelta

from earnings import (
    UI_HIGHLIGHT_CALL_DAYS,
    UI_HIGHLIGHT_RESULT_DAYS,
    _build_comparison_lines,
    build_report_bundle,
    format_earnings_prompt_text,
    _should_ui_highlight_call,
    _should_ui_highlight_result,
)


def test_ui_highlight_result_recent_only():
    recent = (date.today() - timedelta(days=3)).isoformat()
    show, left = _should_ui_highlight_result(recent)
    assert show is True
    assert left == UI_HIGHLIGHT_RESULT_DAYS - 3
    old = (date.today() - timedelta(days=UI_HIGHLIGHT_RESULT_DAYS + 5)).isoformat()
    assert _should_ui_highlight_result(old)[0] is False


def test_comparison_qoq():
    history = [
        {"date": "2025-08-01", "actual_eps": 0.12, "estimate_eps": 0.10, "eps_surprise_pct": 20.0,
         "actual_revenue": 1.2e9, "estimate_revenue": 1.1e9, "revenue_surprise_pct": 9.1},
        {"date": "2025-05-01", "actual_eps": 0.08, "estimate_eps": 0.09, "eps_surprise_pct": -11.1,
         "actual_revenue": 1.0e9, "estimate_revenue": 1.05e9},
    ]
    lines = _build_comparison_lines(history)
    text = "\n".join(lines)
    assert "QoQ EPS" in text
    assert "beat" in text or "miss" in text


def test_prompt_always_includes_history_from_db():
    today = date.today()
    history = [
        {"date": (today - timedelta(days=40)).isoformat(), "actual_eps": 1.0, "estimate_eps": 0.9, "eps_surprise_pct": 11.1},
        {"date": (today - timedelta(days=130)).isoformat(), "actual_eps": 0.8, "estimate_eps": 0.85, "eps_surprise_pct": -5.9},
    ]
    profile = {
        "available": True,
        "ticker": "TEST",
        "next_earnings_date": (today + timedelta(days=10)).isoformat(),
        "days_to_earnings": 10,
    }
    bundle = build_report_bundle(profile, history, today=today)
    text = format_earnings_prompt_text(bundle)
    assert "MongoDB" in text
    assert "실적 이력" in text
    assert "QoQ" in text or "분기 이력" in text


def test_call_ui_highlight():
    recent = (date.today() - timedelta(days=5)).isoformat()
    call = {"earnings_date": recent, "summary": "ok"}
    show, _ = _should_ui_highlight_call(recent, call)
    assert show is True
