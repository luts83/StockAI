from pymongo import MongoClient
from datetime import datetime
from dotenv import load_dotenv
import certifi
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

# ── 분석 저장 ──────────────────────────────────────────
def save_analysis(ticker: str, period: str, indicators: dict,
                  analysis: str, signal: str, news: list, chart_b64: str,
                  user_id: str = "", current_price: float = None,
                  change_pct: float = None, valuation: dict = None) -> str:
    db = get_db()
    doc_id = f"{ticker}_{period}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    doc = {
        "_id":           doc_id,
        "ticker":        ticker,
        "period":        period,
        "created_at":    datetime.now().isoformat(),
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

def get_all_history(limit: int = 20, user_id: str = "") -> list:
    """전체 분석 히스토리 (최신순, 차트 제외)"""
    db = get_db()
    query = {"user_id": user_id} if user_id else {}
    cursor = db["analyses"].find(
        query,
        {"chart_b64": 0, "analysis": 0}  # 무거운 필드 제외
    ).sort("created_at", -1).limit(limit)
    return list(cursor)

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
    db["market_briefs"].replace_one(
        {"_id": brief["_id"]},
        brief,
        upsert=True,
    )
    return brief["_id"]

def get_latest_market_brief(brief_type: str = None) -> dict | None:
    db = get_db()
    query = {"type": brief_type} if brief_type else {}
    return db["market_briefs"].find_one(
        query,
        sort=[("created_at", -1)],
    )

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
