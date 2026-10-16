"""Value-Aware Engine — Gate routing, IREN regression, validator, REDUCE≠SELL."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from signal_value import (
    compute_extension_regime,
    compute_thesis_status,
    compute_value_regime,
    route_value_aware,
)
from signal_engine import decide_signal, ENGINE_VERSION
from analysis_validator import validate_analysis_text


def _feat(**kwargs):
    base = {
        "trend": {
            "above_ma20": False,
            "above_ma60": False,
            "above_ma200": False,
            "price_vs_ma20": -0.10,
            "price_vs_ma60": -0.12,
            "ma20_vs_ma60": -0.03,
            "ma20": 40.0,
            "ma60": 42.0,
        },
        "momentum": {
            "rsi": 28.0,
            "stoch_k": 12.0,
            "macd_above_signal": False,
            "macd_above_zero": False,
            "macd_hist_rising": False,
            "macd": -1.0,
            "macd_signal": -0.5,
        },
        "volatility": {
            "bb_position": 0.12,
            "atr_pct": 0.05,
            "atr": 2.0,
            "gap_risk": 0.0,
        },
        "volume": {"rvol": 0.9, "volume_ratio": 0.9},
        "volume_profile": {
            "ok": True,
            "flags": {"near_support": True, "breakdown_hold": False},
            "nearest_support": {"price": 35.0},
            "nearest_resistance": {"price": 42.0},
        },
        "spatial": {
            "ok": True,
            "spatial_support": "AT_HVN_SUPPORT",
            "at_hvn": True,
            "in_lvn": False,
            "major_hvn_low": 34.0,
            "major_hvn_mid": 35.0,
            "hard_hvn_break": {"broken": False},
        },
        "returns": {"ret_5d": -0.08, "ret_1d": -0.02},
        "relative_strength": {"vs_sector_5d": -0.02, "vs_sector_5d_z": -0.5},
        "market_regime": {"regime": "SIDEWAYS"},
        "valuation": {
            "trailing_pe": None,
            "peg": None,
            "psr": 4.0,
            "drawdown_52w": -0.55,
            "vs_vwap_200": -0.20,
        },
        "price": {
            "close": 35.0,
            "drawdown_from_52w_high": -0.55,
            "vs_vwap_200": -0.20,
        },
        "structure": {},
    }
    for k, v in kwargs.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = {**base[k], **v}
        else:
            base[k] = v
    return base


def test_engine_version_v5():
    assert ENGINE_VERSION == "signal_engine_v5"


def test_value_regime_ignores_rsi():
    f = _feat(
        momentum={"rsi": 85},
        valuation={"psr": 2.0, "drawdown_52w": -0.60, "vs_vwap_200": -0.30},
        price={"drawdown_from_52w_high": -0.60, "vs_vwap_200": -0.30},
    )
    v = compute_value_regime(f)
    assert v["value_regime"] in ("UNDERVALUED", "FAIR")
    # RSI must not appear in value reasons
    assert not any("rsi" in r.lower() for r in v.get("reasons") or [])


def test_extension_oversold_separate_from_value():
    f = _feat(momentum={"rsi": 25}, volatility={"bb_position": 0.08})
    assert compute_extension_regime(f) == "OVERSOLD"


def test_thesis_warning_rs_sigma():
    f = _feat(relative_strength={"vs_sector_5d": -0.10, "vs_sector_5d_z": -2.2})
    t = compute_thesis_status(f)
    assert t["thesis_status"] == "WARNING"


def test_thesis_broken_rs_and_volume_climax():
    f = _feat(
        relative_strength={"vs_sector_5d_z": -3.2},
        volume={"rvol": 3.5},
        returns={"ret_1d": -0.08},
    )
    t = compute_thesis_status(f)
    assert t["thesis_status"] == "BROKEN"


def test_iren_oversold_no_exit():
    """IREN-like: 극과매도 + Thesis INTACT + Value 저평가 + HVN → SCALE_IN or WAIT, never EXIT."""
    f = _feat()
    va = route_value_aware(f)
    action = va["decision"]["action"]
    assert action in ("SCALE_IN", "WAIT", "HOLD")
    assert action != "EXIT"
    d = decide_signal(
        f,
        config={"deploy_flags": {"buy_enabled": True, "sell_enabled": True}},
    )
    assert d["entry_action"] in ("SCALE_IN", "WAIT", "HOLD")
    assert d["signal"] != "SELL"
    assert d["signal_type"] != "SELL"


def test_lvn_blocks_scale_in():
    f = _feat(
        spatial={
            "ok": True,
            "spatial_support": "IN_LVN_VACUUM",
            "at_hvn": False,
            "in_lvn": True,
            "hard_hvn_break": {"broken": False},
            "major_hvn_low": 30.0,
        },
        volume_profile={
            "ok": True,
            "flags": {"near_support": False, "breakdown_hold": True},
            "nearest_support": {"price": 30.0},
        },
    )
    va = route_value_aware(f)
    assert va["decision"]["action"] == "WAIT"
    assert va["decision"]["note"] == "lvn_wait"


def test_overbought_rich_reduce_not_sell():
    f = _feat(
        momentum={"rsi": 78, "stoch_k": 88},
        volatility={"bb_position": 0.92},
        trend={"price_vs_ma20": 0.12},
        valuation={"psr": 25.0, "peer_psr_median": 8.0},
        price={"drawdown_from_52w_high": -0.02, "vs_vwap_200": 0.25, "close": 60.0},
        spatial={
            "ok": True,
            "spatial_support": "AT_RESISTANCE",
            "at_hvn": False,
            "in_lvn": False,
            "hard_hvn_break": {"broken": False},
        },
    )
    va = route_value_aware(f)
    assert va["decision"]["action"] == "REDUCE"
    d = decide_signal(
        f,
        config={"deploy_flags": {"buy_enabled": True, "sell_enabled": True}},
    )
    assert d["entry_action"] == "REDUCE"
    assert d["signal_type"] == "REDUCE"
    assert d["signal"] != "SELL"


def test_hard_exit_on_thesis_broken():
    f = _feat(
        relative_strength={"vs_sector_5d_z": -3.5},
        volume={"rvol": 4.0},
        returns={"ret_1d": -0.10},
    )
    va = route_value_aware(f)
    assert va["decision"]["action"] == "EXIT"
    d = decide_signal(
        f,
        config={"deploy_flags": {"buy_enabled": True, "sell_enabled": True}},
    )
    assert d["entry_action"] == "EXIT"
    assert d["signal"] == "SELL"
    assert d["signal_type"] == "SELL"
    assert d["sub_action"] == "EXIT"


def test_gate4_weak_trend_hold_not_sell():
    """단기 약세만으로 EXIT 금지."""
    f = _feat(
        momentum={"rsi": 45, "stoch_k": 40},
        volatility={"bb_position": 0.40},
        trend={"price_vs_ma20": -0.04},
        spatial={
            "ok": True,
            "spatial_support": "UNKNOWN",
            "at_hvn": False,
            "in_lvn": False,
            "hard_hvn_break": {"broken": False},
        },
        volume_profile={
            "ok": True,
            "flags": {},
            "nearest_support": {"price": 33.0},
        },
    )
    va = route_value_aware(f)
    assert va["decision"]["action"] in ("HOLD", "WAIT")
    assert va["decision"]["action"] != "EXIT"


def test_validator_rejects_momentum_sell():
    bad = "주가가 하락 추세이므로 매도하는 것이 타당합니다. SIGNAL:SELL"
    r = validate_analysis_text(bad, {"entry_action": "HOLD", "decision": {"action": "HOLD"}})
    assert r["ok"] is False
    assert any("momentum_only_sell" in e for e in r["errors"])


def test_validator_ok_value_aware():
    good = (
        "Value=FAIR·Thesis INTACT·과매도 구간이다. "
        "비중은 Core 유지하며 분할매수(SCALE_IN)를 검토한다. "
        "Thesis 훼손 또는 Major HVN 주봉 이탈 시 Invalidation. "
        "HVN 지지 확인이 Next Trigger다."
    )
    r = validate_analysis_text(
        good, {"entry_action": "SCALE_IN", "decision": {"action": "SCALE_IN"}}
    )
    assert r["ok"] is True


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"OK {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    raise SystemExit(1 if failed else 0)
