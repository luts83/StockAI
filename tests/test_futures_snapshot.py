"""선물 스냅샷 방향·포맷 단위 테스트."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_brief import (
    _futures_direction,
    _futures_direction_label,
    _futures_group_tone,
    format_futures_header_block,
)


def test_futures_direction_threshold():
    assert _futures_direction(0.20) == "상승"
    assert _futures_direction(-0.15) == "하락"
    assert _futures_direction(0.02) == "보합"
    assert _futures_direction_label("상승") == "상승중"
    assert _futures_direction_label("하락") == "하락중"
    assert _futures_direction_label("보합") == "보합권"


def test_futures_group_tone():
    quotes = [
        {"direction": "상승"},
        {"direction": "상승"},
        {"direction": "하락"},
    ]
    assert _futures_group_tone(quotes) == "상승세"


def test_format_futures_header_block():
    snap = {
        "as_of": "2026-09-03T08:00:00+09:00",
        "markets": [{
            "label": "간밤 미국 선물",
            "tone": "하락세",
            "quotes": [{
                "ticker": "ES=F",
                "name": "S&P500 선물",
                "price": 5642.5,
                "change_pct": -0.32,
                "direction": "하락",
                "direction_label": "하락중",
            }],
        }],
    }
    block = format_futures_header_block(snap, "kr_premarket")
    assert "### 📡 선물 지수 스냅샷" in block
    assert "하락세" in block
    assert "하락중" in block
    assert "ES=F" in block
