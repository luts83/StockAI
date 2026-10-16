"""
Value-Aware Decision Engine — classifiers + Gate routing.

Value Regime ≠ Extension (MA/BB/RSI never enter Valuation).
Thesis includes market-implied fast-track (RS σ).
Spatial: 120d / 24 bins HVN/LVN.
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd

# Defaults §10.4
DEFAULT_CORE_PCT = 70
DEFAULT_TACTICAL_PCT = 30
DEFAULT_MAX_CAP_PCT = 15
DEFAULT_SCALE_IN_MAX_PER_CYCLE = 3
SCALE_IN_COOLDOWN_DAYS = 5
SCALE_IN_COOLDOWN_ATR = 1.5
SCALE_IN_COOLDOWN_PCT = 0.07
REDUCE_TACTICAL_PCT_LO = 10
REDUCE_TACTICAL_PCT_HI = 15


def _f(x, default=None) -> Optional[float]:
    try:
        if x is None:
            return default
        v = float(x)
        if v != v:
            return default
        return v
    except (TypeError, ValueError):
        return default


def _g(d: dict | None, *keys, default=None):
    cur = d or {}
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


# ── Spatial Support (120d / 24 bins) ───────────────────────────

def compute_spatial_support(
    df: pd.DataFrame,
    *,
    atr: Optional[float] = None,
    lookback: int = 120,
    n_bins: int = 24,
) -> dict[str, Any]:
    """
    AT_HVN_SUPPORT / IN_LVN_VACUUM / AT_RESISTANCE + Major HVN.
    """
    empty = {
        "ok": False,
        "spatial_support": "UNKNOWN",
        "major_hvn_low": None,
        "major_hvn_high": None,
        "major_hvn_mid": None,
        "at_hvn": False,
        "in_lvn": False,
        "poc": None,
    }
    if df is None or df.empty or "Close" not in df.columns or "Volume" not in df.columns:
        return empty

    work = df.copy()
    work.index = pd.to_datetime(work.index).tz_localize(None)
    window = work.tail(max(lookback, 30))
    if len(window) < 30:
        return {**empty, "error": "short"}

    close = float(window["Close"].iloc[-1])
    prev_close = float(window["Close"].iloc[-2]) if len(window) >= 2 else close
    lo = float(window["Low"].min())
    hi = float(window["High"].max())
    if hi <= lo:
        return {**empty, "error": "flat"}

    if atr is None or atr <= 0:
        # crude ATR
        tr = (window["High"] - window["Low"]).astype(float)
        atr = float(tr.tail(14).mean()) if len(tr) else (hi - lo) / n_bins

    edges = np.linspace(lo, hi, n_bins + 1)
    vol_bins = np.zeros(n_bins, dtype=float)
    for _, row in window.iterrows():
        v = float(row.get("Volume") or 0)
        if v <= 0:
            continue
        rlo, rhi = float(row["Low"]), float(row["High"])
        if rhi < rlo:
            rlo, rhi = rhi, rlo
        if rhi == rlo:
            mid = float(row["Close"])
            idx = int(np.clip(np.searchsorted(edges, mid, side="right") - 1, 0, n_bins - 1))
            vol_bins[idx] += v
            continue
        i0 = int(np.clip(np.searchsorted(edges, rlo, side="right") - 1, 0, n_bins - 1))
        i1 = int(np.clip(np.searchsorted(edges, rhi, side="right") - 1, 0, n_bins - 1))
        if i1 < i0:
            i0, i1 = i1, i0
        share = v / (i1 - i0 + 1)
        vol_bins[i0 : i1 + 1] += share

    total = float(vol_bins.sum()) or 1.0
    ranks = vol_bins / total
    order = np.argsort(vol_bins)
    # top 30% by volume share cumulative from densest bins
    sorted_desc = order[::-1]
    cum = 0.0
    hvn_idx = set()
    for i in sorted_desc:
        if vol_bins[i] <= 0:
            continue
        hvn_idx.add(int(i))
        cum += ranks[i]
        if cum >= 0.30:
            break
    # bottom 20% volume bins (among bins with any volume, lowest)
    sorted_asc = order
    cum_lo = 0.0
    lvn_idx = set()
    for i in sorted_asc:
        if vol_bins[i] <= 0:
            continue
        lvn_idx.add(int(i))
        cum_lo += ranks[i]
        if cum_lo >= 0.20:
            break

    poc_i = int(np.argmax(vol_bins))
    poc = float((edges[poc_i] + edges[poc_i + 1]) / 2)
    # Major HVN: POC node edges, or lowest HVN below price for support
    major_i = poc_i
    below = [i for i in hvn_idx if edges[i + 1] <= close + 1e-9]
    if below:
        # densest below price
        major_i = max(below, key=lambda i: vol_bins[i])
    major_low = float(edges[major_i])
    major_high = float(edges[major_i + 1])
    major_mid = (major_low + major_high) / 2

    band = 0.5 * atr
    at_hvn = False
    for i in hvn_idx:
        blo, bhi = float(edges[i]), float(edges[i + 1])
        if (blo - band) <= close <= (bhi + band):
            at_hvn = True
            break

    # current bin
    cur_i = int(np.clip(np.searchsorted(edges, close, side="right") - 1, 0, n_bins - 1))
    in_lvn = cur_i in lvn_idx and close < prev_close  # descending through LVN

    if at_hvn:
        spatial = "AT_HVN_SUPPORT"
    elif in_lvn:
        spatial = "IN_LVN_VACUUM"
    elif close > poc:
        spatial = "AT_RESISTANCE"
    else:
        spatial = "IN_LVN_VACUUM" if cur_i in lvn_idx else "UNKNOWN"

    # Hard break detection helpers from recent closes
    closes = window["Close"].astype(float)
    hard = detect_major_hvn_break(
        closes=closes,
        major_low=major_low,
        atr=atr,
        index=window.index,
    )

    return {
        "ok": True,
        "spatial_support": spatial,
        "at_hvn": at_hvn,
        "in_lvn": in_lvn,
        "poc": round(poc, 4),
        "major_hvn_low": round(major_low, 4),
        "major_hvn_high": round(major_high, 4),
        "major_hvn_mid": round(major_mid, 4),
        "atr_used": round(float(atr), 4),
        "hard_hvn_break": hard,
        "lookback": lookback,
        "n_bins": n_bins,
    }


def detect_major_hvn_break(
    *,
    closes: pd.Series,
    major_low: float,
    atr: float,
    index: pd.DatetimeIndex,
) -> dict[str, Any]:
    """
    Condition A: Friday weekly close below major_low.
    Condition B: 2 consecutive daily closes < major_low - 1.0*ATR.
    """
    thr = major_low - 1.0 * float(atr)
    c = closes.astype(float)
    cond_b = False
    if len(c) >= 2:
        cond_b = bool(c.iloc[-1] < thr and c.iloc[-2] < thr)

    cond_a = False
    try:
        # resample weekly last close; if last bar is week-ending Friday-ish
        weekly = c.resample("W-FRI").last().dropna()
        if len(weekly) >= 1:
            last_w = float(weekly.iloc[-1])
            # only count if latest daily date is within that week end
            last_day = pd.Timestamp(index[-1]).tz_localize(None)
            if last_day.weekday() >= 4:  # Fri or weekend proxy
                cond_a = last_w < major_low
            # also if weekly close already broke and we're still below
            if last_w < major_low and float(c.iloc[-1]) < major_low:
                cond_a = True
    except Exception:
        cond_a = False

    return {
        "broken": bool(cond_a or cond_b),
        "weekly_close_break": cond_a,
        "two_day_deep_break": cond_b,
        "threshold": round(thr, 4),
    }


# ── Value Regime ───────────────────────────────────────────────

def compute_value_regime(features: dict) -> dict[str, Any]:
    """
    Track A: Forward EV/Sales or PSR with hist z / peer (when available).
    Track B: drawdown percentile + 200d VWAP deviation.
    Never uses MA/BB/RSI.
    """
    val = features.get("valuation") or {}
    px = _g(features, "price", "close")
    evs = _f(val.get("forward_ev_sales") or val.get("ev_sales") or val.get("enterpriseToRevenue"))
    psr = _f(val.get("psr") or val.get("priceToSalesTrailing12Months"))
    peer = _f(val.get("peer_ev_sales_median") or val.get("peer_psr_median"))
    hist_z = _f(val.get("ev_sales_z") or val.get("psr_z"))

    track = None
    score = 0.0  # higher = more expensive
    reasons = []

    mult = evs if evs is not None and evs > 0 else psr
    if mult is not None and mult > 0:
        track = "A"
        # relative to peer
        rel = 0.0
        if peer and peer > 0:
            rel = (mult / peer) - 1.0  # >0 expensive
            reasons.append(f"vs_peer={rel:.2f}")
        else:
            # absolute heuristic for growth names
            if mult < 3:
                rel = -0.4
            elif mult < 8:
                rel = 0.0
            elif mult < 15:
                rel = 0.35
            else:
                rel = 0.7
            reasons.append(f"abs_mult={mult:.2f}")
        z_part = hist_z if hist_z is not None else rel * 2
        score = 0.5 * z_part + 0.5 * (rel * 2)
        reasons.append(f"trackA_score={score:.2f}")
    else:
        track = "B"
        dd = _f(_g(features, "price", "drawdown_from_52w_high"))
        if dd is None:
            # from returns/high if present
            dd = _f(val.get("drawdown_52w"))
        vwap_dev = _f(_g(features, "price", "vs_vwap_200") or val.get("vs_vwap_200"))
        # deep drawdown → cheap proxy
        dd_score = 0.0
        if dd is not None:
            # dd negative (e.g. -0.6)
            if dd <= -0.55:
                dd_score = -1.2
            elif dd <= -0.35:
                dd_score = -0.6
            elif dd <= -0.15:
                dd_score = -0.2
            elif dd >= -0.05:
                dd_score = 0.5
            reasons.append(f"dd52={dd:.2f}")
        vw_score = 0.0
        if vwap_dev is not None:
            if vwap_dev <= -0.25:
                vw_score = -0.8
            elif vwap_dev <= -0.10:
                vw_score = -0.3
            elif vwap_dev >= 0.15:
                vw_score = 0.6
            reasons.append(f"vwap200_dev={vwap_dev:.2f}")
        score = 0.55 * dd_score + 0.45 * vw_score
        if dd is None and vwap_dev is None:
            # last resort: trailing PSR-like from valuation peg/pe only (not price TA)
            peg = _f(val.get("peg"))
            pe = _f(val.get("trailing_pe") or val.get("forward_pe"))
            if peg is not None and peg > 3.5:
                score = 0.5
            elif pe is not None and pe > 60:
                score = 0.4
            elif pe is not None and 0 < pe < 15:
                score = -0.3
            else:
                score = 0.0
            reasons.append("sparse_fundamentals")

    if score <= -0.55:
        regime = "UNDERVALUED"
    elif score <= 0.25:
        regime = "FAIR"
    elif score <= 0.75:
        regime = "OVERVALUED"
    else:
        regime = "BUBBLE"

    return {
        "value_regime": regime,
        "track": track,
        "score": round(score, 3),
        "reasons": reasons,
        "multiple_used": mult,
    }


# ── Extension (price TA only — separate from value) ────────────

def compute_extension_regime(features: dict) -> str:
    m = features.get("momentum") or {}
    vol = features.get("volatility") or {}
    t = features.get("trend") or {}
    hot = cold = 0
    rsi = _f(m.get("rsi"))
    if rsi is not None:
        if rsi > 70:
            hot += 2
        elif rsi > 65:
            hot += 1
        elif rsi < 30:
            cold += 2
        elif rsi < 40:
            cold += 1
    bbp = _f(vol.get("bb_position"))
    if bbp is not None:
        pct = bbp * 100 if bbp <= 1.5 else bbp
        if pct > 85:
            hot += 2
        elif pct > 75:
            hot += 1
        elif pct < 15:
            cold += 2
        elif pct < 30:
            cold += 1
    pvm = _f(t.get("price_vs_ma20"))
    if pvm is not None:
        if pvm > 0.08:
            hot += 1
        elif pvm < -0.08:
            cold += 1
    if hot >= 3:
        return "OVERBOUGHT"
    if cold >= 3:
        return "OVERSOLD"
    # weak trend down without deep oversold
    if (pvm is not None and pvm < -0.03) and (rsi is not None and rsi < 50):
        return "WEAK_TREND"
    return "NEUTRAL"


# ── Thesis ─────────────────────────────────────────────────────

def compute_thesis_status(
    features: dict,
    *,
    fundamental_view: str | None = None,
) -> dict[str, Any]:
    """
    INTACT / WARNING / BROKEN.
    Fast-track: RS z vs sector + volume climax down day.
    """
    reasons = []
    status = "INTACT"

    # Earnings / guidance hooks if present
    earn = features.get("earnings_flags") or features.get("thesis_events") or {}
    if earn.get("guidance_cut_pct") is not None and _f(earn.get("guidance_cut_pct"), 0) <= -10:
        status = "BROKEN"
        reasons.append("guidance_cut_ge_10")
    if earn.get("structural_break"):
        status = "BROKEN"
        reasons.append("structural_break")

    rs = features.get("relative_strength") or {}
    rs5 = _f(rs.get("vs_sector_5d"))
    rs_z = _f(rs.get("vs_sector_5d_z"))
    # approximate z if missing: assume 5d RS sigma ~ 4%
    if rs_z is None and rs5 is not None:
        rs_z = rs5 / 0.04
    rvol = _f(_g(features, "volume", "rvol") or _g(features, "volume", "volume_ratio"))
    ret1 = _f(_g(features, "returns", "ret_1d"))
    down_climax = (
        ret1 is not None
        and ret1 < 0
        and rvol is not None
        and rvol >= 3.0
    )

    if rs_z is not None and rs_z <= -3.0 and down_climax:
        status = "BROKEN"
        reasons.append(f"rs_z={rs_z:.2f}_vol_climax")
    elif status != "BROKEN" and rs_z is not None and rs_z <= -2.0:
        status = "WARNING"
        reasons.append(f"rs_z={rs_z:.2f}")

    # fundamental sticky hint
    fv = (fundamental_view or "").upper()
    if status == "INTACT" and fv == "BEARISH":
        status = "WARNING"
        reasons.append("fundamental_bearish")

    return {
        "thesis_status": status,
        "rs_5d": rs5,
        "rs_5d_z": round(rs_z, 3) if rs_z is not None else None,
        "reasons": reasons,
    }


# ── Scale-in guards ────────────────────────────────────────────

def scale_in_allowed(
    portfolio: dict | None,
    features: dict,
    *,
    scale_in_count_cycle: int = 0,
) -> tuple[bool, str]:
    port = portfolio or {}
    weight = _f(port.get("weight_pct"), 0) or 0
    if weight >= DEFAULT_MAX_CAP_PCT:
        return False, "max_cap"
    if scale_in_count_cycle >= DEFAULT_SCALE_IN_MAX_PER_CYCLE:
        return False, "max_scale_ins"

    last = port.get("last_scale_in") or {}
    last_px = _f(last.get("price"))
    last_days = _f(last.get("days_since"), 999) or 999
    atr = _f(_g(features, "volatility", "atr"))
    px = _f(_g(features, "price", "close"))
    if last_px and px and last_days < SCALE_IN_COOLDOWN_DAYS:
        drop = (px - last_px) / last_px
        atr_ok = atr and px <= last_px - SCALE_IN_COOLDOWN_ATR * atr
        pct_ok = drop <= -SCALE_IN_COOLDOWN_PCT
        if not (atr_ok or pct_ok):
            return False, "cooldown"
    return True, "ok"


# ── Gate router ────────────────────────────────────────────────

def route_value_aware(
    features: dict,
    *,
    portfolio: dict | None = None,
    fundamental_view: str | None = None,
    scale_in_count_cycle: int = 0,
) -> dict[str, Any]:
    """
    Gate 1–4. Returns decision block for report/engine.
    """
    value = compute_value_regime(features)
    extension = compute_extension_regime(features)
    thesis = compute_thesis_status(features, fundamental_view=fundamental_view)
    spatial = features.get("spatial") or {}
    if not spatial.get("ok"):
        # soft fallback from volume_profile flags
        vp = features.get("volume_profile") or {}
        flags = (vp.get("flags") or {}) if vp.get("ok") else {}
        if flags.get("near_support"):
            spatial = {
                "ok": True,
                "spatial_support": "AT_HVN_SUPPORT",
                "at_hvn": True,
                "in_lvn": False,
                "hard_hvn_break": {"broken": False},
                "major_hvn_low": (vp.get("nearest_support") or {}).get("price"),
            }
        elif flags.get("breakdown_hold"):
            spatial = {
                "ok": True,
                "spatial_support": "IN_LVN_VACUUM",
                "at_hvn": False,
                "in_lvn": True,
                "hard_hvn_break": {"broken": bool(flags.get("breakdown_hold"))},
                "major_hvn_low": (vp.get("nearest_support") or {}).get("price"),
            }
        else:
            spatial = {
                "ok": True,
                "spatial_support": "UNKNOWN",
                "at_hvn": False,
                "in_lvn": False,
                "hard_hvn_break": {"broken": False},
                "major_hvn_low": None,
            }

    vr = value["value_regime"]
    th = thesis["thesis_status"]
    sp = spatial.get("spatial_support") or "UNKNOWN"
    hard_break = bool((spatial.get("hard_hvn_break") or {}).get("broken"))

    action = "HOLD"
    tranche = "CORE"
    sizing = "포지션 유지 / 관찰"
    gate = "G4"
    note = ""

    # Gate 1
    if th == "BROKEN" or hard_break:
        action, tranche = "EXIT", "ALL"
        sizing = "전량 청산 (Hard EXIT)"
        gate = "G1"
        note = "thesis_broken" if th == "BROKEN" else "major_hvn_break"
    # Gate 2 OVERSOLD
    elif extension == "OVERSOLD":
        gate = "G2"
        if th == "WARNING":
            action, tranche = "WAIT", "TACTICAL"
            sizing = "Thesis WARNING (RS 약세) — SCALE_IN 차단, WAIT"
            note = "warning_or_cap"
        elif vr in ("UNDERVALUED", "FAIR") and th == "INTACT":
            ok_cap, cap_reason = scale_in_allowed(
                portfolio, features, scale_in_count_cycle=scale_in_count_cycle
            )
            if spatial.get("in_lvn") or sp == "IN_LVN_VACUUM":
                action, tranche = "WAIT", "TACTICAL"
                sizing = "LVN 진공 구간 — 지지 확인 전 분할매수 금지"
                note = "lvn_wait"
            elif not ok_cap:
                action, tranche = "WAIT", "TACTICAL"
                sizing = f"Cap/쿨다운 — WAIT ({cap_reason})"
                note = cap_reason
            elif spatial.get("at_hvn") or sp == "AT_HVN_SUPPORT":
                action, tranche = "SCALE_IN", "TACTICAL"
                sizing = (
                    f"2차 분할매수 검토 (목표 포지션 대비 +10~15%p, "
                    f"종목 Cap {DEFAULT_MAX_CAP_PCT}%)"
                )
                note = "scale_in_hvn"
            else:
                action, tranche = "WAIT", "TACTICAL"
                sizing = "과매도이나 HVN 미도달 — 지지 대기"
                note = "wait_support"
        elif vr in ("OVERVALUED", "BUBBLE"):
            action, tranche = "HOLD", "CORE"
            sizing = "고평가·과매도 — 섣부른 저점매수 금지, 탐색만"
            note = "oversold_but_expensive"
        else:
            action, tranche = "WAIT", "TACTICAL"
            sizing = "과매도 — 관망"
            note = "oversold_default"
    # Gate 3 OVERBOUGHT
    elif extension == "OVERBOUGHT":
        gate = "G3"
        if vr in ("OVERVALUED", "BUBBLE"):
            action, tranche = "REDUCE", "TACTICAL"
            sizing = (
                f"공격적 비중 축소 {REDUCE_TACTICAL_PCT_LO}~{REDUCE_TACTICAL_PCT_HI}%p "
                f"(Core {DEFAULT_CORE_PCT}% 가드레일)"
            )
            note = "reduce_rich"
        elif vr in ("UNDERVALUED", "FAIR") and th == "INTACT":
            action, tranche = "REDUCE", "TACTICAL"
            sizing = (
                f"REDUCE_TACTICAL: Tactical만 {REDUCE_TACTICAL_PCT_LO}~"
                f"{REDUCE_TACTICAL_PCT_HI}%p 축소, Core {DEFAULT_CORE_PCT}% 홀딩"
            )
            note = "reduce_tactical"
        else:
            action, tranche = "HOLD", "CORE"
            sizing = "과열 — 신규 추격 금지"
            note = "overbought_hold"
    # Gate 4
    else:
        gate = "G4"
        if th == "INTACT":
            action, tranche = "HOLD", "CORE"
            sizing = "Thesis 유지 — 단기 약세만으로 매도 금지"
            note = "hold_intact"
        elif th == "WARNING":
            action, tranche = "WAIT", "TACTICAL"
            sizing = "Thesis WARNING — 추가 매수 보류"
            note = "wait_warning"
        else:
            action, tranche = "EXIT", "ALL"
            sizing = "전량 청산"
            note = "exit_fallback"

    # Map REDUCE_TACTICAL naming
    sub = action
    if note == "reduce_tactical":
        sub = "REDUCE_TACTICAL"

    legacy, signal_type, sub_action = map_to_legacy(action, sub)

    return {
        "state_summary": {
            "value_regime": vr,
            "extension_state": extension,
            "thesis_status": th,
            "spatial_support": sp,
        },
        "decision": {
            "action": action if sub != "REDUCE_TACTICAL" else "REDUCE",
            "sub_action": sub_action,
            "target_tranche": tranche,
            "sizing_guide": sizing,
            "gate": gate,
            "note": note,
        },
        "value_detail": value,
        "thesis_detail": thesis,
        "spatial_detail": spatial,
        "legacy_signal": legacy,
        "signal_type": signal_type,
        "portfolio_defaults": {
            "core_pct": DEFAULT_CORE_PCT,
            "tactical_pct": DEFAULT_TACTICAL_PCT,
            "max_cap_pct": DEFAULT_MAX_CAP_PCT,
        },
        "rationale_stubs": {
            "value_vs_price": (
                f"Value={vr} (track {value.get('track')}), Extension={extension}. "
                f"가격 모멘텀만으로 매도/매수하지 않음."
            ),
            "invalidation_condition": _invalidation_text(spatial, th),
            "next_trigger": _next_trigger_text(action, spatial, extension),
        },
    }


def map_to_legacy(action: str, sub: str) -> tuple[str, str, str]:
    """
    Returns (legacy_ui_signal, signal_type for DB/KPI, sub_action).
    REDUCE kept separate from SELL/EXIT.
    """
    if action == "BUY":
        return "BUY", "BUY", "INITIAL"
    if action == "SCALE_IN":
        return "BUY", "BUY", "SCALE_IN"
    if action == "HOLD":
        return "WATCH_FLAT", "WATCH", "HOLD"
    if action == "WAIT":
        return "WATCH_FLAT", "WATCH", "WAIT_SUPPORT"
    if action == "REDUCE" or sub == "REDUCE_TACTICAL":
        return "WATCH_DOWN", "REDUCE", "REDUCE_TACTICAL" if sub == "REDUCE_TACTICAL" else "REDUCE"
    if action == "EXIT":
        return "SELL", "SELL", "EXIT"
    return "WATCH_FLAT", "WATCH", "HOLD"


def _invalidation_text(spatial: dict, th: str) -> str:
    lo = spatial.get("major_hvn_low")
    if lo:
        return (
            f"Thesis BROKEN 또는 Major HVN(${lo}) "
            f"주봉 종가 이탈 / 2일 연속 종가 < HVN−1ATR 시 EXIT로 전환."
        )
    return "Thesis BROKEN 또는 핵심 장기 지지 완파 시 EXIT로 전환."


def _next_trigger_text(action: str, spatial: dict, extension: str) -> str:
    mid = spatial.get("major_hvn_mid") or spatial.get("major_hvn_low")
    if action == "SCALE_IN":
        return "변동성 축소·RVOL 개선 확인 후 다음 분할매수; Cap/쿨다운 준수."
    if action == "WAIT" and extension == "OVERSOLD":
        return f"HVN 지지({mid}) 안착 확인 시 SCALE_IN 재검토."
    if action == "REDUCE":
        return "과열 해소(Extension NEUTRAL) 또는 Thesis 변화 시 재평가."
    if action == "HOLD":
        return "과매도+HVN+Value 저평가/적정 시 SCALE_IN; 과열+고평가 시 REDUCE."
    return "Invalidation 또는 Next Trigger 조건을 일별 점검."
