"""
Signal Engine v4.

Trend ≠ Entry ≠ Bottoming.
Bottoming = Oversold(cap) + Stabilization — 과매도 ≠ 바닥 확정.
Valuation(멀티플) ≠ Extension(기술 과열).
Chase guard는 entry_score를 수정하지 않음.
SELL softener는 Bottoming>=60 단독 적용 금지.

LLM은 설명층. 최종 signal / actions 는 엔진이 결정한다.
"""
from __future__ import annotations

from typing import Any, Optional

from signal_v4 import (
    enrich_v4,
    map_entry_to_legacy_signal,
    soften_sell_candidate,
)

ENGINE_VERSION = "signal_engine_v4"

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
        "entry_min": float(thr.get("entry_min", 8)),
        "paper_buy_min": float(thr.get("paper_buy_min", 55)),
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


def _rvol(features: dict) -> Optional[float]:
    """Relative volume = Volume / 20D average. 절대 거래량 미사용."""
    v = features.get("volume") or {}
    r = v.get("rvol")
    if r is None:
        r = v.get("volume_ratio")
    try:
        return float(r) if r is not None else None
    except (TypeError, ValueError):
        return None


def normalize_signal(signal: str | None) -> str:
    s = (signal or "").strip().upper().replace(" ", "_").replace("-", "_")
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
    """
    BUY 차단 게이트.
    - 절대 거래량 threshold 금지 → RVOL만 참고(데이터 유무)
    - RSI/BB 과확장 = entry_blocked (SELL 아님)
    - MACD 단독으로 SELL 금지; BUY 시 약한 모멘텀만 완화 차단
    """
    t = features.get("trend") or {}
    m = features.get("momentum") or {}
    vol = features.get("volatility") or {}
    regime = _g(features, "market_regime", "regime")

    price_vs_ma20 = t.get("price_vs_ma20")
    atr_pct = vol.get("atr_pct")
    bb_pos = vol.get("bb_position")
    rsi = m.get("rsi")

    trend_ok = price_vs_ma20 is None or price_vs_ma20 >= -0.05

    macd = m.get("macd")
    macd_sig = m.get("macd_signal")
    momentum_ok = True
    if macd is not None and macd_sig is not None:
        momentum_ok = macd >= (macd_sig - max(abs(macd_sig) * 0.5, 0.3))

    # 이벤트성 위험만 risk 차단 (과확장은 entry로)
    risk_ok = True
    if atr_pct is not None and atr_pct > 0.12:
        risk_ok = False
    gap = vol.get("gap_risk")
    if gap is not None and abs(gap) > 0.06:
        risk_ok = False

    entry_ok = True
    entry_reasons = []
    if bb_pos is not None and bb_pos > 1.0:
        entry_ok = False
        entry_reasons.append("bb_overextended")
    if rsi is not None and rsi > 72:
        entry_ok = False
        entry_reasons.append("rsi_overbought_entry")
    if price_vs_ma20 is not None and price_vs_ma20 > 0.10:
        entry_ok = False
        entry_reasons.append("extended_vs_ma20")

    # 유동성: 절대 거래량 X. RVOL 데이터 존재 여부만 soft check
    rvol = _rvol(features)
    liquidity_ok = True  # 절대 거래량 게이트 제거

    bear_block = regime != "BEAR"

    buy_allowed = bool(
        trend_ok and momentum_ok and risk_ok and liquidity_ok and bear_block and entry_ok
    )

    return {
        "trend": bool(trend_ok),
        "momentum": bool(momentum_ok),
        "risk": bool(risk_ok),
        "liquidity": bool(liquidity_ok),
        "entry": bool(entry_ok),
        "regime": bool(bear_block),
        "buy_allowed": buy_allowed,
        "entry_block_reasons": entry_reasons,
        "rvol": rvol,
    }


def _trend_score(features: dict, reasons: list[str]) -> float:
    """순수 추세 — 과확장 페널티 없음."""
    t = features.get("trend") or {}
    rs = features.get("relative_strength") or {}
    regime = _g(features, "market_regime", "regime") or "UNKNOWN"
    pts = 0.0

    if t.get("above_ma20"):
        pts += 14
        reasons.append("above_ma20")
    else:
        pts -= 16
        reasons.append("below_ma20")
    if t.get("above_ma60"):
        pts += 10
        reasons.append("above_ma60")
    else:
        pts -= 8
    if t.get("above_ma200"):
        pts += 8
        reasons.append("above_ma200")
    else:
        pts -= 4

    m20_60 = t.get("ma20_vs_ma60")
    if m20_60 is not None:
        if m20_60 > 0:
            pts += 6
        else:
            pts -= 6

    for key, w in (("vs_spy_20d", 8), ("vs_qqq_20d", 4), ("vs_sector_20d", 4)):
        val = rs.get(key)
        if val is None:
            continue
        if val > 0.03:
            pts += w
            reasons.append(f"{key}_strong")
        elif val > 0:
            pts += w * 0.35
        elif val < -0.03:
            pts -= w
            reasons.append(f"{key}_weak")
        else:
            pts -= w * 0.3

    if regime == "BULL":
        pts += 8
        reasons.append("regime_bull")
    elif regime == "BEAR":
        pts -= 14
        reasons.append("regime_bear")
    elif regime == "SIDEWAYS":
        pts -= 2

    vp = features.get("volume_profile") or {}
    if vp.get("ok"):
        flags = vp.get("flags") or {}
        if flags.get("breakout_hold"):
            pts += 6
            reasons.append("vp_breakout_hold")
        if flags.get("breakdown_hold"):
            pts -= 10
            reasons.append("vp_breakdown_hold")

    return _clip(pts)


def _momentum_score(features: dict, reasons: list[str]) -> float:
    """모멘텀 방향. RSI 과매수는 약한 경고만 (SELL 아님)."""
    m = features.get("momentum") or {}
    pts = 0.0
    if m.get("macd_above_signal"):
        pts += 14
        reasons.append("macd_golden")
    else:
        pts -= 12
        reasons.append("macd_dead")
    if m.get("macd_above_zero"):
        pts += 6
    else:
        pts -= 4
    if m.get("macd_hist_rising"):
        pts += 6
        reasons.append("hist_rising")

    rsi = m.get("rsi")
    if rsi is not None:
        if 45 <= rsi <= 65:
            pts += 8
            reasons.append("rsi_momentum_ok")
        elif 65 < rsi <= 75:
            pts += 2  # 강한 모멘텀일 수 있음 — 과매수는 entry에서 처리
            reasons.append("rsi_hot")
        elif rsi > 75:
            pts -= 2
            reasons.append("rsi_very_hot")
        elif rsi < 35:
            pts -= 10
            reasons.append("rsi_weak")
        elif rsi < 45:
            pts -= 4
    return _clip(pts)


def _participation_score(features: dict, reasons: list[str]) -> float:
    """RVOL 중심 참여도. 절대 거래량 미사용."""
    pts = 0.0
    rvol = _rvol(features)
    if rvol is not None:
        if rvol >= 1.5:
            pts += 18
            reasons.append("rvol_strong")
        elif rvol >= 1.2:
            pts += 12
            reasons.append("rvol_confirm")
        elif rvol >= 0.9:
            pts += 4
        elif rvol < 0.6:
            pts -= 12
            reasons.append("rvol_dry")
        else:
            pts -= 4
    else:
        reasons.append("rvol_missing")

    vp = features.get("volume_profile") or {}
    if vp.get("ok"):
        flags = vp.get("flags") or {}
        if flags.get("breakout_hold") and rvol is not None and rvol >= 1.0:
            pts += 6
        if flags.get("near_support"):
            pts += 2
        if flags.get("near_resistance"):
            pts -= 2
    return _clip(pts)


def _entry_quality_score(features: dict, reasons: list[str]) -> float:
    """지금 가격에서 진입해도 되는지. 과확장·고벨류 = 낮은 점수."""
    t = features.get("trend") or {}
    m = features.get("momentum") or {}
    vol = features.get("volatility") or {}
    val = features.get("valuation") or {}
    pts = 10.0  # baseline

    pvm = t.get("price_vs_ma20")
    if pvm is not None:
        if 0 <= pvm <= 0.04:
            pts += 14
            reasons.append("pullback_to_ma20")
        elif 0.04 < pvm <= 0.08:
            pts += 4
        elif pvm > 0.10:
            pts -= 22
            reasons.append("entry_extended")
        elif -0.03 <= pvm < 0:
            pts += 8
            reasons.append("near_ma20_support")
        elif pvm < -0.05:
            pts -= 8

    bbp = vol.get("bb_position")
    if bbp is not None:
        if bbp > 1.0:
            pts -= 20
            reasons.append("bb_overextended")
        elif bbp > 0.85:
            pts -= 10
            reasons.append("bb_upper_caution")
        elif 0.35 <= bbp <= 0.65:
            pts += 8
            reasons.append("bb_mid_ok")
        elif bbp < 0.2:
            pts += 4  # 하단 근처 — 진입 타이밍 후보 (추세와 별개)

    rsi = m.get("rsi")
    if rsi is not None:
        if rsi > 72:
            pts -= 18
            reasons.append("rsi_overbought_entry")
        elif rsi > 65:
            pts -= 8
            reasons.append("rsi_elevated_entry")
        elif 40 <= rsi <= 58:
            pts += 8

    # Valuation = entry risk only (never SELL alone)
    peg = val.get("peg")
    trailing = val.get("trailing_pe")
    if peg is not None and peg > 3.5:
        pts -= 6
        reasons.append("peg_rich_entry")
    if trailing is not None and trailing > 60:
        pts -= 4
        reasons.append("pe_rich_entry")

    vp = features.get("volume_profile") or {}
    if vp.get("ok"):
        flags = vp.get("flags") or {}
        if flags.get("near_support") and not flags.get("near_resistance"):
            pts += 10
            reasons.append("vp_near_support")
        elif flags.get("near_resistance") and not flags.get("near_support"):
            pts -= 12
            reasons.append("vp_near_resistance")
        elif flags.get("at_hvn"):
            pts -= 4
            reasons.append("vp_at_hvn")

    gap = vol.get("gap_risk")
    if gap is not None and abs(gap) > 0.04:
        pts -= 8
        reasons.append("gap_entry_risk")

    return _clip(pts)


def _risk_reward_score(features: dict, reasons: list[str]) -> float:
    """ATR·매물대 기반 대략적 R:R. 높을수록 진입 유리."""
    vol = features.get("volatility") or {}
    price = _g(features, "price", "close")
    atr = vol.get("atr")
    atr_pct = vol.get("atr_pct")
    pts = 0.0

    if atr_pct is not None:
        if atr_pct > 0.09:
            pts -= 14
            reasons.append("rr_high_atr")
        elif atr_pct > 0.06:
            pts -= 6
        elif atr_pct < 0.03:
            pts += 4

    vp = features.get("volume_profile") or {}
    if vp.get("ok") and price:
        ns = vp.get("nearest_support") or {}
        nr = vp.get("nearest_resistance") or {}
        try:
            risk = None
            reward = None
            if ns.get("price") is not None:
                risk = abs(float(price) - float(ns["price"]))
            if nr.get("price") is not None:
                reward = abs(float(nr["price"]) - float(price))
            if risk and risk > 0 and reward is not None:
                rr = reward / risk
                if rr >= 2.0:
                    pts += 16
                    reasons.append("rr_ge_2")
                elif rr >= 1.3:
                    pts += 8
                    reasons.append("rr_ok")
                elif rr < 0.8:
                    pts -= 12
                    reasons.append("rr_poor")
        except (TypeError, ValueError):
            pass
    elif atr and price:
        # fallback: reward ~1.5 ATR upside vs 1 ATR stop
        pts += 2

    return _clip(pts)


def compute_score(features: dict) -> dict:
    """
    분리 점수 + 합성 score.
    - trend/momentum/participation: 방향·참여
    - entry_quality / risk_reward: 지금 살 만한가
    과확장은 entry에만 반영 → 강세+과확장 = 높은 trend + 낮은 entry.
    """
    reasons: list[str] = []
    trend = _trend_score(features, reasons)
    momentum = _momentum_score(features, reasons)
    participation = _participation_score(features, reasons)
    entry_quality = _entry_quality_score(features, reasons)
    risk_reward = _risk_reward_score(features, reasons)

    # 합성: 추세 비중 유지하되 entry가 나쁘면 BUY 후보 score 억제
    directional = 0.40 * trend + 0.25 * momentum + 0.15 * participation
    entry_blend = 0.55 * entry_quality + 0.45 * risk_reward
    # BUY 적합성 점수 (캘리브레이션·threshold용)
    score = _clip(0.55 * directional + 0.45 * entry_blend)

    # 순수 추세 라벨용 (과확장과 무관)
    trend_label = _trend_label(trend)
    entry_stance = _entry_stance(entry_quality, risk_reward, features)

    parts = {
        "trend": round(trend, 2),
        "momentum": round(momentum, 2),
        "participation": round(participation, 2),
        "entry_quality": round(entry_quality, 2),
        "risk_reward": round(risk_reward, 2),
        # 하위호환 키
        "volume": round(participation, 2),
        "relative_strength": round(0.0, 2),
        "regime": round(0.0, 2),
        "risk": round(min(0.0, entry_quality) * 0.3, 2),
        "volume_profile": round(0.0, 2),
    }

    return {
        "score": round(score, 2),
        "scores": {
            "trend": round(trend, 2),
            "momentum": round(momentum, 2),
            "participation": round(participation, 2),
            "entry_quality": round(entry_quality, 2),
            "risk_reward": round(risk_reward, 2),
            "directional": round(directional, 2),
            "entry_blend": round(entry_blend, 2),
        },
        "trend_label": trend_label,
        "entry_stance": entry_stance,
        "components": parts,
        "reason_codes": reasons[:16],
    }


def _trend_label(trend: float) -> str:
    if trend >= 42:
        return "STRONG_BULLISH"
    if trend >= 16:
        return "BULLISH"
    if trend <= -42:
        return "STRONG_BEARISH"
    if trend <= -16:
        return "BEARISH"
    return "NEUTRAL"


def _entry_stance(entry_q: float, rr: float, features: dict) -> str:
    gates = hard_gates(features)
    if not gates.get("entry", True) or entry_q <= -20:
        return "ENTRY_AVOID"
    if entry_q >= 12 and rr >= -5 and gates.get("buy_allowed"):
        return "ENTRY_READY"
    if entry_q >= 8 and rr >= 0:
        return "ENTRY_READY"
    return "ENTRY_WAIT"


def classify_watch(score: float, features: dict, gates: dict, *,
                   watch_up_min: float = WATCH_UP_MIN,
                   watch_down_max: float = WATCH_DOWN_MAX,
                   trend_score: float | None = None) -> str:
    atrp = _g(features, "volatility", "atr_pct")
    gap = _g(features, "volatility", "gap_risk")
    if (atrp is not None and atrp > 0.10) or (gap is not None and abs(gap) > 0.05):
        return "WATCH_RISK"
    if not gates.get("risk", True):
        return "WATCH_RISK"
    # 방향은 합성 score보다 trend를 우선
    lean = trend_score if trend_score is not None else score
    if lean >= watch_up_min or score >= watch_up_min:
        return "WATCH_UP"
    if lean <= watch_down_max or score <= watch_down_max:
        return "WATCH_DOWN"
    return "WATCH_FLAT"


def _trend_breakdown(features: dict, scores: dict) -> bool:
    t = features.get("trend") or {}
    vp = features.get("volume_profile") or {}
    flags = (vp.get("flags") or {}) if vp.get("ok") else {}
    pvm = t.get("price_vs_ma20")
    below = t.get("above_ma20") is False
    deep = pvm is not None and pvm < -0.03
    vp_break = bool(flags.get("breakdown_hold"))
    trend_weak = (scores.get("trend") or 0) <= -20
    return bool((below and deep) or vp_break or (below and trend_weak))


def build_triggers(features: dict, scored: dict, signal: str) -> dict:
    """모든 non-BUY에 BUY/DOWN Trigger + Invalidation 생성."""
    price = _g(features, "price", "close")
    t = features.get("trend") or {}
    vol = features.get("volatility") or {}
    ma20 = t.get("ma20")
    atr = vol.get("atr")
    rvol = _rvol(features)
    vp = features.get("volume_profile") or {}
    ns = (vp.get("nearest_support") or {}) if vp.get("ok") else {}
    nr = (vp.get("nearest_resistance") or {}) if vp.get("ok") else {}

    def money(x):
        try:
            return f"${float(x):.2f}"
        except (TypeError, ValueError):
            return None

    pullback = None
    if ma20 is not None:
        pullback = money(ma20)
    elif ns.get("price") is not None:
        pullback = money(ns["price"])

    resist = money(nr["price"]) if nr.get("price") is not None else None
    support = money(ns["price"]) if ns.get("price") is not None else pullback

    inv_level = None
    if ma20 is not None and atr is not None:
        inv_level = money(float(ma20) - float(atr))
    elif support:
        inv_level = support

    rvol_txt = f"RVOL≥1.0" if rvol is not None else "거래량 평균 이상"
    buy_trigger = (
        f"{pullback or 'MA20/지지'} 종가 회복 + {rvol_txt}"
        + (f" (저항 {resist} 돌파 시 가속)" if resist else "")
    )
    down_trigger = (
        f"{support or '최근 지지'} 종가 이탈"
        + (f" 또는 MA20 하회 지속" if ma20 is not None else "")
    )
    invalidation = (
        f"종가 < {inv_level}" if inv_level else "주요 지지·MA20 붕괴"
    ) + " → 상승 시나리오 무효"

    stance = scored.get("entry_stance") or "ENTRY_WAIT"
    label = scored.get("trend_label") or "NEUTRAL"
    bias = {
        "STRONG_BULLISH": "상승편향",
        "BULLISH": "상승편향",
        "STRONG_BEARISH": "하락편향",
        "BEARISH": "하락편향",
    }.get(label, "중립")

    return {
        "buy_trigger": buy_trigger,
        "down_trigger": down_trigger,
        "invalidation": invalidation,
        "bias": bias,
        "duration": "3~10거래일 내 트리거 확인",
        "entry_stance": stance,
        "trend_label": label,
        "note": (
            f"{label} + {stance}. "
            "추세와 진입 타이밍을 분리해 판단."
        ),
    }


def build_actions(
    signal: str,
    scored: dict,
    features: dict,
    *,
    overextended: bool,
    entry_action: str | None = None,
    position_actions: dict | None = None,
    chase_blocked: bool = False,
) -> dict:
    """투자자 행동별 Action (v4 entry_action 우선)."""
    label = scored.get("trend_label") or "NEUTRAL"
    stance = scored.get("entry_stance") or "ENTRY_WAIT"
    bullish = label in ("STRONG_BULLISH", "BULLISH")
    bearish = label in ("STRONG_BEARISH", "BEARISH")
    breakdown = _trend_breakdown(features, scored.get("scores") or {})
    pos = position_actions or {}

    ea = (entry_action or "").upper()
    if ea == "BUY":
        entry = "BUY"
    elif ea == "SCALE_IN":
        entry = "SCALE_IN"
    elif ea in ("EXIT", "REDUCE") or signal in ("SELL", "AVOID") or stance == "ENTRY_AVOID":
        entry = "AVOID" if ea == "EXIT" or signal in ("SELL", "AVOID") else "WAIT"
        if ea == "REDUCE":
            entry = "WAIT"
    elif chase_blocked or signal == "WATCH_RISK":
        entry = "AVOID" if signal == "WATCH_RISK" else "WAIT"
    else:
        entry = "WAIT"

    # HOLDING
    if ea == "EXIT" or signal == "SELL" or breakdown:
        holding = "EXIT"
    elif ea == "REDUCE" or (overextended and bullish):
        holding = "REDUCE"
    elif pos.get("holder") in ("HOLD", "REDUCE", "EXIT"):
        holding = pos["holder"]
    elif bearish and (scored.get("scores") or {}).get("momentum", 0) < 0:
        holding = "REDUCE"
    else:
        holding = "HOLD"

    # TRADING
    if ea == "BUY" and not chase_blocked:
        trading = "BUY"
    elif ea == "SCALE_IN" and not chase_blocked:
        trading = "SCALE_IN"
    elif chase_blocked or overextended and bullish:
        trading = "NO_CHASE"
    elif signal == "SELL" or ea == "EXIT":
        trading = "WAIT"
    else:
        trading = "WAIT"

    return {
        "entry": entry,
        "holding": holding,
        "trading": trading,
    }


def decide_signal(
    features: dict,
    *,
    thr_override: dict | None = None,
    config: dict | None = None,
    prev_market_state: str | None = None,
    fundamental_view: str | None = None,
) -> dict:
    """최종 시그널 + v4 Bottoming/Entry/Extension + Actions + Triggers."""
    import math
    from signal_calibration import calibrated_p_up, calibrated_confidence

    if config is None and thr_override is None:
        config = _load_config()
    elif config is None:
        config = _load_config() or {}

    thr = dict(get_thresholds(config))
    if thr_override:
        thr.update(thr_override)

    buy_min = thr["buy_min"]
    sell_max = thr["sell_max"]
    watch_up_min = thr["watch_up_min"]
    watch_down_max = thr["watch_down_max"]
    entry_min = thr.get("entry_min", 8)

    deploy = (config or {}).get("deploy_flags") or {}
    buy_enabled = deploy.get("buy_enabled", True)
    sell_enabled = deploy.get("sell_enabled", True)

    gates = hard_gates(features)
    scored = compute_score(features)
    score = scored["score"]
    scores = scored["scores"]
    trend_s = scores["trend"]
    entry_s = scores["entry_quality"]
    label = scored["trend_label"]
    stance = scored["entry_stance"]

    # trend_score -100..100 → 0..100 display
    trend_score_100 = _clip((trend_s + 100) / 2.0, 0, 100)

    v4 = enrich_v4(
        features,
        trend_label=label,
        trend_score_100=trend_score_100,
        prev_market_state=prev_market_state,
        fundamental_view=fundamental_view,
    )

    overextended = (
        not gates.get("entry", True)
        or entry_s < -10
        or (_g(features, "volatility", "bb_position") or 0) > 0.9
        or v4.get("extension_state") in ("EXTENDED", "OVEREXTENDED")
    )

    p_up = calibrated_p_up(score, config)
    breakdown = _trend_breakdown(features, scores)
    p_down_raw = 1 / (1 + math.exp(score / 25))
    if breakdown:
        p_down_raw = max(p_down_raw, 0.45)
    if trend_s > 25 and not breakdown:
        p_down_raw *= 0.75
    p_flat = max(0.08, 1.0 - abs(p_up - p_down_raw) * 0.85)
    ssum = p_up + p_down_raw + p_flat
    prob = {
        "up": round(p_up / ssum, 3),
        "flat": round(p_flat / ssum, 3),
        "down": round(p_down_raw / ssum, 3),
    }
    downside_advantage = prob["down"] >= prob["up"] + 0.05

    sell_candidate = bool(
        sell_enabled
        and breakdown
        and downside_advantage
        and (trend_s <= sell_max or score <= sell_max or trend_s <= -25)
    )
    soft = soften_sell_candidate(
        sell_candidate=sell_candidate,
        trend_label=label,
        bottoming=v4["bottoming_detail"],
        hard_override=v4["hard_sell_override"],
    )
    sell_candidate = soft["sell_candidate"]
    softened_sell = soft["softened"]
    if softened_sell:
        scored["reason_codes"] = list(scored["reason_codes"]) + ["sell_softened_bottoming"]

    # v4 entry_action 우선 — legacy BUY는 entry_action==BUY 이고 deploy/gate 허용일 때만
    entry_action = v4["entry_action"]
    buy_candidate = bool(
        buy_enabled
        and entry_action == "BUY"
        and not v4["chase_blocked"]
        and score >= buy_min
        and entry_s >= entry_min
        and gates["buy_allowed"]
        and label in ("STRONG_BULLISH", "BULLISH", "NEUTRAL")
        and scores["directional"] >= 10
    )

    if buy_candidate:
        signal = "BUY"
        entry_action = "BUY"
    elif sell_candidate and entry_action == "EXIT":
        signal = "SELL"
    elif sell_candidate and not softened_sell and entry_action in ("EXIT", "REDUCE"):
        signal = "SELL" if entry_action == "EXIT" else map_entry_to_legacy_signal(
            entry_action=entry_action,
            trend_label=label,
            softened_sell=softened_sell,
            gates=gates,
        )
    else:
        # v4 action → legacy watch 매핑 (엔진 권한)
        signal = map_entry_to_legacy_signal(
            entry_action=entry_action,
            trend_label=label,
            softened_sell=softened_sell,
            gates=gates,
        )
        if label in ("STRONG_BULLISH", "BULLISH") and entry_action == "WAIT":
            if signal not in ("WATCH_RISK", "WATCH_DOWN"):
                signal = "WATCH_UP"
            scored["reason_codes"] = list(scored["reason_codes"]) + ["trend_ok_entry_wait"]
        if not buy_enabled and entry_action == "BUY":
            signal = "WATCH_UP"
            entry_action = "WAIT"
            scored["reason_codes"] = list(scored["reason_codes"]) + ["buy_deploy_disabled"]
            v4 = {**v4, "entry_action": "WAIT"}
            v4["position_actions"] = {
                **v4["position_actions"],
                "new_investor": "WAIT",
                "trader": "NO_CHASE" if v4["chase_blocked"] else "WAIT",
            }

    # chase: score 불변, action만 WAIT
    if v4["chase_blocked"] and entry_action in ("BUY", "SCALE_IN"):
        entry_action = "WAIT"
        v4 = {**v4, "entry_action": "WAIT", "entry_decision_note": "chase_blocked"}
        signal = map_entry_to_legacy_signal(
            entry_action="WAIT",
            trend_label=label,
            softened_sell=softened_sell,
            gates=gates,
        )
        scored["reason_codes"] = list(scored["reason_codes"]) + ["chase_blocked"]

    scores = {
        **scores,
        "bottoming": v4["bottoming_score"],
        "oversold": v4["oversold_score"],
        "stabilization": v4["stabilization_score"],
        "entry_score": v4["entry_score"],
        "trend_score_100": v4["trend_score"],
    }

    conf = calibrated_confidence(score, signal, config)

    emp = ((config or {}).get("empirical") or {}).get(signal) or {}
    expected_return = emp.get("expected_return") or {}
    expected_dd = emp.get("expected_drawdown") or {}

    # entry_stance를 v4와 정합
    if entry_action == "BUY":
        stance = "ENTRY_READY"
    elif entry_action == "SCALE_IN":
        stance = "ENTRY_READY"
    elif entry_action in ("EXIT",) or v4["chase_blocked"]:
        stance = "ENTRY_AVOID" if entry_action == "EXIT" else "ENTRY_WAIT"
    else:
        stance = "ENTRY_WAIT"
    scored["entry_stance"] = stance

    triggers = build_triggers(features, scored, signal)
    # Thesis invalidation vs risk control 분리
    triggers["thesis_invalidation"] = triggers.get("invalidation")
    triggers["risk_control"] = (
        "보유자 손절·비중은 thesis invalidation과 별도. "
        "ATR/지지 기준 개인 리스크 한도 적용."
    )

    actions = build_actions(
        signal, scored, features,
        overextended=overextended,
        entry_action=entry_action,
        position_actions=v4.get("position_actions"),
        chase_blocked=bool(v4.get("chase_blocked")),
    )

    rs = features.get("relative_strength") or {}
    regime = _g(features, "market_regime", "regime")

    return {
        "signal": signal,
        "signal_strength": int(_clip(abs(score), 0, 100)),
        "score": score,
        "scores": scores,
        "trend_label": label,
        "entry_stance": stance,
        "actions": actions,
        "triggers": triggers,
        "confidence": conf,
        "confidence_calibrated": bool(
            config and (config.get("calibration") or {}).get("confidence_bins")
        ),
        "probability": prob,
        "probability_source": "calibration" if config else "heuristic",
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
        "rvol": _rvol(features),
        "rvol_note": v4.get("rvol_note"),
        "components": scored["components"],
        "reason_codes": scored["reason_codes"],
        "gate_status": gates,
        "engine_version": ENGINE_VERSION,
        "thresholds": thr,
        "deploy_flags": {
            "buy_enabled": bool(buy_enabled),
            "sell_enabled": bool(sell_enabled),
        },
        # ── v4 fields
        "fundamental_view": v4["fundamental_view"],
        "trend": v4["trend"],
        "momentum": v4["momentum"],
        "bottoming": v4["bottoming"],
        "bottoming_score": v4["bottoming_score"],
        "bottoming_label": v4["bottoming_label"],
        "oversold_score": v4["oversold_score"],
        "stabilization_score": v4["stabilization_score"],
        "valuation": v4["valuation"],
        "extension_state": v4["extension_state"],
        "entry_action": entry_action,
        "entry_score": v4["entry_score"],
        "entry_score_band": v4["entry_score_band"],
        "chase_blocked": v4["chase_blocked"],
        "chase_reasons": v4["chase_reasons"],
        "trend_score": v4["trend_score"],
        "market_state": v4["market_state"],
        "market_state_meta": v4["market_state_meta"],
        "position_actions": v4["position_actions"],
        "false_breakdown": v4["false_breakdown"],
        "hard_sell_override": v4["hard_sell_override"],
        "sell_softened": softened_sell,
        "entry_decision_note": v4.get("entry_decision_note"),
    }


def refine_llm_watch(llm_signal: str, features: dict) -> str:
    """구버전 호환: LLM이 WATCH만 낸 경우 feature로 세분화."""
    s = (llm_signal or "").upper()
    if s.startswith("WATCH_") and s in SIGNALS:
        return s
    if s in ("BUY", "SELL", "AVOID"):
        return s
    scored = compute_score(features)
    return classify_watch(
        scored["score"], features, hard_gates(features),
        trend_score=scored["scores"]["trend"],
    )


def infer_watch_from_indicators(
    indicators: dict | None,
    *,
    signal_engine: dict | None = None,
    analysis: str = "",
    change_pct: float | None = None,
) -> str:
    """구형 SIGNAL:WATCH / 히스토리 표시용."""
    eng = signal_engine or {}
    eng_sig = normalize_signal(eng.get("signal")) if eng.get("signal") else None
    if eng_sig and eng_sig.startswith("WATCH_"):
        return eng_sig
    if eng.get("trend_label") in ("STRONG_BULLISH", "BULLISH"):
        return "WATCH_UP"
    if eng.get("trend_label") in ("STRONG_BEARISH", "BEARISH"):
        return "WATCH_DOWN"
    score = eng.get("score")
    if score is not None:
        try:
            return classify_watch(float(score), {}, {"risk": True, "liquidity": True})
        except (TypeError, ValueError):
            pass

    text = (analysis or "").upper()
    if "WATCH_RISK" in text or "WATCH RISK" in text:
        return "WATCH_RISK"
    if "WATCH_UP" in text:
        return "WATCH_UP"
    if "WATCH_DOWN" in text:
        return "WATCH_DOWN"
    if "WATCH_FLAT" in text:
        return "WATCH_FLAT"

    raw = analysis or ""
    if any(k in raw for k in ("WATCH_BIAS", "하락 편향", "약세 편향", "하방")):
        if any(k in raw for k in ("상승", "강세")) and not any(
            k in raw for k in ("하락", "약세", "하방")
        ):
            return "WATCH_UP"
        if any(k in raw for k in ("하락", "약세", "하방")):
            return "WATCH_DOWN"
    if "상승 편향" in raw or "강세 편향" in raw:
        return "WATCH_UP"

    ind = indicators or {}
    lean = 0
    rsi = ind.get("rsi")
    try:
        rsi = float(rsi) if rsi is not None else None
    except (TypeError, ValueError):
        rsi = None
    if rsi is not None:
        if rsi >= 58:
            lean += 2
        elif rsi >= 52:
            lean += 1
        elif rsi <= 42:
            lean -= 2
        elif rsi <= 48:
            lean -= 1

    macd = ind.get("macd")
    macd_sig = ind.get("macd_signal")
    try:
        if macd is not None and macd_sig is not None:
            if float(macd) > float(macd_sig):
                lean += 1
            else:
                lean -= 1
    except (TypeError, ValueError):
        pass

    try:
        if change_pct is not None:
            cp = float(change_pct)
            if cp >= 1.5:
                lean += 1
            elif cp <= -1.5:
                lean -= 1
    except (TypeError, ValueError):
        pass

    if lean >= 2:
        return "WATCH_UP"
    if lean <= -2:
        return "WATCH_DOWN"
    if lean > 0:
        return "WATCH_UP"
    if lean < 0:
        return "WATCH_DOWN"
    return "WATCH_FLAT"


def resolve_display_signal(doc: dict | None) -> str:
    """분석 문서 → UI용 최종 시그널 (구형 WATCH를 방향 세분화)."""
    if not doc:
        return "WATCH_FLAT"
    raw_orig = (doc.get("signal") or "").strip().upper().replace(" ", "_")
    raw = normalize_signal(doc.get("signal"))
    if raw in ("BUY", "SELL", "AVOID"):
        return raw
    if raw_orig in ("WATCH_UP", "WATCH_FLAT", "WATCH_DOWN", "WATCH_RISK"):
        return raw_orig
    return infer_watch_from_indicators(
        doc.get("indicators"),
        signal_engine=doc.get("signal_engine") or {},
        analysis=doc.get("analysis") or "",
        change_pct=doc.get("change_pct"),
    )


def format_engine_for_prompt(meta: dict | None) -> str:
    """LLM 설명층용 — 엔진 객관 결과 (덮어쓰기 금지)."""
    if not meta:
        return "엔진 결과 없음"
    scores = meta.get("scores") or {}
    actions = meta.get("actions") or {}
    trig = meta.get("triggers") or {}
    prob = meta.get("probability") or {}
    er = meta.get("expected_return") or {}
    ed = meta.get("expected_drawdown") or {}
    pos = meta.get("position_actions") or {}
    scale = pos.get("scale_in") or {}
    lines = [
        "=== Signal Engine v4 (덮어쓰기 금지) ===",
        f"- Fundamental: {meta.get('fundamental_view', 'NEUTRAL')}",
        f"- Trend: {meta.get('trend') or meta.get('trend_label')} (trend_score={meta.get('trend_score')})",
        f"- Momentum: {meta.get('momentum')}",
        f"- Bottoming: {meta.get('bottoming')} "
        f"(score={meta.get('bottoming_score')}, oversold={meta.get('oversold_score')}, "
        f"stabilization={meta.get('stabilization_score')}) — 과매도≠바닥확정",
        f"- Valuation: {meta.get('valuation')} (멀티플만 — MA/BB/RSI 아님)",
        f"- Extension: {meta.get('extension_state')} (기술 과열/압축)",
        f"- Entry Score: {meta.get('entry_score')} (chase로 점수 깎지 않음)",
        f"- Chase blocked: {meta.get('chase_blocked')} reasons={meta.get('chase_reasons')}",
        f"- Entry Action: {meta.get('entry_action')}",
        f"- Market State: {meta.get('market_state')}",
        f"- Position: holder={pos.get('holder')} / new={pos.get('new_investor')} / "
        f"trader={pos.get('trader')}",
        f"- Scale-in: {scale.get('label')} target_position_pct={scale.get('target_position_pct')} "
        f"(sizing_basis={scale.get('sizing_basis')} — 포트폴리오 전체 비중 아님)",
        f"- 호환 SIGNAL: {meta.get('signal')}",
        f"- Legacy scores: mom={scores.get('momentum')}, participation={scores.get('participation')}, "
        f"entry_quality={scores.get('entry_quality')}, composite={meta.get('score')}",
        f"- RVOL: {meta.get('rvol')} → {meta.get('rvol_note')}",
        f"- Actions(legacy): ENTRY={actions.get('entry')} / HOLDING={actions.get('holding')} / "
        f"TRADING={actions.get('trading')}",
        f"- 확률(캘리브): up={prob.get('up')} flat={prob.get('flat')} down={prob.get('down')}",
        f"- Expected Return 5/10/20d: {er.get('5d')} / {er.get('10d')} / {er.get('20d')}",
        f"- Expected DD 5/10/20d: {ed.get('5d')} / {ed.get('10d')} / {ed.get('20d')}",
        f"- BUY Trigger: {trig.get('buy_trigger')}",
        f"- DOWN Trigger: {trig.get('down_trigger')}",
        f"- Thesis Invalidation: {trig.get('thesis_invalidation') or trig.get('invalidation')}",
        f"- Risk Control: {trig.get('risk_control')}",
        f"- False breakdown: {meta.get('false_breakdown')}",
        f"- Hard sell override: {meta.get('hard_sell_override')}",
        f"- Sell softened: {meta.get('sell_softened')}",
        f"- reason_codes: {', '.join((meta.get('reason_codes') or [])[:12])}",
        "- 규칙: Fundamental/Trend/Momentum/Bottoming/Valuation/Entry/Position 을 덮어쓰지 말 것.",
        "- RVOL<1 = 기관 이탈 단정 금지. '거래량 확인 부족'으로만 서술.",
        "- Extension 과열은 Entry WAIT/NO-CHASE. Valuation과 섞지 말 것.",
        "- Invalidation(thesis)과 Risk Control(손절)을 분리해 서술.",
    ]
    return "\n".join(lines)
