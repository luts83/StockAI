import re
import anthropic
import yfinance as yf
from datetime import datetime, timedelta
import pytz
from news import fetch_macro_news, format_macro_news_for_brief

TICKERS = {
    "미국": {
        "SPY":  "S&P 500",
        "QQQ":  "NASDAQ 100",
        "DIA":  "DOW Jones",
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
    # 금/유가/선물/러셀 등은 제거 — 시황 복잡도만 높이고 핵심 아님
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

WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]


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


def _fetch_ticker(ticker: str, name: str) -> dict | None:
    """티커별 데이터 수집 — 날짜/타임존 정규화 + 장중 데이터 제외 + 재시도 3회"""
    import time
    from datetime import timezone

    for attempt in range(3):
        try:
            # 한국 지수는 period를 더 넉넉하게 (거래일 확보)
            period = "15d" if ticker.startswith("^K") else "10d"
            hist = yf.Ticker(ticker).history(period=period)

            if hist is None or hist.empty:
                time.sleep(2 ** attempt)
                continue

            # 타임존 정규화 (naive → UTC)
            if hist.index.tz is None:
                hist.index = hist.index.tz_localize("UTC")
            hist.index = hist.index.tz_convert("UTC")

            now_utc = datetime.now(timezone.utc)
            today_utc = now_utc.date()

            # 마감 확정 시각 판단 (장중 불완전 데이터 제외용)
            # 한국 지수: KST 15:30 마감 = UTC 06:30 → UTC 07:00 이후 마감 확정
            # 미국 지수: ET 16:00 마감 = UTC 20:00~21:00 → UTC 21:00 이후 마감 확정
            if ticker.startswith("^K"):
                market_closed = now_utc.hour >= 7
            else:
                market_closed = now_utc.hour >= 21

            last_dt = hist.index[-1].date()
            if last_dt == today_utc and not market_closed:
                hist = hist.iloc[:-1]  # 오늘 장중 불완전 데이터 제외
                if len(hist) < 2:
                    time.sleep(2 ** attempt)
                    continue

            if len(hist) < 2:
                time.sleep(2 ** attempt)
                continue

            prev_close = float(hist["Close"].iloc[-2])
            current    = float(hist["Close"].iloc[-1])
            last_date  = hist.index[-1]

            if hasattr(last_date, "date"):
                ld = last_date.date()
            else:
                ld = last_date.to_pydatetime().date()
            weekday_str = WEEKDAY_KR[ld.weekday()]
            date_label  = f"{ld.strftime('%Y-%m-%d')}({weekday_str})"

            change_pct = (current - prev_close) / prev_close * 100 if prev_close else 0
            volume     = int(hist["Volume"].iloc[-1]) if hist["Volume"].iloc[-1] else 0
            avg_volume = int(hist["Volume"].mean()) if hist["Volume"].mean() else 0
            vol_ratio  = round(volume / avg_volume * 100, 1) if avg_volume else 0

            days_old = (today_utc - ld).days
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


def get_market_data() -> dict:
    result = {}
    for region, tickers in TICKERS.items():
        result[region] = {}
        for ticker, name in tickers.items():
            d = _fetch_ticker(ticker, name)
            if d:
                result[region][ticker] = d
    total = sum(len(v) for v in result.values())
    print(f"[market_brief] 총 {total}개 지수 수집 완료")
    return result


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


def _build_prev_context(recent_briefs: list, current_type: str = "") -> str:
    if not recent_briefs:
        return ""

    # 시황 타입별 참조 우선순위 (마감→장전 검증 등 올바른 대상 선택)
    priority = {
        "premarket":   ["korea_close", "close"],
        "close":       ["premarket", "korea_close"],
        "korea_close": ["close", "premarket"],
    }
    order    = priority.get(current_type, [])
    type_map = {b.get("type"): b for b in recent_briefs}

    TYPE_LABEL = {
        "premarket":   "미국 장전",
        "close":       "미국 마감",
        "korea_close": "한국 마감",
    }

    sections = []
    for t in order:
        brief = type_map.get(t)
        if not brief:
            continue
        analysis = brief.get("analysis", "")
        forecast_text = _extract_forecast(analysis)
        summary_match = re.search(r"###\s*\d+\.\s*💡[^\n]*\n([^\n#]+)", analysis)
        summary_text  = summary_match.group(1).strip() if summary_match else ""

        label = TYPE_LABEL.get(t, t)
        sections.append(
            f"\n[직전 시황 — {brief.get('date')} {label} / SIGNAL:{brief.get('signal')}]\n"
            f"한 줄 요약: {summary_text}\n"
            f"전망: {forecast_text}\n"
            f"[직전 시황 끝]"
        )

    if not sections:
        # 우선순위에 맞는 타입이 없으면 최신 시황으로 fallback
        prev  = recent_briefs[0]
        label = TYPE_LABEL.get(prev.get("type", ""), prev.get("type", ""))
        return (
            f"\n[직전 시황 — {prev.get('date')} {label} / SIGNAL:{prev.get('signal')}]\n"
            f"전망: {_extract_forecast(prev.get('analysis', ''))}\n"
            f"[직전 시황 끝]\n"
            f"[위 전망을 ### 0. 직전 전망 검증에서 반드시 구체적으로 인용할 것]\n"
        )

    return (
        "\n".join(sections)
        + "\n[위 직전 시황의 전망을 ### 0. 직전 전망 검증에서 반드시 한 줄 인용할 것]\n"
    )


async def generate_market_brief(brief_type: str) -> dict:
    from database import get_recent_market_briefs

    market_data = get_market_data()
    macro_news = fetch_macro_news(max_per_source=3)
    news_text = format_macro_news_for_brief(macro_news)

    if not _has_minimum_data(market_data):
        raise RuntimeError("yfinance에서 핵심 지수 데이터를 가져오지 못했습니다")

    bst = pytz.timezone("Europe/London")  # BST/GMT 자동 처리
    now = datetime.now(bst)
    today = now.strftime("%Y-%m-%d")
    weekday_today = WEEKDAY_KR[now.weekday()]

    # 수집된 데이터의 실제 날짜로 today 보정
    # BST 자정 이후 생성 시 제목 날짜와 yfinance 데이터 날짜 불일치 방지
    all_dates = []
    for region_data in market_data.values():
        for d in region_data.values():
            if not d.get("stale") and d.get("last_date"):
                date_str = d["last_date"].split("(")[0]
                all_dates.append(date_str)
    if all_dates:
        from collections import Counter
        from datetime import datetime as _dt
        data_today = Counter(all_dates).most_common(1)[0][0]
        if data_today != today:
            print(f"[market_brief] 날짜 보정: BST {today} → 데이터 {data_today}")
            today = data_today
            _d = _dt.strptime(today, "%Y-%m-%d")
            weekday_today = WEEKDAY_KR[_d.weekday()]

    recent = get_recent_market_briefs(limit=6)

    # 한국 지수 stale 여부 확인 → korea_close 브리프 데이터로 대체
    kr_data = market_data.get("한국", {})
    kr_stale = not kr_data or any(
        v.get("stale") or not v.get("price") for v in kr_data.values()
    )
    if kr_stale:
        korea_brief = next(
            (b for b in recent if b.get("type") == "korea_close"),
            None
        )
        if korea_brief and korea_brief.get("market_data", {}).get("한국"):
            market_data["한국"] = korea_brief["market_data"]["한국"]
            print(
                f"[market_brief] 한국 지수 stale → korea_close 브리프 데이터로 대체 "
                f"({korea_brief['date']})"
            )
        else:
            print("[market_brief] 한국 지수 stale + korea_close 브리프 없음 → 데이터 없음 처리")
            market_data["한국"] = {}

    data_text = _build_data_text(market_data)
    prev_context = _build_prev_context(recent, brief_type)

    try:
        tomorrow_events = _get_tomorrow_events(now)
    except Exception as e:
        print(f"[market_brief] 내일 일정 수집 실패: {e}")
        tomorrow_events = ""

    # 직전 브리프 적중률 저장 (현재 시장 데이터로 이전 전망 검증)
    if recent and len(recent) > 0:
        prev = recent[0]
        prev_signal = prev.get("signal", "")
        prev_id = str(prev.get("_id", ""))
        actual_signal = ""

        if brief_type == "close":
            spy = market_data.get("미국", {}).get("SPY", {})
            if spy and not spy.get("stale"):
                actual_signal = "BULL" if spy.get("change_pct", 0) > 0 else "BEAR"
        elif brief_type in ("korea_close", "premarket"):
            kospi = market_data.get("한국", {}).get("^KS11", {})
            if kospi and not kospi.get("stale"):
                actual_signal = "BULL" if kospi.get("change_pct", 0) > 0 else "BEAR"

        if actual_signal and prev_signal:
            is_correct = (
                (prev_signal == "BULL" and actual_signal == "BULL") or
                (prev_signal == "BEAR" and actual_signal == "BEAR") or
                (prev_signal == "NEUTRAL" and actual_signal == "")
            )
            try:
                from database import save_brief_performance
                save_brief_performance(
                    brief_id=prev_id,
                    predicted=prev_signal,
                    actual=actual_signal,
                    is_correct=is_correct,
                    brief_type=prev.get("type", ""),
                )
                print(f"[market_brief] 적중률 저장: {prev_signal}→{actual_signal} {'✅' if is_correct else '❌'}")
            except Exception as e:
                print(f"[market_brief] 적중률 저장 실패: {e}")

    next_trading_day = _get_next_trading_day(now)
    next_trading_label = (
        f"다음 거래일 ({next_trading_day})"
        if now.weekday() == 4   # 금요일에만
        else "내일"
    )

    # 미국 마감 시황일 때 korea_close 브리프 한 줄 요약을 별도 컨텍스트로 추출
    kr_close_context = ""
    if brief_type == "close":
        korea_brief = next(
            (b for b in recent if b.get("type") == "korea_close"),
            None
        )
        if korea_brief:
            summary_match = re.search(
                r"###\s*\d+\.\s*💡[^\n]*\n([^\n#]+)",
                korea_brief.get("analysis", "")
            )
            kr_one_line = summary_match.group(1).strip() if summary_match else ""
            kr_close_context = f"""
[오늘 한국 장 마감 결과 — korea_close 브리프 {korea_brief['date']} 기준]
{kr_one_line}
SIGNAL: {korea_brief.get('signal', 'NEUTRAL')}
(위 내용을 "{next_trading_label} 한국 시장 전망" 섹션 작성 시 참고할 것)
"""

    # 적중률 자기보정 컨텍스트
    from database import get_brief_accuracy
    accuracy = get_brief_accuracy(limit=20)
    accuracy_context = ""
    if accuracy["total"] >= 3:
        error_text = (
            ", ".join(accuracy["recent_errors"])
            if accuracy["recent_errors"]
            else "없음"
        )
        accuracy_context = f"""
[최근 시황 적중률 — 자기보정 참고]
- 최근 {accuracy['total']}회 중 {accuracy['correct']}회 적중 ({accuracy['accuracy_pct']}%)
- 반복 오류 패턴: {error_text}
- 위 오류 패턴이 있으면 이번 전망에서 반대 방향 가중치를 높일 것
- BULL 전망이 반복 빗나갔으면 → 이번엔 BEAR 또는 NEUTRAL 검토
- BEAR 전망이 반복 빗나갔으면 → 이번엔 BULL 또는 NEUTRAL 검토
"""

    # 현재 시각 컨텍스트
    timing_context = f"""
[현재 시각 정보 — 반드시 확인]
- 영국 시각: {now.strftime('%Y-%m-%d %H:%M')} ({weekday_today}요일, Europe/London)
- 시황 종류: {'장전 시황' if brief_type == 'premarket' else '마감 시황'}
- 다음 거래일: {next_trading_day}
- '내일', '다음날' 표현 대신 반드시 '{next_trading_label}' 형식으로 명시
- 금요일 마감 시황: '내일' 표현 절대 금지

[데이터 신뢰성 원칙]
- [데이터일] 이 오늘({today})과 다른 항목은 ⚠️ 표시되어 있음
- ⚠️ stale 데이터는 해당 시장의 당일 결과 서술에 절대 사용 금지
- stale 데이터가 있는 시장은 반드시 아래 문구로 명시:
  "오늘 [시장명] 데이터 미수집 — 전망에서 제외합니다"
- 데이터에 없는 날짜·요일·수치 추측 금지
"""
    timing_context += accuracy_context

    if brief_type == "premarket":
        prompt = f"""오늘 {today}({weekday_today}) 장전 시황을 아래 데이터만 사용해서 작성해줘.

{STRICT_RULE}
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

    elif brief_type == "korea_close":
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

    else:  # closing
        prompt = f"""오늘 {today}({weekday_today}) 마감 시황을 아래 데이터만 사용해서 작성해줘.

{STRICT_RULE}
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
서술 (2~3문장): 오늘 미국 증시가 어떻게 마감했는지, 왜 그랬는지 자연스럽게 서술.

S&P500 ▲/▼X.XX%  거래량 XXX%
NASDAQ ▲/▼X.XX%  거래량 XXX%
DOW    ▲/▼X.XX%  거래량 XXX%
VIX    XX.XX     ▲/▼X.XX%
📰 관련 뉴스: (있을 때만 1줄)

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

    return {
        "type":        brief_type,
        "date":        today,
        "market_data": market_data,
        "analysis":    analysis_clean,
        "signal":      signal,
        "created_at":  now.isoformat(),
    }
