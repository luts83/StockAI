#!/usr/bin/env python3
"""
Phase 2 feature backfill.

Usage:
  python scripts/backfill_features.py --json ~/Documents/stockai.analyses.json
  python scripts/backfill_features.py --mongo
  python scripts/backfill_features.py --json ~/Documents/stockai.analyses.json --limit 20
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from signal_features import (
    features_from_analysis_doc, clear_feature_cache, FEATURES_VERSION,
)


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
    ap = argparse.ArgumentParser(description="StockAI Phase 2 feature backfill")
    ap.add_argument("--json", type=str)
    ap.add_argument("--mongo", action="store_true")
    ap.add_argument("--out", type=str, default=str(ROOT / "data" / "signal_features.json"))
    ap.add_argument("--limit", type=int, default=5000)
    args = ap.parse_args()

    if not args.json and not args.mongo:
        ap.error("Specify --json and/or --mongo")

    docs: list = []
    if args.json:
        docs = load_json_docs(Path(args.json).expanduser())
        print(f"[features] loaded {len(docs)} from JSON")
    if args.mongo:
        from database import list_analyses_for_backfill
        mongo_docs = list_analyses_for_backfill(limit=args.limit)
        print(f"[features] loaded {len(mongo_docs)} from Mongo")
        by_id = {str(d.get("_id")): d for d in docs}
        for d in mongo_docs:
            by_id[str(d.get("_id"))] = d
        docs = list(by_id.values())

    docs = docs[: args.limit]
    clear_feature_cache()
    features = []
    errors = []
    for i, doc in enumerate(docs, 1):
        ticker = doc.get("ticker")
        if i == 1 or i % 10 == 0 or i == len(docs):
            print(f"[features] {i}/{len(docs)} {ticker}")
        try:
            feat = features_from_analysis_doc(doc)
            features.append(feat)
            if args.mongo:
                from database import upsert_signal_features
                upsert_signal_features(feat)
        except Exception as e:
            errors.append({"ticker": ticker, "id": str(doc.get("_id")), "error": str(e)})
            print(f"  ! {ticker}: {e}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "features_version": FEATURES_VERSION,
        "n": len(features),
        "n_errors": len(errors),
        "errors": errors[:50],
        "items": features,
    }, ensure_ascii=False, indent=2))
    print(f"[features] wrote {len(features)} → {out} (errors={len(errors)})")


if __name__ == "__main__":
    main()
