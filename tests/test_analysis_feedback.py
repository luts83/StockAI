"""analysis_feedback 피드백 루프 검증 (배포 전).

실행: .venv/bin/python -m pytest tests/test_analysis_feedback.py -v
또는: .venv/bin/python tests/test_analysis_feedback.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis_feedback import (
    SIGNAL_KO,
    build_feature_delta,
    build_why_short,
    build_lesson,
    format_feedback_for_prompt,
    ui_payload,
    maybe_create_feedback,
    score_pending_feedbacks,
)


def _iren_pair():
    prev = {
        "_id": "IREN_6mo_20260817_152000",
        "ticker": "IREN",
        "period": "6mo",
        "user_id": "user_test",
        "signal": "WATCH_UP",
        "indicators": {"rsi": 62.0, "macd": 0.5, "macd_signal": 0.3},
        "signal_engine": {
            "score": 32,
            "trend_label": "BULLISH",
            "entry_stance": "ENTRY_WAIT",
            "scores": {"momentum": 20, "entry_quality": 10, "trend": 25},
            "rvol": 1.2,
        },
        "current_price": 20.0,
        "change_pct": 2.0,
    }
    nxt = {
        "ticker": "IREN",
        "period": "6mo",
        "user_id": "user_test",
        "signal": "WATCH_FLAT",
        "indicators": {"rsi": 48.0, "macd": 0.1, "macd_signal": 0.2},
        "signal_engine": {
            "score": 8,
            "trend_label": "NEUTRAL",
            "entry_stance": "ENTRY_AVOID",
            "scores": {"momentum": 5, "entry_quality": 5, "trend": 8},
            "rvol": 0.9,
        },
        "current_price": 18.5,
        "change_pct": -1.5,
    }
    return prev, nxt


def test_feature_delta_iren():
    prev, nxt = _iren_pair()
    d = build_feature_delta(prev, nxt)
    assert d["diff"]["score"] == -24.0
    assert d["diff"]["rsi"] == -14.0
    assert d["label_changes"]["trend_label"]["from"] == "BULLISH"
    assert d["label_changes"]["trend_label"]["to"] == "NEUTRAL"
    assert d["label_changes"]["signal"]["from"] == "WATCH_UP"
    assert d["label_changes"]["signal"]["to"] == "WATCH_FLAT"


def test_why_and_lesson():
    prev, nxt = _iren_pair()
    d = build_feature_delta(prev, nxt)
    why = build_why_short("WATCH_UP", "WATCH_FLAT", d)
    lesson = build_lesson("WATCH_UP", "WATCH_FLAT", d)
    assert "WATCH_UP→WATCH_FLAT" in why or "상승관망→중립관망" in why
    assert "32" in why and "8" in why
    assert "RSI" in why
    assert "상승관망" in lesson and "중립" in lesson


def test_same_signal_skips_feedback():
    prev, nxt = _iren_pair()
    nxt["signal"] = "WATCH_UP"
    with patch("database.save_analysis_feedback") as save:
        out = maybe_create_feedback(prev=prev, next_doc=nxt, next_id="x")
        assert out is None
        save.assert_not_called()


def test_maybe_create_feedback_persists():
    prev, nxt = _iren_pair()
    with patch("database.save_analysis_feedback", return_value="fb_next1") as save:
        out = maybe_create_feedback(prev=prev, next_doc=nxt, next_id="next1")
        assert out is not None
        assert out["prev_signal"] == "WATCH_UP"
        assert out["next_signal"] == "WATCH_FLAT"
        assert out["outcome_checked"] is False
        assert "why_short" in out and out["why_short"]
        assert "lesson" in out and out["lesson"]
        save.assert_called_once()
        saved = save.call_args[0][0]
        assert saved["next_id"] == "next1"
        assert saved["prev_id"] == prev["_id"]
        assert "feature_delta" in saved


def test_ui_payload():
    fb = {
        "_id": "fb_1",
        "prev_signal": "WATCH_UP",
        "next_signal": "WATCH_FLAT",
        "why_short": "score down",
        "lesson": "avoid bias",
        "prev_id": "a",
        "next_id": "b",
    }
    p = ui_payload(fb)
    assert p["prev_label"] == SIGNAL_KO["WATCH_UP"]
    assert p["next_label"] == SIGNAL_KO["WATCH_FLAT"]
    assert ui_payload(None) is None


def test_prompt_injection_text():
    fb = {
        "ticker": "IREN",
        "prev_signal": "WATCH_UP",
        "next_signal": "WATCH_FLAT",
        "why_short": "score 32→8",
        "lesson": "과한 상승 편향 피할 것",
    }
    txt = format_feedback_for_prompt([fb], [])
    assert "과거 시그널 전환" in txt
    assert "WATCH_UP→WATCH_FLAT" in txt
    assert "SIGNAL" in txt  # 덮어쓰기 금지 문구
    assert "바꾸지" in txt or "금지" in txt


def test_build_analysis_prompt_includes_feedback():
    """ai.py 소스에 feedback_context 주입이 들어가 있는지 (import 없이)."""
    src = (ROOT / "ai.py").read_text(encoding="utf-8")
    assert "feedback_context" in src
    assert "과거 시그널 전환 피드백" in src
    assert "def build_analysis_prompt" in src
    assert "def analyze_with_claude" in src
    # analyze_with_claude가 feedback_context를 전달하는지
    assert "feedback_context=feedback_context" in src


def test_score_pending_bullish_favorable():
    pending = [{
        "_id": "fb_s1",
        "next_id": "an1",
        "prev_signal": "WATCH_FLAT",
        "next_signal": "WATCH_UP",
    }]
    outcome = {
        "return_10d": 0.05,  # +5%
        "horizons_complete": {"10d": True},
    }
    updates = []

    def fake_update(fid, fields):
        updates.append((fid, fields))

    with patch("database.list_unchecked_feedback", return_value=pending), \
         patch("database.get_signal_outcome", return_value=outcome), \
         patch("database.update_analysis_feedback", side_effect=fake_update):
        r = score_pending_feedbacks(min_age_days=0, limit=10)
    assert r["checked"] == 1
    assert updates[0][1]["transition_favorable"] is True
    assert abs(updates[0][1]["ret_10d"] - 5.0) < 0.01


def test_score_pending_bearish_favorable_on_drop():
    pending = [{
        "_id": "fb_s2",
        "next_id": "an2",
        "prev_signal": "WATCH_UP",
        "next_signal": "WATCH_DOWN",
    }]
    outcome = {"return_10d": -0.04, "horizons_complete": {"10d": True}}
    updates = []
    with patch("database.list_unchecked_feedback", return_value=pending), \
         patch("database.get_signal_outcome", return_value=outcome), \
         patch("database.update_analysis_feedback", side_effect=lambda i, f: updates.append(f)):
        r = score_pending_feedbacks(min_age_days=0, limit=10)
    assert r["checked"] == 1
    assert updates[0]["transition_favorable"] is True


def test_main_hook_wiring():
    """main.py에 저장 훅·주입·일일 learn이 연결되어 있는지."""
    text = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "build_feedback_context_for_ticker" in text
    assert "maybe_create_feedback" in text
    assert "signal_change" in text
    assert "score_pending_feedbacks" in text
    assert "aggregate_transition_stats" in text
    assert "/feedback/transitions" in text


def test_ui_has_signal_change_block():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    assert "signal_change" in html
    assert "직전 대비" in html


def test_flow_simulation_print():
    """사람이 읽기 쉬운 IREN 시나리오 시뮬레이션 (assert는 위 테스트들)."""
    prev, nxt = _iren_pair()
    d = build_feature_delta(prev, nxt)
    why = build_why_short("WATCH_UP", "WATCH_FLAT", d)
    lesson = build_lesson("WATCH_UP", "WATCH_FLAT", d)
    print("\n=== IREN 시뮬레이션 ===")
    print(f"1) 8/17 SIGNAL: {prev['signal']}")
    print(f"2) 8/19 SIGNAL: {nxt['signal']}")
    print(f"3) why: {why}")
    print(f"4) lesson: {lesson}")
    print(f"5) UI: {ui_payload({'_id':'fb','prev_signal':'WATCH_UP','next_signal':'WATCH_FLAT','why_short':why,'lesson':lesson,'prev_id':'a','next_id':'b'})}")
    ctx = format_feedback_for_prompt([{
        "prev_signal": "WATCH_UP", "next_signal": "WATCH_FLAT",
        "why_short": why, "lesson": lesson,
    }], [])
    print("6) 다음 프롬프트 주입:\n" + ctx)


if __name__ == "__main__":
    import traceback
    tests = [n for n, v in list(globals().items()) if n.startswith("test_") and callable(v)]
    failed = 0
    for name in tests:
        try:
            globals()[name]()
            print(f"PASS {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
