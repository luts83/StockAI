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


def get_market_data() -> dict:
    """주요 지수 + 시장심리 데이터 수집"""
    result = {}
    for region, tickers in TICKERS.items():
        result[region] = {}
        for ticker, name in tickers.items():
            try:
                hist = yf.Ticker(ticker).history(period="5d")
                if hist is None or len(hist) < 2:
                    continue
                prev_close = float(hist["Close"].iloc[-2])
                current    = float(hist["Close"].iloc[-1])
                change_pct = (current - prev_close) / prev_close * 100
                volume     = int(hist["Volume"].iloc[-1])
                avg_volume = int(hist["Volume"].mean())
                vol_ratio  = round(volume / avg_volume * 100, 1) if avg_volume else 0
                result[region][ticker] = {
                    "name":         name,
                    "price":        round(current, 2),
                    "change_pct":   round(change_pct, 2),
                    "volume":       volume,
                    "avg_volume":   avg_volume,
                    "volume_ratio": vol_ratio,
                }
            except Exception as e:
                print(f"[market_brief] {ticker} 오류: {e}")
    return result


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

    market_data  = get_market_data()
    kst          = pytz.timezone("Asia/Seoul")
    now          = datetime.now(kst)
    today        = now.strftime("%Y-%m-%d")
    data_text    = _build_data_text(market_data)

    recent       = get_recent_market_briefs(limit=2)
    prev_context = _build_prev_context(recent)

    if brief_type == "premarket":
        prompt = f"""오늘 {today} 장전 시황을 아래 데이터만 사용해서 분석해줘.

{STRICT_RULE}

[제공 데이터 — 이것만 사용할 것]
{data_text}

{prev_context}

아래 형식으로 한국어로 작성:

## 📊 장전 시황 ({today})

### 0. 직전 전망 검증
(직전 시황 전망이 있을 때만 작성. 없으면 이 섹션 생략)
- 직전 전망 한 줄 인용
- 실제 결과: ✅ 적중 또는 ❌ 빗나감
- 빗나간 경우: 제공 데이터에서 읽히는 원인만 서술. 데이터로 설명 불가하면 "데이터상 원인 불명확"

### 1. 간밤 미국 시장 요약
- 제공된 수치만 사용. 지수명 + % + 거래량 비율로만 서술
- 예: "S&P 500 +0.48%, 거래량 평균 대비 83% → 상승하되 참여도 낮음"

### 2. 시장 심리 지표
- VIX: {{수치}} → 공포(20↑) / 중립 / 탐욕(15↓) 판단
- 달러 인덱스 방향
- 10년물 금리 수준
(위 데이터가 수집되지 않은 항목은 "데이터 없음"으로 표기)

### 3. 오늘 장 전망
- 강세 시나리오: 데이터 근거 있는 경우만
- 약세 시나리오: 데이터 근거 있는 경우만
- 신뢰도: 상/중/하 + 데이터 근거 한 줄
- 데이터만으로 방향 판단 불가 시: "현재 데이터로 방향성 판단 불가" 명시

### 4. 오늘의 한 줄 요약
- 가장 두드러진 수치 1개 기반으로만 작성

SIGNAL:BULL 또는 SIGNAL:NEUTRAL 또는 SIGNAL:BEAR"""

    else:  # close / closing
        prompt = f"""오늘 {today} 마감 시황을 아래 데이터만 사용해서 분석해줘.

{STRICT_RULE}

[제공 데이터 — 이것만 사용할 것]
{data_text}

{prev_context}

아래 형식으로 한국어로 작성:

## 📈 마감 시황 ({today})

### 0. 직전 전망 검증
(직전 시황 전망이 있을 때만 작성. 없으면 이 섹션 생략)
- 직전 전망 한 줄 인용
- 실제 결과: ✅ 적중 또는 ❌ 빗나감
- 빗나간 경우: 제공 데이터에서 읽히는 원인만 서술. 데이터로 설명 불가하면 "데이터상 원인 불명확"

### 1. 오늘 시장 총평
- 제공된 지수별 등락률 + 거래량 비율만으로 서술
- 데이터에 없는 이슈 추가 금지

### 2. 데이터로 읽는 시장 심리
- VIX 수치 기반 공포/탐욕 판단
- 달러 인덱스 방향
- 10년물 금리 수준
- 거래량 패턴 (평균 대비)
(수집되지 않은 항목은 "데이터 없음"으로 표기)

### 3. 내일 장 전망
- 강세 시나리오: 오늘 데이터 패턴 기반으로만
- 약세 시나리오: 오늘 데이터 패턴 기반으로만
- 신뢰도: 상/중/하 + 데이터 근거 한 줄
- 예측 불가 요인은 "외부 변수(데이터 없음)"로 명시

### 4. 오늘의 한 줄 요약
- 가장 두드러진 수치 1개 기반으로만 작성

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
