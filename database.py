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
    # signal_outcomes: Phase 1 Baseline 사후성과
    db["signal_outcomes"].create_index("ticker", background=True)
    db["signal_outcomes"].create_index("signal", background=True)
    db["signal_outcomes"].create_index("entry_date", background=True)
    db["signal_outcomes"].create_index("engine_version", background=True)
    db["baseline_reports"].create_index("generated_at", background=True)
    # signal_features: Phase 2 feature store
    db["signal_features"].create_index("ticker", background=True)
    db["signal_features"].create_index("asof", background=True)
    db["signal_features"].create_index("features_version", background=True)
    db["signal_features"].create_index([("ticker", 1), ("asof", 1)], background=True)
    # analyses: 백필/히스토리 sort 메모리 한도 방지
    db["analyses"].create_index("created_at", background=True)
    db["analyses"].create_index([("user_id", 1), ("created_at", -1)], background=True)
    db["analyses"].create_index(
        [("ticker", 1), ("period", 1), ("user_id", 1), ("created_at", -1)],
        background=True,
    )
    # analysis_feedback: 시그널 전환 자동 비교·교훈
    db["analysis_feedback"].create_index(
        [("ticker", 1), ("period", 1), ("created_at", -1)],
        background=True,
    )
    db["analysis_feedback"].create_index(
        [("prev_signal", 1), ("next_signal", 1)],
        background=True,
    )
    db["analysis_feedback"].create_index("next_id", background=True)
    db["analysis_feedback"].create_index(
        [("outcome_checked", 1), ("created_at", 1)],
        background=True,
    )
    db["ticker_earnings"].create_index("updated_at", background=True)
    db["earnings_history"].create_index([("ticker", 1), ("date", -1)], background=True)

# ── 실적 분기 이력 (영구 보관) ───────────────────────────
def _earnings_record_id(ticker: str, earnings_date: str) -> str:
    return f"{(ticker or '').upper()}_{(earnings_date or '')[:10]}"


def upsert_earnings_record(record: dict) -> str:
    ticker = (record.get("ticker") or "").upper()
    edate = (record.get("date") or "")[:10]
    if not ticker or not edate:
        raise ValueError("ticker and date required")
    rid = _earnings_record_id(ticker, edate)
    existing = get_db()["earnings_history"].find_one({"_id": rid}) or {}
    payload = dict(existing)
    payload.update(record)
    payload["_id"] = rid
    payload["ticker"] = ticker
    payload["date"] = edate
    if existing.get("earnings_call") and not record.get("earnings_call"):
        payload["earnings_call"] = existing["earnings_call"]
    payload["updated_at"] = datetime.utcnow().isoformat()
    if "collected_at" not in payload:
        payload["collected_at"] = payload["updated_at"]
    get_db()["earnings_history"].replace_one(
        {"_id": rid}, _json_safe(payload), upsert=True
    )
    return rid


def get_earnings_record(ticker: str, earnings_date: str) -> dict | None:
    rid = _earnings_record_id(ticker, earnings_date)
    doc = get_db()["earnings_history"].find_one({"_id": rid})
    if doc:
        doc["_id"] = str(doc["_id"])
    return doc


def list_earnings_history(ticker: str, limit: int = 8) -> list:
    t = (ticker or "").upper()
    if not t:
        return []
    cursor = (
        get_db()["earnings_history"]
        .find({"ticker": t})
        .sort("date", -1)
        .limit(limit)
    )
    items = list(cursor)
    for item in items:
        item["_id"] = str(item["_id"])
    return items


def count_earnings_history(ticker: str) -> int:
    t = (ticker or "").upper()
    if not t:
        return 0
    return get_db()["earnings_history"].count_documents({"ticker": t})


# ── 티커별 실적 프로필 (다음 일정·동기화 메타) ─────────
def upsert_ticker_earnings(doc: dict) -> None:
    ticker = (doc.get("ticker") or doc.get("_id") or "").upper()
    if not ticker:
        raise ValueError("ticker required")
    payload = dict(doc)
    payload["_id"] = ticker
    payload["ticker"] = ticker
    payload["updated_at"] = payload.get("updated_at") or datetime.utcnow().isoformat()
    get_db()["ticker_earnings"].replace_one(
        {"_id": ticker}, _json_safe(payload), upsert=True
    )


def get_ticker_earnings(ticker: str) -> dict | None:
    t = (ticker or "").upper()
    if not t:
        return None
    doc = get_db()["ticker_earnings"].find_one({"_id": t})
    if doc:
        doc["_id"] = str(doc["_id"])
    return doc


# ── 분석 저장 ──────────────────────────────────────────
def save_analysis(ticker: str, period: str, indicators: dict,
                  analysis: str, signal: str, news: list, chart_b64: str,
                  user_id: str = "", current_price: float = None,
                  change_pct: float = None, valuation: dict = None,
                  data_date: str = None,
                  llm_signal: str = None,
                  signal_engine: dict = None,
                  earnings_snapshot: dict = None) -> str:
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
        "llm_signal":    llm_signal or signal,
        "signal_engine": signal_engine or {},
        "earnings_snapshot": earnings_snapshot or {},
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

def get_previous_analysis(
    ticker: str,
    period: str,
    user_id: str = "",
    exclude_id: str | None = None,
) -> dict | None:
    """같은 티커·기간의 직전 분석 1건 (최신순). exclude_id는 방금 저장한 문서 제외용."""
    q: dict = {"ticker": ticker, "period": period}
    if user_id:
        q["user_id"] = user_id
    if exclude_id:
        q["_id"] = {"$ne": exclude_id}
    cursor = (
        get_db()["analyses"]
        .find(q, {"chart_b64": 0})
        .sort("created_at", -1)
        .limit(1)
    )
    items = list(cursor)
    return items[0] if items else None


def save_analysis_feedback(doc: dict) -> str:
    """시그널 전환 피드백 upsert (next_id 기준 유일)."""
    next_id = doc.get("next_id")
    if not next_id:
        raise ValueError("next_id required")
    payload = dict(doc)
    payload["_id"] = f"fb_{next_id}"
    payload["updated_at"] = datetime.utcnow().isoformat()
    if "created_at" not in payload:
        payload["created_at"] = payload["updated_at"]
    get_db()["analysis_feedback"].replace_one(
        {"_id": payload["_id"]}, _json_safe(payload), upsert=True
    )
    return payload["_id"]


def get_analysis_feedback(feedback_id: str) -> dict | None:
    return get_db()["analysis_feedback"].find_one({"_id": feedback_id})


def get_feedback_by_next_id(next_id: str) -> dict | None:
    return get_db()["analysis_feedback"].find_one({"next_id": next_id})


def list_ticker_feedback(
    ticker: str,
    period: str | None = None,
    limit: int = 3,
) -> list:
    q: dict = {"ticker": ticker}
    if period:
        q["period"] = period
    cursor = (
        get_db()["analysis_feedback"]
        .find(q)
        .sort("created_at", -1)
        .limit(limit)
    )
    items = list(cursor)
    for item in items:
        item["_id"] = str(item["_id"])
    return _json_safe(items)


def list_pattern_feedback(
    prev_signal: str,
    next_signal: str,
    limit: int = 3,
    exclude_ticker: str | None = None,
) -> list:
    q: dict = {
        "prev_signal": (prev_signal or "").upper(),
        "next_signal": (next_signal or "").upper(),
    }
    if exclude_ticker:
        q["ticker"] = {"$ne": exclude_ticker}
    cursor = (
        get_db()["analysis_feedback"]
        .find(q)
        .sort("created_at", -1)
        .limit(limit)
    )
    items = list(cursor)
    for item in items:
        item["_id"] = str(item["_id"])
    return _json_safe(items)


def list_unchecked_feedback(limit: int = 500, min_age_days: int = 10) -> list:
    """outcome 미채점 피드백 (생성 후 min_age_days 경과)."""
    from datetime import timedelta
    cutoff = (datetime.utcnow() - timedelta(days=min_age_days)).isoformat()
    cursor = (
        get_db()["analysis_feedback"]
        .find({
            "outcome_checked": {"$ne": True},
            "created_at": {"$lte": cutoff},
        })
        .sort("created_at", 1)
        .limit(limit)
    )
    items = list(cursor)
    for item in items:
        item["_id"] = str(item["_id"])
    return items


def update_analysis_feedback(feedback_id: str, fields: dict) -> None:
    get_db()["analysis_feedback"].update_one(
        {"_id": feedback_id},
        {"$set": {**fields, "updated_at": datetime.utcnow().isoformat()}},
    )


def aggregate_transition_stats(limit: int = 2000) -> dict:
    """전환 행렬 + 채점된 피드백 요약."""
    cursor = (
        get_db()["analysis_feedback"]
        .find({}, {"_id": 0, "chart_b64": 0})
        .sort("created_at", -1)
        .limit(limit)
    )
    items = list(cursor)
    matrix: dict = {}
    scored = 0
    favorable = 0
    for fb in items:
        key = f"{fb.get('prev_signal')}→{fb.get('next_signal')}"
        cell = matrix.setdefault(key, {"n": 0, "scored": 0, "favorable": 0, "avg_ret_10d": []})
        cell["n"] += 1
        if fb.get("outcome_checked"):
            scored += 1
            cell["scored"] += 1
            if fb.get("transition_favorable"):
                favorable += 1
                cell["favorable"] += 1
            ret = fb.get("ret_10d")
            if isinstance(ret, (int, float)):
                cell["avg_ret_10d"].append(float(ret))
    for cell in matrix.values():
        vals = cell.pop("avg_ret_10d")
        cell["avg_ret_10d"] = (
            round(sum(vals) / len(vals), 3) if vals else None
        )
        if cell["scored"]:
            cell["favorable_pct"] = round(100.0 * cell["favorable"] / cell["scored"], 1)
        else:
            cell["favorable_pct"] = None
    return {
        "n_feedback": len(items),
        "n_scored": scored,
        "n_favorable": favorable,
        "matrix": matrix,
    }


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

def get_market_brief_by_date(brief_type: str, date: str) -> dict | None:
    """type+date 결정적 _id로 특정일 시황 조회 (마감→당일 장전 검증용)."""
    db = get_db()
    doc_id = f"{brief_type}_{date}"
    doc = db["market_briefs"].find_one({"_id": doc_id})
    if not doc:
        doc = db["market_briefs"].find_one({"type": brief_type, "date": date})
    if not doc:
        return None
    doc["_id"] = str(doc["_id"])
    return _json_safe(doc)

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

def get_market_briefs(limit: int = 30, brief_type: str | None = None) -> list:
    """시황 목록 — preview만 포함 (전문은 get_market_brief_by_id)."""
    db = get_db()
    q: dict = {}
    if brief_type:
        q["type"] = brief_type
    cursor = (
        db["market_briefs"]
        .find(
            q,
            {"market_data": 0, "type": 1, "date": 1, "signal": 1, "created_at": 1, "analysis": 1},
        )
        .sort([("date", -1), ("created_at", -1)])
        .limit(limit)
    )
    items = []
    for doc in cursor:
        text = (doc.get("analysis") or "").replace("\n", " ").strip()
        items.append({
            "_id": str(doc["_id"]),
            "type": doc.get("type"),
            "date": doc.get("date"),
            "signal": doc.get("signal"),
            "created_at": doc.get("created_at"),
            "preview": text[:160] + ("…" if len(text) > 160 else ""),
        })
    return _json_safe(items)


def get_market_brief_by_id(doc_id: str) -> dict | None:
    db = get_db()
    doc = db["market_briefs"].find_one({"_id": doc_id}, {"market_data": 0})
    if not doc:
        return None
    doc["_id"] = str(doc["_id"])
    return _json_safe(doc)


# ── Signal outcomes (Phase 1 Baseline) ──────────────────
def upsert_signal_outcome(outcome: dict) -> str:
    """analysis_id를 _id로 하는 사후성과 upsert."""
    analysis_id = outcome.get("analysis_id")
    if not analysis_id:
        raise ValueError("analysis_id required")
    doc = dict(outcome)
    doc["_id"] = analysis_id
    doc["updated_at"] = datetime.utcnow().isoformat()
    get_db()["signal_outcomes"].replace_one({"_id": analysis_id}, doc, upsert=True)
    return analysis_id


def get_signal_outcome(analysis_id: str) -> dict | None:
    return get_db()["signal_outcomes"].find_one({"_id": analysis_id})


def list_signal_outcomes(
    signal: str | None = None,
    engine_version: str | None = None,
    limit: int = 5000,
) -> list:
    q: dict = {}
    if signal:
        q["signal"] = signal.upper()
    if engine_version:
        q["engine_version"] = engine_version
    cursor = (
        get_db()["signal_outcomes"]
        .find(q)
        .sort("entry_date", -1)
        .limit(limit)
        .allow_disk_use(True)
    )
    items = list(cursor)
    for item in items:
        item["_id"] = str(item["_id"])
    return items


def list_analyses_for_backfill(limit: int = 5000) -> list:
    """사후성과·feature 백필용 analyses.
    대용량 필드 제외 + allowDiskUse로 Atlas sort 32MB 한도 회피."""
    cursor = (
        get_db()["analyses"]
        .find(
            {},
            {
                "ticker": 1,
                "period": 1,
                "created_at": 1,
                "data_date": 1,
                "signal": 1,
                "analysis": 1,
                "current_price": 1,
                "entry_price": 1,
                "valuation": 1,
                "news": 1,
            },
        )
        .sort("created_at", 1)
        .limit(limit)
        .allow_disk_use(True)
    )
    return list(cursor)


def save_baseline_report(report: dict, source: str = "api") -> str:
    doc_id = f"baseline_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    doc = {
        "_id": doc_id,
        "source": source,
        "generated_at": datetime.utcnow().isoformat(),
        "report": _json_safe(report),
    }
    get_db()["baseline_reports"].insert_one(doc)
    return doc_id


def get_latest_baseline_report() -> dict | None:
    doc = get_db()["baseline_reports"].find_one(sort=[("generated_at", -1)])
    if not doc:
        return None
    doc["_id"] = str(doc["_id"])
    return _json_safe(doc)


def save_engine_training_run(config: dict, source: str = "scheduler") -> str:
    """매일 자동학습 이력 저장."""
    doc_id = f"train_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    summary = {
        "_id": doc_id,
        "source": source,
        "created_at": datetime.utcnow().isoformat(),
        "engine_version": config.get("engine_version"),
        "n_rows": config.get("n_rows"),
        "thresholds": config.get("thresholds"),
        "deploy_flags": config.get("deploy_flags"),
        "oos": (config.get("walk_forward") or {}).get("oos"),
        "in_sample": {
            k: (config.get("in_sample") or {}).get(k)
            for k in (
                "n_buy", "n_sell", "n_paper_buy",
                "buy_precision_pct", "sell_precision_pct", "paper_buy_precision_pct",
                "buy_avg_return_pct", "buy_pf",
            )
        },
        "buy_quality_targets": config.get("buy_quality_targets"),
    }
    get_db()["engine_training_runs"].insert_one(_json_safe(summary))
    return doc_id


def get_latest_engine_training_run() -> dict | None:
    doc = get_db()["engine_training_runs"].find_one(sort=[("created_at", -1)])
    if not doc:
        return None
    doc["_id"] = str(doc["_id"])
    return _json_safe(doc)


# ── Signal features (Phase 2) ───────────────────────────
def upsert_signal_features(features: dict) -> str:
    """analysis_id를 _id로 feature 스냅샷 upsert."""
    analysis_id = features.get("analysis_id") or features.get("_id")
    if not analysis_id:
        raise ValueError("analysis_id required")
    doc = dict(features)
    doc["_id"] = analysis_id
    doc["analysis_id"] = analysis_id
    doc["updated_at"] = datetime.utcnow().isoformat()
    get_db()["signal_features"].replace_one({"_id": analysis_id}, _json_safe(doc), upsert=True)
    return str(analysis_id)


def get_signal_features(analysis_id: str) -> dict | None:
    doc = get_db()["signal_features"].find_one({"_id": analysis_id})
    return _json_safe(doc) if doc else None


def list_signal_features(
    ticker: str | None = None,
    features_version: str | None = None,
    limit: int = 5000,
) -> list:
    q: dict = {}
    if ticker:
        q["ticker"] = ticker.upper()
    if features_version:
        q["features_version"] = features_version
    cursor = (
        get_db()["signal_features"]
        .find(q)
        .sort("asof", -1)
        .limit(limit)
        .allow_disk_use(True)
    )
    items = list(cursor)
    for item in items:
        item["_id"] = str(item["_id"])
    return _json_safe(items)


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
