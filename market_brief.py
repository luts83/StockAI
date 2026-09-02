import math
import re
import time
import asyncio
import anthropic
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import pytz
from news import fetch_macro_news, format_macro_news_for_brief


def _is_finite(x) -> bool:
    try:
        return x is not None and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _fast_info_session_close(ticker: str, *, prefer_previous: bool) -> float | None:
    """Yahoo history 봉이 NaN/누락일 때 fast_info로 직전 세션 종가 보완.
    prefer_previous=True (장전·장중): previous_close = 직전 확정 세션
    prefer_previous=False (마감 후): last_price 우선
    """
    try:
        fi = yf.Ticker(ticker).fast_info
        last_p = getattr(fi, "last_price", None)
        prev_p = getattr(fi, "previous_close", None)
        # SPY 등: regular_market_previous_close가 더 오래된 봉을 가리키는 경우 있음 → 1순위 제외
        if prefer_previous:
            for cand in (prev_p, last_p):
                if _is_finite(cand):
                    return float(cand)
        else:
            for cand in (last_p, prev_p):
                if _is_finite(cand):
                    return float(cand)
    except Exception as e:
        print(f"[market_brief] fast_info 보완 실패 {ticker}: {e}")
    return None

# ── 휴장 판정 (하드코딩 X — 데이터 추론 + 라이브러리 교차검증) ──
# 보정 레이어: exchange_calendars가 session=True로 '잘못' 보고하는 실제 휴장일만 교정.
# 전체 휴장일 목록이 아니라 '라이브러리 누락분'만 등재한다(장 시작 전 오탐 방지).
CALENDAR_PATCH = {
    "한국": {
        "2026-06-03",   # 제9회 전국동시지방선거 (임시공휴일) — 라이브러리 누락
        "2026-07-17",   # 제헌절 (2026 공휴일 재지정) — 라이브러리 누락
    },
    "미국": set(),
}


def _verify_with_calendar(region: str, date_str: str):
    """exchange_calendars 교차검증. 모르면 None. 보정 레이어가 최우선."""
    # 라이브러리가 놓친 휴장일 — 무조건 휴장(False)으로 교정
    if date_str in CALENDAR_PATCH.get(region, set()):
        return False
    try:
        import exchange_calendars as xcals
        import pandas as pd
        code = "XKRX" if region == "한국" else "XNYS"
        cal = xcals.get_calendar(code)
        return bool(cal.is_session(pd.Timestamp(date_str)))
    except Exception as e:
        print(f"[calendar] {region} {date_str} 검증 불가: {e}")
        return None


def _latest_data_date(region_data: dict):
    dates = [
        d.get("last_date", "")[:10]
        for d in region_data.values()
        if d.get("last_date")
    ]
    return max(dates) if dates else None


def _prev_session_date(region: str, from_date):
    """from_date 이전의 직전 거래일 (캘린더 우선, 실패 시 평일)."""
    from datetime import date as _date
    d = from_date - timedelta(days=1)
    if not isinstance(d, _date):
        d = d.date() if hasattr(d, "date") else d
    for _ in range(15):
        if d.weekday() < 5:
            cal = _verify_with_calendar(region, d.strftime("%Y-%m-%d"))
            if cal is not False:  # True 또는 None
                return d
        d -= timedelta(days=1)
    return from_date - timedelta(days=1)


def _expected_session_date(region: str, now_local):
    """지금 시점에 '최신으로 기대하는' 거래일.
    장전/주말/휴장 → 직전 거래일, 마감 후 개장일 → 오늘.
    (금→월 주말 갭만으로 금요일 데이터를 stale 처리하지 않기 위함)"""
    today = now_local.date() if hasattr(now_local, "date") else now_local
    if today.weekday() >= 5:
        return _prev_session_date(region, today)
    cal_open = _verify_with_calendar(region, today.strftime("%Y-%m-%d"))
    if cal_open is False:
        return _prev_session_date(region, today)
    if not _is_after_close(region, now_local):
        return _prev_session_date(region, today)
    return today


# 시장별 개장/마감 시각 (현지 기준)
MARKET_HOURS = {
    "한국": {"open": (9, 0),  "close": (15, 30)},
    "미국": {"open": (9, 30), "close": (16, 0)},
}


def _is_after_close(region: str, now_local) -> bool:
    """해당 시장이 오늘 마감했는지 (마감 30분 후부터 True).
    스케줄러(kr_close 16:00 KST / us_close 16:30 ET)와 동일 기준."""
    h, m = MARKET_HOURS[region]["close"]
    close_min = h * 60 + m + 30          # 마감 + 30분 버퍼
    now_min   = now_local.hour * 60 + now_local.minute
    return now_min >= close_min


def _has_today_session_data(
    region_data: dict,
    today: str,
    keys: tuple | list | None = None,
) -> bool:
    """해당 시장 데이터에 오늘(현지) 거래일 봉 + 유효 price가 있는지"""
    if not region_data:
        return False
    subset = (
        {k: region_data[k] for k in keys if k in region_data}
        if keys is not None
        else region_data
    )
    if not subset:
        return False
    if _latest_data_date(subset) != today:
        return False
    return any(
        (d.get("last_date") or "").startswith(today) and d.get("price") is not None
        for d in subset.values()
    )


def get_market_status(region_data: dict, region: str, now_local) -> dict:
    """휴장 판정 — 하드코딩 리스트 없이 데이터에서 추론 + 캘린더 교차검증
    region_data: market_data["미국"] 또는 market_data["한국"]
    now_local:   해당 시장 현지 시각 (미국=ET, 한국=KST)

    ⚠️ "오늘 데이터 없음 = 휴장" 추론은 장 마감 이후에만 유효.
       장 시작 전(PRE_OPEN)엔 오늘 데이터가 없는 게 정상이므로 별도 처리한다.
    """
    today = now_local.strftime("%Y-%m-%d")

    # 1) 주말은 확정
    if now_local.weekday() >= 5:
        return {
            "status": "CLOSED",
            "reason": "주말",
            "last_trading_day": _latest_data_date(region_data),
            "confidence": "확정",
        }

    # 2) 데이터 자체가 없으면 판정 불가
    if not region_data:
        return {
            "status": "UNKNOWN",
            "reason": "데이터 수집 실패",
            "last_trading_day": None,
            "confidence": "없음",
        }

    latest = _latest_data_date(region_data)

    # 3) 오늘 데이터가 있으면 개장 확정
    if latest == today:
        return {
            "status": "OPEN",
            "reason": "",
            "last_trading_day": latest,
            "confidence": "확정",
        }

    # 4) ⚠️ 아직 마감 전이면 오늘 데이터 없는 게 정상 → 휴장 추론 금지
    if not _is_after_close(region, now_local):
        cal_open = _verify_with_calendar(region, today)
        if cal_open is False:
            return {
                "status": "CLOSED",
                "reason": "공휴일(캘린더)",
                "last_trading_day": latest,
                "confidence": "확정",
            }
        # True 또는 None → 개장일로 간주하고 진행
        return {
            "status": "PRE_OPEN",
            "reason": "장 시작 전 (오늘 데이터 미생성은 정상)",
            "last_trading_day": latest,
            "confidence": "확정" if cal_open is True else "추정",
        }

    # 5) 마감 후인데 오늘 데이터 없음 → 여기서만 휴장 추론이 유효
    cal_open = _verify_with_calendar(region, today)
    if cal_open is False:
        return {
            "status": "CLOSED",
            "reason": "공휴일",
            "last_trading_day": latest,
            "confidence": "확정",   # 데이터+캘린더 일치
        }
    if cal_open is True:
        # 불일치 — 캘린더는 개장인데 마감 후에도 데이터가 없음
        # ⚠️ 자가 진단: CALENDAR_PATCH에 없는 신규 휴장일 의심 신호.
        #   한 사이클 안에 사람이 발견 → PATCH에 추가하는 자가 보정 루프의 핵심.
        print(
            f"⚠️ [calendar] {region} {today}: 라이브러리 미반영 휴장일 의심. "
            f"마감 후인데 데이터 없음 + 캘린더는 개장. "
            f"KRX 확인 후 CALENDAR_PATCH 추가 검토 필요"
        )
        return {
            "status": "UNKNOWN",
            "reason": "캘린더상 개장일이나 마감 후에도 데이터 없음 — 수집 실패 또는 신규 휴장일",
            "last_trading_day": latest,
            "confidence": "불일치",
        }
    # 캘린더도 모름 → 데이터 추론만 신뢰
    return {
        "status": "CLOSED",
        "reason": "휴장 추정 (캘린더 검증 불가)",
        "last_trading_day": latest,
        "confidence": "추정",
    }


def get_next_trading_day(region: str, from_date, max_days: int = 10):
    """다음 거래일 — 캘린더 우선, 실패 시 평일 기준"""
    d = from_date + timedelta(days=1)
    for _ in range(max_days):
        if d.weekday() < 5:
            cal_open = _verify_with_calendar(region, d.strftime("%Y-%m-%d"))
            if cal_open is not False:   # True 또는 None이면 거래일로 간주
                return d
        d += timedelta(days=1)
    return from_date + timedelta(days=1)


TICKERS = {
    "미국": {
        "SPY":  "S&P 500",
        "RSP":  "S&P 500 동일가중",   # 시장 폭 판단 핵심
        "QQQ":  "NASDAQ 100",
        "DIA":  "DOW Jones",
        "IWM":  "러셀 2000",           # 중소형주
    },
    "섹터": {
        "SMH": "반도체",
        "XLK": "기술",
        "XLF": "금융",
        "XLE": "에너지",
        "XLV": "헬스케어",
    },
    "한국": {
        "^KS11":     "KOSPI",
        "^KQ11":     "KOSDAQ",
        "005930.KS": "삼성전자",
        "000660.KS": "SK하이닉스",
    },
    "심리지표": {
        "^VIX":      "VIX 공포지수",
        "DX-Y.NYB":  "달러 인덱스(DXY)",
        "KRW=X":     "원/달러(USD/KRW)",
        "2YY=F":     "미국 2년물 금리",
        "^TNX":      "미국 10년물 금리",
    },
    "크립토": {
        "BTC-USD": "비트코인",
        "ETH-USD": "이더리움",
        "IBIT":    "비트코인 ETF",
        "COIN":    "Coinbase",
    },
}

KR_INDEX_TICKERS = ("^KS11", "^KQ11")
KR_MEGA_TICKERS = ("005930.KS", "000660.KS")

# 장전 시황 선물 — yfinance continuous contract (24h 근사)
FUTURES_TICKERS: dict[str, dict[str, str]] = {
    "미국": {
        "ES=F": "S&P500 선물",
        "NQ=F": "나스닥100 선물",
        "YM=F": "다우 선물",
        "RTY=F": "러셀2000 선물",
    },
}
FUTURES_FOR_BRIEF: dict[str, dict[str, tuple[str, ...]]] = {
    "kr_premarket": {"미국": ("ES=F", "NQ=F", "YM=F")},
    "us_premarket": {"미국": ("ES=F", "NQ=F", "YM=F", "RTY=F")},
}
FUTURES_REGION_LABEL = {
    ("kr_premarket", "미국"): "간밤 미국 선물",
    ("us_premarket", "미국"): "미국 장전 선물",
}
FUTURES_FLAT_TOL_PCT = 0.05

# 특징주 스캔 유니버스 (유동성·섹터 대표 — 제공 목록 밖 종목 언급 금지)
US_MOVER_CANDIDATES: dict[str, str] = {
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "NVIDIA", "AMD": "AMD",
    "AVGO": "Broadcom", "MRVL": "Marvell", "MRNA": "Moderna", "GOOGL": "Alphabet",
    "META": "Meta", "AMZN": "Amazon", "TSLA": "Tesla", "COIN": "Coinbase",
    "MSTR": "MicroStrategy", "SMCI": "Super Micro", "INTC": "Intel",
    "QCOM": "Qualcomm", "CRM": "Salesforce", "ORCL": "Oracle", "NFLX": "Netflix",
    "JPM": "JPMorgan", "LLY": "Eli Lilly", "UNH": "UnitedHealth", "PFE": "Pfizer",
    "BA": "Boeing", "HOOD": "Robinhood", "RIOT": "Riot Platforms",
    "MARA": "Marathon Digital", "PLTR": "Palantir", "UBER": "Uber",
}

KR_MOVER_CANDIDATES: dict[str, str] = {
    "005930.KS": "삼성전자", "000660.KS": "SK하이닉스", "035420.KS": "NAVER",
    "005380.KS": "현대차", "051910.KS": "LG화학", "035720.KS": "카카오",
    "006400.KS": "삼성SDI", "373220.KS": "LG에너지솔루션", "207940.KS": "삼성바이오",
    "068270.KS": "셀트리온", "000270.KS": "기아", "105560.KS": "KB금융",
}

# 종가 표기: 금리·환율은 $ 접두 없이
_RATE_TICKERS = frozenset({"^TNX", "^IRX", "^FVX", "^TYX", "2YY=F"})
_FX_TICKERS = frozenset({"KRW=X", "USDKRW=X"})


def _format_level(ticker: str, price) -> str:
    if not _is_finite(price):
        return "—"
    try:
        p = float(price)
    except (TypeError, ValueError):
        return str(price)
    if ticker in _RATE_TICKERS:
        return f"{p:.3f}%"
    if ticker in _FX_TICKERS:
        return f"{p:,.2f}"
    if ticker in ("DX-Y.NYB", "^VIX"):
        return f"{p:.2f}"
    return f"{p:,.2f}"


def _format_quote_line(ticker: str, d: dict) -> str:
    """종가 / 등락률 / 거래량 한 줄."""
    chg = d.get("change_pct")
    if not _is_finite(chg) or not _is_finite(d.get("price")):
        return f"{d.get('name', ticker)}({ticker}) — 가격/등락률 없음"
    chg = float(chg)
    arrow = "▲" if chg > 0 else ("▼" if chg < 0 else "→")
    level = _format_level(ticker, d.get("price"))
    vol = d.get("volume_ratio")
    vol_s = f"거래량 {vol}%" if vol not in (None, 0, 0.0) else "거래량 —"
    return f"{d.get('name', ticker)}({ticker}) {level} / {arrow}{abs(chg)}% / {vol_s}"


def _futures_direction(change_pct: float, *, flat_tol: float = FUTURES_FLAT_TOL_PCT) -> str:
    if not _is_finite(change_pct):
        return "보합"
    chg = float(change_pct)
    if chg > flat_tol:
        return "상승"
    if chg < -flat_tol:
        return "하락"
    return "보합"


def _futures_direction_label(direction: str) -> str:
    return {"상승": "상승중", "하락": "하락중", "보합": "보합권"}.get(direction, "보합권")


def _futures_group_tone(quotes: list[dict]) -> str:
    if not quotes:
        return "보합세"
    ups = sum(1 for q in quotes if q.get("direction") == "상승")
    downs = sum(1 for q in quotes if q.get("direction") == "하락")
    if ups > downs:
        return "상승세"
    if downs > ups:
        return "하락세"
    return "보합세"


def _pack_futures_quote(
    ticker: str,
    name: str,
    last: float,
    prev: float,
    *,
    as_of: str,
) -> dict:
    chg = (float(last) - float(prev)) / float(prev) * 100 if prev else 0.0
    direction = _futures_direction(chg)
    return {
        "ticker": ticker,
        "name": name,
        "price": round(float(last), 2),
        "ref_price": round(float(prev), 2),
        "change_pct": round(chg, 2),
        "direction": direction,
        "direction_label": _futures_direction_label(direction),
        "as_of": as_of,
    }


def _fetch_futures_quote(ticker: str, name: str, *, as_of: str) -> dict | None:
    """선물 현재가 vs 전일 정산가 — 장전 실시간 방향."""
    for attempt in range(3):
        try:
            t = yf.Ticker(ticker)
            fi = t.fast_info
            last = getattr(fi, "last_price", None)
            prev = getattr(fi, "previous_close", None)
            if not _is_finite(prev):
                prev = getattr(fi, "regular_market_previous_close", None)
            if _is_finite(last) and _is_finite(prev) and float(prev) != 0:
                return _pack_futures_quote(ticker, name, float(last), float(prev), as_of=as_of)
            hist = t.history(period="3d", interval="1h")
            if hist is not None and not hist.empty:
                hist = hist[hist["Close"].apply(_is_finite)]
                if len(hist) >= 2:
                    last_v = float(hist["Close"].iloc[-1])
                    prev_v = float(hist["Close"].iloc[-2])
                    return _pack_futures_quote(ticker, name, last_v, prev_v, as_of=as_of)
        except Exception as e:
            print(f"[market_brief] 선물 {ticker} 수집 실패 ({attempt + 1}/3): {e}")
        time.sleep(2 ** attempt)
    print(f"[market_brief] ⚠️ 선물 {ticker} 수집 실패")
    return None


def collect_futures_snapshot(brief_type: str, *, as_of: str | None = None) -> dict | None:
    """장전 시황용 선물 스냅샷."""
    spec = FUTURES_FOR_BRIEF.get(brief_type)
    if not spec:
        return None
    ts = as_of or datetime.now(pytz.UTC).isoformat()
    markets: list[dict] = []
    for region, tickers in spec.items():
        names = FUTURES_TICKERS.get(region, {})
        quotes: list[dict] = []
        jobs = [(tk, names.get(tk, tk)) for tk in tickers if tk in names]
        with ThreadPoolExecutor(max_workers=min(4, len(jobs) or 1)) as pool:
            futs = {
                pool.submit(_fetch_futures_quote, tk, nm, as_of=ts): tk
                for tk, nm in jobs
            }
            for fut in as_completed(futs):
                try:
                    q = fut.result()
                except Exception:
                    q = None
                if q:
                    quotes.append(q)
        quotes.sort(key=lambda q: tickers.index(q["ticker"]) if q["ticker"] in tickers else 99)
        if not quotes:
            continue
        label = FUTURES_REGION_LABEL.get((brief_type, region), f"{region} 선물")
        markets.append({
            "region": region,
            "label": label,
            "tone": _futures_group_tone(quotes),
            "quotes": quotes,
        })
    if not markets:
        return None
    return {"brief_type": brief_type, "as_of": ts, "markets": markets}


def format_futures_prompt_text(snapshot: dict | None) -> str:
    if not snapshot or not snapshot.get("markets"):
        return ""
    lines = ["[선물 지수 — 전일 정산 대비, 장전 리포트 상단에 코드 주입됨]"]
    for mkt in snapshot["markets"]:
        lines.append(f"\n▶ {mkt['label']} — {mkt.get('tone', '보합세')}")
        for q in mkt.get("quotes") or []:
            chg = q.get("change_pct")
            arrow = "▲" if chg and chg > 0 else ("▼" if chg and chg < 0 else "→")
            lines.append(
                f"  · {q['name']}({q['ticker']}) {q.get('price')} "
                f"{arrow}{abs(chg or 0):.2f}% · {q.get('direction_label')}"
            )
    return "\n".join(lines)


def format_futures_header_block(snapshot: dict | None, brief_type: str) -> str:
    """장전 본문 최상단 마크다운."""
    if not snapshot or not snapshot.get("markets"):
        return ""
    tz_name = "Asia/Seoul" if brief_type.startswith("kr") else "America/New_York"
    try:
        as_of_dt = datetime.fromisoformat(snapshot["as_of"].replace("Z", "+00:00"))
        local = as_of_dt.astimezone(pytz.timezone(tz_name))
        as_of_label = local.strftime("%Y-%m-%d %H:%M") + (" KST" if tz_name == "Asia/Seoul" else " ET")
    except (TypeError, ValueError):
        as_of_label = snapshot.get("as_of", "")

    lines = [
        "### 📡 선물 지수 스냅샷",
        f"*기준: {as_of_label} · 전일 정산 대비*",
        "",
    ]
    flag = "🇺🇸" if brief_type.startswith("kr") else "🇺🇸"
    for mkt in snapshot["markets"]:
        tone = mkt.get("tone", "보합세")
        lines.append(f"{flag} **{mkt['label']} — {tone}**")
        for q in mkt.get("quotes") or []:
            chg = q.get("change_pct")
            if not _is_finite(chg):
                continue
            chg_f = float(chg)
            arrow = "▲" if chg_f > 0 else ("▼" if chg_f < 0 else "→")
            lines.append(
                f"- {q['name']}({q['ticker']}) "
                f"{q.get('price'):,.2f} {arrow}{abs(chg_f):.2f}% · **{q.get('direction_label')}**"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _inject_futures_header(analysis: str, block: str) -> str:
    if not block:
        return analysis
    text = re.sub(
        r"###\s*📡[^\n]*선물[\s\S]*?(?=\n---\s*\n|\n###\s*0\.|\n###\s*1\.)",
        "",
        analysis,
        count=1,
    ).strip()
    return block.rstrip() + "\n\n---\n\n" + text


def _is_kr_symbol(ticker: str) -> bool:
    t = (ticker or "").upper()
    return t.startswith("^K") or t.endswith(".KS") or t.endswith(".KQ")


STRICT_RULE = """
[절대 원칙]
1. 제공된 데이터·뉴스 요약·특징주 목록에 없는 내용 창작 금지
2. 근거 없는 표현 ("외국인 매수세" 등) 금지 — 제공 수치·뉴스 요약만
3. 데이터로 설명 불가하면 "데이터상 원인 불명확"으로 표기
4. 숫자 없는 강세/약세 표현 금지 — 지수/종목명 + % 포함
5. 직전 전망이 틀렸을 때 명확히 인정하고 데이터 기반 원인 분석
6. 신뢰도는 상/중/하 세 단계만. '중상'·'중하' 금지
7. Fear & Greed / 크립토 / 특징주는 제공된 경우에만 언급
"""

AUDIENCE_RULE = """
[타깃 독자 — 반드시 염두]
- 어제 장을 종일 못 본 투자자. 3~5분 읽고 "무슨 일이 있었는지" + "내 포지션을 어떻게 대응할지"가 떠올라야 함
- 지수 나열만으로 끝내지 말 것. **촉매 → 연쇄 반응 → 시사점** 한 줄기 흐름 필수
- 마지막 전망·한줄요약은 "무엇을 지켜보며 어떻게 대응할지" actionable 하게
"""

NEWS_RULE = """
[뉴스·촉매 활용]
- [오늘의 촉매 뉴스]의 **제목+요약**은 근거로 사용 가능 (제목만 있을 때 추측 금지)
- 촉매를 ###1 흐름 섹션 **첫 문단**에 반드시 연결: 무슨 뉴스/정책 → 어떤 자산·섹터·종목 → 지수
- 특징주 급등락은 [특징주] 목록 + 가능하면 연결 촉매 뉴스로 설명
- 뉴스 인용: "📰 [제목] → [연결 지표/종목]" 별도 줄
- 제공 뉴스·특징주와 무관한 종목·이슈 창작 금지
"""

BRIEF_STYLE_RULE = """
[시황 작성 스타일]
1. ###1 흐름(4~7문장) → ###2 수치 스냅샷 → ###3 특징주·섹터 → 심리표 → 전망 → 한줄요약
2. 흐름 섹션: 시간·인과 순. "A 때문에 B, 그래서 C" 구조. 숫자는 흐름 안에 자연스럽게
3. 스냅샷 섹션: 종가/등락률/거래량 구조화 (흐름에서 이미 쓴 숫자는 생략 가능)
4. 특징주: [특징주] 목록에서 급등·급락 상위만, 각 1줄 (종목 +% + 가능한 촉매)
5. 전망: 앞 흐름·촉매가 **이어지는지/되돌림인지** 명시 + 검증 가능한 수치 조건
6. 전체 완결 — 중간 끊김 금지
"""

ENGINE_STRUCTURE_RULE = """
[시황 공통 구조 — 헤더는 반드시 ### N. 형식. "0." 단독 번호 금지]
0. (마감) 직전 전망 검증 / (장전) 직전 시장 전망 (참고) — 타입별 프롬프트 지시 따름
1. 📖 오늘 장의 흐름 — 촉매·연쇄·시사점 (필수, 4~7문장)
2. 핵심 수치 스냅샷 — 지수·섹터·시장폭
3. 특징주 & 섹터 — 급등락 종목 + 의미 (재나열 금지)
4. 📊 시장 심리 한 눈에
5. 🔮 전망 + 대응 (강세/약세 조건, 신뢰도, 핵심 체크)
6. 💡 한 줄 요약 — 완전한 한 문장으로 끝낼 것 (중간에 끊기·미완성 ** 금지)

[###0 전망 줄] 프롬프트 【복붙용】 문구(SIGNAL 포함)를 그대로 — 날짜만([YYYY-MM-DD]) 쓰지 말 것

[가독성] 장을 못 본 사람이 스캔해도 흐름이 읽혀야 함. 표·불릿 활용, 긴 줄글 단락 지양
"""

PREMARKET_SECTION0_RULE = """
[장전 ###0 — 마감 리포트와 다름]
- 헤더: **### 0. 직전 시장 전망 (참고)** (「직전 전망 검증」 금지)
- 적중/빗나감·「검증 보류」·「실제 결과」·「판정」 항목 **작성 금지**
- 출처 / 전망 / 오늘 활용 / 채점 예정 4항만 — 프롬프트 【복붙용】 전망 줄 그대로
- **### 📡 선물 지수 스냅샷** 은 코드가 본문 최상단에 주입 — 중복 작성·수치 창작 금지
"""

BREADTH_RULE = """
[시장 폭(Breadth) 해석 — 미국 지수/섹터 언급 시 반드시 적용]
1. SPY vs RSP 갭이 오늘의 진짜 스토리다
   - RSP > SPY (갭 0.5%p 이상): 대형주만 약세, 시장 전반은 견조
     → "지수 하락 = 시장 붕괴"로 서술 금지. "대형 기술주에 국한된 조정"으로 서술
   - RSP < SPY (갭 0.5%p 이상): 소수 대형주가 지수를 떠받침 = 실제론 더 약한 장
   - 갭이 0.5%p 이상이면 스냅샷에서 반드시 언급
2. 섹터 ETF로 원인을 특정할 것
   - 특정 섹터만 급락이면 "시장 전체"가 아니라 "XX 섹터 조정"으로 서술
   - 낙폭/상승폭 상위 2개 섹터만 언급 (5개 전부 나열 금지)
3. IWM(러셀2000)으로 로테이션 확인
   - 대형주 하락 + IWM 보합/상승 = 섹터 로테이션 (약세장 아님)
4. 섹터 데이터가 없으면 언급하지 말 것 (추측 금지)
"""

CROSS_MARKET_RULE = """
[한·미 교차시장 — 양방향, 반도체 축]
- 과거처럼 "미국→한국 일방"만 쓰지 말 것. **반도체(SMH·NVDA·삼성·SK하이닉스)는 양방향 연결 축**
- 미국 영향력이 더 크지만, 한국 마감(삼성·하이닉스 급등락)은 **같은 날·다음 미국장 SMH/QQQ에 역으로 시사**할 수 있음
- 괴리가 있으면 반드시 명시: "미국 SMH ▼인데 한국 반도체 ▲ → 역행, 이유는 [제공 촉매]"
- 장전 리포트: **직전 마감 이후~개장 전** 사이 확정된 양쪽 시장·뉴스·프리마켓 신호를 종합
- 마감 리포트: **오늘 해당 시장 세션 안에서** 무슨 일이 있었는지 확정 스냅샷 + 교차시장 함의
"""

CLOSE_REPORT_RULE = """
[마감 리포트 전용 — us_close / kr_close, ###1~6에 최우선 적용]
1. **시간축**: 오늘 해당 시장 정규장은 **이미 마감**. 독자에게 "오늘 장 결과"를 알려주는 리포트.
2. **과거형 서술**: ###1~6에서 "주목", "지켜볼", "예정", "앞으로", "개장 전" 금지.
   - 세션 중 이미 끝난 이벤트(연준 연설·경제지표·실적): **무슨 일이 있었고 → 시장이 어떻게 반응했는지**까지 과거형으로.
   - ❌ "워시 연설 주목" / ✅ "워시 연설에서 금리 인상 시사 → SMH ▼3.5% 급락"
3. **###0 vs ###1 분리**
   - ###0 '전망:' 줄 = **장전 시점 인용**(그대로, 수정 금지). 미래형·'주목' 표현이 있어도 OK.
   - ###1~6 = 장전 문구 **복붙·반복 금지**. 오늘 세션에서 **실제로 일어난 일**만 간결히 재서술.
4. **###1 흐름**: 4~6문장, 시간순 한 줄기 — (1) 핵심 촉매 (2) 섹터·특징주 (3) 지수·시장폭 (4) 한국 연결(해당 시).
5. **판정 이유(###0)**: SPY/QQQ 등 **채점 벤치마크** 중심. 장전 인용 속 한국·연설 '주목'은 배경만 — 실제 연설·반응은 결과로 서술.
"""


def _brief_type_rule(brief_type: str) -> str:
    """장전 vs 마감, 4종별 작성 목표."""
    rules = {
        "kr_premarket": """
[이 리포트 = 🇰🇷 한국 **장전** — 마감 리포트와 목표가 다름]
- 독자: 어제~오늘 아침까지 장을 못 본 사람 → **오늘 한국장 개장 전** 종합 브리핑
- 시간축: **간밤 미국 마감(확정) + 프리/애프터·뉴스·크립토 + 금리/환율** → 오늘 KOSPI 전망
- 마감 리포트(kr_close)처럼 "오늘 한국장이 어땠는지" 쓰지 말 것 — 아직 장 시작 전
- ###1 흐름: 미국 촉매 → SMH·금리 → 삼성·하이닉스 **개장 시 주목** (미국→한국 경로 우선, 역방향 신호 있으면 병기)
- ###0: 직전 us_close 한국장 전망 **참고 인용만** — 채점은 kr_close에서
""",
        "kr_close": """
[이 리포트 = 🇰🇷 한국 **마감** — 장전과 목표가 다름]
- 독자: 오늘 한국장을 못 본 사람 → **오늘 세션 확정 결과** + 다음 세션 대응
- 시간축: **오늘 KOSPI/KOSDAQ/삼성·하이닉스 마감** + 오늘 촉매 + 간밤 미국과의 괴리/연동
- 장전(kr_premarket) 전망을 ###0에서 채점. 장전 내용을 그대로 반복하지 말고 **오늘 실제 결과** 중심
- ###1 흐름: 오늘 한국 **확정** 촉매 → 반도체·대형주 → 지수. 미국 SMH와 역행/동행 구분
- 한국 반도체 급등락은 **다음 미국 SMH·NVDA** 관점 1문장 포함 (양방향)
""",
        "us_premarket": """
[이 리포트 = 🇺🇸 미국 **장전** — 마감 리포트와 목표가 다름]
- 독자: 어제~오늘 아침(ET)까지 못 본 사람 → **오늘 미국장 개장 전** 종합 브리핑
- 시간축: **오늘 한국 마감(방금 끝남) + 직전 미국 세션 + 간밤 뉴스·금리·크립토** → 오늘 SPY 전망
- 마감(us_close)처럼 "오늘 미국장이 어땠는지" 쓰지 말 것 — 아직 미국 장 시작 전
- ###1 흐름: **한국 마감(삼성·하이닉스·KOSPI)을 오늘 미국장 핵심 입력**으로 — 예전 일방향보다 양방향
- 반도체: 한국 ▲ + 미국 직전 SMH ▼ 같은 괴리면 "오늘 미국 반도체 주목"으로 연결
- ###0: us_close 한국장 전망은 **참고 인용만** (채점은 kr_close)
""",
        "us_close": """
[이 리포트 = 🇺🇸 미국 **마감** — 장전과 목표가 다름]
- 독자: 오늘 미국장을 못 본 사람 → **오늘 US 세션 확정 결과** + 다음 한국장 함의
- 시간축: **오늘 SPY/QQQ/SMH 마감** + **오늘 장 중** 촉매·연설·특징주 + 오늘 한국 마감과의 연결
- 장전(us_premarket) 전망은 ###0에서만 인용·채점. ###1~6에 장전 문구 복붙 금지
- ###1: **오늘 미국장이 어땠는지** 4~6문장 (촉매→반응→섹터→지수). 이미 끝난 연설·지표는 **결과까지** 과거형
- "주목·지켜볼·예정"은 ###5(다음 한국장 전망)에만 — ###1~4 금지
- 마지막: **다음 한국장(삼성·하이닉스)** 시사점은 ###5에
""",
    }
    return rules.get(brief_type, "")


def _verify_block(
    *,
    bench: str = "—",
    result_metrics: str = "",
    mode: str = "score",
    extra: str = "",
    cite: str = "",
    source: str = "",
    score_when: str = "",
    secondary: list[tuple[str, str]] | None = None,
) -> str:
    """공통 ###0 섹션.
    mode=score: 마감 — 벤치마크로 적중 판정
    mode=defer: 장전 — 직전 전망 참고 인용 (채점 없음)
    cite: Mongo '전망:' 복붙 문구 (SIGNAL + 근거)
    """
    cite_line = (cite or "").strip() or "전망 기록 없음 (SIGNAL/본문 미확인)"
    if mode == "defer":
        return f"""### 0. 직전 시장 전망 (참고)
(직전 시황 없으면 생략)
**장전 리포트** — 개장 전 참고용입니다. 적중/빗나감 판정은 하지 않습니다.

- **출처**: {source or "직전 마감 시황"}
- **전망**: {cite_line}
  ※ Mongo 인용 그대로 — 수정·삭제 금지
- **오늘 활용**: {extra or "간밤 확정 수치·뉴스와 연결해 오늘 개장 전 전망 근거로 서술"}
- **채점 예정**: {score_when or "해당 시장 마감 시황에서 벤치마크 등락률로 판정"}
"""

    blocks = [f"""### 0. 직전 전망 검증
(직전 시황 없으면 생략)
**전망 인용 없이 적중/빗나감 판정 금지.** 아래 '전망:'은 Mongo 직전 **장전** 시황 인용(수정 금지).

- 전망: {cite_line}
  ※ 위 줄은 장전 시점 그대로 — '주목' 등 미래형 표현이 있어도 수정하지 말 것
- 실제 결과: {result_metrics}
- 판정: 적중 / 부분 적중 / 빗나감 중 하나
  (인용이 "전망 기록 없음"이면 판정=**검증 불가** — 적중/빗나감 금지)
- 판정 이유: **벤치마크 {bench} 등락**과 SIGNAL 대응 1~2문장 (과거형)
  [판정 기준 — 벤치마크 {bench}, 중립 밴드 ±0.3%]
  · BULL → {bench} ≥ +0.3% 적중, |Δ|<0.3% 부분 적중, ≤ −0.3% 빗나감
  · BEAR → {bench} ≤ −0.3% 적중, |Δ|<0.3% 부분 적중, ≥ +0.3% 빗나감
  · NEUTRAL → |Δ|<0.3% 적중, 그 외 부분 적중. NEUTRAL을 상승/하락 전망으로 해석해 빗나감 금지
- 다음 전망 반영: 다음 시황 생성에 바로 쓸 규칙 1줄 (추상적 감상 금지)
"""]
    for title, sec_cite in secondary or []:
        sec = (sec_cite or "").strip()
        if not sec:
            continue
        blocks.append(f"""
#### {title}
- 전망: {sec}
  ※ 위 줄을 수정·삭제·'-'로 바꾸지 말 것
- 실제 결과: {result_metrics}
- 판정: 적중 / 부분 적중 / 빗나감 / 검증 불가 중 하나
- 판정 이유: 인용 전망과 실제를 대응 (1문장)
""")
    blocks.append(extra or "")
    return "\n".join(blocks).rstrip() + "\n"


def _psych_block(*, impact_header: str) -> str:
    return f"""### 📊 시장 심리 한 눈에
제공된 심리지표만 사용. 없는 행 생략.
| 지표 | 현재값 | 전일 대비 | {impact_header} |
|------|--------|-----------|------------------|
| VIX | | | |
| 달러 인덱스(DXY) | | | |
| 원/달러(USD/KRW) | | | |
| 미국 2년물 금리 | | | |
| 미국 10년물 금리 | | | |
| 10년-2년 금리차 | | | |
"""


def _outlook_block(*, title: str, condition_examples: str) -> str:
    return f"""### 🔮 {title}
서술 2~3문장: 앞 흐름·촉매가 **이어지는지 / 되돌림인지** 연결.

**결론: 강세 우위 / 약세 우위 / 중립 (조건부 XX 우위)**
- 강세 조건: 검증 가능한 수치 조건
- 약세 조건: 검증 가능한 수치 조건
{condition_examples}
※ 모호한 조건 금지 — 지표명 + 임계치 필수

**대응**: 장을 못 본 독자가 내일/다음 세션에 취할 수 있는 관점 1~2문장
(예: 반도체 급등 후 SMH 확인, 금리 하락 수혜 섹터 vs 되돌림 경계 등 — 제공 데이터 범위)

신뢰도: 상/중/하
핵심 체크: 이번에 볼 것 1개
"""


WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]
BRIEF_MAX_TOKENS = 4096
BRIEF_MACRO_NEWS_PER_SOURCE = 3   # RSS 소스당 (기본 fetch_macro_news 5 대비 input·Haiku 절감)
BRIEF_CLAUDE_MODEL = "claude-sonnet-4-5-20250929"
_YF_POOL_WORKERS = 12


def _call_brief_claude(prompt: str, brief_type: str) -> str:
    """Claude 스트리밍 호출 — 긴 응답에서 연결 끊김 방지."""
    client = anthropic.Anthropic(timeout=600.0)
    last_err = None
    for attempt in range(5):
        try:
            parts: list[str] = []
            with client.messages.stream(
                model=BRIEF_CLAUDE_MODEL,
                max_tokens=BRIEF_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                for chunk in stream.text_stream:
                    parts.append(chunk)
            text = "".join(parts).strip()
            if text:
                return text
            raise RuntimeError("Claude empty response")
        except anthropic.APIConnectionError as e:
            last_err = e
            if attempt < 4:
                wait = 10 * (attempt + 1)
                print(
                    f"[market_brief] Claude 연결 실패 ({brief_type}) "
                    f"— {wait}s 후 재시도 ({attempt + 1}/4)"
                )
                time.sleep(wait)
    raise last_err

# 시황 4종 서큘레이션
# - 마감 리포트만 "같은 날 장전 전망"을 수치로 채점
# - 장전 리포트는 직전 마감의 교차시장 전망을 인용하되 대개 검증 보류
# - CIRCULATION_FEED: Mongo 직전 시황의 SIGNAL·핵심 수치를 다음 시황에 명시 주입
BRIEF_TYPES = {
    "kr_premarket": {
        "label":   "🇰🇷 한국장 전 시황",
        "market":  "한국",
        # 직전 미국 마감의 "다음 한국장" 전망을 인용 (장전이라 수치 채점 보류)
        "verify":  "us_close",
        "predict": "오늘 한국장",
        "verify_mode": "defer",
    },
    "kr_close": {
        "label":   "🇰🇷 한국장 마감 시황",
        "market":  "한국",
        # 오늘 한국 장전 전망을 KOSPI로 채점
        "verify":  "kr_premarket",
        "predict": "다음 거래일 한국장",
        "verify_mode": "score",
    },
    "us_premarket": {
        "label":   "🇺🇸 미국장 전 시황",
        "market":  "미국",
        # 직전 미국 마감은 한국장 전망 → 미국 장전에서 SPY 채점 금지, 보류
        "verify":  "us_close",
        "predict": "오늘 미국장",
        "verify_mode": "defer",
    },
    "us_close": {
        "label":   "🇺🇸 미국장 마감 시황",
        "market":  "미국",
        # 오늘 미국 장전 전망을 SPY로 채점
        "verify":  "us_premarket",
        "predict": "다음 거래일 한국장",
        "verify_mode": "score",
    },
}

# us_close → kr_premarket → kr_close → us_premarket → us_close …
# 각 시황이 Mongo에서 읽을 "직전 교차시장 리포트" (검증 짝과 별도, 분석 입력용)
CIRCULATION_FEED = {
    "kr_premarket": [
        {
            "type": "us_close",
            "role": "직전 미국 마감 → 오늘 한국장 선행 입력",
            "tickers": ("SPY", "RSP", "QQQ", "IWM", "SMH", "XLF", "^VIX"),
        },
    ],
    "kr_close": [
        {
            "type": "kr_premarket",
            "role": "오늘 한국 장전 전망 (마감으로 채점)",
            "tickers": ("^KS11", "^KQ11", "005930.KS", "000660.KS"),
        },
        {
            "type": "us_close",
            "role": "간밤 미국→한국 전망 맥락",
            "tickers": ("SPY", "QQQ", "SMH", "XLF"),
        },
    ],
    "us_premarket": [
        {
            "type": "kr_close",
            "role": "오늘 한국 마감 → 오늘 미국장 선행 입력 (핵심)",
            "tickers": ("^KS11", "^KQ11", "005930.KS", "000660.KS"),
        },
        {
            "type": "us_close",
            "role": "직전 미국 세션 마감 맥락",
            "tickers": ("SPY", "RSP", "QQQ", "SMH", "XLF", "^VIX"),
        },
    ],
    "us_close": [
        {
            "type": "us_premarket",
            "role": "오늘 미국 장전 전망 (마감으로 채점)",
            "tickers": ("SPY", "QQQ", "XLF", "SMH"),
        },
        {
            "type": "kr_close",
            "role": "한국 마감이 미국장에 미친 흐름",
            "tickers": ("^KS11", "^KQ11", "005930.KS", "000660.KS"),
        },
    ],
}


def _get_tomorrow_events(now) -> str:
    """yfinance로 다음날 주요 실적발표 일정 수집 (병렬)."""
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    lines = [f"[내일({tomorrow}) 주요 일정]"]

    watch_tickers = [
        "ORCL", "CHWY", "AVGO", "ADBE", "FDX", "COST", "WMT", "TGT", "HD", "LOW",
        "BABA", "DE", "ROST", "AMAT", "MSFT", "NVDA", "CRM", "PANW",
    ]

    def _earn_on(t: str) -> str | None:
        try:
            cal = yf.Ticker(t).calendar
            if cal is None or cal.empty:
                return None
            col0 = cal.columns[0]
            earn_date = str(
                col0.date() if hasattr(col0, "date") else col0
            )[:10]
            return t if earn_date == tomorrow else None
        except Exception:
            return None

    earnings_tomorrow: list[str] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for sym in pool.map(_earn_on, watch_tickers):
            if sym:
                earnings_tomorrow.append(sym)

    if earnings_tomorrow:
        lines.append(f"실적발표: {', '.join(earnings_tomorrow)}")
    else:
        lines.append("실적발표: 주요 종목 없음")

    lines.append("※ CPI/FOMC/고용지표 등 주요 이벤트는 뉴스에서 확인")
    return "\n".join(lines)


def _next_kr_trading_label(now_kst: datetime) -> tuple[str, str]:
    """한국 다음 거래일 라벨.
    - 달력상 내일이 거래일이면 '내일'
    - 주말·휴장이면 '다음 거래일 (MM/DD 요일)'
    반환: (prompt_label, short_mmdd_weekday)
    """
    next_kr = get_next_trading_day("한국", now_kst)
    next_d = next_kr.date() if hasattr(next_kr, "date") else next_kr
    wd = WEEKDAY_KR[next_kr.weekday()]
    short = f"{next_kr.strftime('%m/%d')}({wd})"
    tomorrow = (now_kst + timedelta(days=1)).date()
    if next_d == tomorrow:
        return "내일", short
    return f"다음 거래일 ({next_kr.strftime('%m/%d')} {wd}요일)", short


def _fetch_ticker(ticker: str, name: str, now_ex: datetime | None = None) -> dict | None:
    """티커별 데이터 수집 — 날짜/타임존 정규화 + 장중 데이터 제외 + 재시도 3회.
    now_ex: 백필/재생성 시 기준 시각(거래소 현지). None이면 현재 시각."""
    import time

    for attempt in range(3):
        try:
            # 한국 지수·종목은 period를 더 넉넉하게 (거래일 확보)
            period = "15d" if _is_kr_symbol(ticker) else "10d"
            hist = yf.Ticker(ticker).history(period=period)

            if hist is None or hist.empty:
                time.sleep(2 ** attempt)
                continue

            # 타임존 정규화 — 거래소 현지 기준으로 날짜를 산출해야 하루 밀림 방지
            #   (한국 지수는 KST 자정 인덱스를 UTC로 바꾸면 전날로 밀려 off-by-one 발생)
            ex_tz = "Asia/Seoul" if _is_kr_symbol(ticker) else "America/New_York"
            tz = pytz.timezone(ex_tz)
            if hist.index.tz is None:
                # naive 일봉은 거래소 현지 거래일로 간주
                hist.index = hist.index.tz_localize(ex_tz)
            else:
                hist.index = hist.index.tz_convert(ex_tz)

            if now_ex is None:
                now_ex_local = datetime.now(tz)
            else:
                now_ex_local = (
                    tz.localize(now_ex) if now_ex.tzinfo is None
                    else now_ex.astimezone(tz)
                )
            today_ex = now_ex_local.date()

            # 백필: 기준일 이후 봉 제거
            hist = hist[hist.index.date <= today_ex]
            if hist.empty:
                time.sleep(2 ** attempt)
                continue

            # 마감 확정 = 스케줄/상태판정과 동일 (_is_after_close: 마감+30분)
            region = "한국" if _is_kr_symbol(ticker) else "미국"
            market_closed = _is_after_close(region, now_ex_local)

            last_dt = hist.index[-1].date()
            if last_dt == today_ex and not market_closed:
                hist = hist.iloc[:-1]  # 오늘 장중 불완전 데이터 제외
                if hist.empty:
                    time.sleep(2 ** attempt)
                    continue

            expected = _expected_session_date(region, now_ex_local)

            # NaN Close + 거래량만 있는 스텁 봉의 볼륨 보존 (drop 전에 기록)
            stub_volumes: dict = {}
            for idx, row in hist.iterrows():
                if not _is_finite(row["Close"]) and _is_finite(row.get("Volume")):
                    try:
                        stub_volumes[idx.date()] = int(row["Volume"])
                    except (TypeError, ValueError):
                        pass

            # Yahoo가 거래량만 채운 OHLC=NaN 스텁 봉 → fast_info로 Close 복구
            nan_patched = False
            if not hist.empty and not _is_finite(hist["Close"].iloc[-1]):
                fill = _fast_info_session_close(
                    ticker, prefer_previous=not market_closed
                )
                if fill is not None:
                    hist = hist.copy()
                    hist.iloc[-1, hist.columns.get_loc("Close")] = fill
                    nan_patched = True
                    print(
                        f"[market_brief] 🔧 {ticker} NaN Close → fast_info {fill:.4f} "
                        f"({hist.index[-1].date()})"
                    )

            # 복구 실패한 NaN Close 봉 제거
            hist = hist[hist["Close"].apply(_is_finite)]
            if len(hist) < 1:
                time.sleep(2 ** attempt)
                continue

            ld = hist.index[-1].date()
            current = float(hist["Close"].iloc[-1])
            volume = (
                int(hist["Volume"].iloc[-1])
                if _is_finite(hist["Volume"].iloc[-1]) and hist["Volume"].iloc[-1]
                else 0
            )
            session_patched = False
            prev_close = None

            # history에 기대 거래일 봉이 통째로 없는 경우(VIX 등 Fri→Tue 점프)
            if ld < expected and not market_closed:
                fill = _fast_info_session_close(ticker, prefer_previous=True)
                if fill is not None and abs(fill - current) > 1e-9:
                    prev_close = current
                    current = fill
                    ld = expected
                    volume = stub_volumes.get(expected, 0)
                    session_patched = True
                    print(
                        f"[market_brief] 🔧 {ticker} 누락 세션 {expected} "
                        f"← fast_info previous_close {fill:.4f} "
                        f"(prev={prev_close:.4f})"
                    )

            if prev_close is None:
                if len(hist) < 2:
                    time.sleep(2 ** attempt)
                    continue
                prev_close = float(hist["Close"].iloc[-2])

            if not _is_finite(current) or not _is_finite(prev_close) or prev_close == 0:
                time.sleep(2 ** attempt)
                continue

            weekday_str = WEEKDAY_KR[ld.weekday()]
            date_label = f"{ld.strftime('%Y-%m-%d')}({weekday_str})"

            change_pct = (current - prev_close) / prev_close * 100
            avg_volume = (
                int(hist["Volume"].mean())
                if _is_finite(hist["Volume"].mean()) and hist["Volume"].mean()
                else 0
            )
            vol_ratio = (
                round(volume / avg_volume * 100, 1) if avg_volume and volume else 0
            )

            days_old = (today_ex - ld).days
            stale = ld < expected

            if not _is_finite(change_pct):
                time.sleep(2 ** attempt)
                continue

            patch_tag = ""
            if nan_patched or session_patched:
                patch_tag = " [fast_info]"

            print(
                f"[market_brief] {'⚠️ STALE' if stale else '✅'} {ticker} {date_label} "
                f"{current:.2f} ({change_pct:+.2f}%) vol {vol_ratio}%"
                + (f" — expected={expected}" if stale or ld != today_ex else "")
                + patch_tag
            )

            return {
                "name":         name,
                "price":        round(current, 2),
                "change_pct":   round(change_pct, 2),
                "volume":       volume,
                "avg_volume":   avg_volume,
                "volume_ratio": vol_ratio,
                "last_date":    date_label,
                "stale":        stale,
                "stale_days":   days_old,
            }
        except Exception as e:
            print(f"[market_brief] ❌ {ticker} 오류 (시도 {attempt+1}): {e}")
            time.sleep(2 ** attempt)

    print(f"[market_brief] ⚠️ {ticker} 수집 실패 — 3회 모두 실패")
    return None


def get_market_data(
    now_et: datetime | None = None,
    now_kst: datetime | None = None,
) -> dict:
    """now_et/now_kst를 주면 그 시점 기준으로 봉을 자른다(백필/재생성용)."""
    result = {region: {} for region in TICKERS}
    jobs: list[tuple[str, str, str, datetime | None]] = []
    for region, tickers in TICKERS.items():
        now_ex = now_kst if region == "한국" else now_et
        for ticker, name in tickers.items():
            jobs.append((region, ticker, name, now_ex))

    with ThreadPoolExecutor(max_workers=_YF_POOL_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_ticker, ticker, name, now_ex=now_ex): (region, ticker)
            for region, ticker, name, now_ex in jobs
        }
        for fut in as_completed(futures):
            region, ticker = futures[fut]
            try:
                d = fut.result()
            except Exception:
                d = None
            if d:
                result[region][ticker] = d

    total = sum(len(v) for v in result.values())
    print(f"[market_brief] 총 {total}개 지수 수집 완료")
    return result


async def get_market_data_async(
    now_et: datetime | None = None,
    now_kst: datetime | None = None,
) -> dict:
    return await asyncio.to_thread(get_market_data, now_et=now_et, now_kst=now_kst)


def _flatten_market_data(market_data: dict) -> dict[str, dict]:
    """지수·섹터·크립토 등 flat dict — movers 중복 fetch 방지."""
    flat: dict[str, dict] = {}
    for region_data in (market_data or {}).values():
        if isinstance(region_data, dict):
            flat.update(region_data)
    return flat


def _mover_usable(d: dict | None) -> bool:
    return bool(
        d
        and not d.get("stale")
        and _is_finite(d.get("change_pct"))
    )


def fetch_fear_greed() -> dict | None:
    """CNN Fear & Greed (주식). 실패 시 None — 시황은 계속 생성."""
    import httpx
    from datetime import date as _date

    start = (_date.today().replace(year=_date.today().year - 1)).isoformat()
    url = f"https://production.dataviz.cnn.io/index/fearandgreed/graphdata/{start}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://edition.cnn.com/markets/fear-and-greed",
        "Origin": "https://edition.cnn.com",
    }
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            r = client.get(url, headers=headers)
            r.raise_for_status()
            payload = r.json()
        fg = payload.get("fear_and_greed") or {}
        score = fg.get("score")
        rating = fg.get("rating") or fg.get("description")
        if score is None:
            # fallback: last historical point
            hist = (payload.get("fear_and_greed_historical") or {}).get("data") or []
            if hist:
                last = hist[-1]
                score = last.get("y")
                rating = last.get("rating") or rating
        if score is None:
            print("[market_brief] Fear&Greed 점수 없음")
            return None
        score_f = round(float(score), 1)
        rating_s = str(rating or "").strip().lower().replace("_", " ")
        label_map = {
            "extreme fear": "Extreme Fear",
            "fear": "Fear",
            "neutral": "Neutral",
            "greed": "Greed",
            "extreme greed": "Extreme Greed",
        }
        label = label_map.get(rating_s, rating_s.title() if rating_s else "—")
        out = {"score": score_f, "rating": label, "source": "CNN Fear & Greed"}
        print(f"[market_brief] ✅ Fear&Greed {score_f} ({label})")
        return out
    except Exception as e:
        print(f"[market_brief] ⚠️ Fear&Greed 수집 실패: {e}")
        return None


def _fmt_chg(d: dict | None, ticker: str = "") -> str:
    if not d or not _is_finite(d.get("change_pct")):
        return "데이터 없음"
    if ticker:
        return _format_quote_line(ticker, d)
    chg = d["change_pct"]
    arrow = "▲" if chg > 0 else "▼"
    stale = " (전일/지연)" if d.get("stale") else ""
    return f"{d.get('price')} {arrow}{abs(chg)}%{stale} · RVOL {d.get('volume_ratio', '—')}%"


def build_featured_context(
    market_data: dict,
    *,
    brief_type: str,
    fear_greed: dict | None = None,
    movers: dict | None = None,
) -> str:
    """섹터·특징주·크립토·F&G를 프롬프트용 텍스트로 정리."""
    lines = ["[섹터·특징주·크립토 — 아래 목록만 인용, 없는 종목 창작 금지]"]

    movers = movers or {}
    us_g = movers.get("us_gainers") or []
    us_l = movers.get("us_losers") or []
    kr_g = movers.get("kr_gainers") or []
    kr_l = movers.get("kr_losers") or []

    if us_g or us_l:
        lines.append("\n[미국 특징주 — 당일 등락 상위]")
        for t, d in us_g:
            lines.append(f"  · 급등 {_format_quote_line(t, d)}")
        for t, d in us_l:
            lines.append(f"  · 급락 {_format_quote_line(t, d)}")
    if kr_g or kr_l:
        lines.append("\n[한국 특징주 — 당일 등락 상위]")
        for t, d in kr_g:
            lines.append(f"  · 급등 {_format_quote_line(t, d)}")
        for t, d in kr_l:
            lines.append(f"  · 급락 {_format_quote_line(t, d)}")

    crypto = market_data.get("크립토") or {}
    if crypto:
        lines.append("\n[크립토·관련]")
        for t, d in crypto.items():
            if d and _is_finite(d.get("change_pct")):
                lines.append(f"  · {_format_quote_line(t, d)}")

    sectors = market_data.get("섹터") or {}
    ranked = []
    for t, d in sectors.items():
        if not _is_finite(d.get("change_pct")) or d.get("stale"):
            continue
        ranked.append((t, d))
    ranked.sort(key=lambda x: x[1]["change_pct"], reverse=True)
    if ranked:
        lines.append("섹터 ETF (당일/직전세션):")
        ups = [x for x in ranked if x[1]["change_pct"] > 0][:2]
        downs = sorted(
            [x for x in ranked if x[1]["change_pct"] < 0],
            key=lambda x: x[1]["change_pct"],
        )[:2]
        if not ups:
            ups = ranked[:1]
        if not downs and len(ranked) > 1:
            downs = [ranked[-1]]
        up_keys = {t for t, _ in ups}
        for t, d in ups:
            lines.append(f"  · 강세 {_fmt_chg(d, t)}")
        for t, d in downs:
            if t in up_keys:
                continue
            lines.append(f"  · 약세/상대약세 {_fmt_chg(d, t)}")
        top, bot = ranked[0][1], ranked[-1][1]
        spread = abs(top["change_pct"] - bot["change_pct"])
        lines.append(
            f"  · 섹터 스프레드: {top['name']} vs {bot['name']} = {spread:.2f}%p"
        )
    else:
        lines.append("섹터 ETF: 데이터 없음")

    kr = market_data.get("한국") or {}
    lines.append("국장 대형주 (기본 편입):")
    for t in KR_MEGA_TICKERS:
        d = kr.get(t)
        name = (d or {}).get("name") or TICKERS["한국"].get(t, t)
        if d and _is_finite(d.get("change_pct")):
            lines.append(f"  · {_fmt_chg(d, t)}")
        else:
            lines.append(f"  · {name}({t}): 데이터 없음")

    if brief_type.startswith("us"):
        if fear_greed and fear_greed.get("score") is not None:
            lines.append(
                f"Fear & Greed: {fear_greed['score']} ({fear_greed.get('rating', '—')}) "
                f"— source={fear_greed.get('source', 'CNN')}"
            )
        else:
            lines.append("Fear & Greed: 데이터 없음 (언급 금지)")

    lines.append(
        "작성 규칙: ###1 흐름·###3 특징주에 위 목록을 반드시 활용. "
        "급등·급락 종목은 촉매 뉴스와 연결해 1줄씩 설명. "
        "제공되지 않은 개별주 이름 창작 금지."
    )
    return "\n".join(lines)


async def fetch_movers_async(
    candidates: dict[str, str],
    now_ex,
    *,
    top: int = 5,
    bottom: int = 5,
    prefetched: dict[str, dict] | None = None,
) -> tuple[list[tuple[str, dict]], list[tuple[str, dict]]]:
    """유니버스 스캔 → 당일 급등·급락 상위."""
    prefetched = prefetched or {}
    to_fetch = [
        sym for sym in candidates
        if not _mover_usable(prefetched.get(sym))
    ]
    tasks = [
        asyncio.to_thread(_fetch_ticker, sym, candidates[sym], now_ex=now_ex)
        for sym in to_fetch
    ]
    fetched = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []
    rows: list[tuple[str, dict]] = []
    for sym, d in zip(to_fetch, fetched):
        if isinstance(d, Exception) or not d:
            continue
        if d.get("stale") or not _is_finite(d.get("change_pct")):
            continue
        rows.append((sym, d))
    for sym in candidates:
        if sym in prefetched and _mover_usable(prefetched[sym]):
            cached = prefetched[sym]
            if not any(t == sym for t, _ in rows):
                rows.append((sym, cached))
    skipped = len(candidates) - len(to_fetch)
    if skipped:
        print(f"[market_brief] movers 캐시 재사용 {skipped}종", flush=True)
    rows.sort(key=lambda x: x[1]["change_pct"], reverse=True)
    gainers = [(t, d) for t, d in rows if d["change_pct"] > 0][:top]
    losers = sorted(
        [(t, d) for t, d in rows if d["change_pct"] < 0],
        key=lambda x: x[1]["change_pct"],
    )[:bottom]
    print(
        f"[market_brief] 특징주 스캔 {len(candidates)}종 → "
        f"급등 {len(gainers)} / 급락 {len(losers)}"
    )
    return gainers, losers


async def collect_movers(
    now_et,
    now_kst,
    prefetched: dict[str, dict] | None = None,
) -> dict:
    """미국·한국 특징주 병렬 수집."""
    prefetched = prefetched or {}
    us_g, us_l = await fetch_movers_async(
        US_MOVER_CANDIDATES, now_et, prefetched=prefetched
    )
    kr_g, kr_l = await fetch_movers_async(
        KR_MOVER_CANDIDATES, now_kst, prefetched=prefetched
    )
    return {
        "us_gainers": us_g,
        "us_losers": us_l,
        "kr_gainers": kr_g,
        "kr_losers": kr_l,
    }


def _flow_section_hint(brief_type: str) -> str:
    """brief_type별 ###1 흐름 섹션 가이드 (장전≠마감)."""
    hints = {
        "us_close": (
            "### 1. 📖 오늘 미국장의 흐름 (마감 리포트 — 4~7문장)\n"
            "**오늘 US 세션 안에서** 확정된 일만: 촉매 → 금리/달러/섹터/특징주 → SPY·QQQ·SMH.\n"
            "오늘 한국 마감(삼성·하이닉스)과 SMH가 **동행/역행**이면 반드시 구분.\n"
            "마지막: 다음 한국장(또는 프리마켓)에서 볼 포인트 1문장."
        ),
        "us_premarket": (
            "### 1. 📖 개장 전 종합 흐름 (장전 리포트 — 4~6문장)\n"
            "**마감 리포트가 아님.** 직전 미국 세션 마감 + **오늘 한국 마감(방금)** + 간밤 뉴스·금리·크립토를 한 줄기로.\n"
            "한국 반도체 급등락 → 오늘 미국 SMH·NVDA·QQQ에 미칠 함의 (양방향, 미국 영향력 더 큼).\n"
            "목표: '오늘 미국장 어떻게 열릴지/주목할 섹터' — 오늘 미국 마감 결과는 아직 없음."
        ),
        "kr_close": (
            "### 1. 📖 오늘 한국장의 흐름 (마감 리포트 — 4~7문장)\n"
            "**오늘 KR 세션 안에서** 확정된 일만: 촉매 → 삼성·하이닉스·KOSPI.\n"
            "간밤 미국(SMH·QQQ)과 **역행/동행** + 이유(자사주·금리·국내 이슈 등 제공 촉매).\n"
            "마지막: 다음 미국/한국 세션 대응 시사점."
        ),
        "kr_premarket": (
            "### 1. 📖 개장 전 종합 흐름 (장전 리포트 — 4~6문장)\n"
            "**마감 리포트가 아님.** 간밤 **미국 마감(확정)** + 금리·환율·크립토·뉴스 → 오늘 한국장 전망.\n"
            "미국 SMH·특징주 → 삼성·하이닉스·KOSPI 개장 시나리오 (미국→한국 우선, 역신호 있으면 병기).\n"
            "목표: '오늘 한국장 어떻게 열릴지' — 오늘 한국 마감 결과는 아직 없음."
        ),
    }
    return hints.get(brief_type, hints["us_close"])


# 백필 시 각 시황 타입의 정규 생성 시각 (현지)
_AS_OF_LOCAL = {
    "kr_premarket": (8, 0, "Asia/Seoul"),
    "kr_close":     (16, 0, "Asia/Seoul"),
    "us_premarket": (8, 30, "America/New_York"),
    "us_close":     (16, 30, "America/New_York"),
}


def _clocks_for_as_of(brief_type: str, as_of: str):
    """as_of(YYYY-MM-DD) + 타입별 정규 시각 → now_et/now_kst/now(BST)"""
    if brief_type not in _AS_OF_LOCAL:
        raise RuntimeError(f"as_of 미지원 타입: {brief_type}")
    h, m, tz_name = _AS_OF_LOCAL[brief_type]
    local_tz = pytz.timezone(tz_name)
    d = datetime.strptime(as_of, "%Y-%m-%d")
    local = local_tz.localize(datetime(d.year, d.month, d.day, h, m))
    now_et  = local.astimezone(pytz.timezone("America/New_York"))
    now_kst = local.astimezone(pytz.timezone("Asia/Seoul"))
    now     = local.astimezone(pytz.timezone("Europe/London"))
    return now, now_et, now_kst


def _has_minimum_data(market_data: dict) -> bool:
    us = market_data.get("미국", {})
    kr = market_data.get("한국", {})
    return len(us) >= 1 or len(kr) >= 1


def _build_data_text(market_data: dict) -> str:
    lines = []
    for region, tickers in market_data.items():
        if not tickers:
            lines.append(f"\n### {region} — ⚠️ 데이터 없음 (오늘 데이터 미수집, 전망 생략)")
            continue
        lines.append(f"\n### {region}")
        for ticker, d in tickers.items():
            chg = d.get("change_pct")
            if not _is_finite(chg) or not _is_finite(d.get("price")):
                lines.append(
                    f"- {d['name']}({ticker}) [데이터일: {d.get('last_date')}] — 가격/등락률 없음"
                )
                continue
            quote = _format_quote_line(ticker, d)
            if d.get("stale"):
                lines.append(
                    f"- {quote} "
                    f"[⚠️ {d['last_date']} — 기대 거래일 대비 지연, 전망 활용 금지]"
                )
            else:
                lines.append(f"- {quote} [데이터일: {d['last_date']}]")

        if region == "심리지표":
            tnx = tickers.get("^TNX")
            y2 = tickers.get("2YY=F")
            if (
                tnx and y2
                and tnx.get("price") is not None
                and y2.get("price") is not None
            ):
                spread = round(float(tnx["price"]) - float(y2["price"]), 3)
                lines.append(
                    f"- 10년-2년 금리차: {spread:+.3f}%p "
                    f"(10Y {tnx['price']}% − 2Y {y2['price']}%)"
                )
    return "\n".join(lines)


def _extract_forecast(analysis: str) -> str:
    """직전 시황 분석에서 '전망 섹션'을 추출 — ###0 검증 섹션 제외."""
    if not analysis:
        return ""
    # ###0 섹션은 '전망' 글자 때문에 오매칭 → 제거 후 탐색
    cleaned = re.sub(
        r"###\s*0\.\s*직전 (?:전망 검증|시장 전망\s*\(참고\))[\s\S]*?(?=\n###\s*[1-9]|\n---\s*\n|\Z)",
        "",
        analysis,
        count=1,
    )
    patterns = [
        # 🔮 전망 / ### N. … 전망 (검증·직전 제외)
        r"###\s*[🔮\s]*[^\n]*(?<!직전 )(?<!검증 )(전망|Forecast)[^\n]*\n([\s\S]*?)(?=\n###|\Z)",
        r"###\s*\d+[\.\s🔮📖]*[^\n]*(오늘|내일|다음)[^\n]*(장|시장)[^\n]*\n([\s\S]*?)(?=\n###|\Z)",
        r"\*\*결론\s*:\s*(강세|약세|중립)[^\n]*\n([\s\S]{0,500}?)(?=\n###|\Z)",
        r"(강세 우위|약세 우위|중립\s*\(?조건부)[^\n]*\n([\s\S]{0,400}?)(?=\n###|\Z)",
    ]
    for pat in patterns:
        m = re.search(pat, cleaned, re.IGNORECASE)
        if not m:
            continue
        block = m.group(0).strip()
        if "직전 전망 검증" in block or "직전 시장 전망" in block or re.match(r"###\s*0\.", block):
            continue
        return block[:500]
    # fallback — 한 줄 요약 근처 / 후반부 (검증 블록 제외본)
    return cleaned[-300:].strip()


def _condense_forecast_line(forecast_text: str) -> str:
    """전망 섹션에서 한 줄 근거만 뽑기."""
    if not forecast_text:
        return ""
    keys = ("강세", "약세", "중립", "우위", "결론", "BULL", "BEAR", "NEUTRAL", "때문에")
    skip_prefix = (
        "전망:", "실제 결과", "판정", "다음 전망", "직전 전망",
        "###", "SIGNAL:", "검증",
    )
    for raw in forecast_text.splitlines():
        line = raw.strip().lstrip("-*#• ").strip()
        if len(line) < 8:
            continue
        if any(line.startswith(p) or p in line[:20] for p in skip_prefix):
            continue
        if "직전 전망 검증" in line:
            continue
        if any(k in line for k in keys):
            return line[:200]
    for raw in forecast_text.splitlines():
        line = raw.strip().lstrip("-*#• ").strip()
        if len(line) < 20 or line.startswith("["):
            continue
        if any(p in line for p in ("전망:", "직전 전망", "SIGNAL:BEAR — 0.", "SIGNAL:BULL — 0.")):
            continue
        return line[:200]
    return ""


def _sanitize_cite_body(body: str) -> str:
    """재귀 오염된 인용 본문 정리 (검증 섹션이 전망 본문으로 들어간 경우)."""
    if not body:
        return ""
    text = body.strip()
    # 중첩된 '전망: [date] SIGNAL:XX —' 체인에서 마지막 실질 문장만 남김
    if "직전 전망 검증" in text or text.count("SIGNAL:") > 1 or text.count("전망:") >= 1:
        parts = re.split(r"SIGNAL:(?:BULL|BEAR|NEUTRAL)\s*[—\-–]\s*", text)
        candidates = [p.strip() for p in parts if p and p.strip()]
        # 날짜/검증 찌꺼기 제거
        cleaned = []
        for c in candidates:
            c = re.sub(r"^\[?\d{4}-\d{2}-\d{2}\]?\s*", "", c).strip()
            c = re.sub(r"^0\.\s*직전 전망 검증[^\n—\-–]*[—\-–]?\s*", "", c).strip()
            c = re.sub(r"^전망\s*:\s*", "", c).strip()
            if "직전 전망 검증" in c:
                continue
            if len(c) >= 12:
                cleaned.append(c)
        if cleaned:
            text = cleaned[-1]
    text = re.sub(r"\s+", " ", text).strip()
    # 여전히 오염이면 포기
    if "직전 전망 검증" in text or text.count("SIGNAL:") > 0:
        return ""
    return text[:220]


def _forecast_citation(doc: dict | None) -> str:
    """###0 '전망:' 칸에 넣을 SIGNAL+근거 한 줄 (Mongo 직전 시황)."""
    if not doc:
        return ""
    sig = (doc.get("signal") or "NEUTRAL").strip().upper()
    if sig not in ("BULL", "NEUTRAL", "BEAR"):
        sig = "NEUTRAL"
    analysis = doc.get("analysis") or ""
    one = _sanitize_cite_body(_extract_one_liner(analysis))
    body = one or _sanitize_cite_body(
        _condense_forecast_line(_extract_forecast(analysis))
    )
    if not body:
        body = "(상세 전망 문장 없음 — SIGNAL만 채점)"
    date = doc.get("date") or ""
    return f"[{date}] SIGNAL:{sig} — {body}"


def _forecast_line_incomplete(value: str) -> bool:
    v = (value or "").strip()
    if not v or v in ("-", "—"):
        return True
    if "SIGNAL:" not in v:
        return bool(re.match(r"^\[\d{4}-\d{2}-\d{2}\]\s*-?\s*$", v))
    return len(v) < 35


def _replace_forecast_lines(block: str, cites: list[str]) -> tuple[str, int]:
    """블록 내 '전망:' 줄을 Mongo 인용으로 교체. 사용한 cite 개수 반환."""
    idx = 0

    def _repl(match: re.Match) -> str:
        nonlocal idx
        prefix = match.group(1)
        after = match.group(2) or ""
        glued = re.search(r"\s+[-–]?\s*(실제\s*결과\s*:.*)$", after)
        suffix = f"\n- {glued.group(1).strip()}" if glued else ""
        if not _forecast_line_incomplete(after):
            return match.group(0)
        if idx >= len(cites):
            return match.group(0)
        cite = cites[idx]
        idx += 1
        return f"{prefix}{cite}{suffix}"

    new_block, _ = re.subn(
        r"(^[ \t]*[-*•]?\s*전망\s*:\s*)(.*)$",
        _repl,
        block,
        flags=re.MULTILINE,
    )
    if idx < len(cites):
        new_block = new_block.rstrip() + "\n" + "\n".join(
            f"- 전망: {c}" for c in cites[idx:]
        ) + "\n"
        idx = len(cites)
    return new_block, idx


def _parse_signal_from_cite(cite: str) -> str:
    m = re.search(r"SIGNAL:(BULL|BEAR|NEUTRAL)", cite or "", re.I)
    return (m.group(1) if m else "NEUTRAL").upper()


def _verdict_for_signal(signal: str, change_pct: float, band: float = 0.3) -> str:
    """±band% 중립 밴드 — save_brief_performance와 동일 기준."""
    sig = (signal or "NEUTRAL").upper()
    chg = float(change_pct)
    if sig == "BULL":
        if chg >= band:
            return "적중"
        if abs(chg) < band:
            return "부분 적중"
        return "빗나감"
    if sig == "BEAR":
        if chg <= -band:
            return "적중"
        if abs(chg) < band:
            return "부분 적중"
        return "빗나감"
    if abs(chg) < band:
        return "적중"
    return "부분 적중"


def _fmt_pct_metric(label: str, d: dict | None) -> str:
    if not d or not _is_finite(d.get("change_pct")):
        return f"{label} —"
    chg = float(d["change_pct"])
    sign = "▲" if chg >= 0 else "▼"
    stale = " (전일)" if d.get("stale") else ""
    return f"{label} {sign}{abs(chg):.2f}%{stale}"


def _verdict_reason(
    signal: str,
    verdict: str,
    bench_label: str,
    bench_chg: float,
    extras: list[tuple[str, float]],
) -> str:
    sig = signal.upper()
    parts: list[str] = []
    if verdict == "적중":
        parts.append(
            f"{sig} 전망과 {bench_label} {bench_chg:+.2f}% 방향이 일치했습니다."
        )
    elif verdict == "부분 적중":
        if abs(bench_chg) < 0.3:
            parts.append(
                f"{sig} 전망 대비 {bench_label} {bench_chg:+.2f}%는 "
                f"중립 밴드(±0.3%) 안입니다."
            )
        else:
            parts.append(
                f"{sig} 전망과 {bench_label} {bench_chg:+.2f}%는 방향은 맞으나 "
                f"폭이 약합니다."
            )
        aligned = [
            f"{name} {c:+.2f}%"
            for name, c in extras
            if (sig == "BEAR" and c <= -0.3) or (sig == "BULL" and c >= 0.3)
        ]
        if aligned:
            parts.append(f"보조 지표({', '.join(aligned)})는 전망 방향을 뒷받침합니다.")
    else:
        parts.append(
            f"{sig} 전망과 {bench_label} {bench_chg:+.2f}% 방향이 어긋났습니다."
        )
    return " ".join(parts)


def _find_section0_span(analysis: str) -> tuple[int, int] | None:
    """###0 블록 위치 (마감 검증 + 장전 참고)."""
    if not analysis:
        return None
    patterns = [
        r"###\s*0\.\s*직전 전망 검증",
        r"###\s*0\.\s*직전 시장 전망\s*\(참고\)",
        r"^0\.\s*직전 전망 검증",
        r"^0\.\s*직전 시장 전망",
    ]
    m = None
    for pat in patterns:
        m = re.search(pat, analysis, re.MULTILINE)
        if m:
            break
    if not m:
        return None
    start = m.start()
    rest = analysis[m.end() :]
    end_m = re.search(
        r"\n(?:---\s*\n|###\s*[1-9]\.\s*|^[1-9]\.\s*[📖🇺🇸🇰🇷🔮💡])",
        rest,
        re.MULTILINE,
    )
    end = m.end() + (end_m.start() if end_m else len(rest))
    return start, end


def _find_verify_span(analysis: str) -> tuple[int, int] | None:
    if not analysis:
        return None
    m = re.search(r"###\s*0\.\s*직전 전망 검증", analysis)
    if not m:
        m = re.search(r"^0\.\s*직전 전망 검증", analysis, re.MULTILINE)
    if not m:
        return None
    start = m.start()
    rest = analysis[m.end() :]
    end_m = re.search(
        r"\n(?:---\s*\n|###\s*[1-9]\.\s*|^[1-9]\.\s*[📖🇺🇸🇰🇷🔮💡])",
        rest,
        re.MULTILINE,
    )
    end = m.end() + (end_m.start() if end_m else len(rest))
    return start, end


def _inject_premarket_context(
    analysis: str,
    *,
    cite: str,
    source: str,
    score_when: str,
    usage_hint: str,
) -> str:
    """장전 시황 ###0 — 참고 인용 블록을 코드로 확정."""
    block = _verify_block(
        mode="defer",
        cite=cite,
        source=source,
        score_when=score_when,
        extra=usage_hint,
    ).rstrip() + "\n"
    span = _find_section0_span(analysis)
    if span:
        tail = analysis[span[1] :].lstrip("\n")
        return analysis[: span[0]] + block + ("\n" + tail if tail else "")
    ins = re.search(r"\n---\s*\n|\n###\s*1\.", analysis)
    if ins:
        return analysis[: ins.start()] + "\n\n" + block + analysis[ins.start() :]
    return block + "\n\n" + analysis


def _get_ticker_data(market_data: dict, ticker: str) -> dict | None:
    """TICKERS 중첩 구조에서 티커 데이터 조회 (미국/섹터/한국 등)."""
    for region_data in (market_data or {}).values():
        if isinstance(region_data, dict) and ticker in region_data:
            return region_data[ticker]
    return None


def _inject_scored_verify(
    analysis: str,
    entries: list[dict],
    market_data: dict,
) -> str:
    """마감 시황 ###0 — 수치·판정·이유를 코드로 확정 (LLM 오류·중복 방지)."""
    span = _find_verify_span(analysis)
    if not span or not entries:
        return analysis

    old = analysis[span[0] : span[1]]
    next_m = re.search(
        r"(?:다음\s*(?:전망\s*)?반영)\s*[:：]\s*(.+)",
        old,
        re.MULTILINE,
    )
    next_reflect = (next_m.group(1).strip() if next_m else "")[:220]

    lines = ["### 0. 직전 전망 검증", ""]
    for i, ent in enumerate(entries):
        cite = _sanitize_full_cite(ent.get("cite") or "") or "전망 기록 없음"
        if cite == "전망 기록 없음":
            lines.append("- **전망**: 기록 없음 — 판정 불가")
            lines.append("")
            continue

        region, ticker, label = ent["bench"]
        d = _get_ticker_data(market_data, ticker)
        signal = _parse_signal_from_cite(cite)
        result_parts = [_fmt_pct_metric(label, d)]
        extra_chgs: list[tuple[str, float]] = []
        for er, et, el in ent.get("extras") or []:
            ed = _get_ticker_data(market_data, et)
            result_parts.append(_fmt_pct_metric(el, ed))
            if ed and _is_finite(ed.get("change_pct")):
                extra_chgs.append((el, float(ed["change_pct"])))

        if d and _is_finite(d.get("change_pct")):
            bench_chg = float(d["change_pct"])
            verdict = _verdict_for_signal(signal, bench_chg)
            reason = _verdict_reason(signal, verdict, label, bench_chg, extra_chgs)
            if verdict == "부분 적중" and extra_chgs:
                strong = [n for n, c in extra_chgs if abs(c) >= 0.3]
                if strong and signal == "BEAR" and bench_chg > -0.3:
                    reason += f" {', '.join(strong)} 등은 약세 폭이 컸습니다."
        else:
            verdict = "검증 불가"
            reason = f"{label} 마감 수치가 없어 채점할 수 없습니다."

        title = ent.get("title") or ""
        if title:
            lines.append(f"#### {title}")
        lines.extend([
            f"- **전망** (장전): {cite}",
            f"- **실제 결과**: {', '.join(result_parts)}",
            f"- **판정**: {verdict}",
            f"- **판정 이유**: {reason}",
        ])
        if i == 0 and next_reflect:
            lines.append(f"- **다음 반영**: {next_reflect}")
        lines.append("")

    new_section = "\n".join(lines).rstrip() + "\n"
    return analysis[: span[0]] + new_section + analysis[span[1] :]


def _force_verify_citations(analysis: str, cites: list[str]) -> str:
    """LLM이 ###0 '전망:'을 비우거나 날짜만 남기면 Mongo 인용으로 강제 교체."""
    cites = [_sanitize_full_cite(c) for c in (cites or []) if c and str(c).strip()]
    cites = [c for c in cites if c]
    if not analysis or not cites:
        return analysis

    header_patterns = [
        r"###\s*0\.\s*직전 전망 검증",
        r"###\s*0\.\s*직전 시장 전망\s*\(참고\)",
        r"^0\.\s*직전 전망 검증",
        r"^0\.\s*직전 시장 전망",
    ]
    start = None
    header_len = 0
    for pat in header_patterns:
        m = re.search(pat, analysis, re.MULTILINE)
        if m:
            start = m.start()
            header_len = len(m.group(0))
            break
    if start is None:
        return analysis

    rest = analysis[start + header_len :]
    end_m = re.search(
        r"\n(?:---\s*\n|###\s*[1-9]\.\s*|^[1-9]\.\s*[📖🇺🇸🇰🇷🔮💡])",
        rest,
        re.MULTILINE,
    )
    body_end = start + header_len + (end_m.start() if end_m else len(rest))
    header = analysis[start : start + header_len]
    body = analysis[start + header_len : body_end]
    new_body, used = _replace_forecast_lines(body, cites)
    # 중복 '전망:' 줄 제거 — cite 1개만 유지
    if cites and used:
        cite_line = cites[0]
        kept: list[str] = []
        seen_forecast = False
        for line in new_body.splitlines():
            if re.match(r"^[ \t]*[-*•]?\s*전망\s*:", line):
                if seen_forecast:
                    continue
                seen_forecast = True
                kept.append(f"- **전망**: {cite_line}")
                continue
            kept.append(line)
        new_body = "\n".join(kept)
        if not seen_forecast:
            new_body = f"- **전망**: {cite_line}\n" + new_body
    return analysis[:start] + header + new_body + analysis[body_end:]


def _normalize_brief_headers(analysis: str) -> str:
    """LLM이 붙여 쓴 ###0 헤더·중복 소제목 정리."""
    if not analysis:
        return analysis
    text = analysis
    text = re.sub(
        r"(?:^|\n)(?:###\s*)?0\.\s*직전\s*시장\s*전망\s*\(참고\)\s*[-–—]?\s*",
        "\n### 0. 직전 시장 전망 (참고)\n\n",
        text,
        count=1,
    )
    text = re.sub(
        r"(?:^|\n)(?:###\s*)?0\.\s*직전\s*전망\s*검증\s*[-–—]?\s*",
        "\n### 0. 직전 전망 검증\n\n",
        text,
        count=1,
    )
    text = re.sub(
        r"\n####\s*직전\s*전망\s*\n",
        "\n",
        text,
    )
    return text.strip()


def _repair_truncated_brief_tail(analysis: str) -> str:
    """한 줄 요약·미완성 마크다운 잘림 보정."""
    if not analysis:
        return analysis
    m = re.search(r"(###\s*[💡\s]*한\s*줄\s*요약\s*\n+|💡\s*한\s*줄\s*요약\s*\n+)([\s\S]*)$", analysis)
    if not m:
        return analysis
    body = (m.group(2) or "").strip()
    if len(body) >= 40 and body.count("**") % 2 == 0 and not re.search(
        r'[\(\[\{"\'""]$', body
    ):
        return analysis

    fb = ""
    cm = re.search(r"\*\*결론\s*:\s*([^\n*]+)", analysis)
    if cm:
        fb = cm.group(1).strip()
    if not fb:
        fm = re.search(r"(?:###\s*)?1\.\s*[^\n]*\n([\s\S]{60,500})", analysis)
        if fm:
            fb = re.sub(r"\s+", " ", fm.group(1).strip())[:140]
    if not fb:
        return analysis
    return analysis[: m.start(2)] + fb + "\n"


def _sanitize_full_cite(cite: str) -> str:
    """완성된 '[date] SIGNAL:X — body' 인용 한 줄 정리."""
    cite = (cite or "").strip()
    if not cite:
        return ""
    m = re.match(
        r"^(\[\d{4}-\d{2}-\d{2}\]\s*SIGNAL:(?:BULL|BEAR|NEUTRAL)\s*[—\-–]\s*)(.*)$",
        cite,
        re.DOTALL,
    )
    if m:
        body = _sanitize_cite_body(m.group(2))
        if not body:
            body = "(상세 전망 문장 없음 — SIGNAL만 채점)"
        return m.group(1) + body
    body = _sanitize_cite_body(cite)
    return body


def _load_brief_cite(brief_type: str, prefer_date: str | None = None) -> tuple[dict | None, str]:
    """해당 타입 시황 + 전망 인용 문구. prefer_date 있으면 그날 문서 우선(마감 검증용)."""
    from database import get_recent_market_briefs, get_market_brief_by_date

    doc = None
    if prefer_date:
        doc = get_market_brief_by_date(brief_type, prefer_date)
    if not doc:
        docs = get_recent_market_briefs(limit=1, brief_type=brief_type)
        doc = docs[0] if docs else None
    if not doc:
        return None, ""
    return doc, _forecast_citation(doc)


def _extract_one_liner(analysis: str) -> str:
    """시황 본문에서 '한 줄 요약' 추출."""
    if not analysis:
        return ""
    m = re.search(
        r"###\s*[💡\s]*한\s*줄\s*요약\s*\n+\s*([^\n]+)",
        analysis,
    )
    if m:
        line = m.group(1).strip()[:220]
        if "직전 전망 검증" in line or line.count("SIGNAL:") > 0:
            return ""
        return line
    return ""


def _metrics_from_brief(doc: dict, tickers: tuple) -> list[str]:
    """저장된 market_data에서 지정 티커 핵심 수치 줄 목록."""
    md = doc.get("market_data") or {}
    flat: dict = {}
    for region_map in md.values():
        if isinstance(region_map, dict):
            flat.update(region_map)
    lines = []
    for t in tickers:
        d = flat.get(t)
        if not d:
            continue
        if _is_finite(d.get("change_pct")) and _is_finite(d.get("price")):
            lines.append(
                f"  · {_format_quote_line(t, d)} [{d.get('last_date', '')}]"
            )
        else:
            lines.append(f"  · {d.get('name', t)}({t}): 수치 없음")
    return lines


def _build_circulation_context(brief_type: str) -> str:
    """순환 체인상 직전 시황(들)의 SIGNAL·한줄·핵심 수치를 Mongo에서 주입.
    ###0 검증 짝과 별개 — 교차시장 분석 입력용.
    """
    from database import get_recent_market_briefs

    feeds = CIRCULATION_FEED.get(brief_type) or []
    if not feeds:
        return ""

    blocks = [
        "\n[교차시장 순환 주입 — Mongo 직전 시황]",
        "규칙: 아래 SIGNAL·수치·한줄요약을 오늘 전망의 선행 입력으로 연결할 것.",
        "###0 채점 규칙과 별개. 라이브 제공 데이터와 모순되면 라이브 수치 우선, 괴리는 명시.",
    ]
    for feed in feeds:
        src_type = feed["type"]
        docs = get_recent_market_briefs(limit=1, brief_type=src_type)
        label = BRIEF_TYPES.get(src_type, {}).get("label", src_type)
        predict = BRIEF_TYPES.get(src_type, {}).get("predict", "")
        if not docs:
            blocks.append(f"\n▶ {feed['role']}\n  ({label} 저장분 없음 — 라이브 데이터만 사용)")
            continue
        doc = docs[0]
        one = _extract_one_liner(doc.get("analysis", ""))
        metrics = _metrics_from_brief(doc, tuple(feed.get("tickers") or ()))
        block = (
            f"\n▶ {feed['role']}\n"
            f"  출처: {doc.get('date')} {label} / SIGNAL:{doc.get('signal')} "
            f"/ 전망대상:{predict}"
        )
        if one:
            block += f"\n  한줄: {one}"
        if metrics:
            block += "\n  핵심 수치:\n" + "\n".join(metrics)
        else:
            block += "\n  핵심 수치: 없음"
        blocks.append(block)
    return "\n".join(blocks)


def _build_prev_context(brief_type: str, prefer_date: str | None = None) -> str:
    """검증 짝 시황을 가져와 직전 전망 검증에 사용.
    verify_mode=defer → 인용만, 수치 채점 금지
    verify_mode=score → 같은 세션 장전 전망을 마감 수치로 채점
    prefer_date: 마감 시황 생성 시 당일 장전 문서 우선 조회
    """
    cfg = BRIEF_TYPES.get(brief_type)
    if not cfg:
        return ""
    verify_type = cfg["verify"]
    mode = cfg.get("verify_mode", "score")

    cite_date = prefer_date if mode == "score" else None
    prev, cite = _load_brief_cite(verify_type, prefer_date=cite_date)
    if not prev:
        return ""

    label   = BRIEF_TYPES.get(verify_type, {}).get("label", verify_type)
    predict = BRIEF_TYPES.get(verify_type, {}).get("predict", "")
    forecast_text = _extract_forecast(prev.get("analysis", ""))

    # verify0 블록에 cite가 이미 들어가므로 전망 본문 전체 재주입은 생략 (input 토큰 절감)
    head = (
        f"\n[검증 대상 — {prev.get('date')} {label} / SIGNAL:{prev.get('signal')}]\n"
        f"직전 전망 대상: {predict}\n"
        f"【###0 전망 칸 복붙용】{cite}\n"
    )
    if forecast_text and len(forecast_text) < 120:
        head += f"{forecast_text}\n"

    if mode == "defer":
        return head + "[###0: 참고 인용만 — 판정·검증 보류 문구 금지, 오늘 활용·채점 예정만 작성]\n"

    return (
        head
        + f"[###0: 위 전망({predict})을 오늘 마감 수치로 채점 — 판정 이유 1~2문장]\n"
    )


def resolve_brief_target_date(brief_type: str, as_of: str | None = None) -> str:
    """시황 document date (YYYY-MM-DD) — as_of·시장 타임존 기준."""
    cfg = BRIEF_TYPES.get(brief_type)
    if not cfg:
        raise RuntimeError(f"알 수 없는 시황 타입: {brief_type}")
    target_market = cfg["market"]
    if as_of:
        _, now_et, now_kst = _clocks_for_as_of(brief_type, as_of)
    else:
        now_kst = datetime.now(pytz.timezone("Asia/Seoul"))
        now_et = datetime.now(pytz.timezone("America/New_York"))
    now_target = now_kst if target_market == "한국" else now_et
    return now_target.strftime("%Y-%m-%d")


def resolve_manual_brief_as_of(brief_type: str, as_of: str | None = None) -> str | None:
    """수동 시황 생성: as_of 미지정 + 주말이면 대상 시장 직전 거래일로 자동 설정."""
    if as_of:
        return as_of
    if brief_type not in BRIEF_TYPES:
        return None
    target_market = BRIEF_TYPES[brief_type]["market"]
    region = "한국" if target_market == "한국" else "미국"
    tz_name = "Asia/Seoul" if region == "한국" else "America/New_York"
    now_local = datetime.now(pytz.timezone(tz_name))
    if now_local.weekday() >= 5:
        prev = _prev_session_date(region, now_local.date())
        return prev.strftime("%Y-%m-%d")
    return None


async def generate_market_brief(
    brief_type: str,
    as_of: str | None = None,
    *,
    rescore_accuracy: bool = True,
) -> dict:
    """as_of: 'YYYY-MM-DD' — 해당일 정규 시각 기준으로 백필/재생성."""
    from database import get_recent_market_briefs

    # 시황 타입 검증
    if brief_type not in BRIEF_TYPES:
        raise RuntimeError(f"알 수 없는 시황 타입: {brief_type}")
    cfg           = BRIEF_TYPES[brief_type]
    target_market = cfg["market"]   # "한국" 또는 "미국"

    if as_of:
        now, now_et, now_kst = _clocks_for_as_of(brief_type, as_of)
        print(f"[market_brief] as_of={as_of} → ET {now_et} / KST {now_kst}")
    else:
        bst = pytz.timezone("Europe/London")
        now     = datetime.now(bst)
        now_kst = datetime.now(pytz.timezone("Asia/Seoul"))
        now_et  = datetime.now(pytz.timezone("America/New_York"))

    # 주말은 대상 시장 현지 요일 기준 (백필 as_of가 평일이면 허용)
    now_target = now_kst if target_market == "한국" else now_et
    if now_target.weekday() >= 5:
        wd = WEEKDAY_KR[now_target.weekday()]
        raise RuntimeError(f"주말({wd}요일) — 시황 생성 안 함")

    t_collect = time.monotonic()
    if brief_type.startswith("us"):
        market_data, macro_news, tomorrow_events, fear_greed = await asyncio.gather(
            get_market_data_async(now_et=now_et, now_kst=now_kst),
            asyncio.to_thread(fetch_macro_news, BRIEF_MACRO_NEWS_PER_SOURCE),
            asyncio.to_thread(_get_tomorrow_events, now),
            asyncio.to_thread(fetch_fear_greed),
        )
    else:
        market_data, macro_news, tomorrow_events = await asyncio.gather(
            get_market_data_async(now_et=now_et, now_kst=now_kst),
            asyncio.to_thread(fetch_macro_news, BRIEF_MACRO_NEWS_PER_SOURCE),
            asyncio.to_thread(_get_tomorrow_events, now),
        )
        fear_greed = None

    flat = _flatten_market_data(market_data)
    movers = await collect_movers(now_et, now_kst, prefetched=flat)
    news_text = format_macro_news_for_brief(macro_news)
    print(
        f"[market_brief] 데이터 수집 완료 ({brief_type}, "
        f"{time.monotonic() - t_collect:.0f}s)"
    )

    if not _has_minimum_data(market_data):
        raise RuntimeError("핵심 지수 데이터 수집 실패")

    # 시황 date = 대상 시장 현지 실행일 (kr_*=KST, us_*=ET)
    today         = now_target.strftime("%Y-%m-%d")
    weekday_today = WEEKDAY_KR[now_target.weekday()]
    region_key    = "한국" if target_market == "한국" else "미국"

    # 마감 시황: 오늘 봉이 없으면 재수집 후, 그래도 없으면 저장하지 않음
    # (전날 데이터로 "수집 실패" 리포트를 쓰는 사고 방지)
    if brief_type in ("us_close", "kr_close") and _is_after_close(region_key, now_target):
        for attempt in range(3):
            if _has_today_session_data(
                market_data.get(region_key, {}),
                today,
                keys=KR_INDEX_TICKERS if region_key == "한국" else None,
            ):
                break
            if attempt < 2:
                print(
                    f"[market_brief] {brief_type}: 오늘({today}) {region_key} 봉 미확정 "
                    f"— 45초 후 재수집 ({attempt + 1}/2)"
                )
                await asyncio.sleep(45)
                if not as_of:
                    now_kst = datetime.now(pytz.timezone("Asia/Seoul"))
                    now_et  = datetime.now(pytz.timezone("America/New_York"))
                    now_target = now_kst if target_market == "한국" else now_et
                    today = now_target.strftime("%Y-%m-%d")
                market_data = get_market_data(now_et=now_et, now_kst=now_kst)
        else:
            raise RuntimeError(
                f"{brief_type}: 오늘({today}) {region_key} 마감 데이터 미확정 — "
                f"잘못된 시황 저장 방지"
            )

    # 시장 상태 판정 (하드코딩 없이 데이터 추론 + 캘린더 교차검증)
    us_status = get_market_status(market_data.get("미국", {}), "미국", now_et)
    kr_status = get_market_status(market_data.get("한국", {}), "한국", now_kst)

    # 대상 시장이 휴장이면 생성 안 함
    status = kr_status if target_market == "한국" else us_status
    if status["status"] == "CLOSED":
        raise RuntimeError(f"{target_market} 증시 휴장({status['reason']}) — {brief_type} 스킵")

    recent = get_recent_market_briefs(limit=6)

    # 한국 지수 stale 여부 확인 → kr_close 브리프의 지수만 대체 (삼성·하이닉스는 유지)
    kr_data = market_data.get("한국", {})
    kr_indices = {k: kr_data[k] for k in KR_INDEX_TICKERS if k in kr_data}
    kr_stale = (not kr_indices) or any(
        v.get("stale") or not v.get("price") for v in kr_indices.values()
    )
    if kr_stale:
        korea_brief = next(
            (b for b in recent if b.get("type") == "kr_close"),
            None
        )
        old_kr = (korea_brief or {}).get("market_data", {}).get("한국") or {}
        if old_kr:
            market_data.setdefault("한국", {})
            for k in KR_INDEX_TICKERS:
                if k in old_kr:
                    market_data["한국"][k] = old_kr[k]
            print(
                f"[market_brief] 한국 지수 stale → kr_close 지수만 대체 "
                f"({korea_brief['date']})"
            )
        else:
            print("[market_brief] 한국 지수 stale + kr_close 브리프 없음 → 지수 데이터 없음")
            for k in KR_INDEX_TICKERS:
                market_data.get("한국", {}).pop(k, None)

    data_text = _build_data_text(market_data)
    featured_text = build_featured_context(
        market_data, brief_type=brief_type, fear_greed=fear_greed, movers=movers
    )
    futures_snapshot = None
    if brief_type in FUTURES_FOR_BRIEF:
        futures_snapshot = await asyncio.to_thread(collect_futures_snapshot, brief_type)
    futures_text = format_futures_prompt_text(futures_snapshot)
    verify_pref_date = today if brief_type in ("kr_close", "us_close") else None
    prev_context = "\n".join(
        x for x in (
            _build_prev_context(brief_type, prefer_date=verify_pref_date),
            _build_circulation_context(brief_type),
        ) if x
    ).strip()

    # 적중률 저장 — 마감 시황이 같은 날 장전 전망을 채점
    # (+ 한국 마감 시 직전 us_close의 "다음 한국장" 전망도 KOSPI로 채점)
    def _score_brief_vs_index(prev_doc: dict, chg: float, label: str):
        prev_signal = prev_doc.get("signal", "")
        if not prev_signal:
            return
        if abs(chg) < 0.3:
            actual_signal = "NEUTRAL"
        elif chg > 0:
            actual_signal = "BULL"
        else:
            actual_signal = "BEAR"
        is_correct = (prev_signal == actual_signal)
        try:
            from database import save_brief_performance
            save_brief_performance(
                brief_id=str(prev_doc.get("_id", "")),
                predicted=prev_signal,
                actual=actual_signal,
                is_correct=is_correct,
                brief_type=prev_doc.get("type", ""),
            )
            print(
                f"[market_brief] 적중률 저장({label}): {prev_doc.get('type')} "
                f"{prev_signal}→{actual_signal} {'✅' if is_correct else '❌'}"
            )
        except Exception as e:
            print(f"[market_brief] 적중률 저장 실패: {e}")

    if rescore_accuracy and brief_type in ("kr_close", "us_close"):
        verify_type = cfg["verify"]   # kr_premarket / us_premarket
        from database import get_market_brief_by_date
        prev_doc = get_market_brief_by_date(verify_type, today)
        if not prev_doc:
            prev_list = get_recent_market_briefs(limit=1, brief_type=verify_type)
            prev_doc = prev_list[0] if prev_list else None
        if prev_doc:
            if target_market == "한국":
                idx = market_data.get("한국", {}).get("^KS11", {})
            else:
                idx = market_data.get("미국", {}).get("SPY", {})
            if idx and not idx.get("stale") and idx.get("change_pct") is not None:
                _score_brief_vs_index(prev_doc, float(idx["change_pct"]), "same-day")

        # 한국 마감: 직전 미국 마감의 한국장 전망도 오늘 KOSPI로 채점
        if brief_type == "kr_close":
            us_prev = get_recent_market_briefs(limit=1, brief_type="us_close")
            idx = market_data.get("한국", {}).get("^KS11", {})
            if (
                us_prev
                and idx
                and not idx.get("stale")
                and idx.get("change_pct") is not None
            ):
                _score_brief_vs_index(us_prev[0], float(idx["change_pct"]), "us→kr")

    next_trading_label, next_kr_str = _next_kr_trading_label(now_kst)

    # 적중률 자기보정 컨텍스트
    from database import get_brief_accuracy
    accuracy = get_brief_accuracy(limit=10, market=target_market)
    accuracy_context = ""
    if accuracy["total"] >= 3:
        errs = (accuracy.get("recent_errors") or [])[:3]
        error_text = ", ".join(errs) if errs else "없음"
        accuracy_context = f"""
[최근 {target_market} 시황 적중률 — 자기보정 참고 / 판정 기준: 실제 지수 등락률 ±0.3%는 NEUTRAL]
- 최근 {accuracy['total']}회 중 {accuracy['correct']}회 적중 ({accuracy['accuracy_pct']}%)
- 반복 오류 패턴: {error_text}
- 위 오류 패턴이 있으면 이번 전망에서 반대 방향 가중치를 높일 것
- BULL 전망이 반복 빗나갔으면 → 이번엔 BEAR 또는 NEUTRAL 검토
- BEAR 전망이 반복 빗나갔으면 → 이번엔 BULL 또는 NEUTRAL 검토
"""

    def _status_line(s: dict) -> str:
        r = f" ({s['reason']})" if s.get("reason") else ""
        return f"{s['status']}{r}\n  마지막 거래일: {s['last_trading_day']} / 판정 신뢰도: {s['confidence']}"

    # 현재 시각 + 시장 상태 컨텍스트
    timing_context = f"""
[시각]
- 한국: {now_kst.strftime('%Y-%m-%d %H:%M')} ({WEEKDAY_KR[now_kst.weekday()]})
- 미국: {now_et.strftime('%Y-%m-%d %H:%M')} ET

[시장 상태 — 반드시 이대로 서술]
- 미국: {_status_line(us_status)}
- 한국: {_status_line(kr_status)}
- 다음 한국 거래일: {next_kr_str}

[휴장 서술 원칙 — 위반 시 사용자가 손실을 볼 수 있음]
1. status=CLOSED → "휴장"으로 서술. "데이터 미수집" 표현 절대 금지
   ❌ "한국 데이터 미수집으로 파악 불가"
   ✅ "한국 증시는 {kr_status.get('reason', '휴장')}으로 거래가 없었습니다"
2. status=UNKNOWN → "오늘 마감 데이터 미확정"으로만 표기. 제공된 전일 수치를 오늘 마감처럼 쓰지 말 것
3. 휴장인 시장은 전망 검증 대상에서 제외하고 그 사실을 명시
   ✅ "직전 전망은 한국 증시 휴장으로 검증 대상이 아닙니다"
4. 신뢰도=추정/불일치이면 "휴장으로 추정됩니다 (확인 필요)"로 표기
5. 데이터의 [데이터일]이 오늘이 아니면 반드시 "N월 N일 마감 기준"으로 명시
   절대 과거 거래일을 "오늘 마감"으로 표현하지 말 것
6. "다음 거래일 (MM/DD 요일)" 반복 표기 금지 — 처음 1회만, 이후 "다음 거래일"로만.
   다음 거래일이 달력상 내일이 아니면 "내일"이라고 쓰지 말 것 (주말·공휴일)
7. status=PRE_OPEN → "장 시작 전"으로 서술. 직전 거래일 마감 데이터가 있으면 그걸 정상 기준으로 쓸 것.
   ✅ "미국은 아직 개장 전입니다. 아래는 직전 거래일(금) 마감 기준입니다."
   ❌ "미국 데이터 없음" / "미국 데이터 미수집" / "미국 데이터 부재"
8. status=UNKNOWN → 단정 금지. "수집 실패로 전망 불가"처럼 장황하게 쓰지 말고 전일 기준만 짧게 참고
"""
    timing_context += accuracy_context
    flow_hint = _flow_section_hint(brief_type)
    brief_type_rule = _brief_type_rule(brief_type)

    verify_cites: list[str] = []
    verify_entries: list[dict] = []
    verify_premarket: dict | None = None

    if brief_type == "us_premarket":
        _, us_close_cite = _load_brief_cite("us_close")
        verify_premarket = {
            "cite": us_close_cite or "",
            "source": "직전 미국 마감 시황 (us_close)",
            "score_when": "한국 마감 시황(kr_close) · KOSPI 등락률",
            "usage_hint": (
                "직전 us_close의 한국장 전망 요지와 오늘 한국 마감(코스피·삼성·하이닉스)을 "
                "오늘 미국장 개장 전 전망(###5) 선행 신호로 연결"
            ),
        }
        verify0 = _verify_block(
            mode="defer",
            cite=us_close_cite,
            source=verify_premarket["source"],
            score_when=verify_premarket["score_when"],
            extra=verify_premarket["usage_hint"],
        )
        psych = _psych_block(impact_header="오늘 미국장 영향")
        outlook = _outlook_block(
            title="오늘 미국 장 전망",
            condition_examples=(
                '  예: 강세 "RSP≥SPY 및 QQQ ▲0.5%+" / '
                '약세 "VIX ▲10%+ 또는 SPY ▼0.5%+"'
            ),
        )
        prompt = f"""오늘 {today}({weekday_today}) 미국장 전 시황을 아래 데이터만 사용해서 작성해줘.

{STRICT_RULE}
{AUDIENCE_RULE}
{CROSS_MARKET_RULE}
{ENGINE_STRUCTURE_RULE}
{PREMARKET_SECTION0_RULE}
{BREADTH_RULE}
{BRIEF_STYLE_RULE}
{NEWS_RULE}
{timing_context}

{brief_type_rule}

[제공 데이터]
{data_text}

{futures_text}

{featured_text}
{tomorrow_events}
(내일 실적발표 종목이 있으면 해당 섹터 영향을 전망에 반영할 것)

[최근 뉴스]
{news_text}

{prev_context}

## 📊 장전 시황 · {today} {weekday_today}요일

{verify0}

---

{flow_hint}

---

### 2. 핵심 수치 스냅샷
**🇰🇷 한국 마감** (종가 / 등락률 / 거래량)
- KOSPI … / KOSDAQ … / 삼성·하이닉스(제공 시)

**🇺🇸 미국 직전세션** (종가 / 등락률 / 거래량)
- SPY … / RSP … / QQQ … / IWM …
- **시장 폭**: SPY vs RSP 갭 — 한 줄
- **섹터**: 강세·약세 각 1~2개

---

### 3. 특징주 & 섹터
[특징주] 목록에서 급등·급락 상위만 — 종목명 +% + 촉매 뉴스 연결 (각 1줄).
크립토 움직임이 있으면 리스크온/오프 함의 1문장.

---

{psych}

---

{outlook}

---

### 💡 한 줄 요약
[가장 중요한 수치] 때문에 오늘 미국장은 [핵심 포인트] 주목.

SIGNAL:BULL 또는 SIGNAL:NEUTRAL 또는 SIGNAL:BEAR"""

    elif brief_type == "kr_close":
        _, kr_pm_cite = _load_brief_cite("kr_premarket", prefer_date=today)
        _, us_close_cite = _load_brief_cite("us_close")  # 간밤 — 최근 us_close
        verify_cites = [c for c in (kr_pm_cite, us_close_cite) if c]
        verify_entries = []
        if kr_pm_cite:
            verify_entries.append({
                "cite": kr_pm_cite,
                "bench": ("한국", "^KS11", "KOSPI"),
                "extras": [("한국", "^KQ11", "KOSDAQ")],
            })
        if us_close_cite:
            verify_entries.append({
                "cite": us_close_cite,
                "bench": ("한국", "^KS11", "KOSPI"),
                "extras": [],
                "title": "간밤 미국→한국 전망 (us_close)",
            })
        secondary = []
        if us_close_cite:
            secondary.append(("간밤 미국→한국 전망 (us_close)", us_close_cite))
        verify0 = _verify_block(
            bench="KOSPI",
            result_metrics="KOSPI ▲/▼X.XX%, KOSDAQ ▲/▼X.XX% (제공 수치)",
            mode="score",
            cite=kr_pm_cite,
            secondary=secondary,
            extra=(
                "\n※ 장전(kr_premarket)과 간밤 us_close 전망 모두 "
                "'전망:'에 인용문이 보여야 하며, 비어 있으면 검증 불가."
            ),
        )
        psych = _psych_block(impact_header=f"{next_trading_label} 영향")
        outlook = _outlook_block(
            title=f"{next_trading_label} 한국 시장 전망 (기준일 {next_kr_str})",
            condition_examples=(
                '  예: 강세 "미국 QQQ 선물 ▲0.8%+ 및 SMH ▲1%+" / '
                '약세 "QQQ 선물 ▼0.5%+ 또는 VIX ▲10%+"'
            ),
        )
        prompt = f"""오늘 {today}({weekday_today}) 한국 장 마감 시황을 아래 데이터만 사용해서 작성해줘.

{STRICT_RULE}
{AUDIENCE_RULE}
{CROSS_MARKET_RULE}
{CLOSE_REPORT_RULE}
{ENGINE_STRUCTURE_RULE}
{BRIEF_STYLE_RULE}
{NEWS_RULE}
{timing_context}

{brief_type_rule}

[제공 데이터]
{data_text}

{futures_text}

{featured_text}
{tomorrow_events}
(내일 실적발표 종목이 있으면 해당 섹터 영향을 시황 전망에 반드시 반영할 것)

[최근 24시간 매크로 뉴스]
{news_text}

{prev_context}

## 📈 🇰🇷 마감 시황 · {today} {weekday_today}요일

{verify0}

---

{flow_hint}

---

### 2. 🇰🇷 핵심 수치 스냅샷
- KOSPI … / KOSDAQ … / 삼성전자 … / SK하이닉스 …
- **섹터·시장폭**: 제공 섹터 ETF + 강세/약세 포인트 각 1~2개

---

### 3. 특징주 & 섹터
[특징주]·[크립토]·촉매 뉴스를 연결 — 급등·급락 종목 각 1줄 (재나열 금지).
간밤 미국(SMH 등)과의 괴리/연동이 있으면 명시.

---

{psych}

---

{outlook}

---

### 💡 한 줄 요약
[가장 중요한 수치] 때문에 {next_trading_label} 한국장은 [핵심 포인트] 주목.

SIGNAL:BULL 또는 SIGNAL:NEUTRAL 또는 SIGNAL:BEAR"""

    elif brief_type == "us_close":
        _, us_pm_cite = _load_brief_cite("us_premarket", prefer_date=today)
        if not us_pm_cite:
            print(
                f"[market_brief] us_close: 당일({today}) us_premarket 없음 — "
                f"###0 전망 인용 불가 (장전 시황 먼저 생성 권장)"
            )
        verify_cites = [c for c in (us_pm_cite,) if c]
        verify_entries = [
            {
                "cite": us_pm_cite,
                "bench": ("미국", "SPY", "SPY"),
                "extras": [
                    ("미국", "QQQ", "QQQ"),
                    ("미국", "SMH", "SMH"),
                ],
            }
        ] if us_pm_cite else []
        verify0 = _verify_block(
            bench="SPY",
            result_metrics="SPY ▲/▼X.XX%, QQQ ▲/▼X.XX% (제공 수치)",
            mode="score",
            cite=us_pm_cite,
            extra=(
                "검증 대상: 오늘 us_premarket의 '오늘 미국장' 전망. "
                "판정 이유는 SPY·QQQ·SMH 수치 중심 — 장전 '주목'은 오늘 실제 발생·반응으로 해석."
            ),
        )
        psych = _psych_block(impact_header=f"{next_trading_label} 한국 영향")
        outlook = _outlook_block(
            title=f"{next_trading_label} 한국 시장 전망",
            condition_examples=(
                '  예: 강세 "다음 세션 QQQ ▲0.8%+ + SMH ▲1%+" / '
                '약세 "QQQ ▼0.5%+ 또는 VIX ▲10%+"'
            ),
        )
        prompt = f"""오늘 {today}({weekday_today}) 미국장 마감 시황을 아래 데이터만 사용해서 작성해줘.

{STRICT_RULE}
{AUDIENCE_RULE}
{CROSS_MARKET_RULE}
{CLOSE_REPORT_RULE}
{ENGINE_STRUCTURE_RULE}
{BREADTH_RULE}
{BRIEF_STYLE_RULE}
{NEWS_RULE}
{timing_context}

{brief_type_rule}

[제공 데이터]
{data_text}

{futures_text}

{featured_text}
{tomorrow_events}
(내일 실적발표 종목이 있으면 해당 섹터 영향을 전망에 반영할 것)

[최근 뉴스]
{news_text}

{prev_context}
## 📈 마감 시황 · {today} {weekday_today}요일

{verify0}

---

{flow_hint}

---

### 2. 🇺🇸 핵심 수치 스냅샷
- SPY … / RSP … / QQQ … / DIA … / IWM …
- **시장 폭**: SPY vs RSP 갭 X.XXp
- **섹터**: 강세·약세 각 1~2개 / VIX · Fear&Greed(제공 시)
- **크립토**(제공 시): BTC·ETH·IBIT 등락 한 줄

---

### 3. 특징주 & 섹터
[특징주] 급등·급락 상위 — 종목 +% + 촉매(뉴스 요약) 연결.
한국(삼성·하이닉스) 투자자 관점 함의 1~2문장.

---

{psych}

---

{outlook}

---

### 💡 한 줄 요약
오늘 [가장 중요한 수치] 때문에 {next_trading_label} 한국장은 [핵심 포인트] 주목.

SIGNAL:BULL 또는 SIGNAL:NEUTRAL 또는 SIGNAL:BEAR"""

    else:  # kr_premarket
        us_close_cite = _load_brief_cite("us_close")[1]
        verify_premarket = {
            "cite": us_close_cite or "",
            "source": "직전 미국 마감 시황 (us_close)",
            "score_when": "오늘 한국 마감 시황(kr_close) · KOSPI 등락률",
            "usage_hint": (
                "간밤 SMH·금리·VIX와 연결해 오늘 한국장 개장 전 전망(###5) 근거로 활용. "
                "간밤 미국 수치 상세는 아래 스냅샷(###2)에서 서술"
            ),
        }
        verify0 = _verify_block(
            mode="defer",
            cite=us_close_cite,
            source=verify_premarket["source"],
            score_when=verify_premarket["score_when"],
            extra=verify_premarket["usage_hint"],
        )
        psych = _psych_block(impact_header="오늘 한국장 영향")
        outlook = _outlook_block(
            title="오늘 한국 장 전망",
            condition_examples=(
                '  예: 강세 "SMH 간밤 ▲1%+ 및 원/달러 안정" / '
                '약세 "SMH ▼1%+ 또는 VIX ▲10%+"'
            ),
        )
        prompt = f"""오늘 {today}({weekday_today}) 한국장 전 시황을 아래 데이터만 사용해서 작성해줘.

{STRICT_RULE}
{AUDIENCE_RULE}
{CROSS_MARKET_RULE}
{ENGINE_STRUCTURE_RULE}
{PREMARKET_SECTION0_RULE}
{BREADTH_RULE}
{BRIEF_STYLE_RULE}
{NEWS_RULE}
{timing_context}

{brief_type_rule}

[제공 데이터]
{data_text}

{futures_text}

{featured_text}
{tomorrow_events}
(내일 실적발표 종목이 있으면 해당 섹터 영향을 전망에 반영할 것)

[최근 뉴스]
{news_text}

{prev_context}

## 📊 🇰🇷 한국장 전 시황 · {today} {weekday_today}요일

{verify0}

---

{flow_hint}

---

### 2. 🌙 간밤 미국·크립토 스냅샷
- SPY … / RSP … / QQQ … / SMH …
- **시장 폭** · **섹터** 각 1~2개
- **크립토**(제공 시): BTC·ETH·IBIT

---

### 3. 특징주 & 한국 연결
미국 [특징주] 급등락 + 촉매 → 삼성·하이닉스·KOSPI 함의 (재나열 금지).

---

{psych}

---

{outlook}

---

### 💡 한 줄 요약
간밤 [가장 중요한 수치] 때문에 오늘 한국장은 [핵심 포인트] 주목.

SIGNAL:BULL 또는 SIGNAL:NEUTRAL 또는 SIGNAL:BEAR"""


    t_llm = time.monotonic()
    print(
        f"[market_brief] {brief_type} Claude 생성 시작 "
        f"(prompt ~{len(prompt):,} chars, max_tokens={BRIEF_MAX_TOKENS})"
    )
    analysis = await asyncio.to_thread(_call_brief_claude, prompt, brief_type)
    print(
        f"[market_brief] {brief_type} Claude 응답 수신 "
        f"({len(analysis)} chars, {time.monotonic() - t_llm:.0f}s)"
    )

    signal = "NEUTRAL"
    if "SIGNAL:BULL" in analysis:
        signal = "BULL"
    elif "SIGNAL:BEAR" in analysis:
        signal = "BEAR"

    analysis_clean = re.sub(
        r"\*{0,2}SIGNAL:\*{0,2}\s*(BULL|NEUTRAL|BEAR)[^\n]*\n?",
        "",
        analysis,
    ).strip()

    # 첫 줄이 ## 제목이면 제거 (배너 제목과 중복 방지)
    analysis_clean = re.sub(r'^#{1,3}[^\n]*\n', '', analysis_clean).strip()

    if futures_snapshot:
        futures_block = format_futures_header_block(futures_snapshot, brief_type)
        analysis_clean = _inject_futures_header(analysis_clean, futures_block)

    # ###0 — 마감: 수치·판정 코드 확정 / 장전: 참고 인용 블록 확정
    if verify_entries:
        analysis_clean = _inject_scored_verify(
            analysis_clean, verify_entries, market_data
        )
    elif verify_premarket:
        analysis_clean = _inject_premarket_context(analysis_clean, **verify_premarket)
    elif verify_cites:
        analysis_clean = _force_verify_citations(analysis_clean, verify_cites)
    analysis_clean = _normalize_brief_headers(analysis_clean)
    analysis_clean = _repair_truncated_brief_tail(analysis_clean)

    # created_at은 실제 생성 시각(재생성 시 최신으로 올라오게)
    created = datetime.now(pytz.timezone("Europe/London")).isoformat()

    return {
        "type":        brief_type,
        "date":        today,
        "market_data": market_data,
        "fear_greed":  fear_greed,
        "futures_snapshot": futures_snapshot,
        "analysis":    analysis_clean,
        "signal":      signal,
        "created_at":  created,
    }
