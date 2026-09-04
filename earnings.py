"""실적 DB — yfinance는 주기적 동기화만, 리포트는 MongoDB 이력에서 조립."""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from typing import Any

import feedparser
import yfinance as yf

from database import (
    count_earnings_history,
    delete_earnings_record,
    get_earnings_record,
    get_ticker_earnings,
    list_earnings_history,
    list_earnings_tickers_with_suspect_records,
    upsert_earnings_record,
    upsert_ticker_earnings,
)

# yfinance 재조회 간격 (시간). 리포트마다 외부 호출하지 않음.
SYNC_HOURS = int(os.getenv("EARNINGS_SYNC_HOURS", "48"))

# UI에서 '방금 나온 실적'을 크게 강조하는 기간(일). DB 보관과 무관.
UI_HIGHLIGHT_RESULT_DAYS = int(os.getenv("EARNINGS_UI_HIGHLIGHT_DAYS", "14"))
UI_HIGHLIGHT_CALL_DAYS = int(os.getenv("EARNINGS_CALL_UI_HIGHLIGHT_DAYS", "21"))


def _parse_date(value) -> date | None:
    if value is None:
        return None
    try:
        if hasattr(value, "date"):
            return value.date()
        if isinstance(value, str):
            return datetime.fromisoformat(value[:10]).date()
    except (TypeError, ValueError):
        pass
    return None


def _days_since(date_str: str | None, today: date | None = None) -> int | None:
    d = _parse_date(date_str)
    if d is None:
        return None
    return ((today or date.today()) - d).days


def _safe_float(v) -> float | None:
    try:
        if v is None or v != v:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_finite(v) -> bool:
    try:
        x = float(v)
        return x == x and abs(x) != float("inf")
    except (TypeError, ValueError):
        return False


def _fmt_revenue(v) -> str:
    x = _safe_float(v)
    if x is None:
        return "—"
    if abs(x) >= 1e9:
        return f"${x / 1e9:.2f}B"
    if abs(x) >= 1e6:
        return f"${x / 1e6:.1f}M"
    if abs(x) >= 1e3:
        return f"${x / 1e3:.1f}K"
    return f"${x:.2f}"


INCOMPLETE_EPS_RETRY_HOURS = 6


def _surprise_pct(actual, estimate) -> float | None:
    a, e = _safe_float(actual), _safe_float(estimate)
    if a is None or e is None or e == 0:
        return None
    return round((a - e) / abs(e) * 100, 1)


def _pct_change(new, old) -> float | None:
    n, o = _safe_float(new), _safe_float(old)
    if n is None or o is None or o == 0:
        return None
    return round((n - o) / abs(o) * 100, 1)


def _rev_billions(v) -> float | None:
    x = _safe_float(v)
    if x is None:
        return None
    return round(x / 1e9, 2) if x > 1e6 else round(x, 2)


def _yf_frame(obj):
    import pandas as pd

    if obj is None:
        return None
    if isinstance(obj, pd.DataFrame):
        return obj if not obj.empty else None
    if isinstance(obj, dict):
        if not obj:
            return None
        try:
            df = pd.DataFrame(obj)
            return df if not df.empty else None
        except Exception:
            return None
    return None


def _is_etf_like(ticker: str, stock=None) -> bool:
    """ETF/지수 여부 — analyzer import 없이 판별 (pandas_ta 부작용 방지)."""
    t = (ticker or "").upper()
    if t.startswith("^"):
        return True
    known = {
        "SPY", "QQQ", "IWM", "DIA", "RSP", "SMH", "XLK", "XLF", "XLE", "XLV",
        "TQQQ", "SQQQ", "SPCX", "VOO", "VTI", "ARKK",
    }
    if t in known:
        return True
    try:
        if stock is not None:
            info = getattr(stock, "info", None) or {}
            qt = str(info.get("quoteType") or info.get("typeDisp") or "").upper()
            if qt in ("ETF", "MUTUALFUND", "INDEX", "CURRENCY"):
                return True
    except Exception:
        pass
    return False


def _migrate_legacy_profile(profile: dict) -> None:
    """구형 ticker_earnings(단일 last_earnings) → earnings_history 이전."""
    if not profile:
        return
    ticker = (profile.get("ticker") or profile.get("_id") or "").upper()
    last = profile.get("last_earnings") or {}
    if not ticker or not last.get("date"):
        return
    rec = dict(last)
    rec["ticker"] = ticker
    if profile.get("last_financials"):
        rec["financials"] = profile["last_financials"]
    if profile.get("earnings_call"):
        rec["earnings_call"] = profile["earnings_call"]
    upsert_earnings_record(rec)


def _eps_close(a, b, tol: float = 0.03) -> bool:
    fa, fb = _safe_float(a), _safe_float(b)
    if fa is None or fb is None:
        return False
    return abs(fa - fb) <= tol


def _ts_to_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and value > 1e9:
        try:
            return datetime.utcfromtimestamp(value).date()
        except (TypeError, ValueError, OSError):
            return None
    return _parse_date(value)


def _period_end_label(period_end: str | None) -> str:
    d = _parse_date(period_end)
    if d is None:
        return ""
    return f"{d.year}년 {d.month}월 마감 분기"


def _format_earnings_date_label(rec: dict) -> str:
    """UI·프롬프트용 — 발표일과 회계분기 마감을 구분."""
    announce = rec.get("date") or ""
    period_end = rec.get("period_end") or ""
    if period_end and period_end[:10] != announce[:10]:
        return f"발표 {announce} · {_period_end_label(period_end)}"
    return f"발표 {announce}"


def _fetch_fiscal_quarter_rows(stock, ticker: str) -> list[dict]:
    """yfinance earnings_history — 회계분기 마감일 기준 EPS."""
    eh = _yf_frame(getattr(stock, "earnings_history", None))
    if eh is None:
        return []
    rows: list[dict] = []
    for ts, row in eh.iterrows():
        period_end = _parse_date(ts)
        if period_end is None:
            continue
        actual = row.get("epsActual") if "epsActual" in eh.columns else None
        est = row.get("epsEstimate") if "epsEstimate" in eh.columns else None
        sp = row.get("surprisePercent") if "surprisePercent" in eh.columns else None
        sp_pct = round(float(sp) * 100, 1) if sp is not None and sp == sp else None
        rows.append({
            "ticker": ticker,
            "period_end": period_end.isoformat(),
            "actual_eps": _safe_float(actual),
            "estimate_eps": _safe_float(est),
            "eps_surprise_pct": sp_pct if sp_pct is not None else _surprise_pct(actual, est),
            "source": "yfinance_fiscal",
        })
    rows.sort(key=lambda r: r["period_end"], reverse=True)
    return rows


def _fetch_announcement_rows(stock, ticker: str, limit: int = 16) -> list[dict]:
    """yfinance earnings_dates — 실적 발표일(시장 기준) 기준 EPS."""
    import pandas as pd

    ed = _yf_frame(stock.earnings_dates)
    if ed is None:
        return []
    now_utc = pd.Timestamp.now(tz="UTC")
    if getattr(ed.index, "tz", None) is None:
        try:
            ed = ed.copy()
            ed.index = pd.to_datetime(ed.index).tz_localize("UTC")
        except Exception:
            ed.index = pd.to_datetime(ed.index, utc=True)
    past = ed[ed.index <= now_utc].head(limit)
    rows: list[dict] = []
    for ts, row in past.iterrows():
        actual_eps = row.get("Reported EPS") if "Reported EPS" in past.columns else None
        est_eps = row.get("EPS Estimate") if "EPS Estimate" in past.columns else None
        actual_rev = row.get("Reported Revenue") if "Reported Revenue" in past.columns else None
        est_rev = row.get("Revenue Estimate") if "Revenue Estimate" in past.columns else None
        if _safe_float(actual_eps) is None and _safe_float(est_eps) is None:
            continue
        rows.append({
            "ticker": ticker,
            "announce_date": ts.strftime("%Y-%m-%d"),
            "actual_eps": _safe_float(actual_eps),
            "estimate_eps": _safe_float(est_eps),
            "eps_surprise_pct": _surprise_pct(actual_eps, est_eps),
            "actual_revenue": _safe_float(actual_rev),
            "estimate_revenue": _safe_float(est_rev),
            "revenue_surprise_pct": _surprise_pct(actual_rev, est_rev),
            "source": "yfinance_announce",
        })
    return rows


def _merge_fiscal_and_announcements(fiscal_rows: list[dict], announce_rows: list[dict]) -> list[dict]:
    """회계분기 EPS와 발표일 EPS를 매칭 — date=발표일, period_end=분기마감."""
    used_ann: set[int] = set()
    merged: list[dict] = []

    for f in fiscal_rows:
        rec = dict(f)
        match_i = None
        for i, a in enumerate(announce_rows):
            if i in used_ann:
                continue
            if _eps_close(f.get("actual_eps"), a.get("actual_eps")):
                match_i = i
                break
        if match_i is not None:
            used_ann.add(match_i)
            a = announce_rows[match_i]
            rec.update({
                "date": a["announce_date"],
                "actual_revenue": a.get("actual_revenue"),
                "estimate_revenue": a.get("estimate_revenue"),
                "revenue_surprise_pct": a.get("revenue_surprise_pct"),
                "source": "yfinance_merged",
            })
            if rec.get("estimate_eps") is None:
                rec["estimate_eps"] = a.get("estimate_eps")
            if rec.get("eps_surprise_pct") is None:
                rec["eps_surprise_pct"] = a.get("eps_surprise_pct")
        else:
            rec["date"] = f["period_end"]
            rec["date_note"] = "발표일 미확인 — 분기 마감일 기준"
        merged.append(rec)

    known_dates = {m.get("date") for m in merged}
    for i, a in enumerate(announce_rows):
        if i in used_ann:
            continue
        if a["announce_date"] in known_dates:
            continue
        merged.append({
            "ticker": a["ticker"],
            "date": a["announce_date"],
            "period_end": None,
            "actual_eps": a.get("actual_eps"),
            "estimate_eps": a.get("estimate_eps"),
            "eps_surprise_pct": a.get("eps_surprise_pct"),
            "actual_revenue": a.get("actual_revenue"),
            "estimate_revenue": a.get("estimate_revenue"),
            "revenue_surprise_pct": a.get("revenue_surprise_pct"),
            "source": "yfinance_announce",
        })
        known_dates.add(a["announce_date"])

    merged.sort(key=lambda r: r.get("date") or "", reverse=True)
    return merged[:12]


def _eps_from_income_stmt(stock, period_end: date) -> float | None:
    qs = _yf_frame(stock.quarterly_income_stmt)
    if qs is None:
        return None
    target = period_end.isoformat()
    for col in qs.columns:
        col_date = _parse_date(col)
        if col_date is None:
            continue
        if col_date.isoformat() == target or str(col)[:10] == target:
            for key in ("Diluted EPS", "Basic EPS"):
                if key in qs.index:
                    v = _safe_float(qs.loc[key, col])
                    if v is not None:
                        return round(v, 4)
    return None


def _read_info_announce_meta(stock) -> tuple[date | None, date | None]:
    """yfinance Ticker.info — 최근 실적 발표일·회계분기 마감."""
    try:
        info = stock.info or {}
    except Exception:
        return None, None
    announce = _ts_to_date(info.get("earningsTimestamp"))
    period_end = _ts_to_date(info.get("mostRecentQuarter"))
    if announce is not None and announce > date.today():
        return None, None
    return announce, period_end


def _fiscal_eps_for_period(stock, ticker: str, period_end: date) -> tuple[float | None, float | None, float | None]:
    """분기 EPS·예상·서프라이즈 — earnings_history → income_stmt 순."""
    period_key = period_end.isoformat()
    actual = est = sp = None
    for f in _fetch_fiscal_quarter_rows(stock, ticker):
        if f.get("period_end") == period_key:
            actual = f.get("actual_eps")
            est = f.get("estimate_eps")
            sp = f.get("eps_surprise_pct")
            break
    if actual is None:
        actual = _eps_from_income_stmt(stock, period_end)
    if sp is None:
        sp = _surprise_pct(actual, est)
    return actual, est, sp


def _apply_latest_from_info(stock, ticker: str, history: list[dict]) -> list[dict]:
    """earnings_dates 지연 시 info.earningsTimestamp로 발표일 보정·보강.

    yfinance earnings_history는 분기 마감(period_end)만 먼저 올라오고
    earnings_dates에 발표일(예: 8/27)이 늦게 반영되는 경우가 있어,
    date=period_end 로 잘못 저장된 행을 발표일로 덮어쓴다.
    """
    announce, period_end = _read_info_announce_meta(stock)
    if announce is None or period_end is None:
        return history

    period_key = period_end.isoformat()
    announce_key = announce.isoformat()
    timing = _info_earnings_timing(stock)

    for h in history:
        h_ann = _parse_date(h.get("date"))
        if h_ann and h.get("period_end") == period_key and h_ann == announce:
            if timing and not h.get("announce_timing"):
                h["announce_timing"] = timing
            return history
        if h_ann and abs((h_ann - announce).days) <= 2 and h.get("period_end") in (period_key, None):
            if timing and not h.get("announce_timing"):
                h["announce_timing"] = timing
            return history

    idx = None
    for i, h in enumerate(history):
        pe = h.get("period_end")
        d = h.get("date")
        if pe == period_key or d == period_key:
            idx = i
            break

    actual, est, sp = _fiscal_eps_for_period(stock, ticker, period_end)

    if idx is not None:
        rec = dict(history[idx])
        old_date = rec.get("date")
        had_wrong_note = str(rec.get("date_note") or "").startswith("발표일 미확인")
        rec["date"] = announce_key
        rec["period_end"] = period_key
        rec["ticker"] = ticker
        rec["source"] = "yfinance_info"
        if timing:
            rec["announce_timing"] = timing
        if actual is not None and rec.get("actual_eps") is None:
            rec["actual_eps"] = actual
        if est is not None and rec.get("estimate_eps") is None:
            rec["estimate_eps"] = est
        if sp is not None and rec.get("eps_surprise_pct") is None:
            rec["eps_surprise_pct"] = sp
        elif rec.get("eps_surprise_pct") is None:
            rec["eps_surprise_pct"] = _surprise_pct(rec.get("actual_eps"), rec.get("estimate_eps"))
        if old_date == period_key or had_wrong_note:
            rec.pop("date_note", None)
        if rec.get("actual_eps") is None:
            rec["date_note"] = "EPS·컨센서스 — yfinance 반영 대기 (발표일은 IR 기준)"
        history[idx] = rec
    else:
        rec = {
            "ticker": ticker,
            "date": announce_key,
            "period_end": period_key,
            "actual_eps": actual,
            "estimate_eps": est,
            "eps_surprise_pct": sp,
            "source": "yfinance_info",
        }
        if timing:
            rec["announce_timing"] = timing
        if actual is None:
            rec["date_note"] = "EPS·컨센서스 — yfinance 반영 대기 (발표일은 IR 기준)"
        history.append(rec)

    history.sort(key=lambda r: r.get("date") or "", reverse=True)
    deduped: list[dict] = []
    seen_period: set[str] = set()
    for h in history:
        pe = h.get("period_end") or h.get("date")
        if pe in seen_period:
            continue
        seen_period.add(pe)
        deduped.append(h)
    return deduped[:12]


# 하위 호환 별칭
def _supplement_recent_from_info(stock, ticker: str, history: list[dict]) -> list[dict]:
    return _apply_latest_from_info(stock, ticker, history)


def _timing_from_timestamp(ts) -> str | None:
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return None
    if ts < 1e9:
        return None
    try:
        from zoneinfo import ZoneInfo

        dt = datetime.fromtimestamp(ts, tz=ZoneInfo("America/New_York"))
    except Exception:
        return None
    return "bmo" if dt.hour < 12 else "amc"


def _info_earnings_timing(stock) -> str | None:
    try:
        info = stock.info or {}
    except Exception:
        return None
    return _timing_from_timestamp(info.get("earningsTimestamp"))


def _parse_mdy(value) -> date | None:
    if value is None:
        return None
    s = str(value).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return _parse_date(s)


def _nasdaq_period_end(label: str | None) -> date | None:
    """'Jun 2026' / '6/30/2026' → 분기 마감일."""
    if not label:
        return None
    s = str(label).strip()
    parsed = _parse_mdy(s)
    if parsed:
        return parsed
    parts = s.replace(",", " ").split()
    if len(parts) < 2:
        return None
    mon, year_s = parts[0][:3].title(), parts[-1]
    try:
        year = int(year_s)
    except ValueError:
        return None
    month_end = {
        "Jan": (1, 31), "Feb": (2, 28), "Mar": (3, 31), "Apr": (4, 30),
        "May": (5, 31), "Jun": (6, 30), "Jul": (7, 31), "Aug": (8, 31),
        "Sep": (9, 30), "Oct": (10, 31), "Nov": (11, 30), "Dec": (12, 31),
    }
    pair = month_end.get(mon)
    if not pair:
        return None
    m, d = pair
    if m == 2 and year % 4 == 0:
        d = 29
    try:
        return date(year, m, d)
    except ValueError:
        return None


def _parse_nasdaq_number(value) -> float | None:
    if value is None or value in ("", "--", "N/A"):
        return None
    if isinstance(value, (int, float)):
        return _safe_float(value)
    s = str(value).strip().replace(",", "")
    s = s.replace("(", "-").replace(")", "")
    s = s.replace("$", "").replace("%", "").strip()
    return _safe_float(s)


def _scale_nasdaq_revenue(raw, peer: float | None) -> float | None:
    v = _parse_nasdaq_number(raw)
    if v is None:
        return None
    candidates = [v, v * 1_000, v * 1_000_000]
    if peer and peer > 0:
        import math

        def _dist(x):
            return abs(math.log10(abs(x) + 1) - math.log10(peer))

        return min(candidates, key=_dist)
    if abs(v) < 1_000_000:
        return v * 1_000
    return v


def _nasdaq_json(path: str) -> dict | None:
    import httpx

    url = f"https://api.nasdaq.com/api/{path.lstrip('/')}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nasdaq.com/",
    }
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True, headers=headers) as client:
            r = client.get(url)
            if r.status_code != 200:
                return None
            return r.json()
    except Exception as e:
        print(f"[earnings] nasdaq {path} 오류: {e}")
        return None


def fetch_nasdaq_earnings(ticker: str) -> list[dict]:
    """yfinance보다 빨리 올라오는 발표 EPS·매출 — Nasdaq earnings-surprise + financials."""
    t = (ticker or "").upper()
    if not t:
        return []
    surprise = _nasdaq_json(f"company/{t}/earnings-surprise") or {}
    financials = _nasdaq_json(f"company/{t}/financials?frequency=2") or {}
    rows_out: list[dict] = []

    rev_by_period: dict[str, float] = {}
    table = ((financials.get("data") or {}).get("incomeStatementTable") or {})
    headers = table.get("headers") or {}
    col_periods: dict[str, str] = {}
    for key, label in headers.items():
        if not str(key).startswith("value") or key == "value1":
            continue
        pe = _nasdaq_period_end(str(label))
        if pe:
            col_periods[key] = pe.isoformat()
    for row in table.get("rows") or []:
        name = str(row.get("value1") or "").strip().lower()
        if name != "total revenue":
            continue
        for col, pe in col_periods.items():
            scaled = _scale_nasdaq_revenue(row.get(col), None)
            if scaled is not None:
                rev_by_period[pe] = scaled
        break

    data = surprise.get("data") or {}
    table_s = data.get("earningsSurpriseTable") or {}
    for row in table_s.get("rows") or []:
        announced = _parse_mdy(row.get("dateReported"))
        period = _nasdaq_period_end(row.get("fiscalQtrEnd"))
        if announced is None and period is None:
            continue
        actual = _parse_nasdaq_number(row.get("eps"))
        est = _parse_nasdaq_number(row.get("consensusForecast"))
        sp = _parse_nasdaq_number(row.get("percentageSurprise"))
        if sp is None:
            sp = _surprise_pct(actual, est)
        pe_key = period.isoformat() if period else None
        rec = {
            "ticker": t,
            "date": (announced or period).isoformat(),
            "period_end": pe_key,
            "actual_eps": actual,
            "estimate_eps": est,
            "eps_surprise_pct": round(sp, 1) if sp is not None else None,
            "actual_revenue": rev_by_period.get(pe_key) if pe_key else None,
            "source": "nasdaq_surprise",
        }
        rows_out.append(rec)
    return rows_out


def _fill_from_nasdaq(rec: dict, nasdaq: dict) -> dict:
    out = dict(rec)
    if out.get("actual_eps") is None and nasdaq.get("actual_eps") is not None:
        out["actual_eps"] = nasdaq["actual_eps"]
        if nasdaq.get("estimate_eps") is not None:
            out["estimate_eps"] = nasdaq["estimate_eps"]
        if nasdaq.get("eps_surprise_pct") is not None:
            out["eps_surprise_pct"] = nasdaq["eps_surprise_pct"]
        src = out.get("source") or ""
        out["source"] = f"{src}+nasdaq" if src and "nasdaq" not in src else "nasdaq_surprise"
        note = str(out.get("date_note") or "")
        if "반영 대기" in note or "동기화 중" in note:
            out.pop("date_note", None)
    elif out.get("eps_surprise_pct") is None:
        out["eps_surprise_pct"] = _surprise_pct(out.get("actual_eps"), out.get("estimate_eps"))
    if out.get("actual_revenue") is None and nasdaq.get("actual_revenue") is not None:
        out["actual_revenue"] = nasdaq["actual_revenue"]
    if not out.get("period_end") and nasdaq.get("period_end"):
        out["period_end"] = nasdaq["period_end"]
    if out.get("eps_surprise_pct") is None:
        out["eps_surprise_pct"] = _surprise_pct(out.get("actual_eps"), out.get("estimate_eps"))
    return out


def apply_nasdaq_supplement(history: list[dict], nasdaq_rows: list[dict]) -> list[dict]:
    if not nasdaq_rows:
        return history
    by_date = {(r.get("date") or "")[:10]: r for r in nasdaq_rows}
    by_period = {(r.get("period_end") or "")[:10]: r for r in nasdaq_rows if r.get("period_end")}
    used_dates: set[str] = set()
    out: list[dict] = []
    for h in history:
        rec = dict(h)
        d = (rec.get("date") or "")[:10]
        pe = (rec.get("period_end") or "")[:10]
        n = by_date.get(d) or by_period.get(pe)
        if n:
            rec = _fill_from_nasdaq(rec, n)
            used_dates.add((n.get("date") or "")[:10])
        out.append(rec)
    known = {(h.get("date") or "")[:10] for h in out}
    known_pe = {(h.get("period_end") or "")[:10] for h in out}
    for n in nasdaq_rows:
        d = (n.get("date") or "")[:10]
        pe = (n.get("period_end") or "")[:10]
        if d in known or (pe and pe in known_pe):
            continue
        if n.get("actual_eps") is None:
            continue
        out.append(dict(n))
        known.add(d)
    out.sort(key=lambda r: r.get("date") or "", reverse=True)
    return out[:12]


def fetch_yfinance_earnings(ticker: str) -> dict:
    """yfinance에서 다음 실적일 + 과거 분기 이력(최대 12개) 수집."""
    result: dict[str, Any] = {"available": False, "ticker": (ticker or "").upper(), "history": []}
    t = result["ticker"]
    if not t:
        return result

    try:
        stock = yf.Ticker(t)
        if _is_etf_like(t, stock):
            result["skip_reason"] = "etf_or_index"
            return result

        today = date.today()

        try:
            cal_raw = stock.calendar
            next_date = None
            if isinstance(cal_raw, dict):
                ed = cal_raw.get("Earnings Date") or cal_raw.get("EarningsDate")
                if isinstance(ed, (list, tuple)) and ed:
                    next_date = ed[0]
                elif ed is not None and not isinstance(ed, (list, tuple)):
                    next_date = ed
            else:
                cal = _yf_frame(cal_raw)
                if cal is not None and "Earnings Date" in cal.index:
                    next_date = cal.loc["Earnings Date"]
                    if hasattr(next_date, "iloc"):
                        next_date = next_date.iloc[0]
            nd = _parse_date(next_date)
            if nd is not None:
                result["next_earnings_date"] = nd.isoformat()
                result["days_to_earnings"] = (nd - today).days
                result["is_earnings_week"] = abs(result["days_to_earnings"]) <= 3
                result["available"] = True
        except Exception as e:
            print(f"[earnings] {t} 캘린더 오류: {e}")

        latest_financials = None
        try:
            qs = _yf_frame(stock.quarterly_income_stmt)
            if qs is not None:
                col = qs.columns[0]

                def _b(key):
                    if key in qs.index:
                        v = qs.loc[key, col]
                        if v is not None and v == v:
                            return round(float(v) / 1e9, 2)
                    return None

                latest_financials = {
                    "quarter": str(col)[:10],
                    "revenue_b": _b("Total Revenue"),
                    "net_income_b": _b("Net Income"),
                    "op_income_b": _b("Operating Income"),
                }
                result["available"] = True
        except Exception as e:
            print(f"[earnings] {t} 재무제표 오류: {e}")

        try:
            fiscal_rows = _fetch_fiscal_quarter_rows(stock, t)
            announce_rows = _fetch_announcement_rows(stock, t)
            history = _merge_fiscal_and_announcements(fiscal_rows, announce_rows)
            history = _supplement_recent_from_info(stock, t, history)
            try:
                history = apply_nasdaq_supplement(history, fetch_nasdaq_earnings(t))
            except Exception as ne:
                print(f"[earnings] {t} Nasdaq 보강 오류: {ne}")
            if history:
                if latest_financials:
                    # income stmt가 이전 분기면 최신 발표 레코드에 덮지 않음
                    pe0 = (history[0].get("period_end") or "")[:10]
                    q = (latest_financials.get("quarter") or "")[:10]
                    if pe0 and q and pe0 == q:
                        history[0]["financials"] = latest_financials
                result["history"] = history
                result["available"] = True
        except Exception as e:
            msg = str(e)
            if "lxml" in msg.lower():
                print(f"[earnings] {t} EPS 스킵 (lxml 미설치)")
            else:
                print(f"[earnings] {t} EPS 오류: {e}")

    except Exception as e:
        print(f"[earnings] {t} 전체 오류: {e}")

    return result


def _format_move_label(pct: float, timing: str | None) -> str:
    if pct > 0.05:
        body = f"▲{pct:.2f}%"
    elif pct < -0.05:
        body = f"▼{abs(pct):.2f}%"
    else:
        body = f"{pct:+.2f}%"
    if timing == "amc":
        return f"장마감 후 발표 · 다음 거래일 종가 {body}"
    if timing == "bmo":
        return f"장전 발표 · 당일 종가 {body}"
    return f"발표 후 종가 반응 {body}"


def _compute_post_earnings_move(
    ticker: str,
    earnings_date: str,
    *,
    timing: str | None = None,
) -> dict | None:
    """발표 시점(장전/장마감 후)에 맞는 종가 반응 (뉴스 헤드라인보다 우선)."""
    ed = _parse_date(earnings_date)
    if not ed:
        return None
    t = (ticker or "").upper()
    try:
        stock = yf.Ticker(t)
        hist = stock.history(
            start=(ed - timedelta(days=7)).isoformat(),
            end=(ed + timedelta(days=14)).isoformat(),
        )
        if hist is None or hist.empty:
            return None
        by_date: dict[date, float] = {}
        for ts, row in hist.iterrows():
            d = ts.date() if hasattr(ts, "date") else _parse_date(str(ts)[:10])
            if d and _is_finite(row.get("Close")):
                by_date[d] = float(row["Close"])
        if ed not in by_date:
            return None
        sorted_dates = sorted(by_date.keys())
        i = sorted_dates.index(ed)
        earnings_close = by_date[ed]
        out: dict[str, Any] = {
            "earnings_date": ed.isoformat(),
            "earnings_close": round(earnings_close, 2),
            "source": "yfinance_price",
        }
        if i > 0:
            prev_close = by_date[sorted_dates[i - 1]]
            out["prev_close"] = round(prev_close, 2)
            out["earnings_day_pct"] = round(
                (earnings_close - prev_close) / prev_close * 100, 2
            )
        if i + 1 < len(sorted_dates):
            next_close = by_date[sorted_dates[i + 1]]
            pct = (next_close - earnings_close) / earnings_close * 100
            out["next_close"] = round(next_close, 2)
            out["next_day_pct"] = round(pct, 2)

        resolved = timing if timing in ("amc", "bmo") else None
        if not resolved:
            try:
                info = stock.info or {}
                ann = _ts_to_date(info.get("earningsTimestamp"))
                if ann and abs((ann - ed).days) <= 1:
                    resolved = _timing_from_timestamp(info.get("earningsTimestamp"))
            except Exception:
                resolved = None
        out["timing"] = resolved or "unknown"

        if out["timing"] == "bmo" and out.get("earnings_day_pct") is not None:
            reaction = float(out["earnings_day_pct"])
        elif out.get("next_day_pct") is not None:
            reaction = float(out["next_day_pct"])
        elif out.get("earnings_day_pct") is not None:
            reaction = float(out["earnings_day_pct"])
        else:
            return out
        out["reaction_pct"] = round(reaction, 2)
        out["label"] = _format_move_label(reaction, out["timing"] if out["timing"] != "unknown" else None)
        return out
    except Exception as e:
        print(f"[earnings] {t} post-earnings move 오류: {e}")
        return None


def _filter_earnings_call_news(
    items: list[dict],
    ticker: str,
    earnings_date: str,
) -> list[dict]:
    """실적일과 무관한 구버전·오해 소지 헤드라인 제거."""
    ed = _parse_date(earnings_date)
    if not items:
        return []
    t = (ticker or "").upper()
    scored: list[tuple[int, dict]] = []
    for item in items:
        title = item.get("title") or ""
        low = title.lower()
        score = 0
        if t.lower() in low:
            score += 2
        if ed:
            y, m = ed.year, ed.month
            if str(y) in title or f"q4 {y}" in low or f"fy{str(y)[2:]}" in low:
                score += 3
            if f"{y}-{m:02d}" in title or f"{m}/{ed.day}" in title:
                score += 2
            # 구분기·구년도 기사 (Q2 2025 등) — 최신 실적과 혼동 방지
            if re.search(r"q[1-4]\s*202[0-4]", low) or re.search(r"q[1-2]\s*2025", low):
                if y >= 2026 or (y == 2025 and m >= 8):
                    score -= 10
            if "stock falls" in low and "2025" in low and y >= 2026:
                score -= 8
            if "shares rise" in low and ed and y >= 2026:
                # D+2 이후 단기 반등 헤드라인 — 발표 직후 반응과 혼동
                score -= 3
        if score >= 0:
            scored.append((score, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    filtered = [it for sc, it in scored if sc >= 1]
    return filtered if filtered else [it for _, it in scored[:6]]


def _reaction_pct(price_move: dict | None) -> float | None:
    if not price_move:
        return None
    for key in ("reaction_pct", "next_day_pct", "earnings_day_pct"):
        v = _safe_float(price_move.get(key))
        if v is not None:
            return v
    return None


def _call_text(call: dict | None) -> str:
    call = call or {}
    bits = [call.get("summary") or "", call.get("market_reaction_note") or ""]
    bits.extend(str(h) for h in (call.get("highlights") or []))
    return " ".join(bits)


def _text_contradicts_fundamentals(text: str, facts: dict | None) -> bool:
    """저장된/생성된 요약이 확정 EPS·매출·종가와 모순되는지."""
    if not text:
        return False
    facts = facts or {}
    low = text.lower()
    sp = _safe_float(facts.get("eps_surprise_pct"))
    rsp = _safe_float(facts.get("revenue_surprise_pct"))
    if (("매출" in text and any(w in text for w in ("하회", "미스"))) or "revenue miss" in low):
        if facts.get("actual_revenue") is not None and (rsp is None or rsp >= 0):
            return True
    if (("매출" in text and any(w in text for w in ("상회", "비트"))) or "revenue beat" in low):
        if rsp is not None and rsp < 0:
            return True
    if sp is not None and sp >= 0:
        if any(w in text for w in ("EPS 하회", "실적 미스", "어닝 미스")):
            return True
    if sp is not None and sp < 0:
        if any(w in text for w in ("EPS 상회", "어닝 비트", "실적 서프라이즈")):
            return True
    return False


def _needs_earnings_call_refresh(rec: dict, price_move: dict | None) -> bool:
    """저장된 콜 요약이 종가·실적 숫자와 모순되면 재생성."""
    call = rec.get("earnings_call") or {}
    if not call.get("summary"):
        return True
    text = _call_text(call)
    move = price_move or rec.get("post_earnings_move") or call.get("post_earnings_move")
    pct = _reaction_pct(move)
    if pct is not None:
        if pct <= -1.0 and any(w in text for w in ("상승", " rise", "Rise", " rallied", " gains")):
            return True
        if pct >= 1.0 and any(w in text for w in ("하락", " fall", "Fall", " dropped", " plunged", "급락")):
            return True
        stored = rec.get("post_earnings_move") or call.get("post_earnings_move") or {}
        stored_pct = _reaction_pct(stored)
        if stored_pct is not None and abs(stored_pct - pct) > 0.5:
            return True
    if _text_contradicts_fundamentals(text, rec):
        return True
    # 숫자 확보됐는데 요약이 예전 '대기' 상태 서술이면 갱신
    if rec.get("actual_eps") is not None and any(
        w in text for w in ("반영 대기", "동기화 중", "수치 미확인")
    ):
        return True
    return False


def _latest_eps_missing(ticker: str) -> bool:
    hist = list_earnings_history(ticker, limit=1)
    if not hist:
        return False
    rec = hist[0]
    d = _parse_date(rec.get("date"))
    if d is None or (date.today() - d).days > 45:
        return False
    return rec.get("actual_eps") is None


def _latest_call_contradicts(ticker: str) -> bool:
    hist = list_earnings_history(ticker, limit=1)
    if not hist:
        return False
    date_key = hist[0].get("date")
    rec = get_earnings_record(ticker, date_key) or hist[0]
    if not (rec.get("earnings_call") or {}).get("summary"):
        return False
    days = _days_since(date_key)
    if days is None or days > UI_HIGHLIGHT_CALL_DAYS + 3:
        return False
    return _needs_earnings_call_refresh(rec, rec.get("post_earnings_move"))


def _strip_llm_price_reaction(text: str) -> str:
    """LLM이 뉴스 헤드라인에서 복사한 주가 등락 문장 제거 (소수 % 포함)."""
    if not text:
        return text
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    kept: list[str] = []
    price_words = re.compile(
        r"(?:주가|주식|stock|shares?)",
        re.I,
    )
    move_words = re.compile(
        r"(?:상승|하락|급등|급락|rise|fall|rally|drop|plunge|gains?)",
        re.I,
    )
    pct_move = re.compile(r"\d+(?:\.\d+)?\s*%\s*(?:상승|하락|rise|fall)", re.I)
    for part in parts:
        p = part.strip()
        if not p:
            continue
        if pct_move.search(p):
            continue
        if price_words.search(p) and (move_words.search(p) or "%" in p):
            continue
        kept.append(p)
    return " ".join(kept).strip()


def _strip_contradictory_claims(text: str, facts: dict | None) -> str:
    if not text:
        return text
    facts = facts or {}
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    kept: list[str] = []
    rsp = _safe_float(facts.get("revenue_surprise_pct"))
    for part in parts:
        p = part.strip()
        if not p:
            continue
        if "매출" in p and any(w in p for w in ("하회", "미스")):
            if facts.get("actual_revenue") is not None and (rsp is None or rsp >= 0):
                continue
        if "매출" in p and any(w in p for w in ("상회", "비트")):
            if rsp is not None and rsp < 0:
                continue
        kept.append(p)
    return " ".join(kept).strip()


def _apply_price_move_to_call_summary(data: dict, price_move: dict | None) -> dict:
    """시장 반응은 종가 데이터로 확정 — LLM 헤드라인 착오 방지."""
    if not data:
        return data
    pct = _reaction_pct(price_move)
    if pct is None:
        return data
    label = (price_move or {}).get("label") or _format_move_label(pct, (price_move or {}).get("timing"))
    data["market_reaction_note"] = f"{label} (yfinance 종가 기준)"
    data["post_earnings_move"] = price_move
    summary = _strip_llm_price_reaction((data.get("summary") or "").strip())
    if label not in summary:
        summary = (summary + " " if summary else "") + data["market_reaction_note"] + "."
    data["summary"] = summary.strip()
    hl = [
        h for h in (data.get("highlights") or [])
        if not re.search(r"\d+(?:\.\d+)?\s*%", h)
        and not re.search(r"(?:주가|상승|하락|rise|fall)", h, re.I)
    ]
    direction = "하락" if pct < -0.05 else ("상승" if pct > 0.05 else "보합")
    timing = (price_move or {}).get("timing")
    if timing == "bmo":
        hl.insert(0, f"장전 발표 당일 종가 {pct:+.2f}% ({direction})")
    else:
        hl.insert(0, f"발표 후 다음날 종가 {pct:+.2f}% ({direction})")
    data["highlights"] = hl[:5]
    return data


def _template_call_from_facts(
    ticker: str,
    earnings_date: str,
    facts: dict,
    price_move: dict | None = None,
) -> dict:
    parts: list[str] = []
    highlights: list[str] = []
    actual = facts.get("actual_eps")
    est = facts.get("estimate_eps")
    sp = _safe_float(facts.get("eps_surprise_pct"))
    if actual is not None:
        beat = ""
        if sp is not None:
            beat = "상회" if sp >= 0 else "하회"
        line = f"{ticker} 발표 {earnings_date} EPS ${actual}"
        if est is not None:
            line += f" / 예상 ${est}"
        if beat:
            line += f" ({beat})"
        parts.append(line + ".")
        if beat:
            highlights.append(f"EPS {beat}" + (f" {abs(sp):.1f}%" if sp is not None else ""))
    rev = facts.get("actual_revenue")
    if rev is not None:
        rsp = _safe_float(facts.get("revenue_surprise_pct"))
        rev_s = _fmt_revenue(rev)
        if rsp is not None:
            parts.append(f"매출 {rev_s} ({'상회' if rsp >= 0 else '하회'}).")
        else:
            parts.append(f"매출 {rev_s}.")
        highlights.append(f"매출 {rev_s}")
    data = {
        "earnings_date": earnings_date,
        "summary": " ".join(parts) or f"{ticker} {earnings_date} 실적 발표.",
        "highlights": highlights[:5],
        "guidance_note": None,
        "market_reaction_note": None,
        "sources": [{
            "title": "Nasdaq earnings / financials",
            "url": f"https://www.nasdaq.com/market-activity/stocks/{ticker.lower()}/earnings",
        }],
        "generated_at": datetime.utcnow().isoformat(),
        "from_facts": True,
    }
    return _apply_price_move_to_call_summary(data, price_move)


def fetch_earnings_call_news(ticker: str, limit: int = 12) -> list[dict]:
    t = (ticker or "").upper()
    keywords = ("earnings call", "earnings report", "guidance", "quarterly results")
    items: list[dict] = []
    seen: set[str] = set()

    def _add(title, summary, url, source):
        title = (title or "").strip()
        if not title or title in seen:
            return
        low = title.lower()
        if not any(k in low for k in keywords) and "earnings" not in low:
            return
        seen.add(title)
        items.append({
            "title": title,
            "summary": (summary or "")[:400],
            "url": url or "",
            "source": source or "",
        })

    try:
        for item in (yf.Ticker(t).news or [])[:10]:
            content = item.get("content", {})
            _add(
                content.get("title", item.get("title", "")),
                content.get("summary", ""),
                (content.get("canonicalUrl") or {}).get("url", ""),
                (content.get("provider") or {}).get("displayName", "Yahoo"),
            )
    except Exception as e:
        print(f"[earnings] {t} yfinance 뉴스 오류: {e}")

    try:
        rss = f"https://news.google.com/rss/search?q={t}+earnings+call+guidance&hl=en-US&gl=US&ceid=US:en"
        feed = feedparser.parse(rss)
        for entry in feed.entries[:8]:
            _add(entry.get("title", ""), entry.get("summary", ""), entry.get("link", ""), "Google News")
    except Exception as e:
        print(f"[earnings] {t} RSS 오류: {e}")

    return items[:limit]


def _facts_prompt_block(facts: dict | None, price_move: dict | None, earnings_date: str) -> str:
    facts = facts or {}
    lines = ["[확정 실적 수치 — 뉴스와 다르면 이 숫자를 따름. 없는 항목은 상회/하회 단정 금지]"]
    if facts.get("actual_eps") is not None:
        lines.append(
            f"EPS 실제 ${facts['actual_eps']} / 예상 ${facts.get('estimate_eps', '—')}"
        )
        sp = facts.get("eps_surprise_pct")
        if sp is not None:
            lines.append(_surprise_label(sp))
    else:
        lines.append("EPS 실제값 미확인 — EPS beat/miss 단정 금지")
    if facts.get("actual_revenue") is not None:
        lines.append(f"매출 실제 {_fmt_revenue(facts['actual_revenue'])}")
        rsp = facts.get("revenue_surprise_pct")
        if rsp is not None:
            lines.append(
                f"매출 {_surprise_label(rsp).replace('예상 대비', '컨센서스 대비')}"
            )
        else:
            lines.append("매출 컨센서스 미확인 — 매출 미스/비트 단정 금지")
    else:
        lines.append("매출 수치 미확인 — 매출 미스/비트 단정 금지")
    pct = _reaction_pct(price_move)
    if pct is not None:
        lines.append(
            f"시장 반응(종가): {pct:+.2f}% — summary/highlights에 주가 %를 쓰지 마세요 (시스템이 붙임)."
        )
    lines.append(f"발표일: {earnings_date}")
    return "\n".join(lines)


def generate_earnings_call_summary(
    ticker: str,
    earnings_date: str,
    news_items: list[dict],
    claude_client=None,
    *,
    price_move: dict | None = None,
    facts: dict | None = None,
) -> dict | None:
    facts = facts or {}
    has_facts = facts.get("actual_eps") is not None or facts.get("actual_revenue") is not None
    if not news_items and not has_facts:
        return None

    if not news_items:
        return _template_call_from_facts(ticker, earnings_date, facts, price_move)

    if claude_client is None:
        try:
            import anthropic

            claude_client = anthropic.Anthropic()
        except Exception:
            return _template_call_from_facts(ticker, earnings_date, facts, price_move)

    snippets = []
    sources = []
    for n in news_items[:10]:
        snippets.append(f"- [{n.get('source', '')}] {n.get('title', '')}")
        if n.get("summary"):
            snippets.append(f"  요약: {n['summary'][:200]}")
        sources.append({"title": n.get("title", ""), "url": n.get("url", "")})

    facts_block = _facts_prompt_block(facts, price_move, earnings_date)

    prompt = f"""아래는 {ticker} 실적({earnings_date}) 관련 자료입니다.
{facts_block}

뉴스 헤드라인은 보조 자료입니다. 확정 수치와 충돌하면 확정 수치를 따르세요.
없는 내용은 null. 추측·창작 금지. 주가 등락률은 JSON에 넣지 마세요.
매출/EPS 상회·하회는 확정 수치에 근거할 때만 언급하세요.

{{
  "summary": "2~3문장 요약 (한국어, 실적·매출·가이던스·사업만)",
  "highlights": ["핵심 1", "핵심 2", "핵심 3"],
  "guidance_note": "가이던스 변경 언급 (없으면 null)",
  "market_reaction_note": null
}}

뉴스:
{chr(10).join(snippets)}
"""
    try:
        msg = claude_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return _template_call_from_facts(ticker, earnings_date, facts, price_move)
        data = json.loads(match.group())
        result = {
            "earnings_date": earnings_date,
            "summary": _strip_contradictory_claims(
                (data.get("summary") or "").strip(), facts
            ),
            "highlights": [
                h for h in (data.get("highlights") or [])
                if h and not _text_contradicts_fundamentals(str(h), facts)
            ][:5],
            "guidance_note": data.get("guidance_note"),
            "market_reaction_note": data.get("market_reaction_note"),
            "sources": sources[:5],
            "generated_at": datetime.utcnow().isoformat(),
        }
        result = _apply_price_move_to_call_summary(result, price_move)
        if _text_contradicts_fundamentals(_call_text(result), facts):
            return _template_call_from_facts(ticker, earnings_date, facts, price_move)
        return result
    except Exception as e:
        print(f"[earnings] {ticker} 콜 요약 생성 실패: {e}")
        if has_facts:
            return _template_call_from_facts(ticker, earnings_date, facts, price_move)
        return None


def _is_suspect_fiscal_date_record(rec: dict | None) -> bool:
    """분기 마감일을 발표일(date)로 잘못 저장한 레코드."""
    if not rec:
        return False
    d = (rec.get("date") or "")[:10]
    pe = (rec.get("period_end") or "")[:10]
    if not d or not pe or d != pe:
        return False
    src = rec.get("source") or ""
    note = rec.get("date_note") or ""
    if src == "yfinance_fiscal":
        return True
    if "발표일 미확인" in note or "분기 마감일 기준" in note:
        return True
    return False


def _earnings_record_rank(rec: dict) -> int:
    """동일 period_end 중복 시 우선순위 — 높을수록 canonical."""
    score = 0
    d = (rec.get("date") or "")[:10]
    pe = (rec.get("period_end") or "")[:10]
    if d and pe and d != pe:
        score += 100
    src = rec.get("source") or ""
    if src == "yfinance_info":
        score += 50
    elif src == "yfinance_merged":
        score += 40
    elif src == "yfinance_announce":
        score += 30
    if _is_suspect_fiscal_date_record(rec):
        score -= 200
    if rec.get("earnings_call"):
        score += 5
    if rec.get("actual_eps") is not None:
        score += 2
    return score


def _collapse_period_duplicates(history: list[dict]) -> list[dict]:
    """같은 period_end에 발표일·마감일 레코드가 둘 다 있으면 발표일 쪽만 유지."""
    if not history:
        return []
    buckets: dict[str, dict] = {}
    for rec in history:
        pe = (rec.get("period_end") or rec.get("date") or "")[:10]
        if not pe:
            continue
        prev = buckets.get(pe)
        if prev is None or _earnings_record_rank(rec) > _earnings_record_rank(prev):
            buckets[pe] = rec
    out = list(buckets.values())
    out.sort(key=lambda r: r.get("date") or "", reverse=True)
    return out


def _history_needs_announce_correction(ticker: str) -> bool:
    """DB 최신 실적이 발표일 미확인(마감일=date) 상태인지."""
    raw = list_earnings_history(ticker, limit=8)
    if not raw:
        return False
    collapsed = _collapse_period_duplicates(raw)
    if collapsed and raw and collapsed[0].get("date") != raw[0].get("date"):
        return True
    if any(_is_suspect_fiscal_date_record(r) for r in raw[:3]):
        return True
    return False


def _purge_fiscal_date_duplicates(ticker: str, canonical: list[dict]) -> int:
    """동기화 후 period_end=date 오저장 레코드 삭제 — canonical 발표일 레코드가 있을 때만."""
    t = (ticker or "").upper()
    if not t:
        return 0
    announce_by_period: dict[str, str] = {}
    for rec in canonical or []:
        pe = (rec.get("period_end") or "")[:10]
        ann = (rec.get("date") or "")[:10]
        if pe and ann and pe != ann:
            announce_by_period[pe] = ann

    removed = 0
    for rec in list_earnings_history(t, limit=20):
        if not _is_suspect_fiscal_date_record(rec):
            continue
        pe = (rec.get("period_end") or rec.get("date") or "")[:10]
        canon_ann = announce_by_period.get(pe)
        if not canon_ann:
            continue
        stale_date = (rec.get("date") or "")[:10]
        if stale_date and stale_date != canon_ann:
            removed += delete_earnings_record(t, stale_date)
    return removed


def reconcile_all_suspect_earnings(limit: int = 40, claude_client=None) -> int:
    """플랫폼 전체 — 발표일 오류 suspected 티커 yfinance 재동기화."""
    tickers = list_earnings_tickers_with_suspect_records(limit=limit)
    done = 0
    for t in tickers:
        try:
            _sync_from_yfinance(t, claude_client)
            done += 1
            print(f"[earnings] suspect reconcile OK: {t}")
        except Exception as e:
            print(f"[earnings] suspect reconcile fail {t}: {e}")
    return done


def _needs_sync(ticker: str, profile: dict | None) -> bool:
    t = (ticker or "").upper()
    profile = profile or {}
    if profile.get("skip_reason") == "etf_or_index":
        return False
    if _history_needs_announce_correction(t):
        return True
    if _latest_call_contradicts(t):
        return True
    if count_earnings_history(t) == 0 and not profile.get("next_earnings_date"):
        return True
    last_sync = profile.get("last_sync_at") or profile.get("updated_at")
    incomplete = _latest_eps_missing(t)
    retry_h = INCOMPLETE_EPS_RETRY_HOURS if incomplete else SYNC_HOURS
    if not last_sync:
        return True
    try:
        synced = datetime.fromisoformat(last_sync.replace("Z", "+00:00").replace("+00:00", ""))
        age_h = (datetime.utcnow() - synced).total_seconds() / 3600
        if age_h >= retry_h:
            return True
    except (TypeError, ValueError):
        return True
    nd = _parse_date(profile.get("next_earnings_date"))
    if nd and (date.today() - nd).days >= 0:
        latest = _parse_date(profile.get("latest_earnings_date"))
        if latest is None or latest < nd:
            return True
    latest = _parse_date(profile.get("latest_earnings_date"))
    if latest and (date.today() - latest).days >= 75:
        return True
    if nd and (date.today() - nd).days > 1 and count_earnings_history(t) == 0:
        return True
    return False


def _sync_from_yfinance(ticker: str, claude_client=None) -> dict:
    """yfinance(+Nasdaq 보강) → earnings_history 적재 + 프로필 갱신."""
    t = (ticker or "").upper()
    raw = fetch_yfinance_earnings(t)
    now = datetime.utcnow().isoformat()

    if raw.get("skip_reason") == "etf_or_index":
        profile = {
            "ticker": t,
            "available": False,
            "skip_reason": "etf_or_index",
            "last_sync_at": now,
            "updated_at": now,
            "history_count": 0,
        }
        upsert_ticker_earnings(profile)
        return profile

    for rec in raw.get("history") or []:
        upsert_earnings_record(rec)

    purged = _purge_fiscal_date_duplicates(t, raw.get("history") or [])
    if purged:
        print(f"[earnings] {t} 분기 마감일 오저장 {purged}건 삭제")

    history = _collapse_period_duplicates(list_earnings_history(t, limit=12))
    latest_date = history[0].get("date") if history else None

    if latest_date:
        latest_rec = get_earnings_record(t, latest_date) or (history[0] if history else {})
        price_move = _compute_post_earnings_move(
            t, latest_date, timing=latest_rec.get("announce_timing")
        )
        if price_move:
            merged = dict(latest_rec)
            merged["post_earnings_move"] = price_move
            upsert_earnings_record(merged)
            latest_rec = get_earnings_record(t, latest_date) or merged

        call = latest_rec.get("earnings_call")
        days = _days_since(latest_date)
        need_call = (
            days is not None
            and 0 <= days <= UI_HIGHLIGHT_CALL_DAYS + 3
            and (
                call is None
                or not call.get("summary")
                or _needs_earnings_call_refresh(latest_rec, price_move)
            )
        )
        if need_call:
            raw_news = fetch_earnings_call_news(t)
            news = _filter_earnings_call_news(raw_news, t, latest_date)
            summary = generate_earnings_call_summary(
                t,
                latest_date,
                news,
                claude_client,
                price_move=price_move,
                facts=latest_rec,
            )
            if summary:
                merged = dict(latest_rec)
                merged["earnings_call"] = summary
                if price_move:
                    merged["post_earnings_move"] = price_move
                upsert_earnings_record(merged)

    nd = raw.get("next_earnings_date")
    days_to = raw.get("days_to_earnings")
    profile = {
        "ticker": t,
        "available": bool(raw.get("available")),
        "next_earnings_date": nd,
        "days_to_earnings": days_to,
        "is_earnings_week": raw.get("is_earnings_week", False),
        "last_sync_at": now,
        "updated_at": now,
        "history_count": count_earnings_history(t),
        "latest_earnings_date": latest_date,
    }
    upsert_ticker_earnings(profile)
    return profile


def load_earnings_history(ticker: str, limit: int = 8) -> list[dict]:
    """UI·리포트용 — period_end 중복 collapse 적용."""
    return _collapse_period_duplicates(list_earnings_history(ticker, limit=limit))


def _surprise_label(sp: float | None) -> str:
    if sp is None:
        return "컨센서스 비교 불가"
    if sp >= 0:
        return f"예상 대비 {abs(sp):.1f}% 상회(서프라이즈)"
    return f"예상 대비 {abs(sp):.1f}% 하회(쇼크)"


def _streak_label(sp: float | None) -> str:
    if sp is None:
        return "—"
    return "상회" if sp >= 0 else "하회"


def _build_earnings_summary(history: list[dict]) -> str:
    """UI·리포트용 한글 해설 — 숫자만 나열하지 않고 의미 설명."""
    if not history:
        return ""
    latest = history[0]
    prev = history[1] if len(history) > 1 else None
    parts: list[str] = []

    label = _format_earnings_date_label(latest)
    parts.append(f"{label} 실적입니다.")

    if latest.get("date_note"):
        parts.append(latest["date_note"] + ".")

    actual = latest.get("actual_eps")
    est = latest.get("estimate_eps")
    sp = latest.get("eps_surprise_pct")
    if actual is not None:
        est_txt = f"${est}" if est is not None else "—"
        parts.append(f"주당순이익(EPS) ${actual}(애널리스트 예상 {est_txt}). {_surprise_label(sp)}.")
    elif latest.get("period_end"):
        parts.append("EPS·컨센서스 수치는 yfinance에 아직 없습니다 (IR·증권사 확인 필요).")
        pm = latest.get("post_earnings_move") or {}
        if pm.get("label"):
            parts.append(pm["label"] + " (종가 기준).")
    if latest.get("actual_revenue") is not None:
        rsp = latest.get("revenue_surprise_pct")
        rev_line = f"매출 {_fmt_revenue(latest['actual_revenue'])}"
        if latest.get("estimate_revenue") is not None:
            rev_line += f" / 예상 {_fmt_revenue(latest['estimate_revenue'])}"
        if rsp is not None:
            rev_line += f". {_surprise_label(rsp).replace('예상 대비', '매출 컨센서스 대비')}"
        parts.append(rev_line + ".")

    if prev and actual is not None and prev.get("actual_eps") is not None:
        chg = _pct_change(actual, prev["actual_eps"])
        if chg is not None:
            direction = "개선" if actual >= prev["actual_eps"] else "악화"
            parts.append(
                f"직전 분기(QoQ) ${prev['actual_eps']}→${actual} ({chg:+.1f}%)로 EPS {direction}."
            )

    streak_src = history[:4] if latest.get("eps_surprise_pct") is not None else history[1:5]
    streak = [
        _streak_label(h.get("eps_surprise_pct"))
        for h in streak_src
        if h.get("eps_surprise_pct") is not None
    ]
    if len(streak) >= 2:
        label_streak = "최근" if latest.get("eps_surprise_pct") is not None else "직전"
        parts.append(f"{label_streak} {len(streak)}분기 컨센서스 대비: {' → '.join(streak)} (최신→과거).")

    return " ".join(parts)


def _build_comparison_lines(history: list[dict]) -> list[str]:
    """DB 이력 기반 QoQ·YoY·서프라이즈 추세."""
    if len(history) < 1:
        return []
    lines: list[str] = []
    latest = history[0]
    prev = history[1] if len(history) > 1 else None
    yoy = history[4] if len(history) > 4 else None

    lines.append(f"📌 {_format_earnings_date_label(latest)}")

    pm = latest.get("post_earnings_move") or {}
    if pm.get("reaction_pct") is not None or pm.get("next_day_pct") is not None:
        lines.append(f"  📉 발표 후 시장: {pm.get('label', '')} (yfinance 종가)")

    if latest.get("actual_eps") is not None:
        sp = latest.get("eps_surprise_pct")
        sp_txt = f" — {_surprise_label(sp)}" if sp is not None else ""
        lines.append(
            f"  EPS ${latest['actual_eps']} / 예상 ${latest.get('estimate_eps', '—')}{sp_txt}"
        )
    if latest.get("actual_revenue") is not None:
        rsp = latest.get("revenue_surprise_pct")
        rsp_txt = f" ({'+' if (rsp or 0) >= 0 else ''}{rsp}% vs 예상)" if rsp is not None else ""
        est_rev = latest.get("estimate_revenue")
        est_txt = _fmt_revenue(est_rev) if est_rev is not None else "—"
        lines.append(
            f"  매출 {_fmt_revenue(latest['actual_revenue'])} / 예상 {est_txt}{rsp_txt}"
        )

    if prev and latest.get("actual_eps") is not None and prev.get("actual_eps") is not None:
        chg = _pct_change(latest["actual_eps"], prev["actual_eps"])
        if chg is not None:
            prev_label = _period_end_label(prev.get("period_end")) or f"직전({prev['date']})"
            lines.append(
                f"  QoQ EPS ({prev_label}): ${prev['actual_eps']} → ${latest['actual_eps']} ({chg:+.1f}%)"
            )
    if prev and latest.get("actual_revenue") and prev.get("actual_revenue"):
        chg = _pct_change(latest["actual_revenue"], prev["actual_revenue"])
        if chg is not None:
            lines.append(
                f"  QoQ 매출: {_fmt_revenue(prev['actual_revenue'])} → {_fmt_revenue(latest['actual_revenue'])} ({chg:+.1f}%)"
            )

    if yoy and latest.get("actual_eps") is not None and yoy.get("actual_eps") is not None:
        chg = _pct_change(latest["actual_eps"], yoy["actual_eps"])
        if chg is not None:
            yoy_label = _period_end_label(yoy.get("period_end")) or f"4분기 전({yoy['date']})"
            lines.append(
                f"  YoY EPS ({yoy_label}): ${yoy['actual_eps']} → ${latest['actual_eps']} ({chg:+.1f}%)"
            )

    streak = []
    for h in history[:4]:
        sp = h.get("eps_surprise_pct")
        if sp is None:
            continue
        streak.append(_streak_label(sp))
    if len(streak) >= 2:
        lines.append(f"  컨센서스 추세: {' → '.join(streak)} (최신→과거, 상회=서프라이즈)")

    if len(history) >= 2:
        lines.append("  분기 이력:")
        for h in history[:6]:
            sp = h.get("eps_surprise_pct")
            sp_s = f" · {_streak_label(sp)}" if sp is not None else ""
            eps = h.get("actual_eps")
            when = _format_earnings_date_label(h)
            lines.append(f"    · {when}: EPS ${eps if eps is not None else '—'}{sp_s}")

    return lines


def _should_ui_highlight_result(last_date: str | None, today: date | None = None) -> tuple[bool, int | None]:
    days = _days_since(last_date, today)
    if days is None or days < 0:
        return False, None
    if days <= UI_HIGHLIGHT_RESULT_DAYS:
        return True, UI_HIGHLIGHT_RESULT_DAYS - days
    return False, None


def _should_ui_highlight_call(earnings_date: str | None, call: dict | None, today: date | None = None) -> tuple[bool, int | None]:
    if not call or not earnings_date or call.get("earnings_date") != earnings_date:
        return False, None
    days = _days_since(earnings_date, today)
    if days is None or days < 0 or days > UI_HIGHLIGHT_CALL_DAYS:
        return False, None
    return True, UI_HIGHLIGHT_CALL_DAYS - days


def build_report_bundle(profile: dict, history: list[dict] | None = None, today: date | None = None) -> dict:
    ref = today or date.today()
    history = history or []
    latest = history[0] if history else {}
    ticker = profile.get("ticker") or latest.get("ticker")
    if latest.get("date") and ticker:
        full = get_earnings_record(ticker, latest["date"])
        if full:
            latest = full
        elif not latest.get("post_earnings_move"):
            pm = _compute_post_earnings_move(ticker, latest["date"])
            if pm:
                latest = {**latest, "post_earnings_move": pm}
    nd = profile.get("next_earnings_date")
    days_to = profile.get("days_to_earnings")
    if nd and days_to is None:
        d = _parse_date(nd)
        if d:
            days_to = (d - ref).days

    bundle: dict[str, Any] = {
        "available": bool(profile.get("available")) or bool(history),
        "from_db": True,
        "ticker": profile.get("ticker"),
        "skip_reason": profile.get("skip_reason"),
        "next_earnings_date": nd,
        "days_to_earnings": days_to,
        "is_earnings_week": profile.get("is_earnings_week", False),
        "show_next": bool(nd),
        "history_count": len(history),
        "history": history,
        "last_sync_at": profile.get("last_sync_at"),
    }

    last_date = latest.get("date")
    bundle["last_earnings_date"] = last_date
    bundle["last_earnings"] = latest
    bundle["last_financials"] = latest.get("financials")

    show_result, result_left = _should_ui_highlight_result(last_date, ref)
    bundle["show_result"] = show_result
    bundle["result_days_left"] = result_left

    call = latest.get("earnings_call")
    show_call, call_left = _should_ui_highlight_call(last_date, call, ref)
    bundle["show_call"] = show_call
    bundle["call_days_left"] = call_left
    if show_call:
        bundle["earnings_call"] = call

    bundle["comparison_text"] = _build_comparison_lines(history)
    bundle["summary_text"] = _build_earnings_summary(history)
    bundle["data_sources"] = [
        "yfinance earnings_dates (발표일·EPS)",
        "yfinance earnings_history (분기 마감·EPS)",
        "yfinance info.earningsTimestamp (최신 발표일 보정)",
        "Nasdaq earnings surprise / financials (발표 EPS·매출 보강)",
        "yfinance 종가 (발표 후 시장 반응)",
        "MongoDB earnings_history",
    ]
    if latest:
        bundle["date_label"] = _format_earnings_date_label(latest)
    return bundle


def format_earnings_prompt_text(bundle: dict) -> str:
    """LLM 프롬프트 — DB 이력 + 비교 (외부 API 호출 없음)."""
    if not bundle.get("available") and not bundle.get("show_next"):
        reason = bundle.get("skip_reason")
        if reason == "etf_or_index":
            return "실적 데이터 없음 (ETF/지수 — 실적 일정 해당 없음)"
        return "실적 DB 이력 없음 — 추측 금지"

    lines: list[str] = ["[실적 데이터 출처: StockAI MongoDB — 아래 수치만 인용]"]

    if bundle.get("show_next") and bundle.get("next_earnings_date"):
        days = bundle.get("days_to_earnings")
        nd = bundle["next_earnings_date"]
        if days is not None:
            if days == 0:
                lines.append(f"⚠️ 실적 발표일: {nd} (D-day)")
            elif 1 <= days <= 3:
                lines.append(f"⚠️ 실적 D-{days} ({nd}) — 이벤트 리스크")
            else:
                lines.append(f"📅 다음 실적: {nd} (D-{days})" if days > 0 else f"📅 다음 실적: {nd}")
        else:
            lines.append(f"📅 다음 실적: {nd}")

    comp = bundle.get("comparison_text") or []
    summary = bundle.get("summary_text") or ""
    if summary:
        lines.append(f"📋 실적 요약: {summary}")
    if comp:
        lines.append(f"📈 실적 이력 ({bundle.get('history_count', 0)}분기 DB 보관 — QoQ/YoY 비교):")
        lines.extend(f"  {ln}" if not ln.startswith("  ") else ln for ln in comp)

    if bundle.get("show_call") and bundle.get("earnings_call"):
        ec = bundle["earnings_call"]
        lines.append(f"🎙️ 최신 어닝콜 요약 ({ec.get('earnings_date')}):")
        if ec.get("summary"):
            lines.append(f"  {ec['summary']}")
        for h in (ec.get("highlights") or [])[:3]:
            lines.append(f"  · {h}")
        if ec.get("guidance_note"):
            lines.append(f"  가이던스: {ec['guidance_note']}")
        lines.append("  ※ 뉴스 헤드라인 기반 — IR transcript 미확인 항목 추측 금지")

    lines.append(
        "분석 시 반드시: (1) 최신 vs 직전 분기 QoQ (2) 4분기 전 YoY 가능 시 (3) 컨센서스 beat/miss 연속성. "
        "DB에 없는 수치 생성 금지."
    )
    return "\n".join(lines)


def get_earnings_for_report(ticker: str, claude_client=None, *, force_sync: bool = False) -> dict:
    """리포트용 실적 번들 — 기본 DB 조회, 필요할 때만 yfinance 동기화."""
    t = (ticker or "").upper()
    profile = get_ticker_earnings(t) or {}
    if profile.get("last_earnings"):
        _migrate_legacy_profile(profile)

    if force_sync or _needs_sync(t, profile):
        print(f"[earnings] {t} yfinance 동기화 (force={force_sync})")
        profile = _sync_from_yfinance(t, claude_client)
    elif profile.get("next_earnings_date"):
        d = _parse_date(profile["next_earnings_date"])
        if d:
            profile = dict(profile)
            profile["days_to_earnings"] = (d - date.today()).days
            profile["is_earnings_week"] = abs(profile["days_to_earnings"]) <= 3

    history = load_earnings_history(t, limit=8)
    bundle = build_report_bundle(profile, history)
    bundle["prompt_text"] = format_earnings_prompt_text(bundle)
    return bundle


def get_earnings_display_bundle(ticker: str, snapshot: dict | None = None) -> dict:
    """UI용 실적 — DB 조회, 분기 실적 누락 시 자동 동기화."""
    t = (ticker or "").upper()
    profile = get_ticker_earnings(t) or {}
    force = _needs_sync(t, profile)
    snap_date = _parse_date(
        (snapshot or {}).get("last_earnings_date")
        or ((snapshot or {}).get("last_earnings") or {}).get("date")
    )
    db_latest = _parse_date(profile.get("latest_earnings_date"))
    if snap_date and db_latest and db_latest < snap_date:
        force = True
    return get_earnings_for_report(ticker, claude_client=None, force_sync=force)


# 하위 호환
def sync_and_get_earnings_bundle(ticker: str, claude_client=None) -> dict:
    return get_earnings_for_report(ticker, claude_client, force_sync=False)
