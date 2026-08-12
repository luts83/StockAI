"""
Signal Engine v2 — Phase 4/5.

객관적 feature → Risk-adjusted Score → BUY / SELL / WATCH_* .

LLM은 설명층. 최종 `signal`은 이 엔진이 결정한다.
"""
from __future__ import annotations

from typing import Any, Optional

ENGINE_VERSION = "signal_engine_v2"

SIGNALS = (
    "BUY",
    "SELL",
    "AVOID",
    "WATCH_UP",
    "WATCH_FLAT",
    "WATCH_DOWN",
    "WATCH_RISK",
)

# 기본 임계값 — data/engine_config.json 이 있으면 덮어씀
BUY_SCORE_MIN = 58
SELL_SCORE_MAX = -38
WATCH_UP_MIN = 18
WATCH_DOWN_MAX = -18

_CONFIG_CACHE: dict | None = None
_CONFIG_MTIME: float | None = None


def _load_config() -> dict | None:
    """data/engine_config.json lazy load (mtime 캐시)."""
    global _CONFIG_CACHE, _CONFIG_MTIME
    try:
        from pathlib import Path
        path = Path(__file__).resolve().parent / "data" / "engine_config.json"
        if not path.exists():
            return None
        mtime = path.stat().st_mtime
        if _CONFIG_CACHE is not None and _CONFIG_MTIME == mtime:
            return _CONFIG_CACHE
        import json
        _CONFIG_CACHE = json.loads(path.read_text())
        _CONFIG_MTIME = mtime
        return _CONFIG_CACHE
    except Exception:
        return None


def get_thresholds(config: dict | None = None) -> dict:
    cfg = config if config is not None else _load_config()
    thr = (cfg or {}).get("thresholds") or {}
    return {
        "buy_min": float(thr.get("buy_min", BUY_SCORE_MIN)),
        "sell_max": float(thr.get("sell_max", SELL_SCORE_MAX)),
        "watch_up_min": float(thr.get("watch_up_min", WATCH_UP_MIN)),
        "watch_down_max": float(thr.get("watch_down_max", WATCH_DOWN_MAX)),
    }


def _g(d: dict | None, *keys, default=None):
    cur = d or {}
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _clip(x: float, lo: float = -100.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def normalize_signal(signal: str | None) -> str:
    s = (signal or "").strip().upper().replace(" ", "_")
    if s in SIGNALS:
        return s
    if s == "WATCH":
        return "WATCH_FLAT"
    if s in ("HOLD", "NEUTRAL"):
        return "WATCH_FLAT"
    return "WATCH_FLAT"


def is_watch(signal: str | None) -> bool:
    return normalize_signal(signal).startswith("WATCH")


def is_sell_family(signal: str | None) -> bool:
    return normalize_signal(signal) in ("SELL", "AVOID")


def hard_gates(features: dict) -> dict:
    """위험 BUY만 강제 차단. RSI/MACD/거래량은 Score로만 사용."""
    t = features.get("trend") or {}
    m = features.get("momentum") or {}
    vol = features.get("volatility") or {}
    regime = _g(features, "market_regime", "regime")

    price_vs_ma20 = t.get("price_vs_ma20")
    atr_pct = vol.get("atr_pct")
    bb_pos = vol.get("bb_position")

    trend_ok = price_vs_ma20 is None or price_vs_ma20 >= -0.05

    macd = m.get("macd")
    macd_sig = m.get("macd_signal")
    momentum_ok = True
    if macd is not None and macd_sig is not None:
        # MACD가 signal보다 크게 하회하면 BUY 차단
        momentum_ok = macd >= (macd_sig - max(abs(macd_sig) * 0.5, 0.3))

    risk_ok = True
    if atr_pct is not None and atr_pct > 0.12:
        risk_ok = False
    if bb_pos is not None and bb_pos > 1.15:
        risk_ok = False

    liquidity_ok = True
    avg_vol = _g(features, "volume", "avg_volume_20")
    if avg_vol is not None and avg_vol < 50_000:
        liquidity_ok = False

    bear_block = regime != "BEAR"

    return {
        "trend": bool(trend_ok),
        "momentum": bool(momentum_ok),
        "risk": bool(risk_ok),
        "liquidity": bool(liquidity_ok),
        "regime": bool(bear_block),
        "buy_allowed": bool(trend_ok and momentum_ok and risk_ok and liquidity_ok and bear_block),
    }


def compute_score(features: dict) -> dict:
    """-100~100 Risk-adjusted score + 구성 점수."""
    t = features.get("trend") or {}
    m = features.get("momentum") or {}
    v = features.get("volume") or {}
    rs = features.get("relative_strength") or {}
    vol = features.get("volatility") or {}
    regime = _g(features, "market_regime", "regime") or "UNKNOWN"

    parts: dict[str, float] = {}
    reasons: list[str] = []

    # Trend (max ~25)
    trend_pts = 0.0
    if t.get("above_ma20"):
        trend_pts += 8
        reasons.append("above_ma20")
    else:
        trend_pts -= 10
        reasons.append("below_ma20")
    if t.get("above_ma60"):
        trend_pts += 6
        reasons.append("above_ma60")
    else:
        trend_pts -= 4
    if t.get("above_ma200"):
        trend_pts += 4
    pvm = t.get("price_vs_ma20")
    if pvm is not None:
        if pvm > 0.08:
            trend_pts -= 4  # overextension vs MA20
            reasons.append("extended_vs_ma20")
        elif 0 <= pvm <= 0.05:
            trend_pts += 3
    parts["trend"] = trend_pts

    # Momentum (max ~25)
    mom = 0.0
    if m.get("macd_above_signal"):
        mom += 10
        reasons.append("macd_golden")
    else:
        mom -= 10
        reasons.append("macd_dead")
    if m.get("macd_above_zero"):
        mom += 5
    else:
        mom -= 3
    if m.get("macd_hist_rising"):
        mom += 4
        reasons.append("hist_rising")
    rsi = m.get("rsi")
    if rsi is not None:
        if 45 <= rsi <= 62:
            mom += 6
            reasons.append("rsi_sweet")
        elif 62 < rsi <= 70:
            mom += 1
            reasons.append("rsi_caution")
        elif rsi > 70:
            mom -= 12
            reasons.append("rsi_overbought")
        elif rsi < 35:
            mom -= 6
            reasons.append("rsi_weak")
        elif rsi < 45:
            mom -= 2
    parts["momentum"] = mom

    # Relative strength (max ~20)
    rs_pts = 0.0
    for key, w in (("vs_spy_20d", 10), ("vs_qqq_20d", 5), ("vs_sector_20d", 5)):
        val = rs.get(key)
        if val is None:
            continue
        if val > 0.03:
            rs_pts += w
            reasons.append(f"{key}_strong")
        elif val > 0:
            rs_pts += w * 0.4
        elif val < -0.03:
            rs_pts -= w
            reasons.append(f"{key}_weak")
        else:
            rs_pts -= w * 0.3
    parts["relative_strength"] = rs_pts

    # Volume (max ~14) — BUY 품질에 중요해서 가중
    vol_pts = 0.0
    vr = v.get("volume_ratio")
    if vr is not None:
        if vr >= 1.2:
            vol_pts += 12
            reasons.append("volume_confirm")
        elif vr >= 0.9:
            vol_pts += 5
        elif vr < 0.6:
            vol_pts -= 8
            reasons.append("volume_dry")
    parts["volume"] = vol_pts

    # Volume Profile / 매물대 (max ~12)
    vp_pts = 0.0
    vp = features.get("volume_profile") or {}
    if vp.get("ok"):
        flags = vp.get("flags") or {}
        if flags.get("breakout_hold"):
            vp_pts += 8
            reasons.append("vp_breakout_hold")  # 저항→지지 전환 안착
        if flags.get("breakdown_hold"):
            vp_pts -= 10
            reasons.append("vp_breakdown_hold")  # 지지→저항 전환
        if flags.get("near_support") and not flags.get("near_resistance"):
            vp_pts += 5
            reasons.append("vp_near_support")
        elif flags.get("near_resistance") and not flags.get("near_support"):
            vp_pts -= 6
            reasons.append("vp_near_resistance")
        elif flags.get("at_hvn"):
            vp_pts -= 2
            reasons.append("vp_at_hvn")
        if vp.get("vs_poc") == "above" and flags.get("breakout_hold"):
            vp_pts += 2
        elif vp.get("vs_poc") == "below" and flags.get("breakdown_hold"):
            vp_pts -= 2
    parts["volume_profile"] = vp_pts

    # Market regime (max ~10)
    reg_pts = 0.0
    if regime == "BULL":
        reg_pts += 8
        reasons.append("regime_bull")
    elif regime == "BEAR":
        reg_pts -= 12
        reasons.append("regime_bear")
    elif regime == "SIDEWAYS":
        reg_pts -= 2
    parts["regime"] = reg_pts

    # Risk penalties (drawdown / vol / overextension)
    risk = 0.0
    atrp = vol.get("atr_pct")
    if atrp is not None:
        if atrp > 0.08:
            risk -= 12
            reasons.append("high_atr")
        elif atrp > 0.05:
            risk -= 5
    bbp = vol.get("bb_position")
    if bbp is not None:
        if bbp > 1.0:
            risk -= 10
            reasons.append("bb_overextended")  # not a BUY reason
        elif bbp > 0.85:
            risk -= 4
            reasons.append("bb_upper_caution")
        elif 0.35 <= bbp <= 0.65:
            risk += 2
    gap = vol.get("gap_risk")
    if gap is not None and abs(gap) > 0.04:
        risk -= 6
        reasons.append("gap_risk")
    parts["risk"] = risk

    raw = sum(parts.values())
    score = _clip(raw)
    return {
        "score": round(score, 2),
        "components": {k: round(v, 2) for k, v in parts.items()},
        "reason_codes": reasons[:12],
    }


def classify_watch(score: float, features: dict, gates: dict) -> str:
    atrp = _g(features, "volatility", "atr_pct")
    gap = _g(features, "volatility", "gap_risk")
    if (atrp is not None and atrp > 0.10) or (gap is not None and abs(gap) > 0.05):
        return "WATCH_RISK"
    if not gates.get("risk", True) or not gates.get("liquidity", True):
        return "WATCH_RISK"
    if score >= WATCH_UP_MIN:
        return "WATCH_UP"
    if score <= WATCH_DOWN_MAX:
        return "WATCH_DOWN"
    return "WATCH_FLAT"


def decide_signal(features: dict) -> dict:
    """최종 시그널 결정 (+ calibration / empirical horizon)."""
    import math
    from signal_calibration import calibrated_p_up, calibrated_confidence

    config = _load_config()
    thr = get_thresholds(config)
    buy_min = thr["buy_min"]
    sell_max = thr["sell_max"]
    watch_up_min = thr["watch_up_min"]
    watch_down_max = thr["watch_down_max"]

    gates = hard_gates(features)
    scored = compute_score(features)
    score = scored["score"]

    signal = "WATCH_FLAT"
    if score >= buy_min and gates["buy_allowed"]:
        signal = "BUY"
    elif score <= sell_max:
        signal = "SELL"
    else:
        atrp = _g(features, "volatility", "atr_pct")
        gap = _g(features, "volatility", "gap_risk")
        if (atrp is not None and atrp > 0.10) or (gap is not None and abs(gap) > 0.05):
            signal = "WATCH_RISK"
        elif not gates.get("risk", True) or not gates.get("liquidity", True):
            signal = "WATCH_RISK"
        elif score >= watch_up_min:
            signal = "WATCH_UP"
        elif score <= watch_down_max:
            signal = "WATCH_DOWN"
        else:
            signal = "WATCH_FLAT"

    if score >= buy_min and not gates["buy_allowed"]:
        signal = "WATCH_UP" if gates.get("risk", True) else "WATCH_RISK"
        scored["reason_codes"] = list(scored["reason_codes"]) + ["buy_gate_blocked"]

    p_up = calibrated_p_up(score, config)
    p_down = 1 / (1 + math.exp(score / 25))
    p_flat = max(0.05, 1.0 - abs(p_up - p_down))
    ssum = p_up + p_down + p_flat
    prob = {
        "up": round(p_up / ssum, 3),
        "flat": round(p_flat / ssum, 3),
        "down": round(p_down / ssum, 3),
    }

    conf = calibrated_confidence(score, signal, config)

    emp = ((config or {}).get("empirical") or {}).get(signal) or {}
    expected_return = emp.get("expected_return") or {}
    expected_dd = emp.get("expected_drawdown") or {}

    rs = features.get("relative_strength") or {}
    regime = _g(features, "market_regime", "regime")

    return {
        "signal": signal,
        "signal_strength": int(_clip(abs(score), 0, 100)),
        "score": score,
        "confidence": conf,
        "confidence_calibrated": bool(config and (config.get("calibration") or {}).get("confidence_bins")),
        "probability": prob,
        "expected_return": {
            "5d": expected_return.get("5d"),
            "10d": expected_return.get("10d"),
            "20d": expected_return.get("20d"),
        },
        "expected_drawdown": {
            "5d": expected_dd.get("5d"),
            "10d": expected_dd.get("10d"),
            "20d": expected_dd.get("20d"),
        },
        "horizon": {
            "5d": {
                "expected_return": expected_return.get("5d"),
                "expected_drawdown": expected_dd.get("5d"),
            },
            "10d": {
                "expected_return": expected_return.get("10d"),
                "expected_drawdown": expected_dd.get("10d"),
            },
            "20d": {
                "expected_return": expected_return.get("20d"),
                "expected_drawdown": expected_dd.get("20d"),
            },
        },
        "relative_strength": {
            "vs_spy_20d": rs.get("vs_spy_20d"),
            "vs_qqq_20d": rs.get("vs_qqq_20d"),
            "vs_sector_20d": rs.get("vs_sector_20d"),
        },
        "market_regime": regime,
        "components": scored["components"],
        "reason_codes": scored["reason_codes"],
        "gate_status": gates,
        "engine_version": (config or {}).get("engine_version") or ENGINE_VERSION,
        "thresholds": thr,
    }


def refine_llm_watch(llm_signal: str, features: dict) -> str:
    """구버전 호환: LLM이 WATCH만 낸 경우 feature로 세분화."""
    s = (llm_signal or "").upper()
    if s.startswith("WATCH_") and s in SIGNALS:
        return s
    if s in ("BUY", "SELL", "AVOID"):
        return s
    return classify_watch(compute_score(features)["score"], features, hard_gates(features))
