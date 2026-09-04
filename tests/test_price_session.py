"""정규장 종가 vs 장외 호가 분리 — 분석 기준가는 항상 session close."""


def test_session_price_from_doc_prefers_regular():
    from main import _session_price_from_doc

    doc = {"current_price": 99.0, "regular_price": 121.77, "extended_price": 99.65}
    assert _session_price_from_doc(doc) == 121.77


def test_session_price_fallback_current():
    from main import _session_price_from_doc

    assert _session_price_from_doc({"current_price": 110.0}) == 110.0
    assert _session_price_from_doc({}) is None


def test_prompt_labels_session_close_not_extended_as_current():
    from ai import build_analysis_prompt

    stats = {
        "price": 121.77,
        "change_5d": -2.0,
        "change_20d": -5.0,
        "change_1m": -10.0,
        "vs_spy": -1.0,
        "ma20": 120.0,
        "ma60": 118.0,
        "ma200": 150.0,
        "above_ma20": True,
        "above_ma200": False,
        "rsi": 53.0,
        "macd": 0.1,
        "macd_signal": 0.0,
        "bb_position": 60,
        "stoch_k": 50,
        "stoch_d": 48,
        "52w_high": 226,
        "52w_low": 104,
        "volume": 1_000_000,
        "avg_volume": 500_000,
    }
    price_session = {
        "session_close": 121.77,
        "regular_price": 121.77,
        "extended_price": 99.65,
        "gap_pct": -18.17,
        "has_gap": True,
    }
    prompt = build_analysis_prompt(
        "LULU",
        stats,
        [],
        analysis_date="2026-09-03",
        price_session=price_session,
    )
    assert "분석 기준가 (정규장 종가): $121.77" in prompt
    assert "장외/프리 참고 호가: $99.65" in prompt
    assert "장외가로 재작성 금지" in prompt
    # 구형 '현재가: $장외' 혼선 없어야 함
    assert "현재가: $99.65" not in prompt


def test_output_rule_mentions_session_close():
    from ai import OUTPUT_RULE

    assert "정규장 종가" in OUTPUT_RULE
