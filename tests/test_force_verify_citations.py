"""###0 전망 인용 강제 주입 단위 테스트."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_brief import _force_verify_citations, _forecast_citation


def test_force_replaces_empty_date_only_cite():
    analysis = """### 0. 직전 전망 검증
#### 직전 전망 (오늘 한국장 전 시황)
- 전망: [2026-08-20] -
- 실제 결과: KOSPI ▲5.89%, KOSDAQ ▲1.99%
- 판정: 빗나감

#### 간밤 미국→한국 전망 (us_close)
- 전망: [2026-08-19] -
- 실제 결과: KOSPI ▲5.89%
- 판정: 빗나감

---

### 1. 🇰🇷 한국 시장 마감 결과
본문
"""
    cites = [
        "[2026-08-20] SIGNAL:BEAR — 반도체 약세 우려",
        "[2026-08-19] SIGNAL:BEAR — 미국 SMH 약세 선행",
    ]
    out = _force_verify_citations(analysis, cites)
    assert "[2026-08-20] SIGNAL:BEAR — 반도체 약세 우려" in out
    assert "[2026-08-19] SIGNAL:BEAR — 미국 SMH 약세 선행" in out
    assert "전망: [2026-08-20] -" not in out
    assert "### 1. 🇰🇷 한국 시장 마감 결과" in out


def test_force_splits_glued_actual_result():
    analysis = """### 0. 직전 전망 검증
- 전망: [2026-08-20] - 실제 결과: KOSPI ▲5.89%, KOSDAQ ▲1.99%
- 판정: 빗나감

### 1. 본문
x
"""
    out = _force_verify_citations(
        analysis, ["[2026-08-20] SIGNAL:BEAR — 약세 전망"]
    )
    assert "- 전망: [2026-08-20] SIGNAL:BEAR — 약세 전망" in out
    assert "- 실제 결과: KOSPI ▲5.89%, KOSDAQ ▲1.99%" in out


def test_forecast_citation_format():
    doc = {
        "date": "2026-08-20",
        "signal": "BEAR",
        "analysis": "### 💡 한 줄 요약\nSMH 약세 때문에 오늘 한국장은 반도체 주의.\n",
    }
    cite = _forecast_citation(doc)
    assert cite.startswith("[2026-08-20] SIGNAL:BEAR — ")
    assert "반도체" in cite


def test_forecast_citation_ignores_section0_recursion():
    """###0 검증 섹션이 '전망' 글자 때문에 재귀 오염되면 안 됨."""
    analysis = """### 0. 직전 전망 검증
- 전망: [2026-08-20] SIGNAL:BEAR — 0. 직전 전망 검증- 전망: [2026-08-19] SIGNAL:BEAR — 오염된 인용
- 판정: 검증 보류

### 🔮 내일 한국 시장 전망
**결론: 약세 우위**
- 강세 조건: SMH ▲1%+

### 💡 한 줄 요약
오늘 미국 RSP ▲1.04% vs SPY ▲0.21% 격차 + VIX ▼6.0% 때문에 내일 한국장은 반도체 외 업종 방어 vs 삼성·하이닉스 반등력 주목.
"""
    cite = _forecast_citation({
        "date": "2026-08-19",
        "signal": "BEAR",
        "analysis": analysis,
    })
    assert cite.startswith("[2026-08-19] SIGNAL:BEAR — ")
    assert "직전 전망 검증" not in cite
    assert "0. 직전" not in cite
    assert "RSP" in cite or "VIX" in cite or "반도체" in cite
    assert cite.count("SIGNAL:") == 1


def test_sanitize_nested_cite_body():
    from market_brief import _sanitize_cite_body, _sanitize_full_cite
    nested = (
        "[2026-08-20] SIGNAL:BEAR — 0. 직전 전망 검증- 전망: [2026-08-20] "
        "SIGNAL:BEAR — 0. 직전 전망 검증- 전망: [2026-08-19] SIGNAL:BEAR — "
        "오늘 미국 RSP ▲1.04% vs SPY ▲0.21% 격차 + VIX ▼6.0% 때문에 내일 한국장은 주목."
    )
    cleaned = _sanitize_full_cite(nested)
    assert cleaned.count("SIGNAL:") == 1
    assert "직전 전망 검증" not in cleaned
    assert "RSP" in cleaned


def test_force_fixes_numbered_header_without_hash():
    """LLM이 '0.' 헤더만 쓴 경우에도 전망 인용 보정."""
    analysis = """0. 직전 전망 검증
직전 전망
전망: [2026-08-28]
실제 결과: SPY ▼0.23%, QQQ ▼0.65%
판정: 부분 적중

1. 📖 오늘 미국장의 흐름
본문 시작
"""
    cite = "[2026-08-28] SIGNAL:NEUTRAL — 워시 매파 발언 전 기술주 조정 우려"
    out = _force_verify_citations(analysis, [cite])
    assert cite in out
    assert "전망: [2026-08-28]\n" not in out or cite in out


def test_repair_truncated_summary():
    from market_brief import _repair_truncated_brief_tail
    broken = """### 🔮 다음 거래일 전망
**결론: 약세 우위 (조건부 중립)**

### 💡 한 줄 요약
📰 **워시 "
"""
    out = _repair_truncated_brief_tail(broken)
    assert "워시 \"" not in out or "약세" in out
    assert len(out.split("### 💡 한 줄 요약")[-1].strip()) >= 15


if __name__ == "__main__":
    test_force_replaces_empty_date_only_cite()
    test_force_splits_glued_actual_result()
    test_forecast_citation_format()
    test_forecast_citation_ignores_section0_recursion()
    test_sanitize_nested_cite_body()
    print("all ok")
