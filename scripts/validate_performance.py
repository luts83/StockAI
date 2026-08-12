#!/usr/bin/env python3
"""
성능 검증: Baseline outcomes × Features 조인 + Hard Gate 후보 A/B.

Usage:
  python scripts/validate_performance.py \
    --outcomes data/baseline_outcomes.json \
    --features data/signal_features.json \
    --out data/performance_validation.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from signal_eval import summarize_baseline, _safe_float, _mean, _profit_factor


def _g(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def join_rows(outcomes: list, features: list) -> list:
    by_id = {str(f.get("analysis_id") or f.get("_id")): f for f in features}
    rows = []
    for o in outcomes:
        aid = str(o.get("analysis_id") or o.get("_id") or "")
        f = by_id.get(aid)
        rows.append({"outcome": o, "features": f})
    return rows


def gate_pass(feat: dict | None, name: str) -> bool:
    """Hard gate 후보. feat 없으면 False (게이트 통과 불가)."""
    if not feat:
        return False
    t = feat.get("trend") or {}
    m = feat.get("momentum") or {}
    v = feat.get("volume") or {}
    rs = feat.get("relative_strength") or {}
    vol = feat.get("volatility") or {}
    regime = (feat.get("market_regime") or {}).get("regime")

    if name == "none":
        return True
    if name == "A_trend":
        # 가격이 MA20을 크게 하회하지 않음 (위험 차단)
        pvm = t.get("price_vs_ma20")
        return pvm is not None and pvm >= -0.03
    if name == "B_trend_macd":
        pvm = t.get("price_vs_ma20")
        return (
            pvm is not None and pvm >= -0.03
            and bool(m.get("macd_above_signal"))
        )
    if name == "C_trend_macd_rs":
        pvm = t.get("price_vs_ma20")
        vs = rs.get("vs_spy_20d")
        return (
            pvm is not None and pvm >= -0.03
            and bool(m.get("macd_above_signal"))
            and vs is not None and vs > 0
        )
    if name == "D_risk":
        # ATR% 과다 / BB 과확장 차단
        atrp = vol.get("atr_pct")
        bbp = vol.get("bb_position")
        atr_ok = atrp is None or atrp <= 0.08
        bb_ok = bbp is None or bbp <= 1.0  # 상단 이탈 과확장 주의
        pvm = t.get("price_vs_ma20")
        return (
            pvm is not None and pvm >= -0.03
            and atr_ok and bb_ok
        )
    if name == "E_full":
        pvm = t.get("price_vs_ma20")
        vs = rs.get("vs_spy_20d")
        atrp = vol.get("atr_pct")
        vr = v.get("volume_ratio")
        return (
            pvm is not None and pvm >= -0.02
            and bool(m.get("macd_above_signal"))
            and vs is not None and vs > 0
            and (atrp is None or atrp <= 0.08)
            and regime != "BEAR"
            and (vr is None or vr >= 0.7)
        )
    return False


def eval_signal_subset(outcomes: list, signal: str, horizon: int = 10) -> dict:
    rkey = f"return_{horizon}d"
    subset = [o for o in outcomes if o.get("signal") == signal and o.get(rkey) is not None]
    rets = [float(o[rkey]) for o in subset]
    if not rets:
        return {"n": 0}
    if signal == "BUY":
        hits = sum(1 for r in rets if r > 0)
        strat = rets
    elif signal == "SELL":
        hits = sum(1 for r in rets if r < 0)
        strat = [-r for r in rets]
    else:
        hits = sum(1 for r in rets if r > 0)
        strat = rets
    return {
        "n": len(rets),
        "precision_pct": _safe_float(100 * hits / len(rets), 1),
        "avg_return_pct": _safe_float(100 * _mean(rets), 2),
        "avg_strategy_pnl_pct": _safe_float(100 * _mean(strat), 2),
        "profit_factor": _profit_factor(strat),
        "hit_count": hits,
    }


def feature_slice_stats(rows: list, horizon: int = 10) -> dict:
    """BUY에 대해 feature 조건별 단순 분할 성과."""
    rkey = f"return_{horizon}d"
    buys = [
        r for r in rows
        if r["outcome"].get("signal") == "BUY"
        and r["outcome"].get(rkey) is not None
        and r["features"]
    ]

    def stats(label, pred):
        sub = [r for r in buys if pred(r["features"])]
        rets = [float(r["outcome"][rkey]) for r in sub]
        if not rets:
            return {"label": label, "n": 0}
        return {
            "label": label,
            "n": len(rets),
            "precision_pct": _safe_float(100 * sum(1 for x in rets if x > 0) / len(rets), 1),
            "avg_return_pct": _safe_float(100 * _mean(rets), 2),
        }

    return {
        "horizon": f"{horizon}d",
        "slices": [
            stats("all_BUY", lambda f: True),
            stats("above_ma20", lambda f: bool(_g(f, "trend", "above_ma20"))),
            stats("below_ma20", lambda f: not bool(_g(f, "trend", "above_ma20"))),
            stats("macd_golden", lambda f: bool(_g(f, "momentum", "macd_above_signal"))),
            stats("macd_dead", lambda f: not bool(_g(f, "momentum", "macd_above_signal"))),
            stats("rs_spy20>0", lambda f: (_g(f, "relative_strength", "vs_spy_20d") or -1) > 0),
            stats("rs_spy20<=0", lambda f: (_g(f, "relative_strength", "vs_spy_20d") or 1) <= 0),
            stats("regime_BULL", lambda f: _g(f, "market_regime", "regime") == "BULL"),
            stats("regime_not_BULL", lambda f: _g(f, "market_regime", "regime") != "BULL"),
            stats("vol_ratio>=1", lambda f: (_g(f, "volume", "volume_ratio") or 0) >= 1.0),
        ],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outcomes", default=str(ROOT / "data" / "baseline_outcomes.json"))
    ap.add_argument("--features", default=str(ROOT / "data" / "signal_features.json"))
    ap.add_argument("--out", default=str(ROOT / "data" / "performance_validation.json"))
    ap.add_argument("--horizon", type=int, default=10)
    args = ap.parse_args()

    outcomes = json.loads(Path(args.outcomes).read_text())
    features = json.loads(Path(args.features).read_text())
    if isinstance(features, dict) and "items" in features:
        features = features["items"]

    rows = join_rows(outcomes, features)
    n_feat = sum(1 for r in rows if r["features"])
    print(f"[validate] outcomes={len(outcomes)} features={len(features)} joined_with_feat={n_feat}")

    baseline = summarize_baseline(outcomes)
    h = args.horizon

    gate_names = ["none", "A_trend", "B_trend_macd", "C_trend_macd_rs", "D_risk", "E_full"]
    gate_report = []
    for gname in gate_names:
        # BUY만 게이트 적용 (SELL은 그대로 참고용 유지)
        filtered = []
        for r in rows:
            o = dict(r["outcome"])
            if o.get("signal") == "BUY":
                if not gate_pass(r["features"], gname):
                    # BUY → 게이트 탈락 = 평가에서 제외 (abstention)
                    continue
            filtered.append(o)
        buy = eval_signal_subset(filtered, "BUY", h)
        sell = eval_signal_subset(filtered, "SELL", h)
        coverage = buy.get("n", 0)
        gate_report.append({
            "gate": gname,
            "BUY": buy,
            "SELL": sell,
            "buy_coverage": coverage,
            "buy_coverage_pct": _safe_float(100 * coverage / max(1, eval_signal_subset(outcomes, "BUY", h).get("n", 1)), 1),
        })

    slices = feature_slice_stats(rows, h)

    # SELL 방향성 재확인
    sell10 = eval_signal_subset(outcomes, "SELL", 10)
    buy10 = eval_signal_subset(outcomes, "BUY", 10)

    report = {
        "summary": {
            "n_outcomes": len(outcomes),
            "n_features": len(features),
            "n_joined": n_feat,
            "horizon": f"{h}d",
            "verdict": None,
        },
        "baseline_mix": baseline.get("signal_mix_pct"),
        "baseline_horizons": baseline.get("horizons"),
        "primary_10d": {"BUY": buy10, "SELL": sell10},
        "gates": gate_report,
        "feature_slices_buy": slices,
        "notes": [
            "Gate는 기존 LLM BUY에 사후 적용한 필터(abstention)이다.",
            "Precision↑ + Coverage 유지가 이상적. Coverage 과도 축소는 실패.",
            "SELL PF는 숏 P&L(-return) 기준.",
        ],
    }

    # 자동 판정
    base_prec = buy10.get("precision_pct") or 0
    best = max(
        (g for g in gate_report if g["gate"] != "none" and g["BUY"].get("n", 0) >= 2),
        key=lambda g: (g["BUY"].get("precision_pct") or 0, g["BUY"].get("avg_return_pct") or -999),
        default=None,
    )
    if best and (best["BUY"].get("precision_pct") or 0) > base_prec:
        report["summary"]["verdict"] = (
            f"Gate {best['gate']}가 BUY Precision {base_prec}% → "
            f"{best['BUY'].get('precision_pct')}%로 개선 "
            f"(coverage {best['buy_coverage']})"
        )
    else:
        report["summary"]["verdict"] = (
            f"현재 Hard Gate 후보로는 BUY Precision({base_prec}%)을 유의미하게 못 올림. "
            "표본 BUY n이 작고 Phase 5 Score 필요."
        )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print("\n--- Gate A/B (BUY) ---")
    for g in gate_report:
        b = g["BUY"]
        print(
            f"  {g['gate']:18} n={b.get('n',0):2}  prec={b.get('precision_pct')}%  "
            f"avgR={b.get('avg_return_pct')}%  PF={b.get('profit_factor')}"
        )
    print(f"\n[validate] → {out}")


if __name__ == "__main__":
    main()
