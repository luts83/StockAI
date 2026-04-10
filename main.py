from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional
import anthropic
import math
import os
import uvicorn
from dotenv import load_dotenv

from analyzer import get_stock_data, calculate_indicators
from chart import generate_chart
from news import fetch_news
from ai import analyze_with_claude

load_dotenv(override=True)

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Stock Analyzer API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

claude = anthropic.Anthropic()


class AnalyzeRequest(BaseModel):
    ticker: str
    period: Optional[str] = "6mo"
    interval: Optional[str] = "1d"


class NewsSummaryRequest(BaseModel):
    title: str
    summary: str
    url: str
    source: str
    ticker: str


def _last_valid_or(df, col: str, fallback: float) -> float:
    valid = df[col].dropna()
    if valid.empty:
        return fallback
    return float(valid.iloc[-1])


def _safe(v: float, fallback: float = 0.0) -> float:
    """NaN / Inf 값을 fallback으로 대체해 JSON 직렬화 오류 방지"""
    try:
        f = float(v)
        return fallback if not math.isfinite(f) else f
    except (TypeError, ValueError):
        return fallback


@app.post("/analyze")
async def analyze(req: AnalyzeRequest):
    ticker = req.ticker.upper().strip()

    df = get_stock_data(ticker, req.period, req.interval)
    if df is None or df.empty:
        raise HTTPException(
            status_code=404, detail=f"티커 '{ticker}' 데이터를 찾을 수 없습니다.")

    df = calculate_indicators(df)
    chart_b64 = generate_chart(df, ticker)
    news_items = fetch_news(ticker)
    analysis = await analyze_with_claude(chart_b64, df, ticker, news_items)

    close_series = df["Close"].dropna()
    if close_series.empty:
        raise HTTPException(status_code=500, detail="주가 데이터가 유효하지 않습니다.")
    close_now = float(close_series.iloc[-1])
    close_prev = float(close_series.iloc[-2]) if len(close_series) > 1 else close_now

    ma20 = _last_valid_or(df, "MA20", close_now)
    ma60 = _last_valid_or(df, "MA60", close_now)
    ma200 = _last_valid_or(df, "MA200", ma60)
    rsi = _last_valid_or(df, "RSI", 50.0)
    macd = _last_valid_or(df, "MACD", 0.0)
    macd_signal = _last_valid_or(df, "MACD_Signal", 0.0)

    change_pct = (close_now - close_prev) / close_prev * 100 if close_prev != 0 else 0.0

    return {
        "ticker": ticker,
        "current_price": _safe(round(close_now, 2)),
        "change_pct": _safe(round(change_pct, 2)),
        "indicators": {
            "rsi":          _safe(round(rsi, 2), 50.0),
            "macd":         _safe(round(macd, 4)),
            "macd_signal":  _safe(round(macd_signal, 4)),
            "ma20":         _safe(round(ma20, 2)),
            "ma60":         _safe(round(ma60, 2)),
            "ma200":        _safe(round(ma200, 2)),
        },
        "chart_image": chart_b64,
        "news": news_items,
        "analysis": analysis,
    }


@app.post("/news/summary")
async def news_summary_stream(req: NewsSummaryRequest):
    """뉴스 원문 → 한국어 번역+요약 스트리밍"""

    prompt = f"""다음 주식 뉴스를 한국어로 번역하고 요약해줘.

종목: {req.ticker}
출처: {req.source}
제목: {req.title}
내용: {req.summary or "(본문 없음 — 제목 기반으로 분석)"}

아래 형식으로 작성해줘:

**📌 핵심 요약**
3~4줄로 핵심 내용 요약 (한국어)

**📈 주가 영향**
이 뉴스가 {req.ticker} 주가에 미칠 영향을 긍정/부정/중립으로 평가하고 이유 설명

**🔍 주목 포인트**
투자자가 주목해야 할 핵심 내용 2~3가지 (bullet)"""

    def generate():
        if not os.getenv("ANTHROPIC_API_KEY"):
            yield "Anthropic API 키가 설정되지 않아 뉴스 요약을 생성할 수 없습니다."
            return
        try:
            with claude.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}]
            ) as stream:
                for text in stream.text_stream:
                    yield text
        except Exception:
            yield "뉴스 요약 생성 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."

    return StreamingResponse(generate(), media_type="text/plain; charset=utf-8")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def serve_index():
    """배포 시 같은 도메인에서 UI 제공 (index.html만 노출)"""
    path = BASE_DIR / "index.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(path, media_type="text/html; charset=utf-8")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    # Render/Railway 등은 PORT를 주입하므로 reload 끔
    reload = os.getenv("PORT") is None
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=reload)
