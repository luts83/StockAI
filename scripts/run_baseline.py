#!/usr/bin/env python3
"""
Phase 1 Baseline runner.

Usage:
  # Mongo export JSON으로 성적표 생성 (DB 불필요)
  python scripts/run_baseline.py --json ~/Documents/stockai.analyses.json

  # MongoDB analyses 백필 + 성적표 저장
  python scripts/run_baseline.py --mongo

  # 둘 다
  python scripts/run_baseline.py --json ~/Documents/stockai.analyses.json --mongo
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from signal_eval import build_outcomes_from_docs, summarize_baseline, ENGINE_VERSION


def load_json_docs(path: Path) -> list:
    raw = json.loads(path.read_text())
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for v in raw.values():
            if isinstance(v, list):
                return v
    raise ValueError(f"Unsupported JSON shape: {path}")


def main():
    ap = argparse.ArgumentParser(description="StockAI Signal Engine Baseline v1")
    ap.add_argument("--json", type=str, help="Path to analyses Mongo export JSON")
    ap.add_argument("--mongo", action="store_true", help="Backfill from MongoDB analyses")
    ap.add_argument("--out", type=str, default=str(ROOT / "data" / "baseline_report.json"))
    ap.add_argument("--outcomes-out", type=str, default=str(ROOT / "data" / "baseline_outcomes.json"))
    ap.add_argument("--limit", type=int, default=5000)
    args = ap.parse_args()

    if not args.json and not args.mongo:
        ap.error("Specify --json and/or --mongo")

    docs: list = []
    if args.json:
        docs.extend(load_json_docs(Path(args.json).expanduser()))
        print(f"[baseline] loaded {len(docs)} docs from JSON")

    if args.mongo:
        from database import list_analyses_for_backfill, upsert_signal_outcome, save_baseline_report
        mongo_docs = list_analyses_for_backfill(limit=args.limit)
        print(f"[baseline] loaded {len(mongo_docs)} docs from MongoDB")
        # Prefer mongo docs when both provided (dedupe by _id)
        by_id = {}
        for d in docs:
            by_id[str(d.get("_id"))] = d
        for d in mongo_docs:
            by_id[str(d.get("_id"))] = d
        docs = list(by_id.values())

    docs = docs[: args.limit]
    print(f"[baseline] computing outcomes for {len(docs)} analyses ({ENGINE_VERSION})")
    outcomes = build_outcomes_from_docs(docs, progress=True)
    report = summarize_baseline(outcomes)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"[baseline] report → {out_path}")

    outcomes_path = Path(args.outcomes_out)
    outcomes_path.write_text(json.dumps(outcomes, ensure_ascii=False, indent=2))
    print(f"[baseline] outcomes → {outcomes_path}")

    if args.mongo:
        from database import upsert_signal_outcome, save_baseline_report
        n_ok = 0
        for o in outcomes:
            if not o.get("analysis_id"):
                continue
            upsert_signal_outcome(o)
            n_ok += 1
        rid = save_baseline_report(report, source="scripts/run_baseline.py")
        print(f"[baseline] upserted {n_ok} outcomes, saved report {rid}")

    # Console scorecard
    print("\n======== BASELINE SCORECARD ========")
    print(f"n={report['n_total']}  mix={report.get('signal_mix_pct')}")
    for h, block in report.get("horizons", {}).items():
        print(f"\n--- {h} ---")
        for sig in ("BUY", "SELL", "WATCH"):
            s = block.get(sig, {})
            if not s.get("n"):
                continue
            if sig in ("BUY", "SELL"):
                print(
                    f"  {sig:5} n={s['n']:3}  precision={s.get('precision_pct')}%  "
                    f"avgR={s.get('avg_return_pct')}%  PF={s.get('profit_factor')}  "
                    f"MFE={s.get('avg_mfe_pct')}%  MAE={s.get('avg_mae_pct')}%"
                )
            else:
                print(
                    f"  {sig:5} n={s['n']:3}  up={s.get('up_rate_pct')}%  "
                    f"avoid={s.get('downside_avoidance_pct')}%  "
                    f"oppLoss={s.get('opportunity_loss_pct')}%  avgR={s.get('avg_return_pct')}%"
                )
    print("====================================\n")


if __name__ == "__main__":
    main()
