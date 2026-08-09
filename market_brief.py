import re
import anthropic
import yfinance as yf
from datetime import datetime, timedelta
import pytz
from news import fetch_macro_news, format_macro_news_for_brief

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


def _has_today_session_data(region_data: dict, today: str) -> bool:
    """해당 시장 데이터에 오늘(현지) 거래일 봉 + 유효 price가 있는지"""
    if not region_data:
        return False
    if _latest_data_date(region_data) != today:
        return False
    return any(
        (d.get("last_date") or "").startswith(today) and d.get("price") is not None
        for d in region_data.values()
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
        "^KS11": "KOSPI",
        "^KQ11": "KOSDAQ",
    },
    "심리지표": {
        "^VIX":      "VIX 공포지수",
        "^TNX":      "미국 10년물 금리",
        "DX-Y.NYB":  "달러 인덱스",
    },
}

STRICT_RULE = """
[절대 원칙]
1. 제공된 데이터에 없는 내용 언급 금지
2. 뉴스/실적/경제지표 일정은 데이터로 주어지지 않으면 언급 금지
3. 근거 없는 표현 ("외국인 매수세", "AI 관련주 재조명" 등) 금지
4. 데이터로 설명 불가하면 "데이터상 원인 불명확"으로 표기
5. 숫자 없는 강세/약세 표현 금지 — 반드시 지수명 + % 포함
6. 전망은 현재 데이터 패턴에서만 도출, 외부 변수 추측 금지
7. 직전 전망이 틀렸을 때 명확히 인정하고 데이터 기반 원인 분석
8. 신뢰도는 반드시 상/중/하 세 단계 중 하나만 사용. '중상', '중하' 등 중간 단계 표현 금지
9. 추측성 표현 금지 — "~로 추정", "~가능성" 대신 데이터가 없으면 "데이터 없음"으로 명시
"""

NEWS_RULE = """
[뉴스 활용 원칙]
- 위 뉴스 제목은 참고용으로만 사용
- 제목만으로 내용을 추측해서 분석에 활용 금지
- 뉴스 제목이 시장 데이터(등락률/거래량)와 일치할 때만 연결해서 언급
- 예시 (허용): "[뉴스] 유가 하락 뉴스 + SPY ▼0.5% → 에너지 섹터 약세 가능성"
- 예시 (금지): "연준 발언 뉴스 있음 → 금리 인상 우려로 약세" (수치 없는 추측)
- 뉴스 언급 시 반드시 "[뉴스]" 태그 붙여서 데이터와 구분
"""

BRIEF_STYLE_RULE = """
[시황 작성 스타일 원칙]
1. 각 섹션은 '서술 먼저, 수치는 아래 줄로 분리'
   ✅ 올바른 방식:
      "오늘 미국 증시는 기술주 중심으로 하락했습니다."
      NASDAQ ▼1.73%  거래량 105%
      S&P500 ▼0.13%  거래량 80%
   ❌ 잘못된 방식: "NASDAQ ▼1.73% 급락하며 [뉴스] 칩 매도로..."
2. [뉴스] 태그를 서술 문장 중간에 삽입 금지
   → 서술이 끝난 뒤 별도 줄로만: "📰 관련 뉴스: XXX"
3. "다음 거래일 (07/06 월요일)" 같은 긴 표현은 처음 한 번만,
   이후에는 "내일" 또는 "월요일"로만 축약
4. 강세/약세 조건은 각 2개 이내, 불릿 최대 3개
5. 전체 분석이 완결되어야 함 — 중간에 끊기지 말 것
"""

BREADTH_RULE = """
[시장 폭(Breadth) 해석 — 반드시 적용]
1. SPY vs RSP 갭이 오늘의 진짜 스토리다
   - RSP > SPY (갭 0.5%p 이상): 대형주만 약세, 시장 전반은 견조
     → "지수 하락 = 시장 붕괴"로 서술 금지. "대형 기술주에 국한된 조정"으로 서술
   - RSP < SPY (갭 0.5%p 이상): 소수 대형주가 지수를 떠받침 = 실제론 더 약한 장
   - 갭이 0.5%p 이상이면 1번 섹션에서 반드시 언급
2. 섹터 ETF로 원인을 특정할 것
   - 특정 섹터만 급락이면 "시장 전체"가 아니라 "XX 섹터 조정"으로 서술
   - 예: SMH ▼3% + XLF ▲1% → "반도체 조정, 금융은 강세"
   - 낙폭/상승폭 상위 2개 섹터만 언급 (5개 전부 나열 금지)
3. IWM(러셀2000)으로 로테이션 확인
   - 대형주 하락 + IWM 보합/상승 = 섹터 로테이션 (약세장 아님)
4. 섹터 데이터가 없으면 언급하지 말 것 (추측 금지)
"""

WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]

# 시황 4종 — 시장별 장전/마감이 서로를 검증하는 짝 구조
BRIEF_TYPES = {
    "kr_premarket": {
        "label":   "🇰🇷 한국장 전 시황",
        "market":  "한국",
        "verify":  "kr_close",       # 직전 한국 마감 시황을 검증
        "predict": "오늘 한국장",
    },
    "kr_close": {
        "label":   "🇰🇷 한국장 마감 시황",
        "market":  "한국",
        "verify":  "kr_premarket",   # 오늘 한국 장전 전망을 검증
        "predict": "오늘 밤 미국장 관전 포인트",
    },
    "us_premarket": {
        "label":   "🇺🇸 미국장 전 시황",
        "market":  "미국",
        "verify":  "us_close",       # 직전 미국 마감 시황을 검증
        "predict": "오늘 미국장",
    },
    "us_close": {
        "label":   "🇺🇸 미국장 마감 시황",
        "market":  "미국",
        "verify":  "us_premarket",   # 오늘 미국 장전 전망을 검증
        "predict": "내일 한국장",
    },
}


def _get_tomorrow_events(now) -> str:
    """yfinance로 다음날 주요 실적발표 일정 수집"""
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    lines = [f"[내일({tomorrow}) 주요 일정]"]

    watch_tickers = ["ORCL", "CHWY", "AVGO", "ADBE", "FDX",
                     "COST", "WMT", "TGT", "HD", "LOW"]
    earnings_tomorrow = []
    for t in watch_tickers:
        try:
            cal = yf.Ticker(t).calendar
            if cal is not None and not cal.empty:
                earn_date = str(
                    cal.columns[0].date()
                    if hasattr(cal.columns[0], "date")
                    else cal.columns[0]
                )[:10]
                if earn_date == tomorrow:
                    earnings_tomorrow.append(t)
        except Exception:
            continue

    if earnings_tomorrow:
        lines.append(f"실적발표: {', '.join(earnings_tomorrow)}")
    else:
        lines.append("실적발표: 주요 종목 없음")

    lines.append("※ CPI/FOMC/고용지표 등 주요 이벤트는 뉴스에서 확인")
    return "\n".join(lines)


def _get_next_trading_day(now: datetime) -> str:
    """오늘 기준 다음 거래일 (토→월, 일→월, 평일→내일) 반환"""
    weekday = now.weekday()  # 0=월 ... 4=금, 5=토, 6=일
    if weekday == 4:    # 금요일
        delta = 3
    elif weekday == 5:  # 토요일
        delta = 2
    elif weekday == 6:  # 일요일
        delta = 1
    else:
        delta = 1
    next_day = now + timedelta(days=delta)
    next_weekday = WEEKDAY_KR[next_day.weekday()]
    return f"{next_day.strftime('%m/%d')} {next_weekday}요일"


def _fetch_ticker(ticker: str, name: str, now_ex: datetime | None = None) -> dict | None:
    """티커별 데이터 수집 — 날짜/타임존 정규화 + 장중 데이터 제외 + 재시도 3회.
    now_ex: 백필/재생성 시 기준 시각(거래소 현지). None이면 현재 시각."""
    import time

    for attempt in range(3):
        try:
            # 한국 지수는 period를 더 넉넉하게 (거래일 확보)
            period = "15d" if ticker.startswith("^K") else "10d"
            hist = yf.Ticker(ticker).history(period=period)

            if hist is None or hist.empty:
                time.sleep(2 ** attempt)
                continue

            # 타임존 정규화 — 거래소 현지 기준으로 날짜를 산출해야 하루 밀림 방지
            #   (한국 지수는 KST 자정 인덱스를 UTC로 바꾸면 전날로 밀려 off-by-one 발생)
            ex_tz = "Asia/Seoul" if ticker.startswith("^K") else "America/New_York"
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
            region = "한국" if ticker.startswith("^K") else "미국"
            market_closed = _is_after_close(region, now_ex_local)

            last_dt = hist.index[-1].date()
            if last_dt == today_ex and not market_closed:
                hist = hist.iloc[:-1]  # 오늘 장중 불완전 데이터 제외
                if len(hist) < 2:
                    time.sleep(2 ** attempt)
                    continue

            if len(hist) < 2:
                time.sleep(2 ** attempt)
                continue

            prev_close = float(hist["Close"].iloc[-2])
            current    = float(hist["Close"].iloc[-1])
            ld = hist.index[-1].date()   # 거래소 현지 거래일
            weekday_str = WEEKDAY_KR[ld.weekday()]
            date_label  = f"{ld.strftime('%Y-%m-%d')}({weekday_str})"

            change_pct = (current - prev_close) / prev_close * 100 if prev_close else 0
            volume     = int(hist["Volume"].iloc[-1]) if hist["Volume"].iloc[-1] else 0
            avg_volume = int(hist["Volume"].mean()) if hist["Volume"].mean() else 0
            vol_ratio  = round(volume / avg_volume * 100, 1) if avg_volume else 0

            days_old = (today_ex - ld).days
            stale = days_old > 1

            print(
                f"[market_brief] {'⚠️ STALE' if stale else '✅'} {ticker} {date_label} "
                f"{current:.2f} ({change_pct:+.2f}%) vol {vol_ratio}%"
                + (f" — {days_old}일 지연" if stale else "")
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
    result = {}
    for region, tickers in TICKERS.items():
        result[region] = {}
        now_ex = now_kst if region == "한국" else now_et
        for ticker, name in tickers.items():
            d = _fetch_ticker(ticker, name, now_ex=now_ex)
            if d:
                result[region][ticker] = d
    total = sum(len(v) for v in result.values())
    print(f"[market_brief] 총 {total}개 지수 수집 완료")
    return result


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
            arrow = "▲" if d["change_pct"] > 0 else "▼"
            if d.get("stale"):
                lines.append(
                    f"- {d['name']}({ticker}) "
                    f"[⚠️ {d['last_date']} 데이터 — 당일 미수집, 전망 활용 금지]: "
                    f"${d['price']} {arrow}{abs(d['change_pct'])}% "
                    f"(※ 오늘 데이터 아님)"
                )
            else:
                lines.append(
                    f"- {d['name']}({ticker}) [데이터일: {d['last_date']}]: "
                    f"${d['price']} "
                    f"{arrow}{abs(d['change_pct'])}% "
                    f"(거래량 평균 대비 {d['volume_ratio']}%)"
                )
    return "\n".join(lines)


def _extract_forecast(analysis: str) -> str:
    """직전 시황 분석에서 '전망 섹션'을 추출 — 여러 패턴 대응"""
    if not analysis:
        return ""
    patterns = [
        r"###\s*\d+[\.\s🔮]*[^\n]*(전망|Forecast)[^\n]*\n([\s\S]*?)(?=\n###|\Z)",
        r"###\s*\d+[\.\s🔮]*[^\n]*(오늘|내일|한국)[^\n]*장[^\n]*\n([\s\S]*?)(?=\n###|\Z)",
        r"(강세 우위|약세 우위|중립)[^\n]*\n([\s\S]{0,400}?)(?=\n###|\Z)",
    ]
    for pat in patterns:
        m = re.search(pat, analysis, re.IGNORECASE)
        if m:
            return m.group(0)[:500].strip()
    # 마지막 fallback — 뒤쪽 300자 (전망은 보통 후반부)
    return analysis[-300:].strip()


def _build_prev_context(brief_type: str) -> str:
    """같은 시장의 검증 짝(verify) 시황만 가져와 직전 전망 검증에 사용"""
    from database import get_recent_market_briefs
    cfg = BRIEF_TYPES.get(brief_type)
    if not cfg:
        return ""
    verify_type = cfg["verify"]

    prev_list = get_recent_market_briefs(limit=1, brief_type=verify_type)
    if not prev_list:
        return ""

    prev    = prev_list[0]
    label   = BRIEF_TYPES.get(verify_type, {}).get("label", verify_type)
    predict = BRIEF_TYPES.get(verify_type, {}).get("predict", "")
    forecast_text = _extract_forecast(prev.get("analysis", ""))

    return (
        f"\n[검증 대상 — {prev.get('date')} {label} / SIGNAL:{prev.get('signal')}]\n"
        f"{forecast_text}\n"
        f"[이 전망({predict} 예측)을 오늘 실제 결과와 비교해 "
        f"### 0. 직전 전망 검증에서 한 줄 인용하고 ✅/❌ 판정할 것]\n"
    )


async def generate_market_brief(brief_type: str, as_of: str | None = None) -> dict:
    """as_of: 'YYYY-MM-DD' — 해당일 정규 시각 기준으로 백필/재생성."""
    from database import get_recent_market_briefs
    import asyncio

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

    market_data = get_market_data(now_et=now_et, now_kst=now_kst)
    macro_news = fetch_macro_news(max_per_source=3)
    news_text = format_macro_news_for_brief(macro_news)

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
            if _has_today_session_data(market_data.get(region_key, {}), today):
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

    # 한국 지수 stale 여부 확인 → kr_close 브리프 데이터로 대체
    kr_data = market_data.get("한국", {})
    kr_stale = not kr_data or any(
        v.get("stale") or not v.get("price") for v in kr_data.values()
    )
    if kr_stale:
        korea_brief = next(
            (b for b in recent if b.get("type") == "kr_close"),
            None
        )
        if korea_brief and korea_brief.get("market_data", {}).get("한국"):
            market_data["한국"] = korea_brief["market_data"]["한국"]
            print(
                f"[market_brief] 한국 지수 stale → kr_close 브리프 데이터로 대체 "
                f"({korea_brief['date']})"
            )
        else:
            print("[market_brief] 한국 지수 stale + kr_close 브리프 없음 → 데이터 없음 처리")
            market_data["한국"] = {}

    data_text = _build_data_text(market_data)
    prev_context = _build_prev_context(brief_type)

    try:
        tomorrow_events = _get_tomorrow_events(now)
    except Exception as e:
        print(f"[market_brief] 내일 일정 수집 실패: {e}")
        tomorrow_events = ""

    # 적중률 저장 — 마감 시황일 때 같은 시장의 당일 장전 전망을 검증
    if brief_type in ("kr_close", "us_close"):
        verify_type = cfg["verify"]   # kr_premarket / us_premarket
        prev_list = get_recent_market_briefs(limit=1, brief_type=verify_type)
        if prev_list:
            prev = prev_list[0]
            prev_signal = prev.get("signal", "")
            if target_market == "한국":
                idx = market_data.get("한국", {}).get("^KS11", {})
            else:
                idx = market_data.get("미국", {}).get("SPY", {})
            actual_signal = ""
            if idx and not idx.get("stale"):
                chg = idx.get("change_pct", 0) or 0
                # 중립 밴드 ±0.3%: 보합은 NEUTRAL로 판정 → NEUTRAL 전망도 정당하게 채점
                if abs(chg) < 0.3:
                    actual_signal = "NEUTRAL"
                elif chg > 0:
                    actual_signal = "BULL"
                else:
                    actual_signal = "BEAR"

            if actual_signal and prev_signal:
                is_correct = (prev_signal == actual_signal)
                try:
                    from database import save_brief_performance
                    save_brief_performance(
                        brief_id=str(prev.get("_id", "")),
                        predicted=prev_signal,
                        actual=actual_signal,
                        is_correct=is_correct,
                        brief_type=prev.get("type", ""),
                    )
                    print(
                        f"[market_brief] 적중률 저장: {prev.get('type')} "
                        f"{prev_signal}→{actual_signal} {'✅' if is_correct else '❌'}"
                    )
                except Exception as e:
                    print(f"[market_brief] 적중률 저장 실패: {e}")

    next_trading_day = _get_next_trading_day(now)
    next_trading_label = (
        f"다음 거래일 ({next_trading_day})"
        if now.weekday() == 4   # 금요일에만
        else "내일"
    )

    # 미국 마감 시황일 때 kr_close 브리프 한 줄 요약을 별도 컨텍스트로 추출
    kr_close_context = ""
    if brief_type == "us_close":
        korea_brief = next(
            (b for b in recent if b.get("type") == "kr_close"),
            None
        )
        if korea_brief:
            summary_match = re.search(
                r"###\s*\d+\.\s*💡[^\n]*\n([^\n#]+)",
                korea_brief.get("analysis", "")
            )
            kr_one_line = summary_match.group(1).strip() if summary_match else ""
            kr_close_context = f"""
[오늘 한국 장 마감 결과 — kr_close 브리프 {korea_brief['date']} 기준]
{kr_one_line}
SIGNAL: {korea_brief.get('signal', 'NEUTRAL')}
(위 내용을 "{next_trading_label} 한국 시장 전망" 섹션 작성 시 참고할 것)
"""

    # 적중률 자기보정 컨텍스트
    from database import get_brief_accuracy
    accuracy = get_brief_accuracy(limit=20, market=target_market)
    accuracy_context = ""
    if accuracy["total"] >= 3:
        error_text = (
            ", ".join(accuracy["recent_errors"])
            if accuracy["recent_errors"]
            else "없음"
        )
        accuracy_context = f"""
[최근 {target_market} 시황 적중률 — 자기보정 참고 / 판정 기준: 실제 지수 등락률 ±0.3%는 NEUTRAL]
- 최근 {accuracy['total']}회 중 {accuracy['correct']}회 적중 ({accuracy['accuracy_pct']}%)
- 반복 오류 패턴: {error_text}
- 위 오류 패턴이 있으면 이번 전망에서 반대 방향 가중치를 높일 것
- BULL 전망이 반복 빗나갔으면 → 이번엔 BEAR 또는 NEUTRAL 검토
- BEAR 전망이 반복 빗나갔으면 → 이번엔 BULL 또는 NEUTRAL 검토
"""

    # 다음 한국 거래일 (캘린더 반영)
    next_kr     = get_next_trading_day("한국", now_kst)
    next_kr_str = f"{next_kr.strftime('%m/%d')}({WEEKDAY_KR[next_kr.weekday()]})"

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
6. "다음 거래일 (MM/DD 요일)" 반복 표기 금지 — 처음 1회만, 이후 "다음 거래일"로만
7. status=PRE_OPEN → "장 시작 전"으로 서술. "휴장"/"데이터 미수집"으로 표현 금지
   ✅ "오늘 {target_market}장은 아직 개장 전입니다. 아래는 직전 거래일 마감 기준입니다."
   ❌ "{target_market} 데이터 미수집" / "{target_market} 휴장"
8. status=UNKNOWN → 단정 금지. "수집 실패로 전망 불가"처럼 장황하게 쓰지 말고 전일 기준만 짧게 참고
"""
    timing_context += accuracy_context

    if brief_type == "us_premarket":
        prompt = f"""오늘 {today}({weekday_today}) 미국장 전 시황을 아래 데이터만 사용해서 작성해줘.

{STRICT_RULE}
{BREADTH_RULE}
{BRIEF_STYLE_RULE}
{timing_context}

[제공 데이터]
{data_text}
{tomorrow_events}
(내일 실적발표 종목이 있으면 해당 섹터 영향을 전망에 반영할 것)

[최근 뉴스]
{news_text}

{prev_context}

## 📊 장전 시황 · {today} {weekday_today}요일

### 0. 직전 전망 검증
반드시 직전 시황의 전망을 **한 줄 인용**하고 결과를 비교할 것.
- 전망: "[직전 시황에서 예측한 방향과 근거 인용]"
- 결과: ✅ 적중 또는 ❌ 빗나감 — 실제 수치로 판단
- 원인: 데이터로 읽히는 원인 1개 (수치 포함)
- 교훈: 다음 분석에 반영할 점 한 줄
(직전 시황 없으면 이 섹션 생략)

---

### 1. 🇰🇷 한국 시장 마감 결과
서술 (2~3문장): [데이터일] 기준 한국 증시가 어떻게 마감했는지, 이유와 함께 자연스럽게 서술.

KOSPI  ▲/▼X.XX%  거래량 XXX%
KOSDAQ ▲/▼X.XX%  거래량 XXX%
(데이터 없으면 "오늘 한국 시장 데이터 없음 — 전날 결과 기준 참고"로만 표기)

---

### 2. 🇺🇸 미국 장전 상황
서술 (2~3문장): 전일 미국 마감 결과와 장 시작 전 분위기를 서술.

S&P500 ▲/▼X.XX%  거래량 XXX%
NASDAQ ▲/▼X.XX%  거래량 XXX%
DOW    ▲/▼X.XX%  거래량 XXX%
📰 관련 뉴스: (있을 때만 1줄)

---

### 3. 📊 시장 심리 한 눈에
| 지표 | 수치 | 방향 | 오늘 영향 |
|------|------|------|-----------|
| VIX | XX.XX | ▲/▼ | 공포/중립/탐욕 |
| 달러 | XXX.XX | ▲/▼ | 원화 강세/약세 |
| 금리 | X.XX% | ▲/▼ | 성장주 부담/완화 |

---

### 4. 🔮 오늘 미국 장 전망
결론을 먼저 한 문장으로:

**결론: 강세 우위 / 약세 우위 / 중립**

- 강세: [데이터 기반 근거 1줄]
- 약세: [데이터 기반 근거 1줄]

신뢰도: 상/중/하
핵심 체크: [오늘 장에서 봐야 할 것 1개]

---

### 5. 💡 한 줄 요약
[가장 중요한 수치 1개] 때문에 오늘 [핵심 포인트] 주목.

SIGNAL:BULL 또는 SIGNAL:NEUTRAL 또는 SIGNAL:BEAR"""

    elif brief_type == "kr_close":
        prompt = f"""오늘 {today}({weekday_today}) 한국 장 마감 시황을 아래 데이터만 사용해서 작성해줘.

{STRICT_RULE}
{NEWS_RULE}
{timing_context}

[제공 데이터]
{data_text}
{tomorrow_events}
(내일 실적발표 종목이 있으면 해당 섹터 영향을 시황 전망에 반드시 반영할 것)

[최근 24시간 매크로 뉴스]
{news_text}

{prev_context}

### 0. 직전 전망 검증
(직전 시황 전망 있을 때만. 없으면 생략)
- ✅ 적중 또는 ❌ 빗나감 — 전망 vs 실제 결과 한 줄
- 원인: 데이터 기반 원인 수치 포함
- 교훈: 다음 분석에 반영할 점 한 줄

---

### 1. 🇰🇷 한국 시장 마감 결과
오늘 한국 증시 흐름을 자연스러운 문장으로 먼저 서술한 뒤 수치 정리.
stale 데이터가 있으면 절대 서술하지 말고 "오늘 데이터 미수집" 명시.

KOSPI  X,XXX.XX  ▲/▼X.XX%  거래량 XXX%
KOSDAQ X,XXX.XX  ▲/▼X.XX%  거래량 XXX%

뉴스 연결 (데이터 방향과 일치할 때만):
"[뉴스] XX 이슈가 위 흐름의 배경으로 보입니다."

---

### 2. 📊 시장 심리
- 달러/원 환율 동향 → 외국인 수급 영향 한 줄
- 글로벌 선물 동향 (있을 때만) → 내일 방향성 힌트 한 줄
- 섹터별 특이사항 (있을 때만) → 수치 포함

---

### 3. 🔮 내일 한국 시장 전망
결론을 먼저 한 문장으로.

**결론: 강세 우위 / 약세 우위 / 중립**
강세 조건: 구체적 수치 조건
약세 조건: 구체적 수치 조건
신뢰도: 상 / 중 / 하 (세 가지 중 하나만 사용)
핵심 체크: 내일 한국장에서 봐야 할 것 1개

---

### 4. 💡 한 줄 요약
한 문장, 30자 이내:
"XXX 때문에 내일 한국장은 XXX 예상."

SIGNAL:BULL 또는 SIGNAL:NEUTRAL 또는 SIGNAL:BEAR"""

    elif brief_type == "us_close":
        prompt = f"""오늘 {today}({weekday_today}) 미국장 마감 시황을 아래 데이터만 사용해서 작성해줘.

{STRICT_RULE}
{BREADTH_RULE}
{BRIEF_STYLE_RULE}
{timing_context}

[제공 데이터]
{data_text}
{tomorrow_events}
(내일 실적발표 종목이 있으면 해당 섹터 영향을 전망에 반영할 것)

[최근 뉴스]
{news_text}

{prev_context}
{kr_close_context}
## 📈 마감 시황 · {today} {weekday_today}요일

### 0. 직전 전망 검증
반드시 오늘 장전 시황의 전망을 **한 줄 인용**하고 결과를 비교할 것.
- 전망: "[장전 시황에서 예측한 방향과 근거 인용]"
- 결과: ✅ 적중 또는 ❌ 빗나감 — 실제 마감 수치로 판단
- 원인: 예상과 달랐던 이유 1개 (수치 포함)
- 교훈: 다음 분석에 반영할 점 한 줄
(장전 시황 없으면 이 섹션 생략)

---

### 1. 🇺🇸 미국 시장 마감 결과
서술 (2~4문장): [데이터일] 기준으로 오늘 미국 증시가 어떻게 마감했는지 서술.
반드시 포함:
- SPY vs RSP 갭이 0.5%p 이상이면 시장 폭 해석을 첫 문장에 배치
- 섹터 중 낙폭/상승폭 상위 2개만 원인으로 지목
- "지수만 보면 X, 실제로는 Y" 구조로 서술

S&P500      ▲/▼X.XX%  거래량 XXX%
S&P 동일가중  ▲/▼X.XX%   ← SPY와 갭 있으면 강조
NASDAQ      ▲/▼X.XX%  거래량 XXX%
DOW         ▲/▼X.XX%  거래량 XXX%
러셀2000     ▲/▼X.XX%

섹터 (상위 2개만):
반도체(SMH)  ▲/▼X.XX%
금융(XLF)    ▲/▼X.XX%
📰 관련 뉴스: (데이터 방향과 일치할 때만 1줄)

---

### 2. 🇰🇷 {next_trading_label} 한국 시장 전망
서술 (2~3문장): 오늘 미국 결과가 {next_trading_label} 한국장에 미칠 영향을 미국→한국 경로로 설명.

**결론: 강세 우위 / 약세 우위 / 중립**
- 강세 조건: [구체적 수치 조건]
- 약세 조건: [구체적 수치 조건]

신뢰도: 상/중/하
핵심 체크: [{next_trading_label} 개장 후 봐야 할 것 1개]

---

### 3. 📊 시장 심리 한 눈에
| 지표 | 수치 | 방향 | 내일 영향 |
|------|------|------|-----------|
| VIX | XX.XX | ▲/▼ | 공포/중립/탐욕 |
| 달러 | XXX.XX | ▲/▼ | 원화 강세/약세 |
| 금리 | X.XX% | ▲/▼ | 성장주 부담/완화 |

---

### 4. 💡 한 줄 요약
오늘 [가장 중요한 수치] 때문에 {next_trading_label} 한국장은 [핵심 포인트] 주목.

SIGNAL:BULL 또는 SIGNAL:NEUTRAL 또는 SIGNAL:BEAR"""

    else:  # kr_premarket
        prompt = f"""오늘 {today}({weekday_today}) 한국장 전 시황을 아래 데이터만 사용해서 작성해줘.

{STRICT_RULE}
{BREADTH_RULE}
{BRIEF_STYLE_RULE}
{timing_context}

[제공 데이터]
{data_text}
{tomorrow_events}
(내일 실적발표 종목이 있으면 해당 섹터 영향을 전망에 반영할 것)

[최근 뉴스]
{news_text}

{prev_context}

## 📊 🇰🇷 한국장 전 시황 · {today} {weekday_today}요일

### 0. 직전 전망 검증
직전 한국장 마감 시황의 전망을 **한 줄 인용**하고 간밤 미국장 결과로 검증할 것.
- 전망: "[직전 한국 마감 시황의 관전 포인트 인용]"
- 결과: ✅ 적중 또는 ❌ 빗나감 — 간밤 미국 실제 수치로 판단
- 원인: 데이터로 읽히는 원인 1개 (수치 포함)
- 교훈: 다음 분석에 반영할 점 한 줄
(직전 시황 없으면 이 섹션 생략)

---

### 1. 🌙 간밤 미국 시장 결과
서술 (2~3문장): 간밤 미국 증시 마감 흐름과 오늘 한국장에 줄 영향을 서술.
- SPY vs RSP 갭이 0.5%p 이상이면 시장 폭 해석을 배치
- 섹터 중 낙폭/상승폭 상위 2개만 지목 (특히 반도체는 삼성/하이닉스와 직결)

S&P500      ▲/▼X.XX%  거래량 XXX%
NASDAQ      ▲/▼X.XX%  거래량 XXX%
DOW         ▲/▼X.XX%
러셀2000     ▲/▼X.XX%
반도체(SMH)  ▲/▼X.XX%
📰 관련 뉴스: (데이터 방향과 일치할 때만 1줄)

---

### 2. 📊 시장 심리 한 눈에
| 지표 | 수치 | 방향 | 오늘 한국장 영향 |
|------|------|------|-----------------|
| VIX | XX.XX | ▲/▼ | 공포/중립/탐욕 |
| 달러 | XXX.XX | ▲/▼ | 원화 강세/약세, 외국인 수급 |
| 금리 | X.XX% | ▲/▼ | 성장주 부담/완화 |

---

### 3. 🔮 오늘 한국 장 전망
결론을 먼저 한 문장으로:

**결론: 강세 우위 / 약세 우위 / 중립**

- 강세: [데이터 기반 근거 1줄]
- 약세: [데이터 기반 근거 1줄]

신뢰도: 상/중/하
핵심 체크: [오늘 한국장에서 봐야 할 것 1개]

---

### 4. 💡 한 줄 요약
간밤 [가장 중요한 수치] 때문에 오늘 한국장은 [핵심 포인트] 주목.

SIGNAL:BULL 또는 SIGNAL:NEUTRAL 또는 SIGNAL:BEAR"""

    client = anthropic.Anthropic()
    message = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )
    analysis = message.content[0].text

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

    # created_at은 실제 생성 시각(재생성 시 최신으로 올라오게)
    created = datetime.now(pytz.timezone("Europe/London")).isoformat()

    return {
        "type":        brief_type,
        "date":        today,
        "market_data": market_data,
        "analysis":    analysis_clean,
        "signal":      signal,
        "created_at":  created,
    }
