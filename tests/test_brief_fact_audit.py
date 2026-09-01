"""시황 Phase 1 팩트 검증 단위 테스트."""

from brief_fact_audit import (
    _parse_cited_pct,
    audit_brief_facts,
)


def _sample_market_data():
    return {
        "미국": {
            "SPY": {
                "name": "S&P500",
                "change_pct": 0.85,
                "volume_ratio": 105.0,
                "stale": False,
            },
            "SMH": {
                "name": "반도체",
                "change_pct": -1.20,
                "volume_ratio": 92.0,
                "stale": False,
            },
        },
        "한국": {
            "^KS11": {
                "name": "KOSPI",
                "change_pct": 0.42,
                "volume_ratio": 88.0,
                "stale": False,
            },
        },
    }


def test_parse_cited_pct_signs():
    assert _parse_cited_pct("▲ 1.25") == 1.25
    assert _parse_cited_pct("▼ 0.50") == -0.50
    assert _parse_cited_pct("+2.1") == 2.1
    assert _parse_cited_pct("-0.3") == -0.3


def test_audit_pass_when_numbers_match():
    analysis = """
### 1. 흐름
SPY +0.85%, SMH -1.20%, KOSPI +0.42%로 마감.
"""
    result = audit_brief_facts(analysis, _sample_market_data())
    assert result["status"] == "pass"
    assert result["fail_count"] == 0
    assert result["pass_count"] >= 3


def test_audit_fail_on_mismatch():
    analysis = "SPY +1.50%로 강세 마감."
    result = audit_brief_facts(analysis, _sample_market_data(), change_tol=0.25)
    assert result["status"] == "fail"
    assert result["fail_count"] >= 1
    assert any(c["ticker"] == "SPY" for c in result["failures"])


def test_audit_warn_stale_cited_as_fresh():
    md = _sample_market_data()
    md["미국"]["SPY"]["stale"] = True
    md["미국"]["SPY"]["last_date"] = "2026-08-28"
    analysis = "SPY +0.85% 확정 마감."
    result = audit_brief_facts(analysis, md)
    assert result["warn_count"] >= 1
    assert any(c["field"] == "stale" for c in result["checks"])


def test_audit_skip_without_citations():
    analysis = "오늘 시장은 혼조세였다."
    result = audit_brief_facts(analysis, _sample_market_data())
    assert result["status"] == "skip"
    assert result["pass_count"] == 0
