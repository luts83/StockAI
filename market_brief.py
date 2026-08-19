import math
import re
import anthropic
import yfinance as yf
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
}

KR_INDEX_TICKERS = ("^KS11", "^KQ11")
KR_MEGA_TICKERS = ("005930.KS", "000660.KS")

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


def _is_kr_symbol(ticker: str) -> bool:
    t = (ticker or "").upper()
    return t.startswith("^K") or t.endswith(".KS") or t.endswith(".KQ")


STRICT_RULE = """
[절대 원칙]
1. 제공된 데이터에 없는 내용 언급 금지
2. 뉴스/실적/경제지표 일정은 데이터로 주어지지 않으면 언급 금지
3. 근거 없는 표현 ("외국인 매수세", "AI 관련주 재조명" 등) 금지 — 단, 제공된 수급·특징주 수치 인용은 허용
4. 데이터로 설명 불가하면 "데이터상 원인 불명확"으로 표기
5. 숫자 없는 강세/약세 표현 금지 — 반드시 지수/종목명 + % 포함
6. 전망은 현재 데이터 패턴에서만 도출, 외부 변수 추측 금지
7. 직전 전망이 틀렸을 때 명확히 인정하고 데이터 기반 원인 분석
8. 신뢰도는 반드시 상/중/하 세 단계 중 하나만 사용. '중상', '중하' 등 중간 단계 표현 금지
9. 추측성 표현 금지 — "~로 추정", "~가능성" 대신 데이터가 없으면 "데이터 없음"으로 명시
10. Fear & Greed / 삼성·하이닉스 / 섹터 ETF는 제공된 경우에만 언급
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
1. 서술(짧게) → 수치는 아래 항목으로 분리. 숫자는 "종가 / 등락률 / 거래량" 형식
2. [뉴스] 태그를 서술 문장 중간에 삽입 금지 → 별도 줄: "📰 [제목] → [연결 지표]"
3. "다음 거래일 (MM/DD 요일)" 긴 표현은 처음 1회만, 이후 "다음 거래일"/요일로 축약.
   달력상 내일이 거래일이 아니면 "내일" 금지
4. 강세/약세 조건은 각 2개 이내, 지표명+임계치 필수
5. 전체 분석이 완결되어야 함 — 중간에 끊기지 말 것
"""

ENGINE_STRUCTURE_RULE = """
[시황 엔진 공통 구조 — 장전/마감·한/미 모두 동일]
0. 직전 전망 검증
1. 핵심 시장 스냅샷 — 무엇이 움직였는가 (짧은 한줄 + 구조화 수치)
2. 업종·특징주 — 왜/의미 (앞 섹션 등락률·종가 재나열 금지)
3. 시장 심리 한 눈에 — 지표명 명확히 (VIX, DXY, USD/KRW, 2Y, 10Y, 금리차)
4. 전망 — 데이터→분석→결론 + 검증 가능한 수치 조건
5. 한 줄 요약 — 짧게 (불필요하게 늘리지 말 것)

[가독성]
- 30초~1분 스캔용. 긴 줄글 단락 금지. 항목·표·짧은 문장
- 제공된 심리지표만 표에 넣고, 없는 행은 생략
- "달러"/"금리"처럼 모호한 표기 금지
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


def _verify_block(
    *,
    bench: str,
    result_metrics: str,
    mode: str = "score",
    extra: str = "",
    cite: str = "",
    secondary: list[tuple[str, str]] | None = None,
) -> str:
    """공통 직전 전망 검증 섹션.
    mode=score: 벤치마크로 적중 판정
    mode=defer: 전망 대상이 아직이라 검증 보류 (SPY로 한국전망 채점 금지 등)
    cite: Mongo에서 만든 '전망:' 복붙 문구 (SIGNAL + 근거)
    secondary: [(소제목, 인용문구), ...] — kr_close의 us_close 추가 검증 등
    """
    cite_line = (cite or "").strip() or "전망 기록 없음 (SIGNAL/본문 미확인)"
    if mode == "defer":
        return f"""### 0. 직전 전망 검증
(직전 시황 없으면 생략)
이 시점에서는 **SIGNAL 채점만 하지 말고 검증 보류**한다. 교차시장 숫자는 전망에 쓴다.

- 전망: {cite_line}
  ※ 위 전망 줄을 삭제·'-'·공백으로 바꾸지 말 것 (그대로 두고 판정만 보류)
- 실제 결과: {result_metrics}
- 판정: **검증 보류**
- 판정 이유: {extra or "전망 대상 시장 채점 시점이 아님 (교차시장 입력 활용과 무관)"}
- 다음 전망 반영: 직전 요지 + 오늘 확정된 교차시장 마감을 오늘 전망 입력으로 연결
※ 미국 SPY로 한국장 전망을 채점하거나, 한국 KOSPI로 미국장 전망을 **채점**하는 것만 금지
※ 한국 급락/급등·거래량·뉴스를 미국 장전 전망 근거로 쓰는 것은 필수 (유기적 연결)
※ '검증 보류'·일부 지표 지연만으로 "스냅샷 작성 불가/리포트 불가" 금지 — 있는 수치로 작성
"""

    blocks = [f"""### 0. 직전 전망 검증
(직전 시황 없으면 생략)
**전망 인용 없이 적중/빗나감 판정 금지.** 아래 '전망:'은 Mongo 직전 시황에서 채운 문장이다.

#### 직전 전망
- 전망: {cite_line}
  ※ 위 줄을 수정·삭제·'-'로 바꾸지 말 것. SIGNAL과 근거가 보여야 함
- 실제 결과: {result_metrics}
- 판정: 적중 / 부분 적중 / 빗나감 중 하나
  (인용이 "전망 기록 없음"이면 판정=**검증 불가** — 적중/빗나감 금지)
- 판정 이유: 인용한 전망(SIGNAL·근거)과 실제 수치를 대응해 1~2문장
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
서술 2~3문장: 앞 데이터 → 분석 → 결론이 한 줄기로 연결되게.

**결론: 강세 우위 / 약세 우위 / 중립**
- 강세 조건: 검증 가능한 수치 조건
- 약세 조건: 검증 가능한 수치 조건
{condition_examples}
※ 모호한 조건 금지 — 지표명 + 임계치 필수

신뢰도: 상/중/하
핵심 체크: 이번에 볼 것 1개
"""


WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]

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
) -> str:
    """섹터·국장 대형주·F&G를 프롬프트용 텍스트로 정리."""
    lines = ["[업종·특징주·심리 — 아래 수치만 인용, 없는 종목 창작 금지]"]

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
        "작성 규칙: 위 항목으로 '업종·특징주' 섹션을 3~5줄로만 작성. "
        "앞에서 쓴 등락률 재나열 금지 — 의미·함의 중심. "
        "제공되지 않은 개별 특징주 이름을 만들지 말 것."
    )
    return "\n".join(lines)


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


def _condense_forecast_line(forecast_text: str) -> str:
    """전망 섹션에서 한 줄 근거만 뽑기."""
    if not forecast_text:
        return ""
    keys = ("강세", "약세", "중립", "우위", "결론", "BULL", "BEAR", "NEUTRAL")
    for raw in forecast_text.splitlines():
        line = raw.strip().lstrip("-*#• ").strip()
        if len(line) < 8:
            continue
        if any(k in line for k in keys):
            return line[:200]
    for raw in forecast_text.splitlines():
        line = raw.strip().lstrip("-*#• ").strip()
        if len(line) >= 20 and not line.startswith("["):
            return line[:200]
    return forecast_text.replace("\n", " ").strip()[:200]


def _forecast_citation(doc: dict | None) -> str:
    """###0 '전망:' 칸에 넣을 SIGNAL+근거 한 줄 (Mongo 직전 시황)."""
    if not doc:
        return ""
    sig = (doc.get("signal") or "NEUTRAL").strip().upper()
    if sig not in ("BULL", "NEUTRAL", "BEAR"):
        sig = "NEUTRAL"
    analysis = doc.get("analysis") or ""
    one = _extract_one_liner(analysis)
    body = one or _condense_forecast_line(_extract_forecast(analysis))
    if not body:
        body = "(상세 전망 문장 없음 — SIGNAL만 채점)"
    date = doc.get("date") or ""
    return f"[{date}] SIGNAL:{sig} — {body}"


def _load_brief_cite(brief_type: str) -> tuple[dict | None, str]:
    """최근 해당 타입 시황 + 전망 인용 문구."""
    from database import get_recent_market_briefs
    docs = get_recent_market_briefs(limit=1, brief_type=brief_type)
    if not docs:
        return None, ""
    return docs[0], _forecast_citation(docs[0])


def _extract_one_liner(analysis: str) -> str:
    """시황 본문에서 '한 줄 요약' 추출."""
    if not analysis:
        return ""
    m = re.search(
        r"###\s*[💡\s]*한\s*줄\s*요약\s*\n+\s*([^\n]+)",
        analysis,
    )
    if m:
        return m.group(1).strip()[:220]
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


def _build_prev_context(brief_type: str) -> str:
    """검증 짝 시황을 가져와 직전 전망 검증에 사용.
    verify_mode=defer → 인용만, 수치 채점 금지
    verify_mode=score → 같은 세션 장전 전망을 마감 수치로 채점
    """
    cfg = BRIEF_TYPES.get(brief_type)
    if not cfg:
        return ""
    verify_type = cfg["verify"]
    mode = cfg.get("verify_mode", "score")

    prev, cite = _load_brief_cite(verify_type)
    if not prev:
        return ""

    label   = BRIEF_TYPES.get(verify_type, {}).get("label", verify_type)
    predict = BRIEF_TYPES.get(verify_type, {}).get("predict", "")
    forecast_text = _extract_forecast(prev.get("analysis", ""))

    head = (
        f"\n[검증 대상 — {prev.get('date')} {label} / SIGNAL:{prev.get('signal')}]\n"
        f"직전 전망 대상: {predict}\n"
        f"【###0 전망 칸 복붙용】{cite}\n"
        f"{forecast_text}\n"
    )

    if mode == "defer":
        return (
            head
            + f"[중요] ###0 채점만 보류. 교차시장 데이터 활용은 필수.\n"
            f"- '전망:'에는 위 복붙용 문구를 그대로 둘 것 (비우기/'-' 금지)\n"
            f"- 적중/부분 적중/빗나감 판정 금지 (판정=검증 보류)\n"
            f"- 미국 지수로 한국 전망을 채점하거나 그 반대 금지\n"
            f"- 단, 한국 마감 수치·뉴스는 오늘 미국장 전망의 입력 변수로 적극 사용\n"
            f"- '검증 보류'를 이유로 리포트 작성 불가/스냅샷 생략 금지\n"
        )

    return (
        head
        + f"[이 전망({predict})을 오늘 실제 마감 수치와 비교해 판정할 것]\n"
        f"- '전망:'에는 【복붙용】을 그대로 사용 — 비우거나 '-'만 쓰면 안 됨\n"
        f"- 전망 인용 없이 적중/빗나감 판정 금지\n"
        f"- 실제 결과: 벤치마크 등락률\n"
        f"- 판정 이유 + 다음 전망 반영 규칙 1줄\n"
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

    fear_greed = None
    if brief_type.startswith("us"):
        fear_greed = await asyncio.to_thread(fetch_fear_greed)

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
        market_data, brief_type=brief_type, fear_greed=fear_greed
    )
    prev_context = "\n".join(
        x for x in (
            _build_prev_context(brief_type),
            _build_circulation_context(brief_type),
        ) if x
    ).strip()

    try:
        tomorrow_events = _get_tomorrow_events(now)
    except Exception as e:
        print(f"[market_brief] 내일 일정 수집 실패: {e}")
        tomorrow_events = ""

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

    if brief_type in ("kr_close", "us_close"):
        verify_type = cfg["verify"]   # kr_premarket / us_premarket
        prev_list = get_recent_market_briefs(limit=1, brief_type=verify_type)
        if prev_list:
            if target_market == "한국":
                idx = market_data.get("한국", {}).get("^KS11", {})
            else:
                idx = market_data.get("미국", {}).get("SPY", {})
            if idx and not idx.get("stale") and idx.get("change_pct") is not None:
                _score_brief_vs_index(prev_list[0], float(idx["change_pct"]), "same-day")

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

    if brief_type == "us_premarket":
        _, us_close_cite = _load_brief_cite("us_close")
        verify0 = _verify_block(
            bench="—",
            result_metrics=(
                "채점 대상 아님 — 직전 us_close의 '다음 한국장' 전망은 "
                "한국 마감(kr_close)에서 KOSPI로 채점. "
                "여기선 한국 마감 수치를 인용만"
            ),
            mode="defer",
            cite=us_close_cite,
            extra=(
                "직전 us_close SIGNAL은 한국장 전망이다. "
                "채점은 kr_close에서 하고, 여기서는 한국 마감(코스피/코스닥 등락·거래량)을 "
                "오늘 미국장 전망의 선행 신호로 연결할 것."
            ),
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
{ENGINE_STRUCTURE_RULE}
{BREADTH_RULE}
{BRIEF_STYLE_RULE}
{NEWS_RULE}
{timing_context}

[이 리포트 특성 — 미국 장전]
- 이미 끝난 한국 마감 + 직전 미국 세션을 스냅샷으로 정리한 뒤, 오늘 미국장 전망
- 한국↔미국은 유기적 관계: 한국 마감 급락/급등·거래량·특징주는 오늘 미국장 전망의 핵심 입력
- [교차시장 순환 주입]의 kr_close SIGNAL·핵심 수치를 오늘 미국장 전망에 반드시 연결
- 장중 미확정 봉을 오늘 마감처럼 쓰지 말 것
- 직전 us_close의 '다음 한국장' 전망 → ###0은 SIGNAL 채점만 검증 보류 (채점 시점은 kr_close)
- 일부 심리지표가 stale여도, 제공된 한국·미국 지수/섹터/뉴스로 리포트 완성할 것
- "등락률 누락/데이터 불완전 → 스냅샷 작성 불가" 금지 — 제공된 숫자는 반드시 종가/등락률/거래량으로 기재
- PRE_OPEN은 직전 미국 세션 마감이 정상 기준이다 (없으면 없다고만 짧게)

[제공 데이터]
{data_text}

{featured_text}
{tomorrow_events}
(내일 실적발표 종목이 있으면 해당 섹터 영향을 전망에 반영할 것)

[최근 뉴스]
{news_text}

{prev_context}

## 📊 장전 시황 · {today} {weekday_today}요일

{verify0}

---

### 1. 🇰🇷 한국 시장 마감 결과
한 문장 핵심 (최대 2문장). 숫자는 아래에.

**주요 지수** (종가 / 등락률 / 거래량)
- KOSPI …
- KOSDAQ …
- 삼성전자 / SK하이닉스 (제공 시)

(데이터 없으면 "한국 시장 데이터 없음 — 전일 참고"만)

---

### 2. 🇺🇸 미국 장전·직전세션 스냅샷
한 문장 핵심. 숫자는 아래에.

**주요 지수** (종가 / 등락률 / 거래량)
- S&P500 (SPY) …
- S&P 동일가중 (RSP) …
- NASDAQ (QQQ) …
- DOW (DIA) …
- 러셀2000 (IWM) …

**시장 폭**: SPY vs RSP 갭 — 한 줄
**강세/약세 섹터** (각 1~2개, 종가/등락률/거래량)
**시장 심리**: VIX · Fear & Greed(제공 시)
📰 관련 뉴스: (데이터 방향과 일치할 때만) [제목] → [연결 지표]

---

### 3. 업종·특징주
3~5줄. 수치 재나열 금지. 왜/의미·오늘 미국장 함의. 없는 종목 창작 금지.

---

{psych}

---

{outlook}

---

### 💡 한 줄 요약
[가장 중요한 수치] 때문에 오늘 미국장은 [핵심 포인트] 주목.

SIGNAL:BULL 또는 SIGNAL:NEUTRAL 또는 SIGNAL:BEAR"""

    elif brief_type == "kr_close":
        _, kr_pm_cite = _load_brief_cite("kr_premarket")
        _, us_close_cite = _load_brief_cite("us_close")
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
{ENGINE_STRUCTURE_RULE}
{BRIEF_STYLE_RULE}
{NEWS_RULE}
{timing_context}

[이 리포트 특성 — 한국 마감]
- 오늘 한국 마감 스냅샷 → 의미 → {next_trading_label} 전망
- [교차시장 순환 주입]의 kr_premarket·us_close를 검증·맥락에 반영
- stale 데이터는 전망에 쓰지 말고 명시
- 비거래일을 "내일"로 쓰지 말 것

[제공 데이터]
{data_text}

{featured_text}
{tomorrow_events}
(내일 실적발표 종목이 있으면 해당 섹터 영향을 시황 전망에 반드시 반영할 것)

[최근 24시간 매크로 뉴스]
{news_text}

{prev_context}

## 📈 🇰🇷 마감 시황 · {today} {weekday_today}요일

{verify0}

---

### 1. 🇰🇷 한국 시장 마감 결과
한 문장 핵심 (최대 2문장). 숫자는 아래에.

**주요 지수** (종가 / 등락률 / 거래량)
- KOSPI …
- KOSDAQ …
- 삼성전자 …
- SK하이닉스 …

**강세/약세 포인트** (제공 섹터·대형주만, 각 1~2개)
📰 관련 뉴스: (일치할 때만) [제목] → [연결 지표]

---

### 2. 업종·특징주
3~5줄. 수치 재나열 금지. 왜/의미·수급 함의. 없는 종목 창작 금지.

---

{psych}

---

{outlook}

---

### 💡 한 줄 요약
[가장 중요한 수치] 때문에 {next_trading_label} 한국장은 [핵심 포인트] 주목.

SIGNAL:BULL 또는 SIGNAL:NEUTRAL 또는 SIGNAL:BEAR"""

    elif brief_type == "us_close":
        verify0 = _verify_block(
            bench="SPY",
            result_metrics="SPY ▲/▼X.XX%, QQQ ▲/▼X.XX% (제공 수치)",
            mode="score",
            cite=_load_brief_cite("us_premarket")[1],
            extra="검증 대상은 오늘 us_premarket의 '오늘 미국장' 전망이다.",
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
{ENGINE_STRUCTURE_RULE}
{BREADTH_RULE}
{BRIEF_STYLE_RULE}
{NEWS_RULE}
{timing_context}

[이 리포트 특성 — 미국 마감]
- 오늘 미국 마감 스냅샷 → 의미 → {next_trading_label} 한국 전망
- SPY vs RSP 시장 폭 해석 필수(갭 있을 때)
- [교차시장 순환 주입]의 us_premarket·kr_close SIGNAL·수치를 다음 한국장 전망에 연결

[제공 데이터]
{data_text}

{featured_text}
{tomorrow_events}
(내일 실적발표 종목이 있으면 해당 섹터 영향을 전망에 반영할 것)

[최근 뉴스]
{news_text}

{prev_context}
## 📈 마감 시황 · {today} {weekday_today}요일

{verify0}

---

### 1. 🇺🇸 미국 시장 마감 결과
한 문장 핵심 (최대 2문장). 숫자는 아래에.

**주요 지수** (종가 / 등락률 / 거래량)
- S&P500 (SPY) …
- S&P 동일가중 (RSP) …
- NASDAQ (QQQ) …
- DOW (DIA) …
- 러셀2000 (IWM) …

**시장 폭**: SPY vs RSP 갭 X.XXp — 한 줄
**강세 섹터** (1~2개, 종가/등락률/거래량)
**약세 섹터** (1~2개, 종가/등락률/거래량)
**시장 심리**: VIX · Fear & Greed(제공 시)
📰 관련 뉴스: (일치할 때만) [제목] → [연결 지표]

---

### 2. 업종·특징주
3~5줄. 수치 재나열 금지. 왜/의미·한국(삼성·하이닉스·반도체) 함의. 없는 종목 창작 금지.

---

{psych}

---

{outlook}

---

### 💡 한 줄 요약
오늘 [가장 중요한 수치] 때문에 {next_trading_label} 한국장은 [핵심 포인트] 주목.

SIGNAL:BULL 또는 SIGNAL:NEUTRAL 또는 SIGNAL:BEAR"""

    else:  # kr_premarket
        verify0 = _verify_block(
            bench="—",
            result_metrics="해당 없음 (직전 us_close 전망 대상=다음/오늘 한국장, 아직 장전)",
            mode="defer",
            cite=_load_brief_cite("us_close")[1],
            extra=(
                "직전 미국 마감의 한국장 전망을 인용만 하고 판정은 보류. "
                "실제 채점은 오늘 kr_close에서 KOSPI로 한다. "
                "간밤 미국 수치는 스냅샷 섹션에서 다룰 것."
            ),
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
{ENGINE_STRUCTURE_RULE}
{BREADTH_RULE}
{BRIEF_STYLE_RULE}
{NEWS_RULE}
{timing_context}

[이 리포트 특성 — 한국 장전]
- 간밤 미국 마감이 오늘 한국장 입력 변수. 미국→한국 경로로 전망
- [교차시장 순환 주입]의 us_close SIGNAL·핵심 수치를 오늘 한국장 전망에 반드시 연결
- 삼성·하이닉스·SMH 연결을 우선
- 직전 us_close의 한국장 전망은 ###0에서 검증 보류 (채점은 오늘 kr_close)

[제공 데이터]
{data_text}

{featured_text}
{tomorrow_events}
(내일 실적발표 종목이 있으면 해당 섹터 영향을 전망에 반영할 것)

[최근 뉴스]
{news_text}

{prev_context}

## 📊 🇰🇷 한국장 전 시황 · {today} {weekday_today}요일

{verify0}

---

### 1. 🌙 간밤 미국 시장 결과
한 문장 핵심 (최대 2문장). 숫자는 아래에.

**주요 지수** (종가 / 등락률 / 거래량)
- S&P500 (SPY) …
- S&P 동일가중 (RSP) …
- NASDAQ (QQQ) …
- DOW (DIA) …
- 러셀2000 (IWM) …
- 반도체 (SMH) …

**시장 폭**: SPY vs RSP — 한 줄
**강세/약세 섹터** (각 1~2개)
📰 관련 뉴스: (일치할 때만) [제목] → [연결 지표]

---

### 2. 업종·특징주
3~5줄. 수치 재나열 금지. 왜/의미·오늘 한국(삼성·하이닉스) 함의. 없는 종목 창작 금지.

---

{psych}

---

{outlook}

---

### 💡 한 줄 요약
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
        "fear_greed":  fear_greed,
        "analysis":    analysis_clean,
        "signal":      signal,
        "created_at":  created,
    }
