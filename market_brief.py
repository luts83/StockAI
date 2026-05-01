import re
import anthropic
import yfinance as yf
from datetime import datetime
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

            print(
                f"[market_brief] ✅ {ticker} {date_label} "
                f"${current:.2f} ({change_pct:+.2f}%) vol {vol_ratio}%"
            )

            return {
                "name":         name,
                "price":        round(current, 2),
                "change_pct":   round(change_pct, 2),
                "volume":       volume,
                "avg_volume":   avg_volume,
                "volume_ratio": vol_ratio,
                "last_date":    date_label,
            }
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
            lines.append(f"\n### {region} — ⚠️ 데이터 수집 실패")
            continue
        lines.append(f"\n### {region}")
        for ticker, d in tickers.items():
            arrow = "▲" if d["change_pct"] > 0 else "▼"
            lines.append(
                f"- {d['name']}({ticker}) [데이터일: {d['last_date']}]: "
                f"${d['price']} "
                f"{arrow}{abs(d['change_pct'])}% "
                f"(거래량 평균 대비 {d['volume_ratio']}%)"
            )
    return "\n".join(lines)


def _build_prev_context(recent_briefs: list) -> str:
    if not recent_briefs:
        return ""
    prev = recent_briefs[0]
    prev_type = "장전" if prev.get("type") == "premarket" else "마감"
    analysis = prev.get("analysis", "")

    # 전망 섹션만 추출
    forecast_match = re.search(
        r"(###\s*\d+\.\s*🔮[^\n]*|###\s*\d+\.\s*(내일|오늘)\s*[^\n]*)[\s\S]*?(?=###|\Z)",
        analysis,
    )
    forecast_text = (
        forecast_match.group(0).strip()
        if forecast_match
        else analysis[:400]
    )

    return f"""
[직전 시황 — {prev.get('date')} {prev_type} / SIGNAL:{prev.get('signal')}]
{forecast_text}
[직전 시황 끝 — 이 전망을 기반으로 ### 0. 직전 전망 검증 작성]
"""


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

    data_text = _build_data_text(market_data)
    recent = get_recent_market_briefs(limit=2)
    prev_context = _build_prev_context(recent)

    # 현재 시각 컨텍스트
    timing_context = f"""
[현재 시각 정보 — 반드시 확인]
- 영국 시각: {now.strftime('%Y-%m-%d %H:%M')} ({weekday_today}요일, Europe/London)
- 시황 종류: {'장전 시황' if brief_type == 'premarket' else '마감 시황'}
- 데이터의 [데이터일] 표시를 반드시 확인하여 날짜/요일 오류 방지
- 데이터일이 금요일이면 → 주말을 지나 월요일 시황에서 사용되는 데이터
- 절대 데이터에 없는 날짜나 요일을 추측해서 쓰지 말 것
"""

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
S&P500  ▲X.XX%  거래량 XXX%
NASDAQ  ▲X.XX%  거래량 XXX%
DOW     ▲X.XX%  거래량 XXX%
VIX     XX.XX   ▼XX%

뉴스 연결 (데이터 방향과 일치할 때만):
"[뉴스] XX 이슈가 위 흐름의 배경으로 보입니다."

---

### 2. 🇰🇷 내일 한국 시장 전망
오늘 미국 마감 결과가 내일 한국장에 어떤 영향을 줄지 먼저 서술:

(서술 예시)
"미국 증시 강세가 내일 한국 시장에도 긍정적으로 작용할 것으로 보입니다.
특히 나스닥 상승은 코스닥 기술주에 동조 상승 기대를 높이며..."

**결론: 강세 우위 / 약세 우위 / 중립** (반드시 방향 제시)
강세 조건: 구체적 수치 조건
약세 조건: 구체적 수치 조건
신뢰도: 상/중/하
핵심 체크: 내일 한국장에서 봐야 할 것 1개

---

### 3. 📊 시장 심리
수치 나열 말고 내일 한국장과의 연관성 서술:

- VIX XX ▼XX% → 공포 완화 의미 + 내일 영향 한 줄
- 달러 XX ▼XX% → 원화 강세 의미 + 외국인 영향 한 줄
- 금리 XX% ▼XX% → 성장주 밸류에이션 의미 + 코스닥 영향 한 줄

---

### 4. 💡 한 줄 요약
독자가 자기 전에 딱 한 문장만 읽는다면:
"XXX 덕분에 / 때문에 내일 한국장은 XXX에 주목하세요."

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

    return {
        "type":        brief_type,
        "date":        today,
        "market_data": market_data,
        "analysis":    analysis_clean,
        "signal":      signal,
        "created_at":  now.isoformat(),
    }
