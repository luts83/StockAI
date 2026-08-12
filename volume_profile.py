"""
매물대 (Volume Profile) 분석.

고정 lookback OHLCV로 가격대별 거래량 밀집(HVN/POC)을 구하고,
현재가 위 = 저항 후보, 아래 = 지지 후보로 분류한다.
돌파·종가 안착 시 저항↔지지 역할 전환(flip)을 표시한다.
"""
from __future__ import annotations

import math
from typing import Any, Optional

import numpy as np
import pandas as pd


def _safe(v, nd=4) -> Optional[float]:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, nd)


def compute_volume_profile(
    df: pd.DataFrame,
    *,
    lookback: int = 60,
    n_bins: int = 24,
    hvn_top: int = 5,
    flip_lookback: int = 10,
    accept_bars: int = 2,
) -> dict[str, Any]:
    """
    Returns volume profile summary + support/resistance zones + flips.
    """
    if df is None or df.empty or "Close" not in df.columns or "Volume" not in df.columns:
        return {"ok": False, "error": "insufficient_ohlcv"}

    work = df.copy()
    work.index = pd.to_datetime(work.index).tz_localize(None)
    window = work.tail(max(lookback, 20))
    if len(window) < 20:
        return {"ok": False, "error": "lookback_too_short", "n": len(window)}

    close = float(window["Close"].iloc[-1])
    lo = float(window["Low"].min())
    hi = float(window["High"].max())
    if hi <= lo:
        return {"ok": False, "error": "flat_range"}

    edges = np.linspace(lo, hi, n_bins + 1)
    vol_bins = np.zeros(n_bins, dtype=float)

    # 봉의 [Low, High] 구간에 거래량을 균등 분배 (매물대 근사)
    for _, row in window.iterrows():
        v = float(row.get("Volume") or 0)
        if v <= 0:
            continue
        rlo = float(row["Low"])
        rhi = float(row["High"])
        if rhi < rlo:
            rlo, rhi = rhi, rlo
        if rhi == rlo:
            # 단일 가격 → typical 빈
            mid = float(row["Close"])
            idx = int(np.clip(np.searchsorted(edges, mid, side="right") - 1, 0, n_bins - 1))
            vol_bins[idx] += v
            continue
        # overlapping bins
        i0 = int(np.clip(np.searchsorted(edges, rlo, side="right") - 1, 0, n_bins - 1))
        i1 = int(np.clip(np.searchsorted(edges, rhi, side="right") - 1, 0, n_bins - 1))
        if i1 < i0:
            i0, i1 = i1, i0
        n_overlap = i1 - i0 + 1
        share = v / n_overlap
        vol_bins[i0 : i1 + 1] += share

    total = float(vol_bins.sum()) or 1.0
    mids = (edges[:-1] + edges[1:]) / 2.0
    poc_i = int(np.argmax(vol_bins))
    poc = float(mids[poc_i])

    # Value Area ~70% volume around POC
    order = list(np.argsort(vol_bins)[::-1])
    cum = 0.0
    va_idx = set()
    for i in order:
        va_idx.add(int(i))
        cum += vol_bins[i]
        if cum / total >= 0.70:
            break
    va_low = float(edges[min(va_idx)])
    va_high = float(edges[max(va_idx) + 1])

    # HVN: top nodes by volume (exclude empty)
    ranked = sorted(
        [(i, float(vol_bins[i])) for i in range(n_bins) if vol_bins[i] > 0],
        key=lambda x: x[1],
        reverse=True,
    )
    hvn_idxs = [i for i, _ in ranked[:hvn_top]]

    mean_v = float(np.mean(vol_bins)) if len(vol_bins) else 0.0
    std_v = float(np.std(vol_bins)) if len(vol_bins) else 0.0

    zones = []
    for i in hvn_idxs:
        zlo, zhi = float(edges[i]), float(edges[i + 1])
        zmid = float(mids[i])
        dist = (zmid - close) / close if close else 0.0
        if zhi < close * 0.998:
            role = "support"
        elif zlo > close * 1.002:
            role = "resistance"
        else:
            role = "at_zone"
        zones.append({
            "bin": i,
            "price": _safe(zmid, 4),
            "low": _safe(zlo, 4),
            "high": _safe(zhi, 4),
            "volume": _safe(vol_bins[i], 0),
            "volume_pct": _safe(100 * vol_bins[i] / total, 2),
            "strength": _safe((vol_bins[i] - mean_v) / std_v, 2) if std_v > 0 else 0.0,
            "role": role,
            "distance_pct": _safe(dist * 100, 2),
            "is_poc": i == poc_i,
            "flipped": False,
            "flip_type": None,
        })

    # Role flip detection on recent bars
    recent = work.tail(max(flip_lookback, accept_bars + 2))
    for z in zones:
        zlo, zhi = z["low"], z["high"]
        if zlo is None or zhi is None:
            continue
        closes = recent["Close"].astype(float).values
        # resistance → support: previously below zone top, then closes above zone for accept_bars
        above = closes > zhi
        below = closes < zlo
        if len(closes) >= accept_bars + 1:
            # breakout up acceptance
            if above[-accept_bars:].all() and (below[:-accept_bars].any() or (closes[:-accept_bars] <= zhi).any()):
                # was interacting from below / inside then accepted above
                zmid = float(z.get("price") or ((zlo + zhi) / 2))
                if z["role"] in ("resistance", "at_zone") or (zmid >= close * 0.99):
                    # if now price is above, it's support
                    if close > zhi:
                        z["role"] = "support"
                        z["flipped"] = True
                        z["flip_type"] = "resistance_to_support"
            # breakdown acceptance
            if below[-accept_bars:].all() and (above[:-accept_bars].any() or (closes[:-accept_bars] >= zlo).any()):
                if close < zlo:
                    z["role"] = "resistance"
                    z["flipped"] = True
                    z["flip_type"] = "support_to_resistance"

    supports = sorted(
        [z for z in zones if z["role"] == "support"],
        key=lambda z: abs(z.get("distance_pct") or 999),
    )
    resistances = sorted(
        [z for z in zones if z["role"] == "resistance"],
        key=lambda z: abs(z.get("distance_pct") or 999),
    )
    nearest_sup = supports[0] if supports else None
    nearest_res = resistances[0] if resistances else None

    # Position vs POC / value area
    if close > poc * 1.005:
        vs_poc = "above"
    elif close < poc * 0.995:
        vs_poc = "below"
    else:
        vs_poc = "at"

    in_value_area = va_low <= close <= va_high

    # Composite flags for scoring
    near_support = bool(
        nearest_sup and abs(nearest_sup.get("distance_pct") or 99) <= 2.5
    )
    near_resistance = bool(
        nearest_res and abs(nearest_res.get("distance_pct") or 99) <= 2.5
    )
    breakout_hold = any(
        z.get("flip_type") == "resistance_to_support" and z.get("flipped")
        for z in zones
    )
    breakdown_hold = any(
        z.get("flip_type") == "support_to_resistance" and z.get("flipped")
        for z in zones
    )

    return {
        "ok": True,
        "lookback": int(len(window)),
        "n_bins": n_bins,
        "range": {"low": _safe(lo, 4), "high": _safe(hi, 4)},
        "poc": _safe(poc, 4),
        "value_area": {"low": _safe(va_low, 4), "high": _safe(va_high, 4)},
        "vs_poc": vs_poc,
        "in_value_area": bool(in_value_area),
        "close": _safe(close, 4),
        "zones": zones,
        "nearest_support": nearest_sup,
        "nearest_resistance": nearest_res,
        "flags": {
            "near_support": near_support,
            "near_resistance": near_resistance,
            "breakout_hold": breakout_hold,      # 저항 돌파 후 지지 전환
            "breakdown_hold": breakdown_hold,    # 지지 이탈 후 저항 전환
            "at_hvn": any(z["role"] == "at_zone" for z in zones),
        },
    }


def format_volume_profile_for_prompt(vp: dict | None) -> str:
    """LLM 프롬프트용 매물대 요약."""
    if not vp or not vp.get("ok"):
        return "매물대 데이터 없음"

    lines = [
        f"- 분석구간: 최근 {vp.get('lookback')}거래일 Volume Profile ({vp.get('n_bins')} bins)",
        f"- POC(최대매물): ${vp.get('poc')}",
        f"- Value Area: ${vp.get('value_area', {}).get('low')} ~ ${vp.get('value_area', {}).get('high')}",
        f"- 현재가 vs POC: {vp.get('vs_poc')} / VA 내부: {vp.get('in_value_area')}",
    ]
    ns = vp.get("nearest_support") or {}
    nr = vp.get("nearest_resistance") or {}
    if ns:
        flip = f" [전환:{ns.get('flip_type')}]" if ns.get("flipped") else ""
        lines.append(
            f"- 최근접 지지 매물: ${ns.get('price')} ({ns.get('distance_pct')}%, "
            f"비중 {ns.get('volume_pct')}%){flip}"
        )
    if nr:
        flip = f" [전환:{nr.get('flip_type')}]" if nr.get("flipped") else ""
        lines.append(
            f"- 최근접 저항 매물: ${nr.get('price')} ({nr.get('distance_pct')}%, "
            f"비중 {nr.get('volume_pct')}%){flip}"
        )
    flags = vp.get("flags") or {}
    lines.append(
        f"- 플래그: near_support={flags.get('near_support')}, "
        f"near_resistance={flags.get('near_resistance')}, "
        f"breakout_hold(저항→지지)={flags.get('breakout_hold')}, "
        f"breakdown_hold(지지→저항)={flags.get('breakdown_hold')}"
    )
    lines.append(
        "- 해석 규칙: 현재가 위 매물=저항 후보, 아래 매물=지지 후보. "
        "돌파 후 종가 안착 시 저항→지지(또는 지지→저항) 전환 가능. "
        "매물대를 BUY 단독 근거로 쓰지 말 것."
    )
    # top zones
    for z in (vp.get("zones") or [])[:5]:
        lines.append(
            f"  · ${z.get('price')} [{z.get('role')}] vol={z.get('volume_pct')}% "
            f"dist={z.get('distance_pct')}%"
            + (f" flip={z.get('flip_type')}" if z.get("flipped") else "")
        )
    return "\n".join(lines)
