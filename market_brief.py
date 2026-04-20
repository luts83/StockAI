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
        "^VIX":     "VIX 공포지수",
        "^TNX":     "미국 10년물 금리",
        "DX-Y.NYB": "달러 인덱스",
    },
}

STRICT_RULE = """
[절대 원칙 — 반드시 준수]
1. 아래 제공된 데이터에 없는 내용은 절대 쓰지 말 것
2. 뉴스, 실적발표, 경제지표 일정은 데이터로 주어지지 않으면 언급 금지
3. "외국인 매수세", "AI 관련주 재조명" 같은 근거 없는 표현 금지
4. 데이터로 설명 불가한 내용은 "데이터상 원인 불명확"으로 명시
5. 숫자 없는 강세/약세 표현 금지 — 반드시 지수명 + % 포함
6. 전망은 현재 데이터 패턴에서만 도출. 외부 변수 추측 금지
7. 직전 전망이 틀렸을 때 절대 묻어가지 말 것. 명확히 인정하고 원인 분석
"""


def _fetch_ticker(ticker: str, name: str, retries: int = 3) -> dict | None:
    """단일 티커 데이터 수집 (재시도 포함)"""
    import time
    for attempt in range(1, retries + 1):
        try:
            hist = yf.Ticker(ticker).history(period="5d")
            if hist is None or len(hist) < 2:
                print(f"[market_brief] {ticker} 데이터 부족 (rows={len(hist) if hist is not None else 0})")
                return None
            prev_close = float(hist["Close"].iloc[-2])
            current    = float(hist["Close"].iloc[-1])
            change_pct = (current - prev_close) / prev_close * 100
            volume     = int(hist["Volume"].iloc[-1])
            avg_volume = int(hist["Volume"].mean())
            vol_ratio  = round(volume / avg_volume * 100, 1) if avg_volume else 0
            return {
                "name":         name,
                "price":        round(current, 2),
                "change_pct":   round(change_pct, 2),
                "volume":       volume,
                "avg_volume":   avg_volume,
                "volume_ratio": vol_ratio,
            }
        except Exception as e:
            wait = 2 ** attempt
            print(f"[market_brief] {ticker} 시도 {attempt}/{retries} 실패: {e} → {wait}s 후 재시도")
            if attempt < retries:
                time.sleep(wait)
    print(f"[market_brief] {ticker} 최종 실패 — 데이터 없음으로 처리")
    return None


def get_market_data() -> dict:
    """주요 지수 + 시장심리 데이터 수집"""
    result = {}
    for region, tickers in TICKERS.items():
        result[region] = {}
        for ticker, name in tickers.items():
            data = _fetch_ticker(ticker, name)
            if data:
                result[region][ticker] = data
    return result


def _has_minimum_data(market_data: dict) -> bool:
    """핵심 지수(S&P500 또는 KOSPI) 중 하나 이상 있어야 생성 가능"""
    us  = market_data.get("미국", {})
    kr  = market_data.get("한국", {})
    return bool(us) or bool(kr)


def _build_data_text(market_data: dict) -> str:
    lines = []
    for region, tickers in market_data.items():
        lines.append(f"\n### {region}")
        for ticker, d in tickers.items():
            arrow = "▲" if d["change_pct"] > 0 else "▼"
            vol_line = (
                f"(거래량 평균 대비 {d['volume_ratio']}%)"
                if d.get("volume_ratio") else ""
            )
            lines.append(
                f"- {d['name']}({ticker}): "
                f"{d['price']} "
                f"{arrow}{abs(d['change_pct'])}% "
                f"{vol_line}"
            )
    return "\n".join(lines)


def get_market_timing_context(now: datetime) -> dict:
    """현재 시각 기준 미국 시장 상태 판단"""
    et          = now.astimezone(pytz.timezone("America/New_York"))
    et_hour     = et.hour
    et_weekday  = et.weekday()  # 0=월 ~ 6=일

    if et_weekday >= 5:
        us_status = "WEEKEND"
        us_desc   = "주말 (금요일 종가 유지 중)"
    elif et_hour < 4:
        us_status = "PRE_EARLY"
        us_desc   = "이른 프리마켓 (선물 반영 중)"
    elif et_hour < 9 or (et_hour == 9 and et.minute < 30):
        us_status = "PRE"
        us_desc   = "프리마켓 (장 시작 전)"
    elif et_hour < 16:
        mins_open = (et_hour - 9) * 60 + et.minute - 30
        us_status = "OPEN"
        us_desc   = f"장중 (개장 후 {mins_open}분)"
    elif et_hour < 20:
        us_status = "AFTER"
        us_desc   = "애프터마켓 (장 마감 후)"
    else:
        us_status = "CLOSED"
        us_desc   = "당일 장 종료"

    return {
        "now_kst":    now.strftime("%Y-%m-%d %H:%M KST"),
        "now_et":     et.strftime("%Y-%m-%d %H:%M ET"),
        "us_status":  us_status,
        "us_desc":    us_desc,
        "weekday_kr": ["월","화","수","목","금","토","일"][now.weekday()],
    }


def _build_interpretation_guide(timing: dict) -> str:
    """시점 맥락 + 해석 기준 가이드 (프롬프트에 주입)"""
    status = timing["us_status"]

    vol_note = ""
    if status == "OPEN":
        vol_note = (
            "\n   ※ 현재 장중 → 거래량이 평균 대비 낮은 것은 정상:"
            "\n     - 개장 30분 이내: 20% 이하도 정상"
            "\n     - 개장 2시간 이내: 40% 이하도 정상"
            "\n     - '극저조' 표현 사용 금지 (마감 후 데이터에서만 사용)"
        )
    elif status == "WEEKEND":
        vol_note = "\n   ※ 주말 → yfinance 데이터는 금요일 종가 기준. '금요일 종가 유지' 명시"
    elif status in ("PRE", "PRE_EARLY"):
        vol_note = "\n   ※ 프리마켓 시간대 → 거래량은 선물/호가 기반. '프리마켓 기준' 명시"

    return f"""
[현재 시각 정보]
- 한국 시각: {timing['now_kst']} ({timing['weekday_kr']}요일)
- 미국 시각: {timing['now_et']}
- 미국 시장 상태: {timing['us_desc']}{vol_note}

[해석 주의사항 — 반드시 준수]
1. 소폭 등락 기준
   - ±0.3% 이내: "소폭 조정" 또는 "보합권"
   - ±0.5%~1%: "완만한 변동"
   - ±1% 이상: "의미있는 변동"
   - "방향성 부재"는 ±0.1% 이내일 때만

2. 직전 전망 검증 기준
   - NEUTRAL 전망 후 실제 ±1% 이내 → "적중"
   - BULL 전망 후 +0.5% 이상 → "적중"
   - BEAR 전망 후 -0.5% 이상 하락 → "적중"
   - 장중 데이터라면 "장중 미확정" 명시

3. 주말/프리마켓 데이터 주의
   - 주말: "금요일 종가 유지" 명시
   - 프리마켓: "선물/호가 기반 예상" 명시
"""


def _build_prev_context(recent_briefs: list) -> str:
    """직전 시황에서 전망 섹션만 추출해서 컨텍스트 구성"""
    if not recent_briefs:
        return ""

    prev      = recent_briefs[0]
    prev_type = "장전" if prev.get("type") == "premarket" else "마감"
    analysis  = prev.get("analysis", "")

    # 전망 섹션만 추출 (토큰 절약)
    forecast_match = re.search(
        r"(###\s*\d+\.\s*(내일|오늘)\s*장\s*전망[\s\S]*?)(?=###|\Z)",
        analysis
    )
    forecast_text = (
        forecast_match.group(1).strip()
        if forecast_match
        else analysis[:400]
    )

    return f"""
[직전 시황 전망 — {prev.get('date')} {prev_type} / SIGNAL:{prev.get('signal')}]
{forecast_text}
[직전 시황 끝 — 이 내용을 기반으로 ### 0. 직전 전망 검증을 작성할 것]
"""


async def generate_market_brief(brief_type: str) -> dict:
    from database import get_recent_market_briefs

    market_data = get_market_data()
    kst         = pytz.timezone("Asia/Seoul")
    now         = datetime.now(kst)
    today       = now.strftime("%Y-%m-%d")

    # 핵심 데이터 없으면 생성 불가 — 명확한 에러
    if not _has_minimum_data(market_data):
        raise RuntimeError(
            f"[market_brief] {today} {brief_type} — "
            "yfinance에서 핵심 지수 데이터를 가져오지 못했습니다. "
            "Yahoo Finance 서버 상태를 확인하세요."
        )

    data_text    = _build_data_text(market_data)
    recent       = get_recent_market_briefs(limit=2)
    prev_context = _build_prev_context(recent)
    timing       = get_market_timing_context(now)
    interp_guide = _build_interpretation_guide(timing)

    if brief_type == "premarket":
        prompt = f"""오늘 {today} 장전 시황을 아래 데이터만 사용해서 분석해줘.

{STRICT_RULE}

{interp_guide}

[제공 데이터 — 이것만 사용할 것]
{data_text}

{prev_context}

아래 형식으로 한국어로 작성.
핵심 원칙: 결론 먼저 → 근거는 한 줄. 숫자는 꼭 필요한 것만.

## 📊 장전 시황 ({today})

### 0. 직전 전망 검증
(직전 시황 있을 때만. 없으면 생략)
✅ 적중 또는 ❌ 빗나감 — 이유 한 줄

---

### 1. 🇺🇸 미국 시장 (간밤)
**결론: 강세 / 약세 / 혼조** (한 단어)
- S&P500: 등락률 + 거래량 비율 한 줄
- NASDAQ: 등락률 + 거래량 비율 한 줄
- DOW: 등락률 + 거래량 비율 한 줄
- 종합 한 줄: 가장 두드러진 포인트 1개만

---

### 2. 🇰🇷 한국 시장 전망 (오늘)
**결론: 강세 우위 / 약세 우위 / 중립** (한 단어)
- 강세 근거: 데이터 기반 1줄
- 약세 근거: 데이터 기반 1줄
- 신뢰도: 상/중/하

---

### 3. 📊 시장 심리
- VIX {{수치}} → 공포(20↑) / 중립 / 탐욕(15↓) 한 단어
- 달러: 방향 한 단어 (강세/약세) + 신흥시장 영향 한 줄
- 금리: 방향 한 단어 + 성장주 영향 한 줄

---

### 4. 💡 한 줄 요약
오늘 가장 중요한 숫자 1개 기반으로 딱 한 문장

SIGNAL:BULL 또는 SIGNAL:NEUTRAL 또는 SIGNAL:BEAR"""

    else:  # close / closing
        prompt = f"""오늘 {today} 마감 시황을 아래 데이터만 사용해서 분석해줘.

{STRICT_RULE}

{interp_guide}

[제공 데이터 — 이것만 사용할 것]
{data_text}

{prev_context}

아래 형식으로 한국어로 작성.
핵심 원칙: 결론 먼저 → 근거는 한 줄. 숫자는 꼭 필요한 것만.

## 📈 마감 시황 ({today})

### 0. 직전 전망 검증
(직전 시황 있을 때만. 없으면 생략)
✅ 적중 또는 ❌ 빗나감 — 이유 한 줄

---

### 1. 🇺🇸 미국 시장 (오늘 마감)
**결론: 강세 / 약세 / 혼조** (한 단어)
- S&P500: 등락률 + 거래량 비율 한 줄
- NASDAQ: 등락률 + 거래량 비율 한 줄
- DOW: 등락률 + 거래량 비율 한 줄
- 종합 한 줄: 가장 두드러진 포인트 1개만

---

### 2. 🇰🇷 한국 시장 (오늘 결과)
**결론: 강세 / 약세 / 혼조** (한 단어)
- KOSPI: 등락률 + 거래량 비율 한 줄
- KOSDAQ: 등락률 + 거래량 비율 한 줄
- 종합 한 줄: 가장 두드러진 포인트 1개만

---

### 3. 📊 시장 심리
- VIX {{수치}} → 공포(20↑) / 중립 / 탐욕(15↓) 한 단어
- 달러: 방향 한 단어 + 내일 영향 한 줄
- 금리: 방향 한 단어 + 내일 성장주 영향 한 줄

---

### 4. 🔮 내일 전망
**결론: 강세 우위 / 약세 우위 / 중립** (한 단어)
- 강세 근거: 데이터 기반 1줄
- 약세 근거: 데이터 기반 1줄
- 신뢰도: 상/중/하 + 근거 한 줄

---

### 5. 💡 한 줄 요약
오늘 가장 중요한 숫자 1개 기반으로 딱 한 문장

SIGNAL:BULL 또는 SIGNAL:NEUTRAL 또는 SIGNAL:BEAR"""

    client  = anthropic.Anthropic()
    message = client.messages.create(
        model      = "claude-sonnet-4-5-20250929",
        max_tokens = 1500,
        messages   = [{"role": "user", "content": prompt}],
    )
    analysis = message.content[0].text

    signal = "NEUTRAL"
    if "SIGNAL:BULL" in analysis:
        signal = "BULL"
    elif "SIGNAL:BEAR" in analysis:
        signal = "BEAR"

    analysis_clean = (
        analysis
        .replace(f"\nSIGNAL:{signal}", "")
        .replace(f"SIGNAL:{signal}", "")
        .strip()
    )

    return {
        "type":        brief_type,
        "date":        today,
        "market_data": market_data,
        "analysis":    analysis_clean,
        "signal":      signal,
        "created_at":  now.isoformat(),
    }
