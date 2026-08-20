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


if __name__ == "__main__":
    test_force_replaces_empty_date_only_cite()
    test_force_splits_glued_actual_result()
    test_forecast_citation_format()
    print("all ok")
