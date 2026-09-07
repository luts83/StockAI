"""
Signal Engine v4 helpers.

Bottoming = Oversold(cap) + Stabilization — 과매도 ≠ 바닥 확정.
Valuation = 멀티플만 / Extension = 기술 과열 분리.
Chase guard는 entry_score를 수정하지 않음.
Market state는 hysteresis로 플리커 방지.
"""
from __future__ import annotations

from typing import Any, Optional

ENTRY_ACTIONS = ("BUY", "SCALE_IN", "WAIT", "REDUCE", "EXIT")
MARKET_STATES = (
    "S1_STRONG_DOWN",
    "S2_DOWN",
    "S3_BOTTOMING",
    "S4_REVERSAL",
    "S5_UP",
    "S6_OVEREXTENDED_UP",
)
_STATE_RANK = {s: i for i, s in enumerate(MARKET_STATES)}


def _g(d: dict | None, *keys, default=None):
    cur = d or {}
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _f(x, default=None) -> Optional[float]:
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def _clip(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _rvol(features: dict) -> Optional[float]:
    v = features.get("volume") or {}
    r = v.get("rvol")
    if r is None:
        r = v.get("volume_ratio")
    return _f(r)


def compute_oversold_component(features: dict) -> dict:
    """
    RSI/Stoch/BB/MA20 괴리는 상관 → 개별 기여 후 hard cap 50.
    max 기여 + 보조 기여(부분)로 중복 점수 제한.
    """
    m = features.get("momentum") or {}
    vol = features.get("volatility") or {}
    t = features.get("trend") or {}
    parts: dict[str, float] = {}

    rsi = _f(m.get("rsi"))
    if rsi is not None:
        if rsi < 30:
            parts["rsi"] = 20
        elif rsi < 35:
            parts["rsi"] = 15
        elif rsi < 40:
            parts["rsi"] = 10
        elif rsi < 45:
            parts["rsi"] = 5
        else:
            parts["rsi"] = 0

    stoch = _f(m.get("stoch_k"))
    if stoch is not None:
        if stoch < 10:
            parts["stoch"] = 20
        elif stoch < 20:
            parts["stoch"] = 15
        elif stoch < 30:
            parts["stoch"] = 8
        else:
            parts["stoch"] = 0

    bbp = _f(vol.get("bb_position"))
    # bb_position: 0=하단, 1=상단. 스펙 % → 0~1
    if bbp is not None:
        pct = bbp * 100 if bbp <= 1.5 else bbp
        if pct < 10:
            parts["bb"] = 20
        elif pct < 20:
            parts["bb"] = 15
        elif pct < 30:
            parts["bb"] = 8
        else:
            parts["bb"] = 0

    # price_vs_ma20는 소수( -0.096 = -9.6% )
    pvm = _f(t.get("price_vs_ma20"))
    if pvm is not None:
        dev_pct = pvm * 100
        if dev_pct < -10:
            parts["ma20_dev"] = 15
        elif dev_pct < -7:
            parts["ma20_dev"] = 12
        elif dev_pct < -5:
            parts["ma20_dev"] = 7
        else:
            parts["ma20_dev"] = 0

    if not parts:
        return {"score": 0.0, "parts": {}, "raw_sum": 0.0, "capped": False}

    # 상관 중복 제한: max + 0.35 * (sum - max)
    vals = list(parts.values())
    mx = max(vals)
    raw = sum(vals)
    blended = mx + 0.35 * (raw - mx)
    capped = min(50.0, blended)
    return {
        "score": round(capped, 2),
        "parts": {k: round(v, 2) for k, v in parts.items()},
        "raw_sum": round(raw, 2),
        "capped": capped < raw or blended > 50,
    }


def compute_stabilization_component(features: dict) -> dict:
    """지지 근접·거래량 반전·higher-low·false breakdown 회복 등."""
    pts = 0.0
    reasons: list[str] = []
    vp = features.get("volume_profile") or {}
    flags = (vp.get("flags") or {}) if vp.get("ok") else {}
    struct = features.get("structure") or {}

    near_sup = bool(flags.get("near_support"))
    if near_sup:
        pts += 10
        reasons.append("near_support")

    # 거래량 반전 힌트 (structure 또는 volume 파생)
    vol_rev = struct.get("volume_reversal")
    if vol_rev is True:
        pts += 10
        reasons.append("volume_reversal")
    else:
        rvol = _rvol(features)
        down_vol_falling = struct.get("down_volume_falling")
        up_vol_rising = struct.get("up_volume_rising")
        if down_vol_falling and up_vol_rising and rvol is not None and rvol >= 0.9:
            pts += 10
            reasons.append("volume_reversal")

    if struct.get("higher_low"):
        pts += 12
        reasons.append("higher_low")
    elif struct.get("lower_low") is False and struct.get("fresh_lower_low") is False:
        # 명시적 LL 아님
        pass

    if struct.get("false_breakdown") or flags.get("false_breakdown"):
        pts += 12
        reasons.append("false_breakdown_recover")

    if struct.get("macd_hist_improving") or _g(features, "momentum", "macd_hist_rising"):
        # 약한 안정화 가산 (과매도 맥락에서만 의미 — 호출측에서 oversolds와 합침)
        if pts > 0 or near_sup:
            pts += 6
            reasons.append("macd_hist_improve")

    score = min(50.0, pts)
    return {"score": round(score, 2), "reasons": reasons}


def compute_bottoming_score(features: dict) -> dict:
    oversold = compute_oversold_component(features)
    stab = compute_stabilization_component(features)
    total = round(min(100.0, oversold["score"] + stab["score"]), 2)

    if total >= 76 and stab["score"] >= 20:
        label = "EXTREME"
        view = "REVERSAL_CANDIDATE"
    elif total >= 61 and stab["score"] >= 12:
        label = "HIGH"
        view = "BOTTOMING_HIGH"
    elif total >= 46 or (oversold["score"] >= 30 and stab["score"] < 12):
        label = "WATCH"
        # 과매도만 높고 안정화 약함
        view = "OVERSOLD_WATCH" if stab["score"] < 12 and oversold["score"] >= 25 else "BOTTOMING_WATCH"
    elif total >= 26:
        label = "WATCH"
        view = "OBSERVE"
    else:
        label = "LOW"
        view = "NORMAL"

    alert = bool(_f(_g(features, "momentum", "stoch_k"), 100) < 10 and oversold["score"] >= 15)

    return {
        "score": total,
        "label": label,
        "view": view,
        "oversold_score": oversold["score"],
        "stabilization_score": stab["score"],
        "oversold": oversold,
        "stabilization": stab,
        "stoch_alert": alert,
        # 과매도 ≠ 바닥 확정
        "bottom_confirmed": stab["score"] >= 20 and oversold["score"] >= 20,
    }


def compute_entry_score_v4(features: dict) -> dict:
    """0–100. chase_guard는 여기 반영하지 않음."""
    t = features.get("trend") or {}
    m = features.get("momentum") or {}
    vol = features.get("volatility") or {}
    struct = features.get("structure") or {}
    pts = 40.0  # baseline mid
    reasons: list[str] = []

    if t.get("above_ma20"):
        pts += 15
        reasons.append("ma20_reclaim")
    if t.get("above_ma60"):
        pts += 15
        reasons.append("ma60_reclaim")

    if m.get("macd_above_signal") and m.get("macd_hist_rising"):
        pts += 15
        reasons.append("macd_bullish_cross")
    elif m.get("macd_above_signal"):
        pts += 8
        reasons.append("macd_above_signal")

    rsi = _f(m.get("rsi"))
    if rsi is not None:
        if 45 <= rsi <= 65:
            pts += 10
            reasons.append("rsi_sweet")
        elif rsi > 70:
            pts -= 10
            reasons.append("rsi_hot_penalty")

    rvol = _rvol(features)
    if rvol is not None:
        if rvol >= 1.0:
            pts += 15
            reasons.append("rvol_ok")
        elif rvol < 0.8:
            pts -= 10
            reasons.append("rvol_low")

    vp = features.get("volume_profile") or {}
    flags = (vp.get("flags") or {}) if vp.get("ok") else {}
    if flags.get("breakout_hold") or struct.get("resistance_break"):
        pts += 15
        reasons.append("resistance_break")
    if struct.get("higher_low"):
        pts += 10
        reasons.append("higher_low")

    # risk/reward 양호
    rr_hint = struct.get("rr_ok")
    if rr_hint is True:
        pts += 5
        reasons.append("rr_ok")
    else:
        # VP 기반 간단 추정
        price = _f(_g(features, "price", "close"))
        ns = (vp.get("nearest_support") or {}) if vp.get("ok") else {}
        nr = (vp.get("nearest_resistance") or {}) if vp.get("ok") else {}
        try:
            if price and ns.get("price") and nr.get("price"):
                risk = abs(price - float(ns["price"]))
                reward = abs(float(nr["price"]) - price)
                if risk > 0 and reward / risk >= 1.3:
                    pts += 5
                    reasons.append("rr_ok")
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    bbp = _f(vol.get("bb_position"))
    if bbp is not None:
        pct = bbp * 100 if bbp <= 1.5 else bbp
        if pct > 80:
            pts -= 10
            reasons.append("bb_high")

    if flags.get("near_resistance") and not flags.get("near_support"):
        pts -= 10
        reasons.append("under_resistance")

    ret5 = _f(_g(features, "returns", "ret_5d"))
    if ret5 is not None and ret5 > 0.15:
        pts -= 10
        reasons.append("short_rally_hot")

    pvm = _f(t.get("price_vs_ma20"))
    if pvm is not None and pvm > 0.08:
        pts -= 10
        reasons.append("ma20_extension")

    score = round(_clip(pts), 2)
    if score >= 75:
        band = "BUY"
    elif score >= 60:
        band = "BUY_ADD"
    elif score >= 45:
        band = "WAIT"
    elif score >= 30:
        band = "REDUCE_WAIT"
    else:
        band = "AVOID_EXIT"

    return {"score": score, "band": band, "reasons": reasons}


def compute_chase_guard(features: dict) -> dict:
    """entry_score와 독립. 조건 2개 이상 → chase_blocked."""
    flags: list[str] = []
    t = features.get("trend") or {}
    m = features.get("momentum") or {}
    vol = features.get("volatility") or {}
    vp = features.get("volume_profile") or {}
    vp_flags = (vp.get("flags") or {}) if vp.get("ok") else {}

    ret5 = _f(_g(features, "returns", "ret_5d"))
    if ret5 is not None and ret5 > 0.15:
        flags.append("ret_5d_gt_15")

    rsi = _f(m.get("rsi"))
    if rsi is not None and rsi > 65:
        flags.append("rsi_gt_65")

    bbp = _f(vol.get("bb_position"))
    if bbp is not None:
        pct = bbp * 100 if bbp <= 1.5 else bbp
        if pct > 80:
            flags.append("bb_gt_80")

    pvm = _f(t.get("price_vs_ma20"))
    if pvm is not None and pvm > 0.08:
        flags.append("ma20_dev_gt_8")

    if vp_flags.get("near_resistance"):
        flags.append("near_resistance")

    rvol = _rvol(features)
    if rvol is not None and rvol < 1.0:
        flags.append("rvol_lt_1")

    blocked = len(flags) >= 2
    return {"chase_blocked": blocked, "reasons": flags, "hit_count": len(flags)}


def compute_momentum_state(features: dict) -> str:
    m = features.get("momentum") or {}
    lean = 0
    if m.get("macd_above_signal"):
        lean += 1
    else:
        lean -= 1
    if m.get("macd_hist_rising"):
        lean += 1
    else:
        lean -= 1
    rsi = _f(m.get("rsi"))
    if rsi is not None:
        if rsi >= 55:
            lean += 1
        elif rsi <= 45:
            lean -= 1
    if lean >= 2:
        return "POSITIVE"
    if lean <= -2:
        return "NEGATIVE"
    return "NEUTRAL"


def compute_valuation_state(features: dict) -> str:
    """멀티플만. MA/BB/RSI 사용 금지."""
    val = features.get("valuation") or {}
    peg = _f(val.get("peg"))
    pe = _f(val.get("trailing_pe") or val.get("forward_pe"))
    # 데이터 없으면 FAIR
    if peg is None and pe is None:
        return "FAIR"
    rich = 0
    cheap = 0
    if peg is not None:
        if peg > 3.5:
            rich += 2
        elif peg > 2.5:
            rich += 1
        elif peg < 1.0:
            cheap += 2
        elif peg < 1.5:
            cheap += 1
    if pe is not None and pe > 0:
        if pe > 60:
            rich += 2
        elif pe > 35:
            rich += 1
        elif pe < 12:
            cheap += 2
        elif pe < 18:
            cheap += 1
    if rich >= 2 and rich > cheap:
        return "EXPENSIVE"
    if cheap >= 2 and cheap > rich:
        return "CHEAP"
    return "FAIR"


def compute_extension_state(features: dict) -> str:
    """기술 과열/압축 — valuation과 분리."""
    t = features.get("trend") or {}
    m = features.get("momentum") or {}
    vol = features.get("volatility") or {}
    hot = 0
    cold = 0

    pvm = _f(t.get("price_vs_ma20"))
    if pvm is not None:
        if pvm > 0.10:
            hot += 2
        elif pvm > 0.06:
            hot += 1
        elif pvm < -0.08:
            cold += 2
        elif pvm < -0.04:
            cold += 1

    bbp = _f(vol.get("bb_position"))
    if bbp is not None:
        pct = bbp * 100 if bbp <= 1.5 else bbp
        if pct > 90:
            hot += 2
        elif pct > 80:
            hot += 1
        elif pct < 15:
            cold += 2
        elif pct < 30:
            cold += 1

    rsi = _f(m.get("rsi"))
    if rsi is not None:
        if rsi > 72:
            hot += 2
        elif rsi > 65:
            hot += 1
        elif rsi < 30:
            cold += 2
        elif rsi < 40:
            cold += 1

    if hot >= 3:
        return "OVEREXTENDED"
    if hot >= 2:
        return "EXTENDED"
    if cold >= 3:
        return "COMPRESSED"
    return "NORMAL"


def detect_false_breakdown(features: dict) -> dict:
    struct = features.get("structure") or {}
    if struct.get("false_breakdown"):
        return {"detected": True, "reason": "structure_flag"}
    vp = features.get("volume_profile") or {}
    flags = (vp.get("flags") or {}) if vp.get("ok") else {}
    if flags.get("false_breakdown"):
        return {"detected": True, "reason": "vp_flag"}
    # heuristic: near_support after recent breakdown flag cleared
    if flags.get("near_support") and struct.get("reclaimed_support"):
        return {"detected": True, "reason": "support_reclaim"}
    return {"detected": False, "reason": ""}


def detect_hard_sell_overrides(features: dict, bottoming: dict) -> dict:
    """softener를 막는 hard 조건."""
    struct = features.get("structure") or {}
    vp = features.get("volume_profile") or {}
    flags = (vp.get("flags") or {}) if vp.get("ok") else {}
    rvol = _rvol(features)
    reasons: list[str] = []

    hard_bd = bool(
        flags.get("breakdown_hold")
        or struct.get("hard_breakdown")
        or (
            struct.get("support_break")
            and rvol is not None
            and rvol >= 1.1
            and bottoming.get("stabilization_score", 0) < 12
        )
    )
    if hard_bd:
        reasons.append("hard_breakdown")

    fresh_ll = bool(struct.get("fresh_lower_low") or struct.get("lower_low"))
    # Bottoming 상승에도 LL 지속
    if fresh_ll and bottoming.get("score", 0) >= 46 and bottoming.get("stabilization_score", 0) < 15:
        reasons.append("fresh_lower_low")

    if struct.get("event_shock") or features.get("event_shock"):
        reasons.append("event_shock")

    return {
        "override": bool(reasons),
        "reasons": reasons,
        "hard_breakdown": "hard_breakdown" in reasons,
        "fresh_lower_low": "fresh_lower_low" in reasons,
        "event_shock": "event_shock" in reasons,
    }


def _raw_market_state(
    trend_label: str,
    bottoming: dict,
    extension: str,
    momentum: str,
    entry_score: float,
) -> str:
    """hysteresis 없는 순수 분류."""
    bull = trend_label in ("STRONG_BULLISH", "BULLISH")
    bear = trend_label in ("STRONG_BEARISH", "BEARISH")
    bot = bottoming.get("score", 0)
    stab = bottoming.get("stabilization_score", 0)
    oversold = bottoming.get("oversold_score", 0)

    if extension in ("OVEREXTENDED", "EXTENDED") and bull:
        return "S6_OVEREXTENDED_UP"
    if bull and entry_score >= 60 and momentum == "POSITIVE":
        return "S5_UP"
    if bull or (stab >= 18 and oversold >= 15 and momentum != "NEGATIVE"):
        if stab >= 18 and (bottoming.get("bottom_confirmed") or entry_score >= 55):
            return "S4_REVERSAL"
    if bot >= 60 or (oversold >= 30 and stab >= 8):
        return "S3_BOTTOMING"
    if trend_label == "STRONG_BEARISH" or (bear and bot < 40):
        return "S1_STRONG_DOWN" if trend_label == "STRONG_BEARISH" else "S2_DOWN"
    if bear:
        return "S2_DOWN"
    return "S2_DOWN" if bear else "S5_UP" if bull else "S2_DOWN"


def classify_market_state(
    *,
    trend_label: str,
    bottoming: dict,
    extension: str,
    momentum: str,
    entry_score: float,
    prev_state: str | None = None,
) -> dict:
    """
    진입/이탈 threshold 분리. 인접 ±1 전이 우선.
    """
    raw = _raw_market_state(trend_label, bottoming, extension, momentum, entry_score)
    prev = prev_state if prev_state in _STATE_RANK else None
    if not prev:
        return {"state": raw, "raw": raw, "hysteresis_held": False, "prev": None}

    # 이탈이 더 빡센 조건: S3 유지는 bottoming>=45, S6 유지는 extension still hot
    held = prev
    bot = bottoming.get("score", 0)
    stab = bottoming.get("stabilization_score", 0)

    if prev == "S3_BOTTOMING":
        # 이탈: bot <= 45 and stab < 8 → 아니면 S3 유지 가능
        if bot >= 45 or stab >= 8:
            if raw in ("S2_DOWN", "S1_STRONG_DOWN") and bot >= 45:
                held = "S3_BOTTOMING"
            else:
                held = raw
        else:
            held = raw
    elif prev == "S6_OVEREXTENDED_UP":
        if extension in ("EXTENDED", "OVEREXTENDED"):
            held = "S6_OVEREXTENDED_UP" if raw in ("S5_UP", "S6_OVEREXTENDED_UP", "S4_REVERSAL") else raw
        else:
            held = raw
    elif prev == "S4_REVERSAL":
        if stab >= 10 and trend_label not in ("STRONG_BEARISH",):
            if raw in ("S2_DOWN", "S3_BOTTOMING"):
                held = "S4_REVERSAL"
            else:
                held = raw
        else:
            held = raw
    else:
        held = raw

    # 점프 제한: rank 차이 > 2 이면 한 칸만 이동
    pr, hr = _STATE_RANK[prev], _STATE_RANK[held]
    if abs(hr - pr) > 2:
        step = 1 if hr > pr else -1
        held = MARKET_STATES[pr + step]

    return {
        "state": held,
        "raw": raw,
        "hysteresis_held": held != raw,
        "prev": prev,
    }


def _entry_band(score: float) -> str:
    if score >= 60:
        return "High"
    if score >= 45:
        return "Medium"
    return "Low"


def _bottoming_band(bottoming: dict) -> str:
    view = bottoming.get("view") or ""
    if view == "OVERSOLD_WATCH":
        return "HighOversold"  # 안정화 약함 — SCALE_IN 확정 금지용
    sc = bottoming.get("score", 0)
    if sc >= 61 and bottoming.get("stabilization_score", 0) >= 12:
        return "High"
    if sc >= 46:
        return "Watch"
    return "Low"


def decide_entry_action(
    *,
    trend_label: str,
    bottoming: dict,
    entry_score: float,
    chase_blocked: bool,
    hard_override: dict,
    extension: str,
    false_breakdown: dict,
) -> dict:
    """
    Decision matrix + chase override (score 불변).
    Oversold-only High → SCALE_IN 확정 금지.
    """
    bull = trend_label in ("STRONG_BULLISH", "BULLISH")
    bear = trend_label in ("STRONG_BEARISH", "BEARISH")
    eband = _entry_band(entry_score)
    bband = _bottoming_band(bottoming)
    action = "WAIT"
    note = ""

    if hard_override.get("override") and bear:
        action = "EXIT"
        note = "hard_sell_override"
    elif bear and bband == "Low" and eband == "Low":
        action = "EXIT"
    elif bear and bband in ("High", "Watch", "HighOversold") and eband == "Low":
        action = "REDUCE"
        note = "bearish_bottoming_watch"
    elif bear and bband == "High" and eband == "Medium":
        action = "SCALE_IN"
    elif bear and bband == "HighOversold":
        action = "WAIT"  # 과매도만 — 바닥 미확정
        note = "oversold_not_confirmed"
    elif (not bull and not bear) and bband == "High" and eband == "Medium":
        action = "SCALE_IN"
    elif (not bull and not bear) and eband == "Low":
        action = "WAIT"
    elif bull and eband == "High" and bband != "HighOversold":
        action = "BUY"
    elif bull and eband == "Medium":
        action = "WAIT"
    elif bull and eband == "Low":
        action = "WAIT"
    elif bull and extension in ("EXTENDED", "OVEREXTENDED"):
        action = "WAIT"
        note = "overextended"
    else:
        action = "WAIT"

    if false_breakdown.get("detected") and action == "EXIT" and not hard_override.get("hard_breakdown"):
        action = "REDUCE"
        note = "false_breakdown_soften"

    if chase_blocked and action in ("BUY", "SCALE_IN"):
        action = "WAIT"
        note = "chase_blocked"

    # SCALE_IN은 stabilization 필요
    if action == "SCALE_IN" and bottoming.get("stabilization_score", 0) < 12:
        action = "WAIT"
        note = "scale_in_needs_stabilization"

    return {
        "entry_action": action,
        "entry_band": eband,
        "bottoming_band": bband,
        "note": note,
    }


def soften_sell_candidate(
    *,
    sell_candidate: bool,
    trend_label: str,
    bottoming: dict,
    hard_override: dict,
) -> dict:
    """
    Bottoming>=60 단독으로 SELL 금지하지 않음.
    완화는 bearish + bottoming>=60 + soft 맥락 + hard override 없을 때만.
    """
    if not sell_candidate:
        return {"sell_candidate": False, "softened": False, "reason": ""}
    if hard_override.get("override"):
        return {"sell_candidate": True, "softened": False, "reason": "hard_override"}

    bear = trend_label in ("STRONG_BEARISH", "BEARISH")
    bot = bottoming.get("score", 0)
    stab = bottoming.get("stabilization_score", 0)
    oversold = bottoming.get("oversold_score", 0)

    soft_ctx = bot >= 60 and (stab >= 8 or oversold >= 28)
    if bear and soft_ctx:
        return {
            "sell_candidate": False,
            "softened": True,
            "reason": "bottoming_soft_context",
        }
    return {"sell_candidate": True, "softened": False, "reason": ""}


def build_position_actions(
    *,
    entry_action: str,
    chase_blocked: bool,
    bottoming: dict,
    trend_label: str,
    scale_tranche: int | None = None,
) -> dict:
    """
    holder / new_investor / trader.
    scale-in % = target_position 대비 (포트폴리오 전체 비중 아님).
    """
    bull = trend_label in ("STRONG_BULLISH", "BULLISH")

    # tranche 자동
    if scale_tranche is None:
        if entry_action == "SCALE_IN":
            if bottoming.get("stabilization_score", 0) >= 20 and bottoming.get("score", 0) >= 60:
                scale_tranche = 2
            else:
                scale_tranche = 1
        elif entry_action == "BUY":
            scale_tranche = 4
        else:
            scale_tranche = 0

    tranche_map = {
        0: {"label": None, "target_position_pct": 0},
        1: {"label": "SCALE_IN_1", "target_position_pct": 15},  # 10–20 mid
        2: {"label": "SCALE_IN_2", "target_position_pct": 22},  # 20–25
        3: {"label": "SCALE_IN_3", "target_position_pct": 25},  # 20–30
        4: {"label": "FULL", "target_position_pct": 100},
    }
    tr = tranche_map.get(scale_tranche, tranche_map[0])

    if entry_action == "EXIT":
        holder, new_i, trader = "EXIT", "AVOID", "WAIT"
    elif entry_action == "REDUCE":
        holder, new_i, trader = "REDUCE", "WAIT", "WAIT"
    elif entry_action == "SCALE_IN":
        holder = "HOLD"
        new_i = "SCALE_IN"
        trader = "SCALE_IN"
    elif entry_action == "BUY":
        holder, new_i, trader = "HOLD", "BUY", "BUY"
    else:  # WAIT
        holder = "HOLD" if bull else "HOLD"
        new_i = "WAIT"
        trader = "NO_CHASE" if chase_blocked or bull else "WAIT"

    if chase_blocked:
        new_i = "WAIT"
        trader = "NO_CHASE"
        if entry_action == "BUY":
            holder = "HOLD"

    return {
        "holder": holder,
        "new_investor": new_i,
        "trader": trader,
        "scale_in": {
            "tranche": scale_tranche,
            "label": tr["label"],
            "target_position_pct": tr["target_position_pct"],
            "sizing_basis": "target_position",
            "note": "목표 포지션 대비 %. 포트폴리오 전체 비중 아님.",
        },
    }


def map_entry_to_legacy_signal(
    *,
    entry_action: str,
    trend_label: str,
    softened_sell: bool,
    gates: dict,
) -> str:
    """WATCH_* 호환 매핑."""
    bull = trend_label in ("STRONG_BULLISH", "BULLISH")
    bear = trend_label in ("STRONG_BEARISH", "BEARISH")

    if entry_action == "BUY":
        return "BUY"
    if entry_action == "EXIT":
        return "SELL"
    if entry_action == "REDUCE":
        return "WATCH_DOWN" if softened_sell or not bear else "WATCH_DOWN"
    if entry_action == "SCALE_IN":
        return "WATCH_UP"
    # WAIT
    if not gates.get("risk", True):
        return "WATCH_RISK"
    if bull:
        return "WATCH_UP"
    if bear:
        return "WATCH_DOWN"
    return "WATCH_FLAT"


def rvol_interpretation(rvol: Optional[float]) -> str:
    if rvol is None:
        return "RVOL 데이터 없음"
    if rvol < 0.8:
        return "Low participation (거래량 확인 부족 — 기관 이탈로 단정 금지)"
    if rvol < 1.0:
        return "Normal-low participation (거래량 확인 부족)"
    if rvol <= 1.5:
        return "Confirmed participation"
    return "Strong participation"


def enrich_v4(
    features: dict,
    *,
    trend_label: str,
    trend_score_100: float,
    prev_market_state: str | None = None,
    fundamental_view: str | None = None,
) -> dict:
    """decide_signal에서 호출하는 통합 팩."""
    bottoming = compute_bottoming_score(features)
    entry = compute_entry_score_v4(features)
    chase = compute_chase_guard(features)
    momentum = compute_momentum_state(features)
    valuation = compute_valuation_state(features)
    extension = compute_extension_state(features)
    fb = detect_false_breakdown(features)
    hard = detect_hard_sell_overrides(features, bottoming)
    mstate = classify_market_state(
        trend_label=trend_label,
        bottoming=bottoming,
        extension=extension,
        momentum=momentum,
        entry_score=entry["score"],
        prev_state=prev_market_state,
    )
    decided = decide_entry_action(
        trend_label=trend_label,
        bottoming=bottoming,
        entry_score=entry["score"],
        chase_blocked=chase["chase_blocked"],
        hard_override=hard,
        extension=extension,
        false_breakdown=fb,
    )
    positions = build_position_actions(
        entry_action=decided["entry_action"],
        chase_blocked=chase["chase_blocked"],
        bottoming=bottoming,
        trend_label=trend_label,
    )
    fund = (fundamental_view or "NEUTRAL").upper()
    if fund not in ("BULLISH", "BEARISH", "NEUTRAL"):
        fund = "NEUTRAL"

    return {
        "fundamental_view": fund,
        "trend": trend_label,
        "momentum": momentum,
        "bottoming": bottoming["view"],
        "bottoming_score": bottoming["score"],
        "bottoming_label": bottoming["label"],
        "oversold_score": bottoming["oversold_score"],
        "stabilization_score": bottoming["stabilization_score"],
        "bottoming_detail": bottoming,
        "valuation": valuation,
        "extension_state": extension,
        "entry_score": entry["score"],
        "entry_score_band": entry["band"],
        "entry_score_reasons": entry["reasons"],
        "chase_blocked": chase["chase_blocked"],
        "chase_reasons": chase["reasons"],
        "entry_action": decided["entry_action"],
        "entry_decision_note": decided["note"],
        "trend_score": round(trend_score_100, 2),
        "market_state": mstate["state"],
        "market_state_meta": mstate,
        "position_actions": positions,
        "false_breakdown": fb,
        "hard_sell_override": hard,
        "rvol_note": rvol_interpretation(_rvol(features)),
    }
