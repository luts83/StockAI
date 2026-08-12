"""
Signal Engine v2 — Phase 2 Feature store.

분석 시점의 OHLCV·기술지표·거래량·변동성·상대강도·시장 regime·뉴스 요약을
객관적 feature 스냅샷으로 저장한다. (시그널 결정은 아직 LLM — Phase 5에서 Score 전환)

analyzer/pandas_ta에 의존하지 않고 pandas로 지표를 계산한다 (백필·sandbox 안정성).
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

FEATURES_VERSION = "features_v1"

SECTOR_ETF_MAP = {
    "technology": "XLK",
    "information technology": "XLK",
    "communication services": "XLC",
    "consumer cyclical": "XLY",
    "consumer discretionary": "XLY",
    "consumer defensive": "XLP",
    "consumer staples": "XLP",
    "energy": "XLE",
    "financial services": "XLF",
    "financials": "XLF",
    "healthcare": "XLV",
    "health care": "XLV",
    "industrials": "XLI",
    "basic materials": "XLB",
    "materials": "XLB",
    "real estate": "XLRE",
    "utilities": "XLU",
}

_bench_cache: dict[str, pd.DataFrame] = {}


def _safe(val, nd=6) -> Optional[float]:
    if val is None:
        return None
    try:
        v = float(val)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return round(v, nd)


def _pct(a: float, b: float) -> Optional[float]:
    if b is None or b == 0:
        return None
    return _safe((a / b) - 1.0, 6)


def sector_to_etf(sector: str | None) -> Optional[str]:
    if not sector:
        return None
    return SECTOR_ETF_MAP.get(str(sector).strip().lower())


def _download_ohlc(ticker: str, start: str, end: str) -> pd.DataFrame:
    key = f"{ticker}|{start}|{end}"
    if key in _bench_cache:
        return _bench_cache[key].copy()
    df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
    if df is None or df.empty:
        _bench_cache[key] = pd.DataFrame()
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    keep = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in df.columns]
    out = df[keep].dropna(how="all").copy()
    out.index = pd.to_datetime(out.index).tz_localize(None)
    _bench_cache[key] = out
    return out.copy()


def clear_feature_cache():
    _bench_cache.clear()


def _rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()


def calculate_indicators_pd(df: pd.DataFrame) -> pd.DataFrame:
    """순수 pandas 기술지표 (analyzer.calculate_indicators와 동일 계열)."""
    out = df.copy()
    c, h, l = out["Close"], out["High"], out["Low"]
    out["MA20"] = c.rolling(20).mean()
    out["MA60"] = c.rolling(60).mean()
    out["MA200"] = c.rolling(200).mean()
    out["RSI"] = _rsi(c, 14)

    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    out["MACD"] = ema12 - ema26
    out["MACD_Signal"] = out["MACD"].ewm(span=9, adjust=False).mean()
    out["MACD_Hist"] = out["MACD"] - out["MACD_Signal"]

    mid = c.rolling(20).mean()
    std = c.rolling(20).std()
    out["BB_Mid"] = mid
    out["BB_Upper"] = mid + 2 * std
    out["BB_Lower"] = mid - 2 * std

    low14 = l.rolling(14).min()
    high14 = h.rolling(14).max()
    raw_k = 100 * (c - low14) / (high14 - low14).replace(0, np.nan)
    out["Stoch_K"] = raw_k.rolling(3).mean()
    out["Stoch_D"] = out["Stoch_K"].rolling(3).mean()
    out["ATR"] = _atr(h, l, c, 14)
    return out


def get_stock_history(ticker: str, period: str = "2y") -> pd.DataFrame:
    last_err = None
    for attempt in range(3):
        try:
            df = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=True)
            if df is not None and not df.empty:
                df = df.copy()
                df.index = pd.to_datetime(df.index).tz_localize(None)
                return df
        except Exception as e:
            last_err = e
        if attempt < 2:
            time.sleep(0.6 * (attempt + 1))
    if last_err:
        print(f"[features] history fail {ticker}: {last_err}")
    return pd.DataFrame()


def _ret_n(closes: pd.Series, n: int) -> Optional[float]:
    if len(closes) < n + 1:
        return None
    return _pct(float(closes.iloc[-1]), float(closes.iloc[-(n + 1)]))


def _bench_ret_aligned(bench: pd.DataFrame, asof: pd.Timestamp, n: int) -> Optional[float]:
    if bench.empty or "Close" not in bench.columns:
        return None
    hist = bench[bench.index <= asof]
    if len(hist) < n + 1:
        return None
    return _pct(float(hist["Close"].iloc[-1]), float(hist["Close"].iloc[-(n + 1)]))


def classify_market_regime(spy: pd.DataFrame, asof: pd.Timestamp) -> dict:
    out = {
        "regime": "UNKNOWN",
        "spy_close": None,
        "spy_ma200": None,
        "spy_vs_ma200": None,
        "spy_dd_from_high": None,
    }
    if spy.empty or "Close" not in spy.columns:
        return out
    hist = spy[spy.index <= asof].copy()
    if len(hist) < 50:
        return out
    close = hist["Close"].astype(float)
    ma200 = close.rolling(200).mean()
    c = float(close.iloc[-1])
    m = float(ma200.iloc[-1]) if not math.isnan(ma200.iloc[-1]) else None
    high_252 = float(close.tail(min(252, len(close))).max())
    dd = _pct(c, high_252) if high_252 else None

    out["spy_close"] = _safe(c, 4)
    out["spy_ma200"] = _safe(m, 4) if m is not None else None
    out["spy_vs_ma200"] = _pct(c, m) if m else None
    out["spy_dd_from_high"] = dd

    if m is None or dd is None:
        out["regime"] = "UNKNOWN"
    elif c > m and dd > -0.10:
        out["regime"] = "BULL"
    elif c < m and dd <= -0.20:
        out["regime"] = "BEAR"
    else:
        out["regime"] = "SIDEWAYS"
    return out


def extract_features_from_df(
    df: pd.DataFrame,
    ticker: str,
    *,
    asof: Optional[str] = None,
    sector: Optional[str] = None,
    news: Optional[list] = None,
    valuation: Optional[dict] = None,
    analysis_id: Optional[str] = None,
    signal: Optional[str] = None,
) -> dict:
    """지표 DF(또는 OHLCV)의 asof 봉 기준 feature 스냅샷."""
    if df is None or df.empty:
        raise ValueError("empty dataframe")

    work = df.copy()
    work.index = pd.to_datetime(work.index).tz_localize(None)
    if asof:
        asof_ts = pd.Timestamp(str(asof)[:10])
        work = work[work.index <= asof_ts]
        if work.empty:
            raise ValueError(f"no bars on/before {asof}")
    else:
        asof_ts = pd.Timestamp(work.index[-1].date())

    need = {"MA20", "RSI", "MACD", "ATR"}
    if not need.issubset(set(work.columns)):
        work = calculate_indicators_pd(work)

    latest = work.iloc[-1]
    close = float(latest["Close"])
    high = float(latest["High"])
    low = float(latest["Low"])
    open_ = float(latest["Open"]) if "Open" in work.columns else close
    volume = float(latest["Volume"]) if "Volume" in work.columns else 0.0

    def _f(col):
        if col not in work.columns or pd.isna(latest.get(col)):
            return None
        return float(latest[col])

    ma20, ma60, ma200 = _f("MA20"), _f("MA60"), _f("MA200")
    rsi, macd, macd_sig, macd_hist = _f("RSI"), _f("MACD"), _f("MACD_Signal"), _f("MACD_Hist")
    atr, stoch_k, stoch_d = _f("ATR"), _f("Stoch_K"), _f("Stoch_D")
    bb_u, bb_l = _f("BB_Upper"), _f("BB_Lower")

    bb_width = bb_pos = None
    if bb_u is not None and bb_l is not None and bb_u > bb_l:
        bb_width = _pct(bb_u - bb_l, close)
        bb_pos = _safe((close - bb_l) / (bb_u - bb_l), 4)

    closes = work["Close"].astype(float)
    rets_1 = closes.pct_change().dropna()
    vol_20 = float(rets_1.tail(20).std() * math.sqrt(252)) if len(rets_1) >= 20 else None
    vol_60 = float(rets_1.tail(60).std() * math.sqrt(252)) if len(rets_1) >= 60 else None

    avg_vol_20 = float(work["Volume"].tail(20).mean()) if "Volume" in work.columns else None
    vol_ratio = _safe(volume / avg_vol_20, 4) if avg_vol_20 and avg_vol_20 > 0 else None

    gap_risk = None
    if len(work) >= 2 and "Open" in work.columns:
        gap_risk = _pct(open_, float(work["Close"].iloc[-2]))

    start = (asof_ts - timedelta(days=400)).strftime("%Y-%m-%d")
    end = (asof_ts + timedelta(days=5)).strftime("%Y-%m-%d")
    spy = _download_ohlc("SPY", start, end)
    qqq = _download_ohlc("QQQ", start, end)

    sector_name = sector or (valuation or {}).get("sector") or ""
    sector_etf = sector_to_etf(sector_name)
    sect = _download_ohlc(sector_etf, start, end) if sector_etf else pd.DataFrame()

    rs = {}
    for n in (5, 10, 20):
        stock_r = _ret_n(closes, n)
        spy_r = _bench_ret_aligned(spy, asof_ts, n)
        qqq_r = _bench_ret_aligned(qqq, asof_ts, n)
        sec_r = _bench_ret_aligned(sect, asof_ts, n) if not sect.empty else None
        rs[f"ret_{n}d"] = stock_r
        rs[f"vs_spy_{n}d"] = _safe(stock_r - spy_r, 6) if stock_r is not None and spy_r is not None else None
        rs[f"vs_qqq_{n}d"] = _safe(stock_r - qqq_r, 6) if stock_r is not None and qqq_r is not None else None
        rs[f"vs_sector_{n}d"] = _safe(stock_r - sec_r, 6) if stock_r is not None and sec_r is not None else None

    hist_prev = None
    if macd_hist is not None and "MACD_Hist" in work.columns and len(work) >= 2:
        prev = work["MACD_Hist"].iloc[-2]
        if pd.notna(prev):
            hist_prev = float(prev)

    features = {
        "_id": analysis_id,
        "analysis_id": analysis_id,
        "ticker": ticker.upper(),
        "asof": str(asof_ts.date()),
        "signal": signal,
        "features_version": FEATURES_VERSION,
        "created_at": datetime.utcnow().isoformat(),
        "price": {
            "open": _safe(open_, 4),
            "high": _safe(high, 4),
            "low": _safe(low, 4),
            "close": _safe(close, 4),
            "volume": _safe(volume, 0),
        },
        "trend": {
            "ma20": _safe(ma20, 4),
            "ma60": _safe(ma60, 4),
            "ma200": _safe(ma200, 4),
            "price_vs_ma20": _pct(close, ma20) if ma20 else None,
            "price_vs_ma60": _pct(close, ma60) if ma60 else None,
            "price_vs_ma200": _pct(close, ma200) if ma200 else None,
            "ma20_vs_ma60": _pct(ma20, ma60) if ma20 and ma60 else None,
            "above_ma20": bool(ma20 is not None and close > ma20),
            "above_ma60": bool(ma60 is not None and close > ma60),
            "above_ma200": bool(ma200 is not None and close > ma200),
        },
        "momentum": {
            "rsi": _safe(rsi, 2),
            "macd": _safe(macd, 6),
            "macd_signal": _safe(macd_sig, 6),
            "macd_hist": _safe(macd_hist, 6),
            "macd_above_signal": bool(macd is not None and macd_sig is not None and macd > macd_sig),
            "macd_above_zero": bool(macd is not None and macd > 0),
            "macd_hist_rising": bool(
                macd_hist is not None and hist_prev is not None and macd_hist > hist_prev
            ),
            "stoch_k": _safe(stoch_k, 2),
            "stoch_d": _safe(stoch_d, 2),
        },
        "volatility": {
            "atr": _safe(atr, 4),
            "atr_pct": _pct(atr, close) if atr else None,
            "vol_20d": _safe(vol_20, 6),
            "vol_60d": _safe(vol_60, 6),
            "bb_width": bb_width,
            "bb_position": bb_pos,
            "gap_risk": gap_risk,
        },
        "volume": {
            "volume": _safe(volume, 0),
            "avg_volume_20": _safe(avg_vol_20, 0) if avg_vol_20 is not None else None,
            "volume_ratio": vol_ratio,
        },
        "returns": {
            "ret_1d": _ret_n(closes, 1),
            "ret_5d": rs.get("ret_5d"),
            "ret_10d": rs.get("ret_10d"),
            "ret_20d": rs.get("ret_20d"),
        },
        "relative_strength": {
            "sector": sector_name or None,
            "sector_etf": sector_etf,
            "vs_spy_5d": rs.get("vs_spy_5d"),
            "vs_spy_10d": rs.get("vs_spy_10d"),
            "vs_spy_20d": rs.get("vs_spy_20d"),
            "vs_qqq_5d": rs.get("vs_qqq_5d"),
            "vs_qqq_10d": rs.get("vs_qqq_10d"),
            "vs_qqq_20d": rs.get("vs_qqq_20d"),
            "vs_sector_5d": rs.get("vs_sector_5d"),
            "vs_sector_10d": rs.get("vs_sector_10d"),
            "vs_sector_20d": rs.get("vs_sector_20d"),
        },
        "market_regime": classify_market_regime(spy, asof_ts),
        "news": {
            "count": len(news or []),
            "has_news": bool(news),
        },
        "valuation": {
            "sector": sector_name or None,
            "trailing_pe": _safe((valuation or {}).get("per") or (valuation or {}).get("trailing_pe"), 2),
            "forward_pe": _safe((valuation or {}).get("forward_per") or (valuation or {}).get("forward_pe"), 2),
            "peg": _safe((valuation or {}).get("peg"), 2),
        } if valuation or sector_name else {},
    }
    if not analysis_id:
        features.pop("_id", None)
    return features


def build_features_for_ticker(
    ticker: str,
    *,
    period: str = "2y",
    asof: Optional[str] = None,
    sector: Optional[str] = None,
    news: Optional[list] = None,
    valuation: Optional[dict] = None,
    analysis_id: Optional[str] = None,
    signal: Optional[str] = None,
) -> dict:
    df = get_stock_history(ticker, period=period)
    if df.empty:
        raise ValueError(f"no price data for {ticker}")
    df = calculate_indicators_pd(df)
    return extract_features_from_df(
        df, ticker,
        asof=asof, sector=sector, news=news, valuation=valuation,
        analysis_id=analysis_id, signal=signal,
    )


def features_from_analysis_doc(doc: dict) -> dict:
    ticker = doc.get("ticker", "")
    analysis_id = doc.get("_id")
    if isinstance(analysis_id, dict):
        analysis_id = analysis_id.get("$oid", str(analysis_id))
    else:
        analysis_id = str(analysis_id) if analysis_id else None

    asof = doc.get("data_date")
    if not asof:
        created = doc.get("created_at")
        if isinstance(created, dict):
            created = created.get("$date")
        asof = str(created)[:10] if created else None

    valuation = doc.get("valuation") or {}
    return build_features_for_ticker(
        ticker,
        period="2y",
        asof=asof,
        sector=valuation.get("sector"),
        news=doc.get("news") or [],
        valuation=valuation,
        analysis_id=analysis_id,
        signal=doc.get("signal"),
    )
