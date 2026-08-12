"""
Signal Engine v2 — Phase 1 Baseline evaluation.

고정 horizon(1/5/10/20 거래일) Forward Return, MFE, MAE, Max Drawdown,
SPY Excess Return을 계산하고 Baseline KPI 성적표를 만든다.

LLM 시그널을 바꾸지 않는다. 기존 BUY/SELL/WATCH 성과를 측정만 한다.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

import pandas as pd
import yfinance as yf

ENGINE_VERSION = "baseline_v1"
HORIZONS = (1, 5, 10, 20)
BENCHMARK = "SPY"

# 가격 캐시: ticker -> Close/High/Low DataFrame (tz-naive)
_price_cache: dict[str, pd.DataFrame] = {}


def _safe_float(x: Any, ndigits: int = 6) -> Optional[float]:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return round(v, ndigits)


def extract_confidence(analysis: str | None) -> Optional[str]:
    """CONFIDENCE:상|중|하 → high|mid|low (없으면 None)."""
    if not analysis:
        return None
    m = re.search(r"CONFIDENCE\s*[:：]\s*(상|중|하|HIGH|MID|LOW|high|mid|low)", analysis)
    if not m:
        return None
    raw = m.group(1).lower()
    return {"상": "high", "중": "mid", "하": "low", "high": "high", "mid": "mid", "low": "low"}.get(raw, raw)


def normalize_signal(signal: str | None) -> str:
    s = (signal or "").strip().upper().replace(" ", "_")
    if s in ("BUY", "SELL", "AVOID", "WATCH",
             "WATCH_UP", "WATCH_FLAT", "WATCH_DOWN", "WATCH_RISK"):
        return s
    return s or "UNKNOWN"


def signal_family(signal: str | None) -> str:
    s = normalize_signal(signal)
    if s.startswith("WATCH"):
        return "WATCH"
    if s == "AVOID":
        return "SELL"
    return s



def parse_timestamp(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, dict) and "$date" in value:
        value = value["$date"]
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def get_ohlc(ticker: str, start: str, end: str) -> pd.DataFrame:
    """OHLC 다운로드 (캐시). auto_adjust=True."""
    key = f"{ticker}|{start}|{end}"
    if key in _price_cache:
        return _price_cache[key].copy()

    df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
    if df is None or df.empty:
        _price_cache[key] = pd.DataFrame()
        return pd.DataFrame()

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    keep = [c for c in ("Open", "High", "Low", "Close") if c in df.columns]
    out = df[keep].dropna(how="all").copy()
    out.index = pd.to_datetime(out.index).tz_localize(None)
    _price_cache[key] = out
    return out.copy()


def clear_price_cache():
    _price_cache.clear()


def _max_drawdown(closes: pd.Series) -> Optional[float]:
    """종가 기준 보유기간 최대 낙폭 (음수 또는 0)."""
    if closes is None or len(closes) < 2:
        return 0.0 if closes is not None and len(closes) else None
    equity = closes.astype(float)
    peak = equity.cummax()
    dd = (equity / peak) - 1.0
    return _safe_float(float(dd.min()), 6)


def compute_forward_metrics(
    ticker: str,
    asof: datetime | str,
    entry_price: Optional[float] = None,
    benchmark: str = BENCHMARK,
) -> Optional[dict]:
    """
    분석일(asof) 당일 또는 그 이후 첫 거래일 종가를 진입가로 사용.
    MFE: horizon 내 High 기준 최대 상승률
    MAE: horizon 내 Low 기준 최대 하락률 (음수)
    """
    if isinstance(asof, str):
        asof_dt = datetime.fromisoformat(asof.replace("Z", "+00:00"))
    else:
        asof_dt = asof

    asof_date = pd.Timestamp(asof_dt.date())
    start = (asof_date - timedelta(days=7)).strftime("%Y-%m-%d")
    end = (asof_date + timedelta(days=120)).strftime("%Y-%m-%d")

    ohlc = get_ohlc(ticker, start, end)
    if ohlc.empty or "Close" not in ohlc.columns:
        return None

    after = ohlc[ohlc.index >= asof_date]
    if after.empty:
        return None

    entry_date = after.index[0]
    close_entry = float(after["Close"].iloc[0])
    entry = float(entry_price) if entry_price and entry_price > 0 else close_entry

    pos0 = ohlc.index.get_loc(entry_date)
    if not isinstance(pos0, int):
        pos0 = int(getattr(pos0, "start", 0))

    # benchmark alignment
    b_ohlc = get_ohlc(benchmark, start, end)
    b_entry = None
    if not b_ohlc.empty and "Close" in b_ohlc.columns:
        b_after = b_ohlc[b_ohlc.index >= asof_date]
        if not b_after.empty:
            b_entry = float(b_after["Close"].iloc[0])

    out: dict[str, Any] = {
        "ticker": ticker.upper(),
        "entry_date": str(entry_date.date()),
        "entry_price": _safe_float(entry, 4),
        "benchmark": benchmark,
        "engine_version": ENGINE_VERSION,
        "horizons_complete": {},
    }

    for h in HORIZONS:
        pos = pos0 + h
        complete = pos < len(ohlc)
        out["horizons_complete"][f"{h}d"] = complete

        if not complete:
            out[f"close_{h}d"] = None
            out[f"return_{h}d"] = None
            out[f"mfe_{h}d"] = None
            out[f"mae_{h}d"] = None
            out[f"max_drawdown_{h}d"] = None
            out[f"excess_return_{h}d"] = None
            continue

        window = ohlc.iloc[pos0 : pos + 1]
        px = float(ohlc["Close"].iloc[pos])
        ret = (px / entry) - 1.0

        high = float(window["High"].max()) if "High" in window else float(window["Close"].max())
        low = float(window["Low"].min()) if "Low" in window else float(window["Close"].min())
        mfe = (high / entry) - 1.0
        mae = (low / entry) - 1.0
        mdd = _max_drawdown(window["Close"])

        excess = None
        if b_entry and not b_ohlc.empty:
            # same calendar date as stock horizon close if possible
            h_date = ohlc.index[pos]
            b_row = b_ohlc[b_ohlc.index <= h_date]
            if not b_row.empty:
                b_px = float(b_row["Close"].iloc[-1])
                b_ret = (b_px / b_entry) - 1.0
                excess = ret - b_ret

        out[f"close_{h}d"] = _safe_float(px, 4)
        out[f"return_{h}d"] = _safe_float(ret, 6)
        out[f"mfe_{h}d"] = _safe_float(mfe, 6)
        out[f"mae_{h}d"] = _safe_float(mae, 6)
        out[f"max_drawdown_{h}d"] = mdd
        out[f"excess_return_{h}d"] = _safe_float(excess, 6) if excess is not None else None

    return out


def outcome_from_analysis_doc(doc: dict) -> Optional[dict]:
    """analyses 문서 → signal_outcome 문서(성과 필드 포함)."""
    ticker = (doc.get("ticker") or "").upper()
    if not ticker:
        return None

    ts = parse_timestamp(doc.get("created_at"))
    data_date = doc.get("data_date")
    if data_date:
        try:
            asof = datetime.fromisoformat(str(data_date)[:10])
        except Exception:
            asof = ts or datetime.utcnow()
    else:
        asof = ts or datetime.utcnow()

    analysis_id = doc.get("_id")
    if isinstance(analysis_id, dict):
        analysis_id = analysis_id.get("$oid") or str(analysis_id)
    else:
        analysis_id = str(analysis_id)

    signal = normalize_signal(doc.get("signal"))
    confidence = extract_confidence(doc.get("analysis"))
    entry_hint = doc.get("current_price") or doc.get("entry_price")

    metrics = compute_forward_metrics(ticker, asof, entry_price=entry_hint)
    if not metrics:
        return {
            "analysis_id": analysis_id,
            "ticker": ticker,
            "timestamp": ts.isoformat() if ts else None,
            "data_date": str(data_date)[:10] if data_date else (asof.strftime("%Y-%m-%d")),
            "entry_price": _safe_float(entry_hint, 4),
            "signal": signal,
            "confidence": confidence,
            "engine_version": ENGINE_VERSION,
            "error": "price_data_unavailable",
            "updated_at": datetime.utcnow().isoformat(),
        }

    return {
        "analysis_id": analysis_id,
        "ticker": ticker,
        "timestamp": ts.isoformat() if ts else None,
        "data_date": str(data_date)[:10] if data_date else metrics["entry_date"],
        "signal": signal,
        "confidence": confidence,
        "updated_at": datetime.utcnow().isoformat(),
        **metrics,
    }


def _mean(vals: list[float]) -> Optional[float]:
    return _safe_float(sum(vals) / len(vals), 6) if vals else None


def _median(vals: list[float]) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    return _safe_float(s[len(s) // 2], 6)


def _profit_factor(returns: list[float]) -> Optional[float]:
    gains = sum(r for r in returns if r > 0)
    losses = sum(-r for r in returns if r < 0)
    if losses == 0:
        return None if gains == 0 else None  # undefined / infinite → None in JSON
    return _safe_float(gains / losses, 4)


def summarize_baseline(
    outcomes: Iterable[dict],
    horizons: tuple[int, ...] = (5, 10, 20),
) -> dict:
    """
    Baseline 성적표.

    Precision (단순 방향):
      BUY_Hd  = P(return_Hd > 0 | BUY, available)
      SELL_Hd = P(return_Hd < 0 | SELL, available)

    WATCH:
      up_rate_Hd           = P(R>0 | WATCH)
      downside_avoid_Hd    = P(R > -5% | WATCH)  # 큰 하락 회피
      opportunity_loss_Hd  = P(R >= +8% | WATCH) # 놓친 강한 상승
    """
    rows = [o for o in outcomes if o and signal_family(o.get("signal")) in ("BUY", "SELL", "WATCH")]
    by_signal: dict[str, list] = defaultdict(list)
    for o in rows:
        by_signal[signal_family(o["signal"])].append(o)

    report: dict[str, Any] = {
        "engine_version": ENGINE_VERSION,
        "generated_at": datetime.utcnow().isoformat(),
        "n_total": len(rows),
        "n_by_signal": {k: len(v) for k, v in by_signal.items()},
        "signal_mix_pct": {
            k: _safe_float(100 * len(v) / len(rows), 1) if rows else 0
            for k, v in by_signal.items()
        },
        "horizons": {},
        "notes": [
            "Precision은 단순 방향 적중(BUY: R>0, SELL: R<0).",
            "평가 기준은 다음 분석 시점이 아니라 고정 거래일 horizon.",
            "Excess return은 SPY 대비.",
            "BUY Profit Factor는 롱 P&L, SELL은 숏 P&L(-return) 기준.",
            "이 Baseline보다 개선되지 않은 수정은 배포하지 않는다.",
        ],
    }

    for h in horizons:
        rkey = f"return_{h}d"
        mfek = f"mfe_{h}d"
        maek = f"mae_{h}d"
        mddk = f"max_drawdown_{h}d"
        xkey = f"excess_return_{h}d"
        block: dict[str, Any] = {}

        for sig in ("BUY", "SELL", "WATCH"):
            subset = [o for o in by_signal.get(sig, []) if o.get(rkey) is not None]
            rets = [float(o[rkey]) for o in subset]
            mfes = [float(o[mfek]) for o in subset if o.get(mfek) is not None]
            maes = [float(o[maek]) for o in subset if o.get(maek) is not None]
            mdds = [float(o[mddk]) for o in subset if o.get(mddk) is not None]
            xs = [float(o[xkey]) for o in subset if o.get(xkey) is not None]

            if sig == "BUY":
                hits = sum(1 for r in rets if r > 0)
                precision = _safe_float(100 * hits / len(rets), 1) if rets else None
                strategy_rets = rets  # long
            elif sig == "SELL":
                hits = sum(1 for r in rets if r < 0)
                precision = _safe_float(100 * hits / len(rets), 1) if rets else None
                strategy_rets = [-r for r in rets]  # short P&L
            else:
                precision = None
                hits = sum(1 for r in rets if r > 0)
                strategy_rets = rets

            entry = {
                "n": len(subset),
                "precision_pct": precision,
                "avg_return_pct": _safe_float(100 * _mean(rets), 2) if rets else None,
                "median_return_pct": _safe_float(100 * _median(rets), 2) if rets else None,
                "avg_excess_return_pct": _safe_float(100 * _mean(xs), 2) if xs else None,
                "avg_mfe_pct": _safe_float(100 * _mean(mfes), 2) if mfes else None,
                "avg_mae_pct": _safe_float(100 * _mean(maes), 2) if maes else None,
                "avg_max_drawdown_pct": _safe_float(100 * _mean(mdds), 2) if mdds else None,
                "max_loss_pct": _safe_float(100 * min(rets), 2) if rets else None,
                "max_gain_pct": _safe_float(100 * max(rets), 2) if rets else None,
                "profit_factor": _profit_factor(strategy_rets),
                "avg_strategy_pnl_pct": _safe_float(100 * _mean(strategy_rets), 2) if strategy_rets else None,
            }

            if sig == "WATCH":
                up = sum(1 for r in rets if r > 0)
                avoid = sum(1 for r in rets if r > -0.05)
                opp = sum(1 for r in rets if r >= 0.08)
                entry.update({
                    "up_rate_pct": _safe_float(100 * up / len(rets), 1) if rets else None,
                    "downside_avoidance_pct": _safe_float(100 * avoid / len(rets), 1) if rets else None,
                    "opportunity_loss_pct": _safe_float(100 * opp / len(rets), 1) if rets else None,
                })

            if sig == "BUY":
                entry["hit_count"] = hits if rets else 0
            if sig == "SELL":
                entry["hit_count"] = hits if rets else 0

            block[sig] = entry

        report["horizons"][f"{h}d"] = block

    return report


def build_outcomes_from_docs(docs: list[dict], progress: bool = True) -> list[dict]:
    """분석 문서 리스트 → outcome 리스트."""
    clear_price_cache()
    outcomes = []
    n = len(docs)
    for i, doc in enumerate(docs, 1):
        if progress and (i == 1 or i % 10 == 0 or i == n):
            print(f"[baseline] {i}/{n} {doc.get('ticker')} {doc.get('signal')}")
        try:
            outcomes.append(outcome_from_analysis_doc(doc))
        except Exception as e:
            aid = doc.get("_id")
            outcomes.append({
                "analysis_id": str(aid),
                "ticker": doc.get("ticker"),
                "signal": normalize_signal(doc.get("signal")),
                "error": str(e),
                "engine_version": ENGINE_VERSION,
                "updated_at": datetime.utcnow().isoformat(),
            })
    return [o for o in outcomes if o]
