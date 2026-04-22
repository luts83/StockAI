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

from analyzer import get_stock_data, calculate_indicators, get_valuation_data, get_extended_price
from chart import generate_chart
from news import fetch_news
from ai import analyze_with_claude
from database import (
    save_analysis, get_analysis, get_history,
    get_all_history, get_history_count, append_chat, delete_analysis,
    upsert_user,
    save_market_brief, get_latest_market_brief, get_market_briefs,
    get_recent_market_briefs,
    get_today_analysis, update_analysis_news,
    get_today_public_analysis, save_public_analysis,
    ensure_indexes,
)
from market_brief import generate_market_brief
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from auth import (
    get_redirect_uri, get_google_auth_url,
    exchange_code_for_token, get_google_userinfo,
    create_jwt, decode_jwt
)

app = FastAPI(title="Stock Analyzer API")
scheduler = AsyncIOScheduler()  # 모듈 레벨 전역 — health 엔드포인트에서도 접근 가능
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
    force: Optional[bool] = False

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
    m = _re.search(r"\*{0,2}SIGNAL:\*{0,2}\s*(BUY|WATCH|SELL)", analysis)
    return m.group(1) if m else "WATCH"


def clean_analysis(analysis: str) -> str:
    """SIGNAL / WATCH_TRIGGER 관련 라인 모두 제거"""
    text = _re.sub(r"\*{0,2}SIGNAL:\*{0,2}\s*(BUY|WATCH|SELL)[^\n]*\n?", "", analysis)
    text = _re.sub(r"\*{0,2}WATCH_(BUY|SELL)_TRIGGER:\*{0,2}[^\n]*\n?", "", text)
    return text.strip()


# ── 분석 백그라운드 작업 저장소 ──────────────────────────
import uuid as _uuid

_jobs: dict = {}

class AnalyzeJob:
    def __init__(self):
        self.status = "pending"   # pending / running / done / error
        self.result = None
        self.error  = None

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
    ticker  = req.ticker.upper().strip()
    user    = get_current_user(token=stockai_token, authorization=authorization)
    user_id = user.get("sub", "") if user else ""

    # 비로그인 유저 — 당일 공용 캐시 조회 (force=False일 때)
    if not user_id and not req.force:
        pub = get_today_public_analysis(ticker, req.period)
        if pub:
            print(f"[PUBLIC CACHE] hit: {ticker} {req.period}")
            return {
                "doc_id":         "",
                "ticker":         pub["ticker"],
                "current_price":  pub.get("current_price"),
                "change_pct":     pub.get("change_pct", 0),
                "indicators":     pub.get("indicators", {}),
                "valuation":      pub.get("valuation", {}),
                "chart_image":    pub.get("chart_b64", ""),
                "news":           pub.get("news", []),
                "analysis":       pub["analysis"],
                "signal":         pub.get("signal", "WATCH"),
                "is_saved":       False,
                "cached":         True,
                "has_new_news":   False,
                "new_news_count": 0,
                "data_date":      pub.get("data_date", pub.get("created_at", "")[:10]),
            }

    # 로그인 유저이고 force=False면 당일 동일 종목+기간 캐시 반환 (뉴스만 실시간 갱신)
    if user_id and not req.force:
        existing = get_today_analysis(ticker, req.period, user_id)
        print(f"[CACHE] ticker={ticker} period={req.period} user={user_id[:8]}... hit={existing is not None}")
        if existing:
            fresh_news = await asyncio.to_thread(fetch_news, ticker)
            existing_urls = {n.get("url", "") for n in existing.get("news", [])}
            new_news = [n for n in fresh_news if n.get("url", "") not in existing_urls]
            if new_news:
                updated_news = (new_news + existing.get("news", []))[:15]
                update_analysis_news(existing["_id"], updated_news)
                existing["news"] = updated_news
            return {
                "doc_id":          existing["_id"],
                "ticker":          existing["ticker"],
                "current_price":   existing.get("current_price"),
                "change_pct":      existing.get("change_pct", 0),
                "indicators":      existing.get("indicators", {}),
                "valuation":       existing.get("valuation", {}),
                "chart_image":     existing.get("chart_b64", ""),
                "news":            existing["news"],
                "analysis":        existing["analysis"],
                "signal":          existing.get("signal", "WATCH"),
                "is_saved":        True,
                "cached":          True,
                "has_new_news":    bool(new_news),
                "new_news_count":  len(new_news),
                "data_date":       existing.get("data_date", existing.get("created_at", "")[:10]),
            }

    # 신규 분석 — 백그라운드 job 생성 후 job_id 즉시 반환
    job_id = str(_uuid.uuid4())
    _jobs[job_id] = AnalyzeJob()
    asyncio.create_task(_run_analysis_job(job_id, ticker, req, user_id))
    return {"job_id": job_id, "status": "pending"}


async def _run_analysis_job(job_id: str, ticker: str,
                             req: AnalyzeRequest, user_id: str):
    job = _jobs.get(job_id)
    if not job:
        return
    job.status = "running"
    try:
        df = get_stock_data(ticker, req.period, req.interval)
        if df is None or df.empty:
            raise Exception(f"티커 '{ticker}' 데이터를 찾을 수 없습니다.")

        df            = calculate_indicators(df)
        chart_b64     = generate_chart(df, ticker)
        news_items    = fetch_news(ticker)
        valuation     = await asyncio.to_thread(get_valuation_data, ticker)
        analysis_date = df.index[-1].strftime("%Y-%m-%d")
        analysis_raw  = await analyze_with_claude(
            chart_b64, df, ticker, news_items, valuation,
            analysis_date=analysis_date,
        )
        signal   = extract_signal(analysis_raw)
        analysis = clean_analysis(analysis_raw)
        extended = await asyncio.to_thread(get_extended_price, ticker)

        indicators = {
            "rsi":         safe(df["RSI"].iloc[-1]),
            "macd":        safe(df["MACD"].iloc[-1], 4),
            "macd_signal": safe(df["MACD_Signal"].iloc[-1], 4),
            "ma20":        safe(df["MA20"].iloc[-1]),
            "ma60":        safe(df["MA60"].iloc[-1]),
            "ma200":       safe(df["MA200"].iloc[-1]),
        }

        regular_price_val  = safe(df["Close"].iloc[-1])
        extended_price_val = extended.get("extended_price")
        current_price_val  = extended_price_val or regular_price_val
        change_pct_val     = safe(
            (df["Close"].iloc[-1] - df["Close"].iloc[-2])
            / df["Close"].iloc[-2] * 100
        )

        doc_id = ""
        if user_id:
            doc_id = save_analysis(
                ticker=ticker, period=req.period,
                indicators=indicators, analysis=analysis,
                signal=signal, news=news_items,
                chart_b64=chart_b64, user_id=user_id,
                current_price=current_price_val,
                change_pct=change_pct_val,
                valuation=valuation,
                data_date=analysis_date,
            )
        else:
            save_public_analysis(
                ticker=ticker, period=req.period,
                indicators=indicators, analysis=analysis,
                signal=signal, news=news_items,
                chart_b64=chart_b64,
                current_price=current_price_val,
                change_pct=change_pct_val,
                valuation=valuation,
            )

        job.result = {
            "doc_id":          doc_id,
            "ticker":          ticker,
            "current_price":   current_price_val,
            "regular_price":   regular_price_val,
            "extended_price":  extended_price_val,
            "has_gap":         extended.get("has_gap", False),
            "gap_pct":         extended.get("gap_pct"),
            "change_pct":      change_pct_val,
            "indicators":      indicators,
            "valuation":       valuation,
            "chart_image":     chart_b64,
            "news":            news_items,
            "analysis":        analysis,
            "signal":          signal,
            "is_saved":        bool(user_id),
            "data_date":       analysis_date,
        }
        job.status = "done"
        print(f"[job] {job_id[:8]} 완료: {ticker} {req.period}")

    except Exception as e:
        job.status = "error"
        job.error  = str(e)
        print(f"[job] {job_id[:8]} 오류: {e}")
    finally:
        # 1시간 후 메모리에서 자동 제거
        await asyncio.sleep(3600)
        _jobs.pop(job_id, None)


@app.get("/analyze/status/{job_id}")
async def analyze_status(job_id: str):
    """분석 진행 상태 폴링"""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status == "done":
        return {"status": "done", "result": job.result}
    if job.status == "error":
        return {"status": "error", "error": job.error}
    return {"status": job.status}

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
    from datetime import datetime as _dt
    _data_date = doc.get("data_date") or doc.get("created_at", "")[:10]
    system = f"""당신은 주식 기술적 분석 전문가입니다.

[분석 메타데이터 — 절대 무시 금지]
- 분석 대상: {doc['ticker']}
- 분석 기준일: {_data_date}
- 분석 기준가: ${doc.get('current_price', '—')}
- 오늘 날짜: {_dt.utcnow().strftime('%Y-%m-%d')} (UTC 기준)

[답변 규칙 — 반드시 준수]
1. 길이 제한
   - 기본: 3~5문장 (150자 이내)
   - 복잡한 질문도 최대 10문장 (400자 이내)
   - 절대 구조화된 긴 보고서 형식으로 답변 금지

2. 형식 금지
   - H1~H3 헤더 사용 금지 (###, ## 등)
   - 번호 목록 (1, 2, 3...) 3개 이상 금지
   - 불릿 포인트는 최대 3개까지만

3. 서술 방식
   - 서론 없이 결론부터
   - "안녕하세요", "좋은 질문이네요" 같은 인사말 절대 금지
   - 설명 나열보다 "핵심 한 줄 -> 근거 1~2개" 구조

4. 숫자
   - 숫자 인용은 꼭 필요한 것만
   - "RSI, MACD, 거래량, PER..." 나열 금지, 가장 중요한 1~2개만

5. 질문이 "왜?", "어떻게?"로 끝나면 답변도 짧게
   - ❌ 10줄짜리 분석
   - ✅ "거래량 47% 감소 때문입니다. 상승 모멘텀 꺾이고 있다는 신호예요."

예시:
Q: "지금 매수해도 될까요?"
❌ 나쁜 답변: 10문장 분석 + 3개 시나리오 + 주의사항
✅ 좋은 답변: "추천하지 않습니다. RSI 71로 과매수 구간이라 조정 가능성이 높아요.
$42 근처로 내려오면 그때 분할 매수를 고려하세요."

질문 섹션: {req.section}

=== 기존 분석 ({_data_date} 기준) ===
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
            max_tokens=1000,  # 안전마진, 실제 답변은 500토큰 내외 유도
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
    limit: int = 5,
    skip: int = 0,
    authorization: Optional[str] = Header(None),
    stockai_token: Optional[str] = Cookie(None),
):
    user = get_current_user(token=stockai_token, authorization=authorization)
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    uid = user.get("sub", "")
    items = get_all_history(limit=limit, skip=skip, user_id=uid)
    total = get_history_count(uid)
    for item in items:
        item["_id"] = str(item["_id"])
    return {"items": items, "total": total, "skip": skip, "limit": limit}

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

    # 카드 전용 가로형 차트 생성 (16:10 비율 — 실패 시 기존 차트 사용)
    card_chart_b64 = None
    try:
        from chart import generate_chart_for_card
        _ticker = doc.get("ticker", "")
        _period = doc.get("period", "6mo")
        _df = await asyncio.to_thread(get_stock_data, _ticker, _period)
        if _df is not None and not _df.empty:
            _df = calculate_indicators(_df)
            card_chart_b64 = await asyncio.to_thread(
                generate_chart_for_card, _df, _ticker
            )
            print(f"[card] 가로형 차트 생성 완료: {_ticker}")
    except Exception as _e:
        print(f"[card] 가로형 차트 생성 실패 — 기존 차트 사용: {_e}")

    from card import generate_cards
    cards = await generate_cards(doc, card_data, card_chart_b64=card_chart_b64)

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
    return {"status": "ok", "scheduler_running": scheduler.running}

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

@app.get("/debug/scheduler")
async def debug_scheduler():
    """스케줄러 상태 + 최신 시황 생성 시각 확인"""
    import pytz
    from datetime import datetime
    kst = pytz.timezone("Asia/Seoul")
    now_kst = datetime.now(kst)

    close_brief   = get_latest_market_brief("close")
    pre_brief     = get_latest_market_brief("premarket")

    def _brief_info(b):
        if not b:
            return {"exists": False}
        return {
            "exists":     True,
            "id":         str(b.get("_id", "")),
            "date":       b.get("date"),
            "created_at": b.get("created_at"),
            "signal":     b.get("signal"),
        }

    return {
        "server_time_kst":    now_kst.strftime("%Y-%m-%d %H:%M:%S KST"),
        "scheduler_jobs": [
            {"name": "마감 시황", "cron": "평일 KST 05:30 (BST 21:30)"},
            {"name": "장전 시황", "cron": "평일 KST 21:30 (BST 13:30)"},
        ],
        "latest_close":     _brief_info(close_brief),
        "latest_premarket": _brief_info(pre_brief),
    }

# ── 시황 엔드포인트 ────────────────────────────────────

@app.get("/market/brief/latest")
async def latest_brief():
    """최신 시황 + 직전 시황 조회 (비회원 접근 가능)"""
    briefs = get_recent_market_briefs(limit=2)
    if not briefs:
        return {"brief": None, "prev_brief": None}
    return {
        "brief":      briefs[0],
        "prev_brief": briefs[1] if len(briefs) > 1 else None,
    }


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

    brief  = await generate_market_brief(brief_type)
    doc_id = save_market_brief(brief)
    return {"ok": True, "id": doc_id, "signal": brief["signal"]}


# ── 스케줄러 ───────────────────────────────────────────

async def _run_brief(brief_type: str):
    try:
        brief  = await generate_market_brief(brief_type)
        doc_id = save_market_brief(brief)
        print(f"[scheduler] 시황 생성 완료: {doc_id}")
    except Exception as e:
        print(f"[scheduler] 시황 생성 오류: {e}")


@app.on_event("startup")
async def startup():
    ensure_indexes()
    print("[db] 인덱스 확인 완료")

@app.on_event("startup")
async def start_scheduler():
    import pytz
    from datetime import datetime

    # 장전 시황 — 정식: UTC 12:30 (BST 13:30)
    scheduler.add_job(
        _run_brief,
        CronTrigger(hour=12, minute=30, day_of_week="mon-fri", timezone="UTC",
                    start_date="2026-04-18"),
        args=["premarket"],
        id="premarket_brief",
        replace_existing=True,
    )
    # 마감 시황: UTC 21:30 (BST 22:30)
    scheduler.add_job(
        _run_brief,
        CronTrigger(hour=21, minute=30, day_of_week="mon-fri", timezone="UTC"),
        args=["close"],
        id="closing_brief",
        replace_existing=True,
    )
    scheduler.start()
    print("[scheduler] 스케줄러 시작 — 테스트 UTC 13:15 / 장전 UTC 12:30(~04-18) / 마감 UTC 21:30")

    # ── 재배포 후 누락된 오늘 시황 자동 보완 ──────────────
    utc = pytz.utc
    now_utc = datetime.now(utc)
    is_weekday = now_utc.weekday() < 5
    today_str = now_utc.strftime("%Y-%m-%d")

    if is_weekday:
        current_minutes = now_utc.hour * 60 + now_utc.minute

        # 마감 시황: UTC 21:30 지났고 오늘 데이터 없으면 즉시 생성
        if current_minutes >= 21 * 60 + 30:
            today_close = get_latest_market_brief("close")
            if not today_close or today_close.get("date") != today_str:
                print("[scheduler] 오늘 마감 시황 누락 감지 → 즉시 생성")
                asyncio.create_task(_run_brief("close"))

        # 장전 시황: UTC 12:30 지났고 오늘 데이터 없으면 즉시 생성
        if current_minutes >= 12 * 60 + 30:
            today_pre = get_latest_market_brief("premarket")
            if not today_pre or today_pre.get("date") != today_str:
                print("[scheduler] 오늘 장전 시황 누락 감지 → 즉시 생성")
                asyncio.create_task(_run_brief("premarket"))


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
