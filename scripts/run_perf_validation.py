#!/usr/bin/env python3
"""
빠른 feature 백필 + 성능 검증 (티커별 OHLCV 1회 다운로드).
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signal_features import (
    calculate_indicators_pd,
    get_stock_history,
    extract_features_from_df,
    clear_feature_cache,
    FEATURES_VERSION,
)
from signal_eval import summarize_baseline


def load_docs(path: Path) -> list:
    raw = json.loads(path.read_text())
    return raw if isinstance(raw, list) else next(v for v in raw.values() if isinstance(v, list))


def backfill_features_fast(docs: list) -> list:
    clear_feature_cache()
    by_ticker = defaultdict(list)
    for d in docs:
        by_ticker[str(d.get("ticker", "")).upper()].append(d)

    # warm ticker histories once
    hist = {}
    for i, ticker in enumerate(sorted(by_ticker), 1):
        print(f"[hist] {i}/{len(by_ticker)} {ticker}")
        df = get_stock_history(ticker, period="2y")
        if df.empty:
            print(f"  ! no data {ticker}")
            continue
        hist[ticker] = calculate_indicators_pd(df)

    features = []
    errors = []
    n = len(docs)
    for i, doc in enumerate(docs, 1):
        ticker = str(doc.get("ticker", "")).upper()
        if i == 1 or i % 20 == 0 or i == n:
            print(f"[feat] {i}/{n} {ticker}")
        try:
            aid = doc.get("_id")
            if isinstance(aid, dict):
                aid = aid.get("$oid", str(aid))
            else:
                aid = str(aid) if aid else None
            asof = doc.get("data_date")
            if not asof:
                created = doc.get("created_at")
                if isinstance(created, dict):
                    created = created.get("$date")
                asof = str(created)[:10] if created else None
            valuation = doc.get("valuation") or {}
            df = hist.get(ticker)
            if df is None or df.empty:
                raise ValueError(f"no price data for {ticker}")
            feat = extract_features_from_df(
                df, ticker,
                asof=asof,
                sector=valuation.get("sector"),
                news=doc.get("news") or [],
                valuation=valuation,
                analysis_id=aid,
                signal=doc.get("signal"),
            )
            features.append(feat)
        except Exception as e:
            errors.append({"ticker": ticker, "id": str(doc.get("_id")), "error": str(e)})
    print(f"[feat] done n={len(features)} errors={len(errors)}")
    return features, errors


def main():
    docs = load_docs(Path("/Users/sjlee/Documents/stockai.analyses.json"))
    outcomes = json.loads((ROOT / "data" / "baseline_outcomes.json").read_text())

    features, errors = backfill_features_fast(docs)
    feat_path = ROOT / "data" / "signal_features.json"
    feat_path.write_text(json.dumps({
        "features_version": FEATURES_VERSION,
        "n": len(features),
        "n_errors": len(errors),
        "errors": errors[:30],
        "items": features,
    }, ensure_ascii=False, indent=2))
    print(f"wrote {feat_path}")

    import subprocess
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "validate_performance.py"),
         "--outcomes", str(ROOT / "data" / "baseline_outcomes.json"),
         "--features", str(feat_path),
         "--out", str(ROOT / "data" / "performance_validation.json")],
        cwd=str(ROOT),
    )
    if r.returncode:
        sys.exit(r.returncode)

    baseline = summarize_baseline(outcomes)
    print("\nBASELINE 10d:")
    for sig in ("BUY", "SELL", "WATCH"):
        print(sig, baseline["horizons"]["10d"].get(sig))


if __name__ == "__main__":
    main()
