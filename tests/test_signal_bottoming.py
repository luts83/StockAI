"""Signal Engine v4 — Bottoming/Entry/Chase/Hysteresis + IREN invariants."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from signal_v4 import (
    classify_market_state,
    compute_bottoming_score,
    compute_chase_guard,
    compute_entry_score_v4,
    compute_extension_state,
    compute_oversold_component,
    compute_valuation_state,
    soften_sell_candidate,
)
from signal_engine import decide_signal, ENGINE_VERSION


def _feat(**kwargs):
    base = {
        "trend": {
            "above_ma20": False,
            "above_ma60": False,
            "above_ma200": False,
            "price_vs_ma20": -0.096,
            "price_vs_ma60": -0.12,
            "ma20_vs_ma60": -0.03,
            "ma20": 40.0,
            "ma60": 42.0,
        },
        "momentum": {
            "rsi": 32.0,
            "stoch_k": 11.5,
            "macd_above_signal": False,
            "macd_above_zero": False,
            "macd_hist_rising": False,
            "macd": -1.0,
            "macd_signal": -0.5,
        },
        "volatility": {
            "bb_position": 0.141,
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
        "returns": {"ret_5d": -0.08, "ret_1d": -0.02},
        "relative_strength": {},
        "market_regime": {"regime": "SIDEWAYS"},
        "valuation": {"trailing_pe": 80.0, "peg": 4.0},
        "price": {"close": 36.82},
        "structure": {},
    }
    for k, v in kwargs.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = {**base[k], **v}
        else:
            base[k] = v
    return base


def test_engine_version_v4():
    assert ENGINE_VERSION == "signal_engine_v4"


def test_oversold_correlation_cap():
    """RSI+Stoch+BB+MA20 모두 극단이어도 oversold ≤ 50."""
    f = _feat(
        momentum={"rsi": 25, "stoch_k": 5},
        volatility={"bb_position": 0.05},
        trend={"price_vs_ma20": -0.12},
    )
    o = compute_oversold_component(f)
    assert o["score"] <= 50
    assert o["raw_sum"] > o["score"]


def test_oversold_not_equal_bottom_confirmed():
    f = _feat(structure={})  # no stabilization
    b = compute_bottoming_score(f)
    assert b["oversold_score"] >= 25
    assert b["stabilization_score"] < 20 or not b["bottom_confirmed"]
    assert b["view"] in ("OVERSOLD_WATCH", "BOTTOMING_WATCH", "OBSERVE", "WATCH")


def test_valuation_ignores_ma_bb_rsi():
    f = _feat(
        valuation={"trailing_pe": 10.0, "peg": 0.8},
        momentum={"rsi": 85},
        volatility={"bb_position": 0.95},
        trend={"price_vs_ma20": 0.15},
    )
    assert compute_valuation_state(f) == "CHEAP"
    assert compute_extension_state(f) in ("EXTENDED", "OVEREXTENDED")


def test_chase_does_not_modify_entry_score():
    f = _feat(
        trend={"above_ma20": True, "above_ma60": True, "price_vs_ma20": 0.09},
        momentum={
            "rsi": 68,
            "macd_above_signal": True,
            "macd_hist_rising": True,
            "stoch_k": 70,
        },
        volatility={"bb_position": 0.85},
        returns={"ret_5d": 0.18},
        volume={"rvol": 0.7},
        volume_profile={"ok": True, "flags": {"near_resistance": True, "breakout_hold": True}},
    )
    entry = compute_entry_score_v4(f)
    chase = compute_chase_guard(f)
    assert chase["chase_blocked"] is True
    # score 독립 — chase 전후 동일 함수 재호출
    entry2 = compute_entry_score_v4(f)
    assert entry["score"] == entry2["score"]

    d = decide_signal(f, config={"deploy_flags": {"buy_enabled": True, "sell_enabled": True}, "thresholds": {"buy_min": 40}})
    assert d["entry_score"] == entry["score"]
    assert d["chase_blocked"] is True
    assert d["entry_action"] == "WAIT"


def test_soften_requires_more_than_bottoming_60():
    bottoming = {
        "score": 65,
        "stabilization_score": 0,
        "oversold_score": 65,
    }
    # soft_ctx needs stab>=8 or oversold>=28 — oversold high → can soften
    soft = soften_sell_candidate(
        sell_candidate=True,
        trend_label="BEARISH",
        bottoming=bottoming,
        hard_override={"override": False},
    )
    assert soft["softened"] is True

    hard = soften_sell_candidate(
        sell_candidate=True,
        trend_label="BEARISH",
        bottoming=bottoming,
        hard_override={"override": True, "reasons": ["hard_breakdown"]},
    )
    assert hard["softened"] is False
    assert hard["sell_candidate"] is True


def test_hysteresis_prevents_flicker():
    bottoming = {
        "score": 55,
        "stabilization_score": 10,
        "oversold_score": 40,
        "bottom_confirmed": False,
        "view": "BOTTOMING_WATCH",
    }
    a = classify_market_state(
        trend_label="BEARISH",
        bottoming=bottoming,
        extension="COMPRESSED",
        momentum="NEGATIVE",
        entry_score=40,
        prev_state="S3_BOTTOMING",
    )
    # bot 55 >= 45 → S3 유지 가능
    assert a["state"] == "S3_BOTTOMING"

    b = classify_market_state(
        trend_label="BEARISH",
        bottoming={**bottoming, "score": 30, "stabilization_score": 0, "oversold_score": 20},
        extension="NORMAL",
        momentum="NEGATIVE",
        entry_score=30,
        prev_state="S3_BOTTOMING",
    )
    assert b["state"] != "S6_OVEREXTENDED_UP"


def test_scale_in_uses_target_position_pct():
    f = _feat(
        structure={"higher_low": True, "volume_reversal": True},
        momentum={"rsi": 33, "stoch_k": 12, "macd_hist_rising": True},
    )
    d = decide_signal(
        f,
        config={"deploy_flags": {"buy_enabled": False, "sell_enabled": True}},
        fundamental_view="BULLISH",
    )
    scale = (d.get("position_actions") or {}).get("scale_in") or {}
    assert scale.get("sizing_basis") == "target_position"
    assert "포트폴리오" in (scale.get("note") or "") or scale.get("sizing_basis") == "target_position"


def test_iren_sep2_invariant_not_plain_sell():
    """
    IREN 9/2 근사: 과매도+Bottoming 후보.
    강제 BUY 아님. 단순 SELL/EXIT 단독 종료만 금지 (hard override 없을 때).
    """
    f = _feat()  # stoch 11.5, bb 14%, ma20 -9.6%, near support
    f["structure"] = {"volume_reversal": False, "fresh_lower_low": False}
    d = decide_signal(
        f,
        config={
            "deploy_flags": {"buy_enabled": True, "sell_enabled": True},
            "thresholds": {"buy_min": 75, "sell_max": -40},
        },
        fundamental_view="BULLISH",
    )
    assert d["oversold_score"] >= 20 or d["bottoming_score"] >= 40
    # hard override 없으면 EXIT+SELL 동시 확정만으로 끝내면 안 됨
    if not (d.get("hard_sell_override") or {}).get("override"):
        plain_sell = d["signal"] == "SELL" and d["entry_action"] == "EXIT"
        assert not plain_sell, (
            f"과매도 바닥 후보에서 단순 SELL/EXIT 금지: signal={d['signal']} "
            f"entry={d['entry_action']} bottoming={d['bottoming']} "
            f"bot_score={d['bottoming_score']}"
        )
    # BUY 강제 아님
    assert d["entry_action"] != "BUY" or d.get("stabilization_score", 0) >= 12


def test_iren_sep7_chase_wait_invariant():
    """9/7 근사: 상승 가능해도 추격이면 entry ≠ BUY."""
    f = _feat(
        trend={
            "above_ma20": True,
            "above_ma60": True,
            "price_vs_ma20": 0.09,
            "ma20_vs_ma60": 0.02,
        },
        momentum={
            "rsi": 68,
            "stoch_k": 75,
            "macd_above_signal": True,
            "macd_hist_rising": True,
            "macd_above_zero": True,
        },
        volatility={"bb_position": 0.82},
        returns={"ret_5d": 0.214},
        volume={"rvol": 0.79},
        volume_profile={
            "ok": True,
            "flags": {"near_resistance": True, "breakout_hold": True},
            "nearest_support": {"price": 40.0},
            "nearest_resistance": {"price": 46.0},
        },
        price={"close": 44.68},
        valuation={"trailing_pe": 90.0, "peg": 5.0},
    )
    d = decide_signal(
        f,
        config={"deploy_flags": {"buy_enabled": True, "sell_enabled": True}, "thresholds": {"buy_min": 40}},
        fundamental_view="BULLISH",
    )
    assert d["chase_blocked"] is True
    assert d["entry_action"] == "WAIT"
    pos = d["position_actions"]
    assert pos["new_investor"] == "WAIT"
    assert pos["holder"] in ("HOLD", "REDUCE")
    assert d["valuation"] == "EXPENSIVE"
    assert d["extension_state"] in ("EXTENDED", "OVEREXTENDED", "NORMAL")


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
