from fastapi import FastAPI, HTTPException, Request, Cookie, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional
import anthropic
import asyncio
import io
import json as _json
import math
import os
import re as _re
import zipfile
import uvicorn
from dotenv import load_dotenv

load_dotenv()  # .env 먼저 로드 후 환경변수 읽기
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "")

from analyzer import get_stock_data, calculate_indicators, get_valuation_data
from chart import generate_chart
from news import fetch_news
from ai import analyze_with_claude
from database import (
    save_analysis, get_analysis, get_history,
    get_all_history, append_chat, delete_analysis,
    upsert_user,
    save_market_brief, get_latest_market_brief, get_market_briefs,
)
from market_brief import generate_market_brief
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from auth import (
    get_redirect_uri, get_google_auth_url,
    exchange_code_for_token, get_google_userinfo,
    create_jwt, decode_jwt
)

app = FastAPI(title="Stock Analyzer API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "http://127.0.0.1:8000",
        "https://luts83.github.io",
        "https://web-production-3b251.up.railway.app",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)
claude = anthropic.Anthropic()

# ── 모델 ──────────────────────────────────────────────
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

class ChatRequest(BaseModel):
    doc_id: str
    question: str
    section: Optional[str] = "전체"

class CompareRequest(BaseModel):
    doc_id_a: str
    doc_id_b: str

# ── 유틸 ──────────────────────────────────────────────
def safe(val, decimals=2):
    try:
        f = round(float(val), decimals)
        return None if math.isnan(f) or math.isinf(f) else f
    except:
        return None

def extract_signal(analysis: str) -> str:
    import re
    m = re.search(r"SIGNAL:(BUY|WATCH|SELL)", analysis)
    return m.group(1) if m else "WATCH"

def get_current_user(
    token: Optional[str] = None,
    authorization: Optional[str] = None,
) -> Optional[dict]:
    """Authorization 헤더(Bearer) 또는 쿠키 토큰으로 현재 유저 반환"""
    if authorization and authorization.startswith("Bearer "):
        token = authorization[len("Bearer "):]
    if not token:
        return None
    return decode_jwt(token)

# ── 인증 엔드포인트 ────────────────────────────────────

@app.get("/auth/login")
async def login(request: Request):
    """Google 로그인 시작"""
    redirect_uri = get_redirect_uri(str(request.base_url))
    auth_url = get_google_auth_url(redirect_uri)
    return RedirectResponse(auth_url)

@app.get("/auth/callback")
async def auth_callback(request: Request, code: str):
    """Google OAuth 콜백 — JWT 발급 후 프론트로 리다이렉트"""
    redirect_uri = get_redirect_uri(str(request.base_url))

    token_data = await exchange_code_for_token(code, redirect_uri)
    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail="Google 인증 실패")

    userinfo = await get_google_userinfo(access_token)
    user_id  = userinfo.get("id", "")
    email    = userinfo.get("email", "")
    name     = userinfo.get("name", "")
    picture  = userinfo.get("picture", "")

    # DB에 유저 저장
    upsert_user(user_id, email, name, picture)

    # JWT 발급
    jwt_token = create_jwt(user_id, email, name, picture)

    # 프론트로 리다이렉트 — URL 파라미터로 토큰 전달 (크로스 도메인 쿠키 문제 우회)
    is_local = "localhost" in str(request.base_url) or "127.0.0.1" in str(request.base_url)
    frontend_base = "http://127.0.0.1:5500/index.html" if is_local else "https://luts83.github.io/StockAI/"
    frontend_url = f"{frontend_base}?token={jwt_token}"
    return RedirectResponse(url=frontend_url)

@app.get("/auth/me")
async def get_me(
    authorization: Optional[str] = Header(None),
    stockai_token: Optional[str] = Cookie(None),   # 로컬 하위 호환
):
    """현재 로그인 유저 정보"""
    user = get_current_user(token=stockai_token, authorization=authorization)
    if not user:
        return {"user": None}
    email = user.get("email", "")
    return {"user": {
        "id":       user.get("sub"),
        "email":    email,
        "name":     user.get("name"),
        "picture":  user.get("picture"),
        "is_admin": bool(ADMIN_EMAIL and email.strip().lower() == ADMIN_EMAIL.strip().lower()),
    }}

@app.post("/auth/logout")
async def logout():
    return JSONResponse({"ok": True})

# ── 분석 엔드포인트 ────────────────────────────────────

@app.post("/analyze")
async def analyze(
    req: AnalyzeRequest,
    authorization: Optional[str] = Header(None),
    stockai_token: Optional[str] = Cookie(None),
):
    ticker = req.ticker.upper().strip()
    user = get_current_user(token=stockai_token, authorization=authorization)
    user_id = user.get("sub", "") if user else ""

    df = get_stock_data(ticker, req.period, req.interval)
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail=f"티커 '{ticker}' 데이터를 찾을 수 없습니다.")

    df = calculate_indicators(df)
    chart_b64 = generate_chart(df, ticker)
    news_items = fetch_news(ticker)
    valuation = await asyncio.to_thread(get_valuation_data, ticker)
    analysis = await analyze_with_claude(chart_b64, df, ticker, news_items, valuation)
    signal = extract_signal(analysis)

    indicators = {
        "rsi":         safe(df["RSI"].iloc[-1]),
        "macd":        safe(df["MACD"].iloc[-1], 4),
        "macd_signal": safe(df["MACD_Signal"].iloc[-1], 4),
        "ma20":        safe(df["MA20"].iloc[-1]),
        "ma60":        safe(df["MA60"].iloc[-1]),
        "ma200":       safe(df["MA200"].iloc[-1]),
    }

    # 로그인 유저만 DB 저장
    doc_id = ""
    if user_id:
        doc_id = save_analysis(
            ticker=ticker, period=req.period,
            indicators=indicators, analysis=analysis,
            signal=signal, news=news_items,
            chart_b64=chart_b64, user_id=user_id,
            current_price=safe(df["Close"].iloc[-1]),
            change_pct=safe((df["Close"].iloc[-1] - df["Close"].iloc[-2]) / df["Close"].iloc[-2] * 100),
            valuation=valuation,
        )

    return {
        "doc_id":        doc_id,
        "ticker":        ticker,
        "current_price": safe(df["Close"].iloc[-1]),
        "change_pct":    safe((df["Close"].iloc[-1] - df["Close"].iloc[-2]) / df["Close"].iloc[-2] * 100),
        "indicators":    indicators,
        "valuation":     valuation,
        "chart_image":   chart_b64,
        "news":          news_items,
        "analysis":      analysis,
        "signal":        signal,
        "is_saved":      bool(user_id),
    }

@app.post("/chat")
async def chat_stream(
    req: ChatRequest,
    authorization: Optional[str] = Header(None),
    stockai_token: Optional[str] = Cookie(None),
):
    user = get_current_user(token=stockai_token, authorization=authorization)
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")

    doc = get_analysis(req.doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="분석 데이터를 찾을 수 없습니다.")

    # 본인 분석인지 확인
    if doc.get("user_id") != user.get("sub"):
        raise HTTPException(status_code=403, detail="접근 권한이 없습니다.")

    history = doc.get("chat_history", [])[-8:]
    messages = []
    system = f"""당신은 주식 기술적 분석 전문가입니다.
아래는 {doc['ticker']} 종목에 대해 이미 수행된 분석입니다. 이 분석을 바탕으로 사용자의 후속 질문에 답변하세요.
질문이 특정 섹션({req.section})에 관한 것이라면 해당 부분에 집중해서 답변하세요.
한국어로 답변하고, 구체적인 수치와 근거를 포함하세요.

답변 규칙:
- 서론 없이 바로 본론으로 시작
- 핵심 내용만 간결하게 (3~5문장 이내)
- 숫자와 구체적 근거 반드시 포함
- 추가 궁금한 점은 사용자가 직접 질문하도록 유도

=== 기존 분석 ===
{doc['analysis']}
=== 분석 종료 ==="""

    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": req.question})

    append_chat(req.doc_id, "user", req.question, req.section)
    full_response = []

    def generate():
        with claude.messages.stream(
            model="claude-sonnet-4-5-20250929",
            max_tokens=600,
            system=system,
            messages=messages,
        ) as stream:
            for text in stream.text_stream:
                full_response.append(text)
                yield text
        append_chat(req.doc_id, "assistant", "".join(full_response), req.section)

    return StreamingResponse(generate(), media_type="text/plain; charset=utf-8")

@app.get("/history")
async def all_history(
    authorization: Optional[str] = Header(None),
    stockai_token: Optional[str] = Cookie(None),
):
    user = get_current_user(token=stockai_token, authorization=authorization)
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    items = get_all_history(limit=20, user_id=user.get("sub", ""))
    for item in items:
        item["_id"] = str(item["_id"])
    return items

@app.get("/history/{ticker}")
async def ticker_history(
    ticker: str,
    authorization: Optional[str] = Header(None),
    stockai_token: Optional[str] = Cookie(None),
):
    user = get_current_user(token=stockai_token, authorization=authorization)
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    items = get_history(ticker.upper(), limit=10, user_id=user.get("sub", ""))
    for item in items:
        item["_id"] = str(item["_id"])
    return items

@app.get("/analysis/{doc_id}")
async def load_analysis(
    doc_id: str,
    authorization: Optional[str] = Header(None),
    stockai_token: Optional[str] = Cookie(None),
):
    user = get_current_user(token=stockai_token, authorization=authorization)
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    doc = get_analysis(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="분석을 찾을 수 없습니다.")
    if doc.get("user_id") != user.get("sub"):
        raise HTTPException(status_code=403, detail="접근 권한이 없습니다.")
    return doc

@app.delete("/analysis/{doc_id}")
async def remove_analysis(
    doc_id: str,
    authorization: Optional[str] = Header(None),
    stockai_token: Optional[str] = Cookie(None),
):
    user = get_current_user(token=stockai_token, authorization=authorization)
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    doc = get_analysis(doc_id)
    if doc and doc.get("user_id") != user.get("sub"):
        raise HTTPException(status_code=403, detail="접근 권한이 없습니다.")
    delete_analysis(doc_id)
    return {"deleted": doc_id}

@app.post("/compare")
async def compare_analyses(
    req: CompareRequest,
    authorization: Optional[str] = Header(None),
    stockai_token: Optional[str] = Cookie(None),
):
    user = get_current_user(token=stockai_token, authorization=authorization)
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")

    doc_a = get_analysis(req.doc_id_a)
    doc_b = get_analysis(req.doc_id_b)
    if not doc_a or not doc_b:
        raise HTTPException(status_code=404, detail="분석 데이터를 찾을 수 없습니다.")

    prompt = f"""다음 두 시점의 {doc_a['ticker']} 분석을 비교해줘.

=== 분석 A ({doc_a['created_at'][:10]}) ===
지표: RSI {doc_a['indicators'].get('rsi')}, MACD {doc_a['indicators'].get('macd')}
시그널: {doc_a.get('signal')}
{doc_a['analysis'][:1500]}

=== 분석 B ({doc_b['created_at'][:10]}) ===
지표: RSI {doc_b['indicators'].get('rsi')}, MACD {doc_b['indicators'].get('macd')}
시그널: {doc_b.get('signal')}
{doc_b['analysis'][:1500]}

## 1. 주요 지표 변화
## 2. 추세 변화
## 3. 예측 vs 실제
## 4. 현재 시점 시사점"""

    def generate():
        with claude.messages.stream(
            model="claude-sonnet-4-5-20250929",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for text in stream.text_stream:
                yield text

    return StreamingResponse(generate(), media_type="text/plain; charset=utf-8")

@app.post("/news/summary")
async def news_summary_stream(req: NewsSummaryRequest):
    prompt = f"""다음 주식 뉴스를 한국어로 번역하고 요약해줘.
종목: {req.ticker} / 출처: {req.source}
제목: {req.title}
내용: {req.summary or "(본문 없음)"}

**📌 핵심 요약** (3~4줄)
**📈 주가 영향** (긍정/부정/중립 + 이유)
**🔍 주목 포인트** (2~3가지 bullet)"""

    def generate():
        with claude.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        ) as stream:
            for text in stream.text_stream:
                yield text

    return StreamingResponse(generate(), media_type="text/plain; charset=utf-8")

def _extract_card_data_sync(claude_client, analysis_text: str) -> dict:
    """Claude로 분석 텍스트 → 카드뉴스용 구조화 JSON 추출"""
    prompt = f"""아래 주식 분석 텍스트에서 카드뉴스용 데이터를 JSON으로 추출해줘.
JSON만 반환, 코드블록이나 설명 없이. 없는 데이터는 null로.

{{
  "trend_summary": "전반적 추세 2~3문장으로 상세히 (100자 이내)",
  "resistance": ["1차저항 $XXX", "2차저항 $XXX", "3차저항 $XXX"],
  "support": ["1차지지 $XXX", "2차지지 $XXX", "3차지지 $XXX"],
  "volume_note": "거래량 현황 (평균 대비 몇%, 없으면 null)",
  "bull_prob": 강세확률정수(0-100),
  "bear_prob": 약세확률정수(0-100),
  "bull_targets": ["1차 목표 $XXX", "2차 목표 $XXX", "최대 목표 $XXX"],
  "bull_conditions": ["강세 조건1", "강세 조건2", "강세 조건3", "강세 조건4", "강세 조건5"],
  "bear_warnings": ["약세 경고1", "약세 경고2", "약세 경고3", "약세 경고4", "약세 경고5"],
  "stop_loss": "손절 레벨 $XXX (구체적 수치)",
  "strategy_conservative": "보수적 투자자 전략 2~3문장 (80자 이내)",
  "strategy_aggressive": "공격적 투자자 전략 2~3문장 (80자 이내)",
  "checkpoints": ["체크포인트1 (구체적 설명 포함)", "체크포인트2 (구체적 설명 포함)", "체크포인트3 (구체적 설명 포함)"],
  "conclusion": "종합 결론 2~3문장 (100자 이내)",
  "key_event": "주요 이벤트/날짜/촉매 (없으면 null)"
}}

분석 텍스트:
{analysis_text[:4000]}
"""
    try:
        msg = claude_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}]
        )
        text = msg.content[0].text.strip()
        match = _re.search(r'\{[\s\S]*\}', text)
        if match:
            return _json.loads(match.group())
    except Exception as e:
        print(f"[card_data extract error] {e}")
    return {}


@app.get("/card/{doc_id}")
async def create_card(
    doc_id: str,
    authorization: Optional[str] = Header(None),
    stockai_token: Optional[str] = Cookie(None),
):
    """분석 결과 → 인스타그램 카드 4장 ZIP 다운로드 (관리자 전용)"""
    user = get_current_user(token=stockai_token, authorization=authorization)
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")

    # 관리자 이메일 체크
    if ADMIN_EMAIL and user.get("email", "").strip().lower() != ADMIN_EMAIL.strip().lower():
        raise HTTPException(status_code=403, detail="관리자 전용 기능입니다.")

    doc = get_analysis(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="분석을 찾을 수 없습니다.")
    if doc.get("user_id") != user.get("sub"):
        raise HTTPException(status_code=403, detail="접근 권한이 없습니다.")

    # Claude로 분석 텍스트 구조화 추출 (스레드풀 — sync 클라이언트)
    card_data = await asyncio.to_thread(
        _extract_card_data_sync, claude, doc.get("analysis", "")
    )

    from card import generate_cards
    cards = await generate_cards(doc, card_data)

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename, data in cards:
            zf.writestr(filename, data)
    zip_buf.seek(0)

    ticker = doc.get("ticker", "card")
    date   = (doc.get("created_at") or "")[:10].replace("-", "")
    fname  = f"{ticker}_{date}_cards.zip"

    return StreamingResponse(
        zip_buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/debug/admin")
def debug_admin():
    """임시 디버그: ADMIN_EMAIL 설정 확인 (값 일부만 노출)"""
    raw = os.getenv("ADMIN_EMAIL", "")
    masked = (raw[:3] + "***" + raw[-8:]) if len(raw) > 11 else ("SET" if raw else "NOT_SET")
    return {
        "admin_email_set": bool(raw),
        "admin_email_masked": masked,
        "admin_email_len": len(raw),
        "has_leading_space": raw != raw.lstrip(),
        "has_trailing_space": raw != raw.rstrip(),
    }

# ── 시황 엔드포인트 ────────────────────────────────────

@app.get("/market/brief/latest")
async def latest_brief():
    """최신 시황 조회 (비회원 접근 가능)"""
    brief = get_latest_market_brief()
    if not brief:
        return {"brief": None}
    brief["_id"] = str(brief["_id"])
    return {"brief": brief}


@app.get("/market/brief/list")
async def brief_list():
    """시황 목록 조회"""
    return get_market_briefs(limit=10)


@app.post("/market/brief/generate")
async def generate_brief(
    brief_type: str = "close",
    authorization: Optional[str] = Header(None),
    stockai_token: Optional[str] = Cookie(None),
):
    """수동 시황 생성 (관리자 전용)"""
    token = stockai_token or (
        authorization.replace("Bearer ", "") if authorization else None
    )
    if not token:
        raise HTTPException(status_code=401, detail="로그인 필요")
    user = decode_jwt(token)
    if not user or user.get("email", "").strip().lower() != ADMIN_EMAIL.strip().lower():
        raise HTTPException(status_code=403, detail="관리자 전용")

    brief = await generate_market_brief(brief_type)
    save_market_brief(brief)
    return {"ok": True, "id": brief["_id"], "signal": brief["signal"]}


# ── 스케줄러 ───────────────────────────────────────────

async def _run_brief(brief_type: str):
    try:
        brief = await generate_market_brief(brief_type)
        save_market_brief(brief)
        print(f"[scheduler] 시황 생성 완료: {brief['_id']}")
    except Exception as e:
        print(f"[scheduler] 시황 생성 오류: {e}")


@app.on_event("startup")
async def start_scheduler():
    scheduler = AsyncIOScheduler(timezone="Asia/Seoul")
    # 장전 시황: 평일 오전 8:30 KST
    scheduler.add_job(
        lambda: asyncio.create_task(_run_brief("premarket")),
        "cron", day_of_week="mon-fri", hour=8, minute=30,
    )
    # 마감 시황: 평일 오전 6:30 KST (미국 마감 후)
    scheduler.add_job(
        lambda: asyncio.create_task(_run_brief("close")),
        "cron", day_of_week="mon-fri", hour=6, minute=30,
    )
    scheduler.start()
    print("[scheduler] 시황 스케줄러 시작 (장전 08:30 / 마감 06:30 KST)")


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
