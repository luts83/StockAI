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
}


def get_market_data() -> dict:
    """주요 지수 데이터 수집"""
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
        lines.append(f"\n### {region} 시장")
        for ticker, d in tickers.items():
            arrow = "▲" if d["change_pct"] > 0 else "▼"
            lines.append(
                f"- {d['name']}({ticker}): "
                f"${d['price']} "
                f"{arrow} {abs(d['change_pct'])}% "
                f"(거래량 평균 대비 {d['volume_ratio']}%)"
            )
    return "\n".join(lines)


async def generate_market_brief(brief_type: str) -> dict:
    """
    brief_type: "premarket" (장전) | "close" (마감)
    """
    market_data = get_market_data()
    kst  = pytz.timezone("Asia/Seoul")
    now  = datetime.now(kst)
    today = now.strftime("%Y-%m-%d")
    data_text = _build_data_text(market_data)

    if brief_type == "premarket":
        prompt = f"""오늘 {today} 장전 시황을 분석해줘.

{data_text}

아래 형식으로 한국어로 작성:

## 📊 장전 시황 ({today})

### 1. 간밤 미국 시장 요약
- 주요 지수 등락과 의미 (2~3줄)

### 2. 오늘 주목할 포인트
- 핵심 이슈 3가지 (bullet)

### 3. 오늘 장 전망
- 강세/약세 시나리오 한 줄씩

### 4. 오늘의 한 줄 요약
- 핵심 메시지 딱 한 문장

SIGNAL:BULL 또는 SIGNAL:NEUTRAL 또는 SIGNAL:BEAR"""

    else:
        prompt = f"""오늘 {today} 마감 시황을 분석해줘.

{data_text}

아래 형식으로 한국어로 작성:

## 📈 마감 시황 ({today})

### 1. 오늘 시장 총평
- 주요 지수 등락과 의미 (2~3줄)

### 2. 오늘의 핵심 이슈
- 시장에 영향 준 이슈 3가지 (bullet)

### 3. 내일 주목할 이벤트
- 실적발표, 경제지표 등

### 4. 내일 장 전망
- 강세/약세 시나리오 한 줄씩

### 5. 오늘의 한 줄 요약
- 핵심 메시지 딱 한 문장

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

    analysis_clean = (
        analysis
        .replace(f"\nSIGNAL:{signal}", "")
        .replace(f"SIGNAL:{signal}", "")
        .strip()
    )

    return {
        "_id":         f"market_{today}_{brief_type}",
        "type":        brief_type,
        "date":        today,
        "market_data": market_data,
        "analysis":    analysis_clean,
        "signal":      signal,
        "created_at":  now.isoformat(),
    }
