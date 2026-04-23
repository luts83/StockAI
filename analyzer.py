import time

import yfinance as yf
import pandas as pd
import pandas_ta as ta
import numpy as np

def _pick_col(df: pd.DataFrame, prefix: str) -> str:
    """pandas-ta 버전에 따라 달라질 수 있는 컬럼명을 prefix로 선택"""
    for col in df.columns:
        if col.startswith(prefix):
            return col
    raise KeyError(f"지표 컬럼을 찾을 수 없습니다: {prefix}")

def _last_valid(series: pd.Series, default: float = 0.0) -> float:
    """Series의 마지막 유효값을 반환하고 없으면 기본값 사용"""
    valid = series.dropna()
    if valid.empty:
        return default
    return float(valid.iloc[-1])

def get_stock_data(ticker: str, period: str = "6mo", interval: str = "1d") -> pd.DataFrame:
    """yfinance로 OHLCV 데이터 수집 (Yahoo 일시 오류 시 짧게 재시도)"""
    last_err = None
    for attempt in range(3):
        try:
            stock = yf.Ticker(ticker)
            df = stock.history(period=period, interval=interval)
            if df is not None and not df.empty:
                df.index = pd.to_datetime(df.index)
                return df
        except Exception as e:
            last_err = e
        if attempt < 2:
            time.sleep(0.8 * (attempt + 1))
    if last_err:
        print(f"데이터 수집 오류 ({ticker}): {last_err}")
    return None

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """기술적 지표 계산"""
    # 기본 컬럼을 먼저 만들어 두면 지표 계산 실패 시에도 후속 로직이 안정적으로 동작한다.
    for col in [
        "MA20", "MA60", "MA200", "RSI",
        "MACD", "MACD_Signal", "MACD_Hist",
        "BB_Upper", "BB_Lower", "BB_Mid",
        "Stoch_K", "Stoch_D", "ATR",
    ]:
        if col not in df.columns:
            df[col] = np.nan

    # 이동평균
    df["MA20"]  = ta.sma(df["Close"], length=20)
    df["MA60"]  = ta.sma(df["Close"], length=60)
    df["MA200"] = ta.sma(df["Close"], length=200)

    # RSI
    df["RSI"] = ta.rsi(df["Close"], length=14)

    # MACD
    macd = ta.macd(df["Close"], fast=12, slow=26, signal=9)
    if macd is not None and not macd.empty:
        df["MACD"]        = macd["MACD_12_26_9"]
        df["MACD_Signal"] = macd["MACDs_12_26_9"]
        df["MACD_Hist"]   = macd["MACDh_12_26_9"]

    # 볼린저밴드
    bb = ta.bbands(df["Close"], length=20)
    if bb is not None and not bb.empty:
        bb_upper_col = _pick_col(bb, "BBU_20_2.0")
        bb_lower_col = _pick_col(bb, "BBL_20_2.0")
        bb_mid_col   = _pick_col(bb, "BBM_20_2.0")
        df["BB_Upper"] = bb[bb_upper_col]
        df["BB_Lower"] = bb[bb_lower_col]
        df["BB_Mid"]   = bb[bb_mid_col]

    # 스토캐스틱
    stoch = ta.stoch(df["High"], df["Low"], df["Close"])
    if stoch is not None and not stoch.empty:
        df["Stoch_K"] = stoch["STOCHk_14_3_3"]
        df["Stoch_D"] = stoch["STOCHd_14_3_3"]

    # ATR (변동성)
    df["ATR"] = ta.atr(df["High"], df["Low"], df["Close"], length=14)

    return df

def get_valuation_data(ticker: str) -> dict:
    """yfinance에서 밸류에이션 지표 수집 (ETF/개별주식 분리)"""
    try:
        info = yf.Ticker(ticker).info

        is_etf   = info.get("quoteType", "").upper() == "ETF"
        country  = info.get("country", "")
        exchange = info.get("exchange", "")
        is_foreign = (
            country not in ("", "United States") or
            ticker.endswith(".KS") or
            ticker.endswith(".KQ") or
            exchange in ("ASX", "TSX", "LSE")
        )

        def _r(val, decimals=1):
            try:
                v = float(val or 0)
                return round(v, decimals) if v else 0
            except:
                return 0

        def _div_yield(info):
            # yfinance dividendYield는 소수(0.0105 = 1.05%) 형식
            # 값이 1.0 초과면 이미 % 단위로 잘못 들어온 것 → ×100 생략
            val = info.get("dividendYield") or info.get("trailingAnnualDividendYield") or 0
            try:
                v = float(val)
                if v == 0:
                    return 0
                if v > 1.0:
                    return round(v, 4)   # 이미 % 단위 그대로
                return round(v * 100, 2)  # 소수 → % 변환
            except:
                return 0

        base = {
            "is_etf":         is_etf,
            "is_foreign":     is_foreign,
            "per":            _r(info.get("trailingPE"), 1),
            "dividend_yield": _div_yield(info),
            "market_cap":     info.get("marketCap"),
        }

        if is_etf:
            # ETF: PBR/PSR/EPS/매출성장/이익률은 의미 없음 → 0으로 명시
            return {
                **base,
                "forward_per":    0,
                "pbr":            0,
                "psr":            0,
                "eps":            0,
                "revenue_growth": 0,
                "profit_margin":  0,
                "sector":         info.get("category", ""),  # ETF는 category 필드
            }
        else:
            # 개별 주식: 전체 지표 수집
            return {
                **base,
                "forward_per":    _r(info.get("forwardPE"), 1),
                "pbr":            _r(info.get("priceToBook"), 2),
                "psr":            _r(info.get("priceToSalesTrailing12Months"), 2),
                "eps":            _r(info.get("trailingEps"), 2),
                "revenue_growth": _r((info.get("revenueGrowth") or 0) * 100, 1),
                "profit_margin":  _r((info.get("profitMargins") or 0) * 100, 1),
                "sector":         info.get("sector", ""),
            }
    except Exception as e:
        print(f"[valuation] {ticker} 데이터 수집 실패: {e}")
        return {}


def get_summary_stats(df: pd.DataFrame, ticker: str = "") -> dict:
    """분석용 핵심 통계 추출"""
    latest = df.iloc[-1]

    ma20 = _last_valid(df["MA20"], float(latest["Close"]))
    ma200 = _last_valid(df["MA200"], float(latest["Close"]))
    bb_upper = _last_valid(df["BB_Upper"], float(latest["Close"]))
    bb_lower = _last_valid(df["BB_Lower"], float(latest["Close"]))
    bb_width = max(bb_upper - bb_lower, 1e-9)

    rsi = _last_valid(df["RSI"], 50.0)
    macd = _last_valid(df["MACD"], 0.0)
    macd_signal = _last_valid(df["MACD_Signal"], 0.0)
    stoch_k = _last_valid(df["Stoch_K"], 50.0)
    stoch_d = _last_valid(df["Stoch_D"], 50.0)
    current_price = float(latest["Close"])

    ma60_val  = _last_valid(df["MA60"],  0.0) if "MA60"  in df.columns else 0.0
    ma200_val = _last_valid(df["MA200"], 0.0) if "MA200" in df.columns else 0.0

    # 최근 N일 등락률 계산 (데이터 부족/0 나눗셈 방지)
    def _pct_change(days: int):
        if len(df) < days + 1:
            return None
        past_price = float(df["Close"].iloc[-(days + 1)])
        if past_price == 0:
            return None
        return round((current_price - past_price) / past_price * 100, 2)

    change_5d = _pct_change(5)
    change_20d = _pct_change(20)

    # S&P500 대비 초과 수익 (최근 20일)
    vs_spy = None
    if ticker.upper() != "SPY" and change_20d is not None:
        try:
            spy_df = yf.Ticker("SPY").history(period="1mo")
            if spy_df is not None and not spy_df.empty and len(spy_df) >= 21:
                spy_now = float(spy_df["Close"].iloc[-1])
                spy_past = float(spy_df["Close"].iloc[-21])
                if spy_past != 0:
                    spy_change = (spy_now - spy_past) / spy_past * 100
                    vs_spy = round(change_20d - spy_change, 2)
        except Exception as e:
            print(f"[summary_stats] SPY 비교 실패: {e}")

    return {
        "price":        round(float(latest["Close"]), 2),
        "volume":       int(latest["Volume"]),
        "avg_volume":   int(df["Volume"].tail(20).mean()),
        "52w_high":     round(float(df["High"].tail(252).max()), 2),
        "52w_low":      round(float(df["Low"].tail(252).min()), 2),
        "rsi":          round(rsi, 2),
        "macd":         round(macd, 4),
        "macd_signal":  round(macd_signal, 4),
        "above_ma20":   bool(float(latest["Close"]) > ma20),
        "above_ma200":  bool(float(latest["Close"]) > ma200),
        "bb_position":  round(float((float(latest["Close"]) - bb_lower) / bb_width * 100), 1),
        "stoch_k":      round(stoch_k, 2),
        "stoch_d":      round(stoch_d, 2),
        "ma20":         round(float(ma20), 2) if ma20 else None,
        "ma60":         round(float(ma60_val), 2) if ma60_val else None,
        "ma200":        round(float(ma200_val), 2) if ma200_val else None,
        "change_5d":    change_5d,
        "change_20d":   change_20d,
        "vs_spy":       vs_spy,
    }


def get_extended_price(ticker: str) -> dict:
    """프리/애프터마켓 포함 현재가 수집"""
    try:
        info           = yf.Ticker(ticker).fast_info
        regular_price  = round(float(info.last_price), 2)
        previous_close = round(float(info.previous_close), 2)

        extended_price = None
        try:
            df_1m = yf.Ticker(ticker).history(
                period="1d", interval="1m", prepost=True
            )
            if df_1m is not None and not df_1m.empty:
                extended_price = round(float(df_1m["Close"].iloc[-1]), 2)
        except Exception:
            pass

        gap_pct = None
        if extended_price and regular_price:
            gap_pct = round(
                (extended_price - regular_price) / regular_price * 100, 2
            )

        return {
            "regular_price":  regular_price,
            "extended_price": extended_price,
            "previous_close": previous_close,
            "has_gap":        bool(gap_pct and abs(gap_pct) >= 1.0),
            "gap_pct":        gap_pct,
        }
    except Exception as e:
        print(f"[extended_price] {ticker} 오류: {e}")
        return {}


def get_earnings_context(ticker: str) -> dict:
    """yfinance로 실적발표 컨텍스트 수집"""
    from datetime import datetime
    result = {}
    try:
        stock = yf.Ticker(ticker)

        # 다음 실적발표일
        try:
            cal = stock.calendar
            if cal is not None and not cal.empty and "Earnings Date" in cal.index:
                next_date = cal.loc["Earnings Date"].iloc[0]
                if hasattr(next_date, "date"):
                    next_date = next_date.date()
                today     = datetime.now().date()
                days_diff = (next_date - today).days
                result["next_earnings_date"] = str(next_date)
                result["days_to_earnings"]   = days_diff
                result["is_earnings_week"]   = abs(days_diff) <= 3
        except Exception as e:
            print(f"[earnings] {ticker} 캘린더 오류: {e}")

        # 최근 EPS 서프라이즈
        try:
            ed = stock.earnings_dates
            if ed is not None and not ed.empty:
                now_utc = pd.Timestamp.now(tz="UTC")
                past = ed[ed.index <= now_utc].dropna(
                    subset=["Reported EPS", "EPS Estimate"], how="all"
                )
                if not past.empty:
                    row      = past.iloc[0]
                    actual   = row.get("Reported EPS")
                    estimate = row.get("EPS Estimate")
                    surprise_pct = None
                    if actual is not None and estimate is not None and float(estimate) != 0:
                        surprise_pct = round(
                            (float(actual) - float(estimate)) / abs(float(estimate)) * 100, 1
                        )
                    result["recent_earnings"] = {
                        "date":         past.index[0].strftime("%Y-%m-%d"),
                        "actual_eps":   float(actual)   if actual   is not None else None,
                        "estimate_eps": float(estimate) if estimate is not None else None,
                        "surprise_pct": surprise_pct,
                    }
        except Exception as e:
            print(f"[earnings] {ticker} EPS 오류: {e}")

        # 최근 분기 재무제표
        try:
            qs = stock.quarterly_income_stmt
            if qs is not None and not qs.empty:
                col = qs.columns[0]

                def _b(key):
                    if key in qs.index:
                        v = qs.loc[key, col]
                        if v is not None and v == v:
                            return round(float(v) / 1e9, 2)
                    return None

                result["recent_financials"] = {
                    "quarter":      str(col)[:10],
                    "revenue_b":    _b("Total Revenue"),
                    "net_income_b": _b("Net Income"),
                    "op_income_b":  _b("Operating Income"),
                }
        except Exception as e:
            print(f"[earnings] {ticker} 재무제표 오류: {e}")

    except Exception as e:
        print(f"[earnings] {ticker} 전체 오류: {e}")

    return result
