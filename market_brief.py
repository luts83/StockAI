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
    "시장심리": {
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

WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]


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
    """티커별 데이터 수집 — 날짜 포함 + 재시도 3회"""
    import time

    for attempt in range(3):
        try:
            hist = yf.Ticker(ticker).history(period="10d")
            if hist is None or hist.empty or len(hist) < 2:
                time.sleep(2 ** attempt)
                continue

            # 오늘 장중 불완전 데이터 제외
            from datetime import timezone
            now_utc = datetime.now(timezone.utc)
            last_dt  = hist.index[-1]
            if hasattr(last_dt, "date"):
                last_date_val = last_dt.date()
            else:
                last_date_val = last_dt.to_pydatetime().date()

            today_utc = now_utc.date()
            et_hour   = now_utc.hour - 4  # ET 근사
            us_market_open = 9 <= et_hour < 16

            if last_date_val == today_utc and us_market_open:
                hist = hist.iloc[:-1]  # 장중 불완전 데이터 제외
                if len(hist) < 2:
                    continue

            prev_close = float(hist["Close"].iloc[-2])
            current    = float(hist["Close"].iloc[-1])
            last_date  = hist.index[-1]

            # 날짜 + 요일 계산
            if hasattr(last_date, "date"):
                ld = last_date.date()
            else:
                ld = last_date.to_pydatetime().date()
            weekday_str = WEEKDAY_KR[ld.weekday()]
            date_label  = f"{ld.strftime('%Y-%m-%d')}({weekday_str})"

            change_pct = (current - prev_close) / prev_close * 100
            volume     = int(hist["Volume"].iloc[-1])
            avg_volume = int(hist["Volume"].mean()) if hist["Volume"].mean() else 0
            vol_ratio  = round(volume / avg_volume * 100, 1) if avg_volume else 0

            today_utc2 = datetime.now(timezone.utc).date()
            days_old = (today_utc2 - ld).days
            stale = days_old > 1

            print(
                f"[market_brief] {'⚠️ STALE' if stale else '✅'} {ticker} {date_label} "
                f"${current:.2f} ({change_pct:+.2f}%) vol {vol_ratio}%"
                + (f" — {days_old}일 지연" if stale else "")
            )

            result = {
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

            if stale:
                print(f"[market_brief] ⚠️ {ticker} 데이터 {days_old}일 지연 — stale 처리")

            return result
        except Exception as e:
            print(f"[market_brief] ❌ {ticker} 오류 (시도 {attempt+1}): {e}")
            time.sleep(2 ** attempt)
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


def _build_prev_context(recent_briefs: list, current_type: str = "") -> str:
    if not recent_briefs:
        return ""

    # 시황 타입별 참조 우선순위
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

        forecast_match = re.search(
            r"(###\s*\d+\.\s*🔮[^\n]*|###\s*\d+\.\s*(내일|오늘|한국)[^\n]*)[\s\S]*?(?=###|\Z)",
            analysis,
        )
        summary_match = re.search(
            r"###\s*\d+\.\s*💡[^\n]*\n([^\n#]+)",
            analysis,
        )
        forecast_text = forecast_match.group(0).strip() if forecast_match else analysis[:300]
        summary_text  = summary_match.group(1).strip() if summary_match else ""

        label = TYPE_LABEL.get(t, t)
        sections.append(
            f"\n[참조 시황 — {brief.get('date')} {label} / SIGNAL:{brief.get('signal')}]\n"
            f"한 줄 요약: {summary_text}\n"
            f"{forecast_text}\n"
            f"[참조 끝]"
        )

    if not sections:
        # 우선순위에 맞는 타입이 없으면 최신 시황으로 fallback
        prev      = recent_briefs[0]
        label     = TYPE_LABEL.get(prev.get("type", ""), prev.get("type", ""))
        analysis  = prev.get("analysis", "")
        forecast_match = re.search(
            r"(###\s*\d+\.\s*🔮[^\n]*|###\s*\d+\.\s*(내일|오늘|한국)[^\n]*)[\s\S]*?(?=###|\Z)",
            analysis,
        )
        forecast_text = forecast_match.group(0).strip() if forecast_match else analysis[:400]
        return (
            f"\n[직전 시황 — {prev.get('date')} {label} / SIGNAL:{prev.get('signal')}]\n"
            f"{forecast_text}\n"
            f"[직전 시황 끝 — 이 전망을 기반으로 ### 0. 직전 전망 검증 작성]\n"
        )

    return "\n".join(sections) + "\n[위 참조 시황의 전망을 기반으로 ### 0. 직전 전망 검증 작성]\n"


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
    kr_stale = any(v.get("stale") for v in kr_data.values())
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

    next_trading_day = _get_next_trading_day(now)
    next_trading_label = (
        f"다음 거래일 ({next_trading_day})"
        if now.weekday() == 4   # 금요일에만
        else "내일"
    )

    # 적중률 자기보정 컨텍스트
    from database import get_brief_accuracy
    accuracy = get_brief_accuracy(limit=20)
    accuracy_context = ""
    if accuracy["total"] >= 3:
        accuracy_context = f"""
[최근 시황 적중률 — 자기보정 참고용]
- 최근 {accuracy['total']}회 중 {accuracy['correct']}회 적중 ({accuracy['accuracy_pct']}%)
- 반복 오류 패턴: {', '.join(accuracy['recent_errors']) if accuracy['recent_errors'] else '없음'}
- 위 오류 패턴이 있으면 이번 전망에서 반대 방향 가중치를 높일 것
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
{NEWS_RULE}
{timing_context}

[제공 데이터]
{data_text}

[최근 24시간 매크로 뉴스]
{news_text}

{prev_context}

## 📊 장전 시황 ({today} {weekday_today}요일)

### 0. 직전 전망 검증
(직전 시황 전망 있을 때만. 없으면 생략)
- ✅ 적중 또는 ❌ 빗나감 — 전망 vs 실제 결과 한 줄
- 원인: 데이터+뉴스 기반 원인 1~2개 (수치 포함)
- 교훈: 다음 분석에 반영할 점 한 줄

---

### 1. 🇰🇷 한국 시장 마감 결과
[데이터일] 기준 한국 시장 결과를 자연스러운 문장으로 먼저 설명한 뒤
아래 형식으로 수치 정리:

(서술 예시)
"오늘 코스피는 보합 마감했습니다. 미국발 기술주 약세 여파로 외국인 매도세가 이어졌으며..."
KOSPI  ▲X.XX%  거래량 XXX%
KOSDAQ ▼X.XX%  거래량 XXX%

---

### 2. 🇺🇸 미국 장전 현재 상황
[데이터일] 기준 미국 최근 마감 결과를 자연스러운 문장으로 설명한 뒤 수치 정리:

(서술 예시)
"간밤 미국 증시는 빅테크 실적 발표를 앞두고 차익실현 매물이 쏟아지며 하락 마감했습니다.
특히 NASDAQ은 금리 상승 부담으로..."
NASDAQ  ▲X.XX%  거래량 XXX%
S&P500  ▼X.XX%  거래량 XXX%
DOW     ▼X.XX%  거래량 XXX%

뉴스가 있으면 한 줄 연결:
"[뉴스] XX 이슈가 위 흐름과 연관됩니다."
(뉴스 제목만으로 내용 추측 금지. 데이터와 방향이 일치할 때만 언급)

---

### 3. 📊 시장 심리
수치만 나열하지 말고 내일 한국장과의 연관성을 한 줄씩 서술:

- VIX XX → (공포/중립/탐욕) — 내일 한국장에 미치는 영향 한 줄
- 달러 XX ▲/▼XX% → 원화/외국인 영향 한 줄
- 금리 XX% ▲/▼XX% → 성장주/가치주 영향 한 줄

---

### 4. 🔮 오늘 미국 장 전망
결론을 먼저 한 문장으로:
"오늘 미국 장은 XX 가능성이 높습니다. XX 때문입니다."

**결론: 강세 우위 / 약세 우위 / 중립** (반드시 방향 제시)
강세 조건: 구체적 수치 조건
약세 조건: 구체적 수치 조건
신뢰도: 상/중/하
핵심 체크: 오늘 봐야 할 것 1개

---

### 5. 💡 한 줄 요약
독자가 출근길에 딱 한 문장만 읽는다면 뭘 알아야 하는지:
"XXX 때문에 오늘 XXX에 주목하세요."

SIGNAL:BULL 또는 SIGNAL:NEUTRAL 또는 SIGNAL:BEAR"""

    elif brief_type == "korea_close":
        prompt = f"""오늘 {today}({weekday_today}) 한국 장 마감 시황을 아래 데이터만 사용해서 작성해줘.

{STRICT_RULE}
{NEWS_RULE}
{timing_context}

[제공 데이터]
{data_text}

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
{NEWS_RULE}
{timing_context}

[제공 데이터]
{data_text}

[최근 24시간 매크로 뉴스]
{news_text}

{prev_context}

## 📈 마감 시황 ({today} {weekday_today}요일)

### 0. 직전 전망 검증
(오늘 장전 전망 있을 때만. 없으면 생략)
- ✅ 적중 또는 ❌ 빗나감 — 장전 전망 vs 실제 마감 결과
- 원인: 데이터+뉴스 기반 원인 수치 포함
- 교훈: 다음 분석에 반영할 점 한 줄

---

### 1. 🇺🇸 미국 시장 마감 결과
오늘 미국 증시 흐름을 자연스러운 문장으로 먼저 서술한 뒤 수치 정리:

(서술 예시)
"오늘 미국 증시는 연준 금리 동결 발표와 빅테크 실적 호조가 겹치며 강하게 반등했습니다.
특히 다우존스가 1.63% 급등하며 상승을 주도했는데, 캐터필러와 알파벳의 어닝 서프라이즈가..."
S&P500  $X,XXX.XX  ▲/▼X.XX%  거래량 XXX%
NASDAQ  $X,XXX.XX  ▲/▼X.XX%  거래량 XXX%
DOW     $X,XXX.XX  ▲/▼X.XX%  거래량 XXX%
VIX     XX.XX      ▲/▼XX%

뉴스 연결 (데이터 방향과 일치할 때만):
"[뉴스] XX 이슈가 위 흐름의 배경으로 보입니다."

---

### 2. 🇰🇷 {next_trading_label} 한국 시장 전망
오늘 미국 마감 결과가 {next_trading_label} 한국장에 어떤 영향을 줄지 먼저 서술:

(서술 예시)
"미국 증시 강세가 내일 한국 시장에도 긍정적으로 작용할 것으로 보입니다.
특히 나스닥 상승은 코스닥 기술주에 동조 상승 기대를 높이며..."

**결론: 강세 우위 / 약세 우위 / 중립** (반드시 방향 제시)
강세 조건: 구체적 수치 조건
약세 조건: 구체적 수치 조건
신뢰도: 상/중/하
핵심 체크: {next_trading_label} 한국장에서 봐야 할 것 1개

---

### 3. 📊 시장 심리
수치 나열 말고 {next_trading_label} 한국장과의 연관성 서술:

- VIX XX ▼XX% → 공포 완화 의미 + {next_trading_label} 영향 한 줄
- 달러 XX ▼XX% → 원화 강세 의미 + 외국인 영향 한 줄
- 금리 XX% ▼XX% → 성장주 밸류에이션 의미 + 코스닥 영향 한 줄

---

### 4. 💡 한 줄 요약
독자가 자기 전에 딱 한 문장만 읽는다면:
"XXX 덕분에 / 때문에 {next_trading_label} 한국장은 XXX에 주목하세요."

SIGNAL:BULL 또는 SIGNAL:NEUTRAL 또는 SIGNAL:BEAR"""

    client = anthropic.Anthropic()
    message = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=2500,
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
