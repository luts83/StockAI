"""
시그널 전환 자동 비교·피드백 루프.

SIGNAL이 바뀌면 직전 분석과 차분/원인/교훈을 저장하고,
다음 분석 프롬프트에 주입한다. 최종 SIGNAL은 엔진이 결정한다.
"""
from __future__ import annotations

from typing import Any, Optional


SIGNAL_KO = {
    "BUY": "매수",
    "SELL": "매도",
    "AVOID": "진입회피",
    "WATCH_UP": "상승관망",
    "WATCH_FLAT": "중립관망",
    "WATCH_DOWN": "하락관망",
    "WATCH_RISK": "위험관망",
    "WATCH": "관망",
}


def _f(v) -> Optional[float]:
    try:
        if v is None:
            return None
        x = float(v)
        if x != x:  # NaN
            return None
        return x
    except (TypeError, ValueError):
        return None


def _snap(doc: dict) -> dict:
    ind = doc.get("indicators") or {}
    eng = doc.get("signal_engine") or {}
    scores = eng.get("scores") or {}
    return {
        "signal": (doc.get("signal") or "").upper(),
        "rsi": _f(ind.get("rsi")),
        "macd": _f(ind.get("macd")),
        "price": _f(doc.get("current_price")),
        "change_pct": _f(doc.get("change_pct")),
        "score": _f(eng.get("score")),
        "trend_label": eng.get("trend_label"),
        "entry_stance": eng.get("entry_stance"),
        "momentum": _f(scores.get("momentum")),
        "entry_quality": _f(scores.get("entry_quality")),
        "trend": _f(scores.get("trend")),
        "rvol": _f(eng.get("rvol")),
    }


def build_feature_delta(prev: dict, nxt: dict) -> dict:
    a, b = _snap(prev), _snap(nxt)
    keys_num = (
        "rsi", "macd", "price", "change_pct", "score",
        "momentum", "entry_quality", "trend", "rvol",
    )
    delta = {
        "prev": a,
        "next": b,
        "diff": {},
        "label_changes": {},
    }
    for k in keys_num:
        pa, pb = a.get(k), b.get(k)
        if pa is not None and pb is not None:
            delta["diff"][k] = round(pb - pa, 4)
        else:
            delta["diff"][k] = None
    for k in ("trend_label", "entry_stance", "signal"):
        if a.get(k) != b.get(k):
            delta["label_changes"][k] = {"from": a.get(k), "to": b.get(k)}
    return delta


def _fmt_num(v, digits=1) -> str:
    if v is None:
        return "—"
    return f"{v:.{digits}f}"


def build_why_short(prev_signal: str, next_signal: str, delta: dict) -> str:
    """규칙 기반 원인 한두 문장."""
    d = delta.get("diff") or {}
    labels = delta.get("label_changes") or {}
    parts = [
        f"{SIGNAL_KO.get(prev_signal, prev_signal)}→{SIGNAL_KO.get(next_signal, next_signal)}"
        f" ({prev_signal}→{next_signal})"
    ]
    bits = []
    if d.get("score") is not None:
        bits.append(
            f"score {_fmt_num(delta['prev'].get('score'))}→{_fmt_num(delta['next'].get('score'))}"
            f" (Δ{_fmt_num(d['score'])})"
        )
    if d.get("rsi") is not None:
        bits.append(
            f"RSI {_fmt_num(delta['prev'].get('rsi'))}→{_fmt_num(delta['next'].get('rsi'))}"
        )
    if labels.get("trend_label"):
        t = labels["trend_label"]
        bits.append(f"trend {t.get('from')}→{t.get('to')}")
    if labels.get("entry_stance"):
        t = labels["entry_stance"]
        bits.append(f"entry {t.get('from')}→{t.get('to')}")
    if d.get("momentum") is not None:
        bits.append(f"mom Δ{_fmt_num(d['momentum'])}")
    if bits:
        parts.append(" | ".join(bits))
    return " — ".join(parts)


def build_lesson(prev_signal: str, next_signal: str, delta: dict) -> str:
    """다음 분석용 교훈 1줄."""
    key = f"{prev_signal}→{next_signal}"
    d = delta.get("diff") or {}
    templates = {
        "WATCH_UP→WATCH_FLAT": (
            "상승관망에서 중립으로 꺾일 때 score·RSI 둔화를 먼저 확인하고 "
            "과한 상승 편향 서술을 피할 것"
        ),
        "WATCH_FLAT→WATCH_UP": (
            "중립→상승관망 전환 시 trend/momentum 동반 확인 없이 낙관만 쓰지 말 것"
        ),
        "WATCH_UP→WATCH_DOWN": (
            "상승관망→하락관망은 급반전 — invalidation·entry 붕괴 근거를 명시할 것"
        ),
        "WATCH_DOWN→WATCH_UP": (
            "하락관망→상승관망은 반등 함정 주의 — participation(RVOL)·entry_quality 확인"
        ),
        "BUY→WATCH_UP": (
            "매수→상승관망 후퇴 시 Gate 미충족 이유를 수치로 설명할 것"
        ),
        "BUY→WATCH_FLAT": (
            "매수에서 중립으로 내려오면 추격 금지·조건 재점검을 강조할 것"
        ),
        "WATCH_UP→BUY": (
            "상승관망→매수는 BUY Gate(점수·RVOL·entry) 충족 근거를 반드시 병기"
        ),
        "SELL→WATCH_DOWN": (
            "매도 후 하락관망 유지는 추세 붕괴가 해소됐는지부터 점검"
        ),
        "WATCH_DOWN→SELL": (
            "하락관망→매도는 단독 과열이 아니라 추세 붕괴+하락 우위인지 재확인"
        ),
    }
    base = templates.get(key)
    if base:
        return base
    if d.get("score") is not None and abs(d["score"]) >= 15:
        direction = "약화" if d["score"] < 0 else "강화"
        return (
            f"{prev_signal}→{next_signal} 전환 시 composite score {direction}"
            f"(Δ{_fmt_num(d['score'])})를 설명의 중심에 둘 것"
        )
    return (
        f"{prev_signal}→{next_signal} 전환 근거(점수·RSI·trend·entry)를 "
        "수치로 대조하고 이전 편향을 반복하지 말 것"
    )


def format_feedback_for_prompt(
    ticker_feedbacks: list,
    pattern_feedbacks: list,
) -> str:
    """LLM 설명층 주입 텍스트. SIGNAL 덮어쓰기 금지."""
    if not ticker_feedbacks and not pattern_feedbacks:
        return ""
    lines = [
        "[과거 시그널 전환 교훈 — 설명·시나리오에만 반영, SIGNAL 변경 금지]",
    ]
    if ticker_feedbacks:
        lines.append(f"이 종목 최근 전환 ({len(ticker_feedbacks)}건):")
        for fb in ticker_feedbacks[:3]:
            lines.append(
                f"  · {fb.get('prev_signal')}→{fb.get('next_signal')}: "
                f"{fb.get('why_short') or '—'}"
            )
            if fb.get("lesson"):
                lines.append(f"    교훈: {fb['lesson']}")
            if fb.get("outcome_checked") and fb.get("transition_favorable") is not None:
                tag = "유리" if fb.get("transition_favorable") else "불리/중립"
                ret = fb.get("ret_10d")
                ret_s = f", 10d {ret:+.1f}%" if isinstance(ret, (int, float)) else ""
                lines.append(f"    사후: {tag}{ret_s}")
    if pattern_feedbacks:
        lines.append("유사 전환 패턴 (다른 종목):")
        for fb in pattern_feedbacks[:3]:
            lines.append(
                f"  · {fb.get('ticker')} {fb.get('prev_signal')}→{fb.get('next_signal')}: "
                f"{fb.get('lesson') or fb.get('why_short') or '—'}"
            )
    lines.append(
        "규칙: 위 교훈은 서술·주의·시나리오에만 사용. "
        "엔진 SIGNAL/Actions를 바꾸지 말 것."
    )
    return "\n".join(lines)


def build_feedback_context_for_ticker(
    ticker: str,
    period: str,
    current_signal: str | None = None,
) -> str:
    """다음 분석 프롬프트용 컨텍스트 조립."""
    from database import list_ticker_feedback, list_pattern_feedback

    ticker_fbs = list_ticker_feedback(ticker, period=period, limit=3)
    pattern_fbs: list = []
    # 최근 티커 전환이 있으면 그 패턴으로 전역 유사 사례
    if ticker_fbs:
        latest = ticker_fbs[0]
        pattern_fbs = list_pattern_feedback(
            latest.get("prev_signal") or "",
            latest.get("next_signal") or "",
            limit=3,
            exclude_ticker=ticker,
        )
    elif current_signal:
        # 직전 전환은 없지만 현재 시그널 관련 최근 유입 패턴 (느슨)
        for prev in ("WATCH_UP", "WATCH_FLAT", "WATCH_DOWN", "BUY", "SELL"):
            if prev == current_signal:
                continue
            pattern_fbs = list_pattern_feedback(
                prev, current_signal, limit=2, exclude_ticker=ticker
            )
            if pattern_fbs:
                break
    return format_feedback_for_prompt(ticker_fbs, pattern_fbs)


def maybe_create_feedback(
    *,
    prev: dict,
    next_doc: dict,
    next_id: str,
) -> Optional[dict]:
    """SIGNAL 변경 시에만 feedback 저장. 실패 시 None."""
    from database import save_analysis_feedback

    prev_sig = (prev.get("signal") or "").upper().replace(" ", "_")
    next_sig = (next_doc.get("signal") or "").upper().replace(" ", "_")
    if not prev_sig or not next_sig or prev_sig == next_sig:
        return None

    delta = build_feature_delta(prev, next_doc)
    why = build_why_short(prev_sig, next_sig, delta)
    lesson = build_lesson(prev_sig, next_sig, delta)
    doc = {
        "ticker": next_doc.get("ticker") or prev.get("ticker"),
        "period": next_doc.get("period") or prev.get("period"),
        "user_id": next_doc.get("user_id") or prev.get("user_id") or "",
        "prev_id": str(prev.get("_id", "")),
        "next_id": next_id,
        "prev_signal": prev_sig,
        "next_signal": next_sig,
        "feature_delta": delta,
        "why_short": why,
        "lesson": lesson,
        "outcome_checked": False,
        "transition_favorable": None,
        "ret_10d": None,
    }
    fb_id = save_analysis_feedback(doc)
    doc["_id"] = fb_id
    print(
        f"[feedback] {doc['ticker']} {prev_sig}→{next_sig} "
        f"saved={fb_id} | {why[:80]}"
    )
    return doc


def score_pending_feedbacks(min_age_days: int = 10, limit: int = 500) -> dict:
    """next_id outcome의 10d 수익률로 전환 유리 여부 채점."""
    from database import (
        list_unchecked_feedback,
        update_analysis_feedback,
        get_signal_outcome,
    )

    items = list_unchecked_feedback(limit=limit, min_age_days=min_age_days)
    checked = skipped = errors = 0
    for fb in items:
        try:
            oid = fb.get("next_id")
            out = get_signal_outcome(oid) if oid else None
            if not out:
                skipped += 1
                continue
            horizons = out.get("horizons") or out.get("returns") or {}
            ret = None
            if isinstance(horizons, dict):
                ret = horizons.get("10d")
                if isinstance(ret, dict):
                    ret = ret.get("return_pct") or ret.get("ret")
            if ret is None:
                ret = out.get("return_10d")
            if ret is None:
                ret = out.get("ret_10d")
            complete = (out.get("horizons_complete") or {}).get("10d")
            if ret is None:
                skipped += 1
                continue
            # return_10d는 소수(0.05=5%)일 수 있음
            ret_f = float(ret)
            if abs(ret_f) <= 1.5 and complete is not False:
                # 대부분 |ret|<1.5 이면 비율로 간주 → %로
                ret_pct = ret_f * 100.0
            else:
                ret_pct = ret_f
            prev_s = fb.get("prev_signal")
            next_s = fb.get("next_signal")
            bearish_next = next_s in ("SELL", "AVOID", "WATCH_DOWN", "WATCH_RISK")
            bullish_next = next_s in ("BUY", "WATCH_UP")
            if bearish_next:
                favorable = ret_pct <= 0
            elif bullish_next:
                favorable = ret_pct >= 0
            else:
                favorable = abs(ret_pct) < 3.0

            update_analysis_feedback(
                fb["_id"],
                {
                    "outcome_checked": True,
                    "ret_10d": round(ret_pct, 3),
                    "transition_favorable": bool(favorable),
                    "scored_prev_signal": prev_s,
                    "scored_next_signal": next_s,
                },
            )
            checked += 1
        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"[feedback] score fail: {e}")
    return {
        "pending": len(items),
        "checked": checked,
        "skipped": skipped,
        "errors": errors,
    }


def ui_payload(fb: dict | None) -> Optional[dict]:
    """프론트 시그널 박스용 요약."""
    if not fb:
        return None
    return {
        "prev_signal": fb.get("prev_signal"),
        "next_signal": fb.get("next_signal"),
        "prev_label": SIGNAL_KO.get(fb.get("prev_signal"), fb.get("prev_signal")),
        "next_label": SIGNAL_KO.get(fb.get("next_signal"), fb.get("next_signal")),
        "why_short": fb.get("why_short"),
        "lesson": fb.get("lesson"),
        "prev_id": fb.get("prev_id"),
        "next_id": fb.get("next_id"),
        "feedback_id": fb.get("_id"),
    }
