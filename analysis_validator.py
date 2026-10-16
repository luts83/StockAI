"""LLM analysis negative validator (Value-Aware). Kept separate from ai.py to avoid heavy imports."""
from __future__ import annotations

_NEGATIVE_SELL_PATTERNS = (
    "하락 추세이므로 매도",
    "하락세이므로 매도",
    "하락 추세이므로 청산",
    "하락세이므로 청산",
    "이평 역배열로 청산",
    "이평 역배열로 매도",
    "역배열로 전량",
    "과매도이니 손절",
    "과매도이므로 매도",
    "과매도이므로 청산",
    "주가 하락이므로 매도",
    "가격 하락으로 매도",
    "추세 약화로 전량",
    "하락 중이므로 매도",
    "하락 중이므로 청산",
)


def validate_analysis_text(text: str, signal_engine: dict | None = None) -> dict:
    """
    LLM 출력 네거티브 검증.
    returns {ok, errors, warnings, missing_slots}
    """
    errors: list[str] = []
    warnings: list[str] = []
    t = text or ""
    low = t.replace(" ", "")

    for p in _NEGATIVE_SELL_PATTERNS:
        if p.replace(" ", "") in low:
            errors.append(f"momentum_only_sell:{p}")

    slots = {
        "value_vs_price": any(
            k in t for k in ("가치", "밸류", "Value", "저평가", "고평가", "적정")
        ),
        "action_sizing": any(
            k in t
            for k in (
                "비중",
                "분할",
                "SCALE",
                "REDUCE",
                "HOLD",
                "WAIT",
                "sizing",
                "축소",
                "유지",
            )
        ),
        "invalidation": any(
            k in t for k in ("Invalidation", "무효화", "훼손", "이탈", "BROKEN", "Hard")
        ),
        "next_trigger": any(
            k in t
            for k in ("Trigger", "트리거", "전환 조건", "다음", "SCALE_IN", "재검토")
        ),
    }
    missing = [k for k, ok in slots.items() if not ok]
    if missing:
        warnings.append(f"missing_slots:{','.join(missing)}")

    eng = signal_engine or {}
    va_action = (
        (eng.get("decision") or {}).get("action") or eng.get("entry_action") or ""
    ).upper()
    if va_action in ("HOLD", "WAIT", "SCALE_IN") and any(
        x in t for x in ("전량 청산", "전량매도", "EXIT 권고", "즉시 매도")
    ):
        errors.append("contradicts_engine_non_exit")

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "missing_slots": missing,
    }
