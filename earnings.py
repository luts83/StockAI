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
    get_earnings_record,
    get_ticker_earnings,
    list_earnings_history,
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


def _supplement_recent_from_info(stock, ticker: str, history: list[dict]) -> list[dict]:
    """earnings_dates 지연 시 info.earningsTimestamp + mostRecentQuarter로 최신 발표 보강."""
    try:
        info = stock.info or {}
    except Exception:
        return history

    announce = _ts_to_date(info.get("earningsTimestamp"))
    period_end = _ts_to_date(info.get("mostRecentQuarter"))
    if announce is None or period_end is None:
        return history
    if announce > date.today():
        return history

    period_key = period_end.isoformat()
    for h in history:
        if h.get("period_end") == period_key:
            return history
        h_ann = _parse_date(h.get("date"))
        if h_ann and abs((h_ann - announce).days) <= 2:
            return history

    actual = _eps_from_income_stmt(stock, period_end)
    est = None
    sp = None
    for f in _fetch_fiscal_quarter_rows(stock, ticker):
        if f.get("period_end") == period_key:
            est = f.get("estimate_eps")
            sp = f.get("eps_surprise_pct")
            if actual is None:
                actual = f.get("actual_eps")
            break

    rec: dict[str, Any] = {
        "ticker": ticker,
        "date": announce.isoformat(),
        "period_end": period_key,
        "actual_eps": actual,
        "estimate_eps": est,
        "eps_surprise_pct": sp if sp is not None else _surprise_pct(actual, est),
        "source": "yfinance_info",
    }
    if actual is None:
        rec["date_note"] = "최신 실적 발표 확인 — EPS·컨센서스 동기화 대기"
    history = [rec] + history
    history.sort(key=lambda r: r.get("date") or "", reverse=True)
    return history[:12]


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
            if history:
                if latest_financials:
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


def generate_earnings_call_summary(
    ticker: str,
    earnings_date: str,
    news_items: list[dict],
    claude_client=None,
) -> dict | None:
    if not news_items:
        return None
    if claude_client is None:
        import anthropic

        claude_client = anthropic.Anthropic()

    snippets = []
    sources = []
    for n in news_items[:10]:
        snippets.append(f"- [{n.get('source', '')}] {n.get('title', '')}")
        if n.get("summary"):
            snippets.append(f"  요약: {n['summary'][:200]}")
        sources.append({"title": n.get("title", ""), "url": n.get("url", "")})

    prompt = f"""아래는 {ticker} 실적({earnings_date}) 관련 뉴스 헤드라인입니다.
제공된 헤드라인·요약만 사용해 어닝콜/실적 발표 핵심을 JSON으로 추출하세요.
없는 내용은 null. 추측·창작 금지.

{{
  "summary": "2~3문장 요약 (한국어)",
  "highlights": ["핵심 1", "핵심 2", "핵심 3"],
  "guidance_note": "가이던스 변경 언급 (없으면 null)",
  "market_reaction_note": "시장 반응 언급 (없으면 null)"
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
            return None
        data = json.loads(match.group())
        return {
            "earnings_date": earnings_date,
            "summary": (data.get("summary") or "").strip(),
            "highlights": [h for h in (data.get("highlights") or []) if h][:5],
            "guidance_note": data.get("guidance_note"),
            "market_reaction_note": data.get("market_reaction_note"),
            "sources": sources[:5],
            "generated_at": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        print(f"[earnings] {ticker} 콜 요약 생성 실패: {e}")
        return None


def _needs_sync(ticker: str, profile: dict | None) -> bool:
    t = (ticker or "").upper()
    profile = profile or {}
    if profile.get("skip_reason") == "etf_or_index":
        return False
    if count_earnings_history(t) == 0 and not profile.get("next_earnings_date"):
        return True
    last_sync = profile.get("last_sync_at") or profile.get("updated_at")
    if not last_sync:
        return True
    try:
        synced = datetime.fromisoformat(last_sync.replace("Z", "+00:00").replace("+00:00", ""))
        age_h = (datetime.utcnow() - synced).total_seconds() / 3600
        if age_h >= SYNC_HOURS:
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
    """yfinance → earnings_history 적재 + 프로필 갱신."""
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

    history = list_earnings_history(t, limit=1)
    latest_date = history[0].get("date") if history else None

    if latest_date:
        latest_rec = get_earnings_record(t, latest_date) or (history[0] if history else {})
        call = latest_rec.get("earnings_call")
        days = _days_since(latest_date)
        need_call = (
            days is not None
            and 0 <= days <= UI_HIGHLIGHT_CALL_DAYS + 3
            and (call is None or not call.get("summary"))
        )
        if need_call:
            news = fetch_earnings_call_news(t)
            if news:
                summary = generate_earnings_call_summary(t, latest_date, news, claude_client)
                if summary:
                    merged = dict(latest_rec)
                    merged["earnings_call"] = summary
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
        parts.append("EPS·컨센서스 수치는 아직 동기화 중입니다.")

    if prev and actual is not None and prev.get("actual_eps") is not None:
        chg = _pct_change(actual, prev["actual_eps"])
        if chg is not None:
            direction = "개선" if actual >= prev["actual_eps"] else "악화"
            parts.append(
                f"직전 분기(QoQ) ${prev['actual_eps']}→${actual} ({chg:+.1f}%)로 EPS {direction}."
            )

    streak = [_streak_label(h.get("eps_surprise_pct")) for h in history[:4] if h.get("eps_surprise_pct") is not None]
    if len(streak) >= 2:
        parts.append(f"최근 {len(streak)}분기 컨센서스 대비: {' → '.join(streak)} (최신→과거).")

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

    if latest.get("actual_eps") is not None:
        sp = latest.get("eps_surprise_pct")
        sp_txt = f" — {_surprise_label(sp)}" if sp is not None else ""
        lines.append(
            f"  EPS ${latest['actual_eps']} / 예상 ${latest.get('estimate_eps', '—')}{sp_txt}"
        )
    if latest.get("actual_revenue") is not None:
        rsp = latest.get("revenue_surprise_pct")
        rsp_txt = f" ({'+' if (rsp or 0) >= 0 else ''}{rsp}% vs 예상)" if rsp is not None else ""
        lines.append(
            f"  매출 ${_rev_billions(latest['actual_revenue'])}B / 예상 ${_rev_billions(latest.get('estimate_revenue'))}B{rsp_txt}"
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
                f"  QoQ 매출: ${_rev_billions(prev['actual_revenue'])}B → ${_rev_billions(latest['actual_revenue'])}B ({chg:+.1f}%)"
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

    history = list_earnings_history(t, limit=8)
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
