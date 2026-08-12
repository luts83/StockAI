#!/usr/bin/env python3
"""
과거 feature에 signal_engine_v1을 재적용해 Baseline LLM 신호와 비교.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signal_engine import decide_signal
from signal_eval import signal_family, _mean, _safe_float, _profit_factor


def main():
    feat_doc = json.loads((ROOT / "data" / "signal_features.json").read_text())
    features = feat_doc["items"] if isinstance(feat_doc, dict) else feat_doc
    outcomes = json.loads((ROOT / "data" / "baseline_outcomes.json").read_text())
    by_id = {str(o.get("analysis_id")): o for o in outcomes}

    rows = []
    for f in features:
        aid = str(f.get("analysis_id") or f.get("_id"))
        o = by_id.get(aid)
        if not o:
            continue
        d = decide_signal(f)
        rows.append({
            "analysis_id": aid,
            "ticker": f.get("ticker"),
            "llm_signal": signal_family(o.get("signal")),
            "engine_signal": d["signal"],
            "score": d["score"],
            "return_10d": o.get("return_10d"),
        })

    mix = Counter(r["engine_signal"] for r in rows)
    print("Engine signal mix:", dict(mix))
    print("LLM family mix:", dict(Counter(r["llm_signal"] for r in rows)))

    def eval_sig(key, pred):
        sub = [r for r in rows if pred(r) and r.get("return_10d") is not None]
        rets = [float(r["return_10d"]) for r in sub]
        if not rets:
            return {"n": 0}
        if key == "BUY":
            hits = sum(1 for x in rets if x > 0)
            strat = rets
        else:
            hits = sum(1 for x in rets if x < 0)
            strat = [-x for x in rets]
        return {
            "n": len(rets),
            "precision_pct": _safe_float(100 * hits / len(rets), 1),
            "avg_return_pct": _safe_float(100 * _mean(rets), 2),
            "profit_factor": _profit_factor(strat),
        }

    report = {
        "engine_mix": dict(mix),
        "BUY_engine": eval_sig("BUY", lambda r: r["engine_signal"] == "BUY"),
        "SELL_engine": eval_sig("SELL", lambda r: r["engine_signal"] in ("SELL", "AVOID")),
        "BUY_llm": eval_sig("BUY", lambda r: r["llm_signal"] == "BUY"),
        "SELL_llm": eval_sig("SELL", lambda r: r["llm_signal"] == "SELL"),
        "WATCH_UP": eval_sig("BUY", lambda r: r["engine_signal"] == "WATCH_UP"),  # direction check
    }
    # WATCH_UP: measure up-rate instead
    up = [r for r in rows if r["engine_signal"] == "WATCH_UP" and r.get("return_10d") is not None]
    if up:
        rets = [float(r["return_10d"]) for r in up]
        report["WATCH_UP"] = {
            "n": len(rets),
            "up_rate_pct": _safe_float(100 * sum(1 for x in rets if x > 0) / len(rets), 1),
            "avg_return_pct": _safe_float(100 * _mean(rets), 2),
        }

    out = ROOT / "data" / "engine_v1_replay.json"
    out.write_text(json.dumps({"n": len(rows), **report, "samples": rows[:20]}, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("→", out)


if __name__ == "__main__":
    main()
