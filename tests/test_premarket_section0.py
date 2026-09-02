"""장전 시황 ###0 참고 인용 블록 테스트."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_brief import _inject_premarket_context, _verify_block


def test_premarket_verify_block_no_verdict():
    block = _verify_block(
        mode="defer",
        cite="[2026-09-01] SIGNAL:BEAR — SMH 약세",
        source="직전 미국 마감 시황 (us_close)",
        score_when="오늘 한국 마감 시황(kr_close) · KOSPI 등락률",
        extra="간밤 수치를 오늘 전망 근거로 활용",
    )
    assert "직전 시장 전망 (참고)" in block
    assert "검증 보류" not in block
    assert "실제 결과" not in block
    assert "- 판정:" not in block
    assert "**판정**" not in block
    assert "SIGNAL:BEAR" in block


def test_inject_premarket_replaces_old_defer_section():
    analysis = """### 0. 직전 전망 검증
- 전망: [2026-09-01] SIGNAL:BEAR — 구버전
- 판정: 검증 보류

---

### 1. 흐름
본문
"""
    cite = "[2026-09-01] SIGNAL:BEAR — SMH ▼2%"
    out = _inject_premarket_context(
        analysis,
        cite=cite,
        source="직전 미국 마감 시황 (us_close)",
        score_when="kr_close · KOSPI",
        usage_hint="오늘 활용",
    )
    assert "직전 시장 전망 (참고)" in out
    assert "검증 보류" not in out
    assert cite in out
    assert "### 1. 흐름" in out
