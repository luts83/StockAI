import re
import anthropic
import yfinance as yf
from datetime import datetime
import pytz

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

    if not _has_minimum_data(market_data):
        raise RuntimeError("yfinance에서 핵심 지수 데이터를 가져오지 못했습니다")

    kst = pytz.timezone("Asia/Seoul")
    now = datetime.now(kst)
    today = now.strftime("%Y-%m-%d")
    weekday_today = WEEKDAY_KR[now.weekday()]

    data_text = _build_data_text(market_data)
    recent = get_recent_market_briefs(limit=2)
    prev_context = _build_prev_context(recent)

    # 현재 시각 컨텍스트
    timing_context = f"""
[현재 시각 정보 — 반드시 확인]
- 한국 시각: {now.strftime('%Y-%m-%d %H:%M')} ({weekday_today}요일)
- 시황 종류: {'장전 시황' if brief_type == 'premarket' else '마감 시황'}
- 데이터의 [데이터일] 표시를 반드시 확인하여 날짜/요일 오류 방지
- 데이터일이 금요일이면 → 주말을 지나 월요일 시황에서 사용되는 데이터
- 절대 데이터에 없는 날짜나 요일을 추측해서 쓰지 말 것
"""

    if brief_type == "premarket":
        prompt = f"""오늘 {today}({weekday_today}) 장전 시황을 아래 데이터만 사용해서 분석해줘.

{STRICT_RULE}
{timing_context}

[제공 데이터 — 이것만 사용할 것]
{data_text}

{prev_context}

## 📊 장전 시황 ({today} {weekday_today}요일)

### 0. 직전 전망 검증
(직전 시황의 전망이 있을 때만 작성, 없으면 생략)
- ✅ 적중 또는 ❌ 빗나감 — 전망 한 줄 vs 실제 결과 한 줄
- 원인: 데이터에서 읽히는 원인 수치 포함 1~2개
- 교훈: 다음 분석에 반영할 점 한 줄

---

### 1. 🇰🇷 한국 시장 마감 결과
[데이터일] 기준 가장 최근 한국 시장 결과
- KOSPI / KOSDAQ 등락률 + 거래량 비율
- 한 줄 특징

---

### 2. 🇺🇸 미국 장전 현재 상황
[데이터일] 기준 가장 최근 미국 마감 결과
- 각 지수 등락률 + 거래량 비율
- 오늘 미국 장 시작 전 주목할 변수
  (데이터로 확인 가능한 것만, 없으면 "추가 데이터 없음")

---

### 3. 📊 시장 심리
- VIX: 수치 → 공포(20↑)/중립/탐욕(15↓) + 한 줄 해석
- 달러: 방향 + 영향 한 줄
- 금리: 수치 + 성장주 영향 한 줄
(수집 안 된 항목은 "데이터 없음"으로 표기)

---

### 4. 🔮 오늘 미국 장 전망
**결론: 강세 우위 / 약세 우위 / 중립** (반드시 방향 제시)
- 강세 근거: 데이터 기반 1줄
- 약세 근거: 데이터 기반 1줄
- 신뢰도: 상/중/하 + 근거 한 줄
- 핵심 체크포인트: 오늘 봐야 할 것 1개

---

### 5. 💡 한 줄 요약
가장 중요한 수치 1개 기반으로 딱 한 문장

SIGNAL:BULL 또는 SIGNAL:NEUTRAL 또는 SIGNAL:BEAR"""

    else:  # closing
        prompt = f"""오늘 {today}({weekday_today}) 마감 시황을 아래 데이터만 사용해서 분석해줘.

{STRICT_RULE}
{timing_context}

[제공 데이터 — 이것만 사용할 것]
{data_text}

{prev_context}

## 📈 마감 시황 ({today} {weekday_today}요일)

### 0. 직전 전망 검증
(오늘 장전 시황의 전망이 있을 때만 작성, 없으면 생략)
- ✅ 적중 또는 ❌ 빗나감 — 장전 전망 vs 실제 마감 결과
- 원인: 데이터에서 읽히는 원인 수치 포함
- 교훈: 다음 분석에 반영할 점 한 줄

---

### 1. 🇺🇸 미국 시장 마감 결과
[데이터일] 기준 오늘 미국 마감 결과
- 각 지수 등락률 + 거래량 비율
- 오늘 장의 핵심 흐름 한 줄

---

### 2. 🇰🇷 내일 한국 시장 전망
오늘 미국 마감 → 내일 한국장 예측
**결론: 강세 우위 / 약세 우위 / 중립** (반드시 방향 제시)
- 미국→한국 영향 경로: 구체적으로
  예) NASDAQ ▲1.9% → KOSDAQ 기술주 동조 예상
- 강세 근거: 데이터 기반 1줄
- 약세 근거: 데이터 기반 1줄
- 신뢰도: 상/중/하 + 근거 한 줄
- 핵심 체크포인트: 내일 한국장에서 봐야 할 것 1개

---

### 3. 📊 시장 심리
- VIX: 수치 → 내일 한국장 영향 한 줄
- 달러: 방향 + 원화/수출주 영향 한 줄
- 금리: 수치 + 내일 성장주 영향 한 줄
(수집 안 된 항목은 "데이터 없음"으로 표기)

---

### 4. 💡 한 줄 요약
오늘 수치 1개 + 내일 핵심 포인트 한 문장

SIGNAL:BULL 또는 SIGNAL:NEUTRAL 또는 SIGNAL:BEAR"""

    client = anthropic.Anthropic()
    message = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=1500,
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
