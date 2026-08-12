#!/usr/bin/env python3
"""Walk-forward + calibration → data/engine_config.json (Phase 6–8)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signal_calibration import train_engine


def main():
    feat_path = ROOT / "data" / "signal_features.json"
    out_path = ROOT / "data" / "baseline_outcomes.json"
    if not feat_path.exists() or not out_path.exists():
        print("Need data/signal_features.json and data/baseline_outcomes.json")
        print("Run: python scripts/run_perf_validation.py")
        sys.exit(1)

    features = json.loads(feat_path.read_text())
    if isinstance(features, dict):
        features = features.get("items") or []
    outcomes = json.loads(out_path.read_text())

    cfg_path = ROOT / "data" / "engine_config.json"
    config = train_engine(features, outcomes, cfg_path)

    print("=== Walk-forward OOS ===")
    print(json.dumps(config.get("walk_forward", {}).get("oos"), ensure_ascii=False, indent=2))
    print("\n=== Chosen thresholds ===")
    print(json.dumps(config.get("thresholds"), ensure_ascii=False, indent=2))
    print("\n=== In-sample ===")
    print(json.dumps(config.get("in_sample"), ensure_ascii=False, indent=2))
    print("\n=== Calibration bins ===")
    print(json.dumps(config.get("calibration", {}).get("confidence_bins"), ensure_ascii=False, indent=2))
    print(f"\n→ {cfg_path}")


if __name__ == "__main__":
    main()
