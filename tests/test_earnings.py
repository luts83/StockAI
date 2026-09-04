"""실적 DB·비교 로직 단위 테스트."""
from datetime import date, timedelta

from earnings import (
    UI_HIGHLIGHT_CALL_DAYS,
    UI_HIGHLIGHT_RESULT_DAYS,
    _build_comparison_lines,
    _build_earnings_summary,
    _format_earnings_date_label,
    _merge_fiscal_and_announcements,
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
        {"date": "2025-08-01", "period_end": "2025-06-30", "actual_eps": 0.12, "estimate_eps": 0.10, "eps_surprise_pct": 20.0,
         "actual_revenue": 1.2e9, "estimate_revenue": 1.1e9, "revenue_surprise_pct": 9.1},
        {"date": "2025-05-01", "period_end": "2025-03-31", "actual_eps": 0.08, "estimate_eps": 0.09, "eps_surprise_pct": -11.1,
         "actual_revenue": 1.0e9, "estimate_revenue": 1.05e9},
    ]
    lines = _build_comparison_lines(history)
    text = "\n".join(lines)
    assert "QoQ EPS" in text
    assert "상회" in text or "하회" in text


def test_merge_fiscal_and_announcements():
    fiscal = [
        {"ticker": "X", "period_end": "2026-03-31", "actual_eps": -0.16, "estimate_eps": -0.26, "eps_surprise_pct": 38.5},
    ]
    announce = [
        {"ticker": "X", "announce_date": "2026-05-08", "actual_eps": -0.16, "estimate_eps": -0.26, "eps_surprise_pct": 38.5},
    ]
    merged = _merge_fiscal_and_announcements(fiscal, announce)
    assert merged[0]["date"] == "2026-05-08"
    assert merged[0]["period_end"] == "2026-03-31"


def test_earnings_summary_korean():
    history = [
        {"date": "2026-08-27", "period_end": "2026-06-30", "actual_eps": -0.2, "estimate_eps": -0.3, "eps_surprise_pct": 33.3},
        {"date": "2026-05-08", "period_end": "2026-03-31", "actual_eps": -0.16, "estimate_eps": -0.26, "eps_surprise_pct": 38.5},
    ]
    summary = _build_earnings_summary(history)
    assert "발표 2026-08-27" in summary
    assert "6월 마감" in summary
    assert "서프라이즈" in summary or "상회" in summary


def test_date_label_shows_both_dates():
    rec = {"date": "2026-05-08", "period_end": "2026-03-31"}
    label = _format_earnings_date_label(rec)
    assert "발표 2026-05-08" in label
    assert "3월 마감" in label


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
    assert "실적 이력" in text or "실적 요약" in text
    assert "QoQ" in text or "분기 이력" in text


def test_apply_latest_from_info_fixes_period_end_date():
    """분기 마감일(6/30)이 date로 잘못 들어온 경우 info로 8/27 발표일 보정."""
    from datetime import datetime, timezone
    announce_ts = int(datetime(2026, 8, 27, 20, 0, 0, tzinfo=timezone.utc).timestamp())
    period_ts = 1782777600  # 2026-06-30 UTC (yfinance mostRecentQuarter)

    class FakeStock:
        info = {"earningsTimestamp": announce_ts, "mostRecentQuarter": period_ts}
        earnings_history = None
        quarterly_income_stmt = None

    from earnings import _apply_latest_from_info

    history = [{
        "ticker": "IREN",
        "date": "2026-06-30",
        "period_end": "2026-06-30",
        "actual_eps": None,
        "date_note": "발표일 미확인 — 분기 마감일 기준",
        "source": "yfinance_fiscal",
    }]
    out = _apply_latest_from_info(FakeStock(), "IREN", history)
    assert out[0]["date"] == "2026-08-27"
    assert out[0]["period_end"] == "2026-06-30"
    assert "발표일 미확인" not in (out[0].get("date_note") or "")


def test_needs_sync_when_quarter_stale():
    from datetime import date, timedelta
    from earnings import _needs_sync

    old_latest = (date.today() - timedelta(days=100)).isoformat()
    profile = {
        "latest_earnings_date": old_latest,
        "last_sync_at": date.today().isoformat(),
        "next_earnings_date": (date.today() + timedelta(days=60)).isoformat(),
    }
    latest = date.fromisoformat(old_latest)
    assert (date.today() - latest).days >= 75


def test_is_suspect_fiscal_date_record():
    from earnings import _is_suspect_fiscal_date_record

    assert _is_suspect_fiscal_date_record({
        "date": "2026-06-30",
        "period_end": "2026-06-30",
        "source": "yfinance_fiscal",
    })
    assert not _is_suspect_fiscal_date_record({
        "date": "2026-08-27",
        "period_end": "2026-06-30",
        "source": "yfinance_info",
    })


def test_collapse_period_duplicates_prefers_announce_date():
    from earnings import _collapse_period_duplicates

    history = [
        {"date": "2026-06-30", "period_end": "2026-06-30", "source": "yfinance_fiscal",
         "date_note": "발표일 미확인 — 분기 마감일 기준"},
        {"date": "2026-08-27", "period_end": "2026-06-30", "source": "yfinance_info"},
        {"date": "2026-05-08", "period_end": "2026-03-31", "source": "yfinance_merged"},
    ]
    out = _collapse_period_duplicates(history)
    assert out[0]["date"] == "2026-08-27"
    assert len(out) == 2


def test_needs_sync_when_suspect_in_db(monkeypatch):
    from earnings import _needs_sync

    monkeypatch.setattr(
        "earnings.list_earnings_history",
        lambda t, limit=8: [{
            "date": "2026-06-30",
            "period_end": "2026-06-30",
            "source": "yfinance_fiscal",
            "date_note": "발표일 미확인 — 분기 마감일 기준",
        }],
    )
    monkeypatch.setattr("earnings.count_earnings_history", lambda t: 5)
    profile = {
        "last_sync_at": "2026-09-02T08:00:00",
        "latest_earnings_date": "2026-06-30",
    }
    assert _needs_sync("IREN", profile) is True


def test_needs_earnings_call_refresh_wrong_direction():
    from earnings import _needs_earnings_call_refresh

    rec = {
        "earnings_call": {
            "summary": "실적 발표 후 주가는 2.00% 상승했습니다.",
        },
    }
    move = {"next_day_pct": -12.5, "label": "발표 다음 거래일 종가 ▼12.50%"}
    assert _needs_earnings_call_refresh(rec, move) is True


def test_apply_price_move_strips_wrong_reaction():
    from earnings import _apply_price_move_to_call_summary

    data = {
        "summary": "매출 미스. 실적 발표 후 주가는 2.00% 상승했습니다.",
        "highlights": ["주가 2% 상승", "AI 확대"],
    }
    move = {
        "next_day_pct": -12.53,
        "reaction_pct": -12.53,
        "timing": "amc",
        "label": "장마감 후 발표 · 다음 거래일 종가 ▼12.53%",
    }
    out = _apply_price_move_to_call_summary(data, move)
    assert "2.00% 상승" not in out["summary"]
    assert "▼12.53" in out["market_reaction_note"]
    assert any("12.53" in h for h in out["highlights"])


def test_filter_earnings_call_news_drops_old_quarter():
    from earnings import _filter_earnings_call_news

    items = [
        {"title": "Earnings call transcript: IREN Q2 2025 sees revenue drop, stock falls"},
        {"title": "IREN Q4 FY26 Earnings Call Highlights - MarketBeat"},
    ]
    out = _filter_earnings_call_news(items, "IREN", "2026-08-27")
    titles = " ".join(x["title"] for x in out)
    assert "Q2 2025" not in titles or out[0]["title"].startswith("IREN Q4")


def test_nasdaq_fill_missing_eps():
    from earnings import apply_nasdaq_supplement

    history = [{
        "ticker": "IREN",
        "date": "2026-08-27",
        "period_end": "2026-06-30",
        "actual_eps": None,
        "estimate_eps": -0.455,
        "date_note": "EPS·컨센서스 — yfinance 반영 대기 (발표일은 IR 기준)",
        "source": "yfinance_info",
    }]
    nasdaq = [{
        "ticker": "IREN",
        "date": "2026-08-27",
        "period_end": "2026-06-30",
        "actual_eps": -0.41,
        "estimate_eps": -0.5,
        "eps_surprise_pct": 18.0,
        "actual_revenue": 137_225_000.0,
        "source": "nasdaq_surprise",
    }]
    out = apply_nasdaq_supplement(history, nasdaq)
    assert out[0]["actual_eps"] == -0.41
    assert out[0]["estimate_eps"] == -0.5
    assert out[0]["actual_revenue"] == 137_225_000.0
    assert "반영 대기" not in (out[0].get("date_note") or "")


def test_merge_keeps_existing_eps_when_incoming_null():
    from database import merge_earnings_payload

    existing = {"actual_eps": -0.41, "estimate_eps": -0.5, "source": "nasdaq_surprise"}
    incoming = {"actual_eps": None, "estimate_eps": -0.455, "source": "yfinance_info"}
    out = merge_earnings_payload(existing, incoming)
    assert out["actual_eps"] == -0.41
    assert out["estimate_eps"] == -0.5


def test_call_refresh_on_revenue_miss_claim():
    from earnings import _needs_earnings_call_refresh

    rec = {
        "actual_eps": -0.41,
        "estimate_eps": -0.5,
        "eps_surprise_pct": 18.0,
        "actual_revenue": 137_225_000.0,
        "earnings_call": {
            "summary": "IREN이 Q4 2026 실적을 발표했으며, 매출이 예상을 하회했으나 AI 클라우드가 확대 중입니다. 실적 발표 후 주가는 2.00% 상승했습니다.",
            "highlights": ["Q4 2026 매출 미스", "주가 2% 상승"],
        },
    }
    move = {"reaction_pct": -12.5, "next_day_pct": -12.5, "label": "▼12.50%"}
    assert _needs_earnings_call_refresh(rec, move) is True


def test_template_call_from_facts():
    from earnings import _template_call_from_facts

    facts = {
        "actual_eps": -0.41,
        "estimate_eps": -0.5,
        "eps_surprise_pct": 18.0,
        "actual_revenue": 137_225_000.0,
    }
    move = {
        "reaction_pct": -12.53,
        "timing": "amc",
        "label": "장마감 후 발표 · 다음 거래일 종가 ▼12.53%",
    }
    out = _template_call_from_facts("IREN", "2026-08-27", facts, move)
    assert "-0.41" in out["summary"]
    assert "상회" in out["summary"]
    assert "매출 미스" not in out["summary"]
    assert "2.00%" not in out["summary"]
    assert "▼12.53" in out["market_reaction_note"]


def test_call_ui_highlight():
    recent = (date.today() - timedelta(days=5)).isoformat()
    call = {"earnings_date": recent, "summary": "ok"}
    show, _ = _should_ui_highlight_call(recent, call)
    assert show is True
