from pymongo import MongoClient
from datetime import datetime, date
from dotenv import load_dotenv
import certifi
import math
import os

load_dotenv()

MONGO_URI = os.getenv("MONGODB_URI", "")
_client = None

def get_db():
    global _client
    if _client is None:
        _client = MongoClient(
            MONGO_URI,
            tls=True,
            tlsCAFile=certifi.where(),
            serverSelectionTimeoutMS=10000,
        )
    return _client["stockai"]


def _json_safe(obj):
    """FastAPI JSON 응답용 — NaN/Inf 등 비직렬화 값을 None으로 치환."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    # numpy 스칼라 등이 섞인 경우
    if hasattr(obj, "item") and callable(obj.item) and not isinstance(obj, (bytes, str)):
        try:
            return _json_safe(obj.item())
        except Exception:
            pass
    return obj

def ensure_indexes():
    """TTL 인덱스 등 필수 인덱스 보장 (서버 시작 시 1회 호출)"""
    db = get_db()
    # public_cache: created_at 기준 7일 후 자동 삭제
    db["public_cache"].create_index(
        "created_at",
        expireAfterSeconds=604800,  # 7일
        background=True,
    )

# ── 분석 저장 ──────────────────────────────────────────
def save_analysis(ticker: str, period: str, indicators: dict,
                  analysis: str, signal: str, news: list, chart_b64: str,
                  user_id: str = "", current_price: float = None,
                  change_pct: float = None, valuation: dict = None,
                  data_date: str = None) -> str:
    db = get_db()
    doc_id = f"{ticker}_{period}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    doc = {
        "_id":           doc_id,
        "ticker":        ticker,
        "period":        period,
        "created_at":    datetime.now().isoformat(),
        "data_date":     data_date,
        "current_price": current_price,
        "change_pct":    change_pct,
        "indicators":    indicators,
        "valuation":     valuation or {},
        "analysis":      analysis,
        "signal":        signal,
        "news":          news,
        "chart_b64":     chart_b64,
        "chat_history":  [],
        "user_id":       user_id,
    }
    db["analyses"].insert_one(doc)
    return doc_id

# ── 분석 조회 ──────────────────────────────────────────
def get_analysis(doc_id: str) -> dict | None:
    return get_db()["analyses"].find_one({"_id": doc_id})

def update_analysis_news(doc_id: str, news: list):
    """분석의 뉴스만 업데이트"""
    get_db()["analyses"].update_one(
        {"_id": doc_id},
        {"$set": {"news": news, "news_updated_at": datetime.now().isoformat()}},
    )

def get_today_public_analysis(ticker: str, period: str) -> dict | None:
    """비로그인용 공용 캐시 — user_id 없이 ticker+period+UTC 날짜 기준"""
    from datetime import timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return get_db()["public_cache"].find_one(
        {"ticker": ticker, "period": period, "date": today}
    )

def save_public_analysis(ticker: str, period: str, indicators: dict,
                          analysis: str, signal: str, news: list, chart_b64: str,
                          current_price: float, change_pct: float, valuation: dict) -> None:
    """비로그인용 공용 캐시 저장 (별도 컬렉션, 당일 1회)"""
    from datetime import timezone
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    doc = {
        "ticker":        ticker,
        "period":        period,
        "date":          today,
        "created_at":    now.isoformat(),
        "indicators":    indicators,
        "analysis":      analysis,
        "signal":        signal,
        "news":          news,
        "chart_b64":     chart_b64,
        "current_price": current_price,
        "change_pct":    change_pct,
        "valuation":     valuation or {},
    }
    # upsert: 같은 날 같은 ticker+period면 덮어쓰지 않고 존재하는 게 있으면 유지
    get_db()["public_cache"].update_one(
        {"ticker": ticker, "period": period, "date": today},
        {"$setOnInsert": doc},
        upsert=True,
    )

def get_today_analysis(ticker: str, period: str, user_id: str) -> dict | None:
    """당일 동일 종목+기간 분석 조회 (캐시 재사용)"""
    today = date.today().isoformat()  # "2026-04-16"
    # find_one(sort=...) 구문이 pymongo 버전에 따라 무시될 수 있으므로 cursor 방식 사용
    cursor = get_db()["analyses"].find(
        {
            "ticker":     ticker,
            "period":     period,
            "user_id":    user_id,
            "created_at": {"$regex": f"^{today}"},
        }
    ).sort("created_at", -1).limit(1)
    items = list(cursor)
    return items[0] if items else None

def get_history(ticker: str, limit: int = 10, user_id: str = "") -> list:
    """특정 종목의 분석 히스토리 (최신순, 차트 제외)"""
    db = get_db()
    query = {"ticker": ticker}
    if user_id: query["user_id"] = user_id
    cursor = db["analyses"].find(
        query,
        {"chart_b64": 0}          # 차트 이미지 제외 (용량 절감)
    ).sort("created_at", -1).limit(limit)
    return list(cursor)

def get_all_history(limit: int = 5, skip: int = 0, user_id: str = "") -> list:
    """전체 분석 히스토리 (최신순, 차트 제외, 페이지네이션 지원)"""
    db = get_db()
    query = {"user_id": user_id} if user_id else {}
    cursor = db["analyses"].find(
        query,
        {"chart_b64": 0, "analysis": 0}
    ).sort("created_at", -1).skip(skip).limit(limit)
    return list(cursor)

def get_user_analyses(user_id: str, limit: int = 30) -> list:
    """성과 트래커용 — signal/current_price 포함, 차트 제외"""
    db = get_db()
    cursor = db["analyses"].find(
        {"user_id": user_id},
        {"chart_b64": 0, "analysis": 0, "news": 0, "chat_history": 0}
    ).sort("created_at", -1).limit(limit)
    return list(cursor)

def get_history_count(user_id: str = "") -> int:
    """전체 분석 개수"""
    db = get_db()
    query = {"user_id": user_id} if user_id else {}
    return db["analyses"].count_documents(query)

# ── 대화 저장 ──────────────────────────────────────────
def append_chat(doc_id: str, role: str, content: str, section: str = ""):
    db = get_db()
    entry = {
        "role":      role,
        "content":   content,
        "section":   section,
        "timestamp": datetime.now().isoformat(),
    }
    db["analyses"].update_one(
        {"_id": doc_id},
        {"$push": {"chat_history": entry}}
    )

def get_chat_history(doc_id: str) -> list:
    doc = get_analysis(doc_id)
    return doc.get("chat_history", []) if doc else []

# ── 분석 삭제 ──────────────────────────────────────────
def delete_analysis(doc_id: str):
    get_db()["analyses"].delete_one({"_id": doc_id})


# ── 시황 저장/조회 ──────────────────────────────────────
def save_market_brief(brief: dict) -> str:
    db = get_db()
    # type+date 기반 결정적 _id (중복 방지 + upsert 안전)
    doc_id = f"{brief['type']}_{brief['date']}"
    doc = {k: v for k, v in brief.items() if k != "_id"}
    doc["_id"] = doc_id
    doc = _json_safe(doc)
    db["market_briefs"].replace_one({"_id": doc_id}, doc, upsert=True)
    return doc_id

def delete_market_brief(doc_id: str) -> int:
    """시황 삭제 — 잘못 생성된 브리프 제거용. 삭제 개수 반환"""
    db = get_db()
    res = db["market_briefs"].delete_one({"_id": doc_id})
    return res.deleted_count

def get_latest_market_brief(brief_type: str = None) -> dict | None:
    db = get_db()
    query = {"type": brief_type} if brief_type else {}
    return db["market_briefs"].find_one(
        query,
        sort=[("created_at", -1)],
    )

def get_recent_market_briefs(limit: int = 2, brief_type: str = None) -> list:
    """최근 시황 N개 반환 (최신순). brief_type 지정 시 해당 타입만 — 전망 검증용.
    market_data 포함(한국 stale 대체·검증 짝 조회에 필요)."""
    db = get_db()
    q = {"type": brief_type} if brief_type else {}
    cursor = db["market_briefs"].find(q).sort("created_at", -1).limit(limit)
    items = list(cursor)
    for item in items:
        item["_id"] = str(item["_id"])
    return _json_safe(items)


def migrate_brief_types() -> dict:
    """기존 시황 타입 → 4종 체계로 1회 마이그레이션 (idempotent).
    premarket→us_premarket, close→us_close, korea_close→kr_close"""
    db = get_db()
    mapping = {
        "premarket":   "us_premarket",
        "close":       "us_close",
        "korea_close": "kr_close",
    }
    result = {}
    for old, new in mapping.items():
        r = db["market_briefs"].update_many({"type": old}, {"$set": {"type": new}})
        if r.modified_count:
            result[f"{old}→{new}"] = r.modified_count
    if result:
        print(f"[db] 시황 타입 마이그레이션: {result}")
    return result

def get_market_briefs(limit: int = 10) -> list:
    db = get_db()
    cursor = db["market_briefs"].find(
        {},
        {"market_data": 0},
    ).sort("created_at", -1).limit(limit)
    items = list(cursor)
    for item in items:
        item["_id"] = str(item["_id"])
    return items


# ── 시황 적중률 ──────────────────────────────────────────
def save_brief_performance(brief_id: str, predicted: str, actual: str,
                           is_correct: bool, brief_type: str = ""):
    """시황 예측 결과 저장"""
    get_db()["brief_performance"].insert_one({
        "brief_id":   brief_id,
        "predicted":  predicted,
        "actual":     actual,
        "is_correct": is_correct,
        "brief_type": brief_type,
        "created_at": datetime.utcnow().isoformat(),
    })

def get_brief_accuracy(limit: int = 20, market: str = None) -> dict:
    """최근 N회 시황 적중률 계산.
    market="미국"/"한국" 지정 시 해당 시장(us_/kr_) 전망만 집계 → 시장별 적중률."""
    q = {}
    if market == "미국":
        q = {"brief_type": {"$regex": "^us_"}}
    elif market == "한국":
        q = {"brief_type": {"$regex": "^kr_"}}

    records = list(
        get_db()["brief_performance"]
        .find(q, {"_id": 0})
        .sort("created_at", -1)
        .limit(limit)
    )
    if not records:
        return {"total": 0, "correct": 0, "accuracy_pct": 0, "recent_errors": []}

    def _mkt(bt: str) -> str:
        if bt.startswith("us_"):
            return "🇺🇸"
        if bt.startswith("kr_"):
            return "🇰🇷"
        return ""

    correct = sum(1 for r in records if r["is_correct"])
    errors  = [
        f"{r.get('created_at', '')[:10]} {_mkt(r.get('brief_type', ''))} "
        f"{r['predicted']}→실제{r['actual']}".strip()
        for r in records if not r["is_correct"]
    ][:3]

    return {
        "total":         len(records),
        "correct":       correct,
        "accuracy_pct":  round(correct / len(records) * 100, 1),
        "recent_errors": errors,
    }


# ── 유저 저장/조회 ──────────────────────────────────────
def upsert_user(user_id: str, email: str, name: str, picture: str) -> dict:
    """Google 로그인 시 유저 생성 또는 업데이트"""
    db = get_db()
    user = {
        "_id":        user_id,
        "email":      email,
        "name":       name,
        "picture":    picture,
        "updated_at": datetime.now().isoformat(),
    }
    db["users"].update_one(
        {"_id": user_id},
        {"$set": user, "$setOnInsert": {"created_at": datetime.now().isoformat()}},
        upsert=True
    )
    return user

def get_user(user_id: str) -> dict | None:
    return get_db()["users"].find_one({"_id": user_id})
