"""
Signal Engine training: Walk-forward + Confidence Calibration (Phase 6–7).

Random split 금지. 시간순 fold로 threshold를 고르고, score→확률을 보정한다.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from signal_engine import (
    compute_score,
    hard_gates,
    classify_watch,
    ENGINE_VERSION as DEFAULT_ENGINE_VERSION,
)
from signal_eval import _mean, _safe_float, _profit_factor

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT / "data" / "engine_config.json"


def _asof(row: dict) -> str:
    f = row.get("features") or {}
    o = row.get("outcome") or {}
    return str(f.get("asof") or o.get("entry_date") or o.get("data_date") or "")[:10]


def join_feature_outcomes(features: list, outcomes: list) -> list[dict]:
    by_id = {str(o.get("analysis_id") or o.get("_id")): o for o in outcomes}
    rows = []
    for f in features:
        aid = str(f.get("analysis_id") or f.get("_id"))
        o = by_id.get(aid)
        if not o or o.get("return_10d") is None:
            continue
        rows.append({"features": f, "outcome": o, "asof": _asof({"features": f, "outcome": o})})
    rows.sort(key=lambda r: r["asof"])
    return rows


def decide_with_thresholds(features: dict, thr: dict) -> dict:
    gates = hard_gates(features)
    scored = compute_score(features)
    score = scored["score"]
    buy_min = thr["buy_min"]
    sell_max = thr["sell_max"]
    watch_up = thr.get("watch_up_min", 18)
    watch_down = thr.get("watch_down_max", -18)

    if score >= buy_min and gates["buy_allowed"]:
        signal = "BUY"
    elif score <= sell_max:
        signal = "SELL"
    else:
        atrp = (features.get("volatility") or {}).get("atr_pct")
        gap = (features.get("volatility") or {}).get("gap_risk")
        if (atrp is not None and atrp > 0.10) or (gap is not None and abs(gap) > 0.05):
            signal = "WATCH_RISK"
        elif not gates.get("risk", True) or not gates.get("liquidity", True):
            signal = "WATCH_RISK"
        elif score >= watch_up:
            signal = "WATCH_UP"
        elif score <= watch_down:
            signal = "WATCH_DOWN"
        else:
            signal = "WATCH_FLAT"

    if score >= buy_min and not gates["buy_allowed"]:
        signal = "WATCH_UP" if gates.get("risk", True) else "WATCH_RISK"

    return {"signal": signal, "score": score, "gates": gates, **scored}


def eval_rows(rows: list[dict], thr: dict, horizon: str = "return_10d") -> dict:
    preds = []
    paper_rets = []
    paper_min = float(thr.get("paper_buy_min", 55))

    for r in rows:
        d = decide_with_thresholds(r["features"], thr)
        ret = r["outcome"].get(horizon)
        excess = r["outcome"].get("excess_return_10d") if horizon == "return_10d" else None
        mae = r["outcome"].get("mae_10d") if horizon == "return_10d" else None
        ret_f = float(ret) if ret is not None else None
        preds.append({
            **d,
            "ret": ret_f,
            "excess": float(excess) if excess is not None else None,
            "mae": float(mae) if mae is not None else None,
        })
        # Paper BUY: Gate 통과 + score≥paper_min (실제 BUY 배출 여부와 무관)
        if (
            ret_f is not None
            and d["score"] >= paper_min
            and (d.get("gates") or {}).get("buy_allowed")
        ):
            paper_rets.append(ret_f)

    def subset(sig):
        return [p for p in preds if p["signal"] == sig and p["ret"] is not None]

    buy = subset("BUY")
    sell = subset("SELL")
    buy_rets = [p["ret"] for p in buy]
    sell_rets = [p["ret"] for p in sell]
    buy_excess = [p["excess"] for p in buy if p["excess"] is not None]
    buy_mae = [p["mae"] for p in buy if p["mae"] is not None]

    buy_prec = (
        _safe_float(100 * sum(1 for x in buy_rets if x > 0) / len(buy_rets), 1)
        if buy_rets else None
    )
    sell_prec = (
        _safe_float(100 * sum(1 for x in sell_rets if x < 0) / len(sell_rets), 1)
        if sell_rets else None
    )
    paper_prec = (
        _safe_float(100 * sum(1 for x in paper_rets if x > 0) / len(paper_rets), 1)
        if paper_rets else None
    )

    return {
        "n": len(preds),
        "n_buy": len(buy_rets),
        "n_sell": len(sell_rets),
        "n_paper_buy": len(paper_rets),
        "buy_precision_pct": buy_prec,
        "sell_precision_pct": sell_prec,
        "paper_buy_precision_pct": paper_prec,
        "buy_avg_return_pct": _safe_float(100 * _mean(buy_rets), 2) if buy_rets else None,
        "sell_avg_return_pct": _safe_float(100 * _mean(sell_rets), 2) if sell_rets else None,
        "paper_buy_avg_return_pct": _safe_float(100 * _mean(paper_rets), 2) if paper_rets else None,
        "buy_avg_excess_pct": _safe_float(100 * _mean(buy_excess), 2) if buy_excess else None,
        "buy_avg_mae_pct": _safe_float(100 * _mean(buy_mae), 2) if buy_mae else None,
        "buy_pf": _profit_factor(buy_rets) if buy_rets else None,
        "sell_pf": _profit_factor([-x for x in sell_rets]) if sell_rets else None,
        "paper_buy_pf": _profit_factor(paper_rets) if paper_rets else None,
        "coverage_buy_pct": _safe_float(100 * len(buy_rets) / max(1, len(preds)), 1),
        "coverage_sell_pct": _safe_float(100 * len(sell_rets) / max(1, len(preds)), 1),
        "mix": {
            s: sum(1 for p in preds if p["signal"] == s)
            for s in ("BUY", "SELL", "WATCH_UP", "WATCH_FLAT", "WATCH_DOWN", "WATCH_RISK")
        },
    }


def objective(metrics: dict) -> float:
    """BUY 품질 최우선: Precision + 위험조정수익 + PF. SELL은 보조."""
    bp = metrics.get("buy_precision_pct")
    sp = metrics.get("sell_precision_pct")
    nb, ns = metrics.get("n_buy", 0), metrics.get("n_sell", 0)
    br = metrics.get("buy_avg_return_pct") or 0.0
    bex = metrics.get("buy_avg_excess_pct")
    bmae = metrics.get("buy_avg_mae_pct")
    bpf = metrics.get("buy_pf") or 0.0
    score = 0.0

    if bp is not None and nb >= 2:
        score += bp * 2.0 * min(1.0, nb / 5)
        score += max(0.0, br) * 3.0
        if bex is not None:
            score += max(0.0, bex) * 2.0
        if bmae is not None:
            score += max(-15.0, bmae) * 0.5
        if bpf:
            score += min(bpf, 5.0) * 8.0
        if bp < 45:
            score -= (45 - bp) * 1.5
    elif bp is not None and nb == 1:
        score += bp * 0.15
    else:
        pp = metrics.get("paper_buy_precision_pct")
        pn = metrics.get("n_paper_buy") or 0
        if pp is not None and pn >= 3:
            score += pp * 0.4 * min(1.0, pn / 8)

    if sp is not None and ns >= 3:
        score += sp * 0.6 * min(1.0, ns / 8)
    elif sp is not None and ns >= 1:
        score += sp * 0.2

    if nb + ns == 0:
        score -= 25
    return score


def grid_search_thresholds(train_rows: list[dict]) -> tuple[dict, dict]:
    buy_grid = [52, 55, 58, 60, 62, 65, 68, 72, 75, 78]
    sell_grid = [-28, -32, -36, -40, -44, -50, -55]
    best_thr = {
        "buy_min": 58, "sell_max": -38,
        "watch_up_min": 18, "watch_down_max": -18,
        "paper_buy_min": 55,
    }
    best_m = eval_rows(train_rows, best_thr)
    best_obj = objective(best_m)

    for b in buy_grid:
        for s in sell_grid:
            thr = {
                "buy_min": b, "sell_max": s,
                "watch_up_min": 18, "watch_down_max": -18,
                "paper_buy_min": min(55.0, float(b)),
            }
            m = eval_rows(train_rows, thr)
            obj = objective(m)
            if obj > best_obj:
                best_obj, best_thr, best_m = obj, thr, m
    return best_thr, best_m


def month_key(asof: str) -> str:
    return asof[:7] if asof else "unknown"


def walk_forward(rows: list[dict]) -> dict:
    """Expanding window by calendar month."""
    by_month: dict[str, list] = defaultdict(list)
    for r in rows:
        by_month[month_key(r["asof"])].append(r)
    months = sorted(k for k in by_month if k != "unknown")
    if len(months) < 2:
        # fallback: 70/30 time split
        cut = max(1, int(len(rows) * 0.7))
        train, test = rows[:cut], rows[cut:]
        thr, train_m = grid_search_thresholds(train)
        test_m = eval_rows(test, thr)
        return {
            "mode": "time_split_70_30",
            "folds": [{
                "train_end": train[-1]["asof"] if train else None,
                "test": test[0]["asof"] if test else None,
                "thresholds": thr,
                "train": train_m,
                "test": test_m,
            }],
            "oos": test_m,
            "chosen_thresholds": thr,
        }

    folds = []
    oos_preds = []
    for i in range(1, len(months)):
        train_months = months[:i]
        test_month = months[i]
        train = [r for m in train_months for r in by_month[m]]
        test = by_month[test_month]
        if len(train) < 10 or len(test) < 3:
            continue
        thr, train_m = grid_search_thresholds(train)
        test_m = eval_rows(test, thr)
        folds.append({
            "train_months": train_months,
            "test_month": test_month,
            "thresholds": thr,
            "train": train_m,
            "test": test_m,
        })
        for r in test:
            d = decide_with_thresholds(r["features"], thr)
            oos_preds.append({
                "asof": r["asof"],
                "signal": d["signal"],
                "score": d["score"],
                "ret": r["outcome"].get("return_10d"),
                "fold": test_month,
            })

    # final thresholds: last fold's thr (most recent train) or re-fit on all but last month
    if folds:
        chosen = folds[-1]["thresholds"]
        # re-fit on all data except last month for production config
        last = months[-1]
        train_all = [r for m in months[:-1] for r in by_month[m]]
        if len(train_all) >= 10:
            chosen, _ = grid_search_thresholds(train_all)
    else:
        chosen, _ = grid_search_thresholds(rows)

    # aggregate OOS
    oos_buy = [p for p in oos_preds if p["signal"] == "BUY" and p["ret"] is not None]
    oos_sell = [p for p in oos_preds if p["signal"] == "SELL" and p["ret"] is not None]
    oos = {
        "n": len(oos_preds),
        "n_buy": len(oos_buy),
        "n_sell": len(oos_sell),
        "buy_precision_pct": (
            _safe_float(100 * sum(1 for p in oos_buy if p["ret"] > 0) / len(oos_buy), 1)
            if oos_buy else None
        ),
        "sell_precision_pct": (
            _safe_float(100 * sum(1 for p in oos_sell if p["ret"] < 0) / len(oos_sell), 1)
            if oos_sell else None
        ),
        "buy_avg_return_pct": _safe_float(100 * _mean([p["ret"] for p in oos_buy]), 2) if oos_buy else None,
        "sell_avg_return_pct": _safe_float(100 * _mean([p["ret"] for p in oos_sell]), 2) if oos_sell else None,
    }

    return {
        "mode": "expanding_month",
        "months": months,
        "folds": folds,
        "oos": oos,
        "chosen_thresholds": chosen,
    }


def _interp(x: float, points: list[list[float]]) -> float:
    """Piecewise linear; points sorted by x."""
    if not points:
        return 0.5
    pts = sorted(points, key=lambda p: p[0])
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        if x0 <= x <= x1:
            if x1 == x0:
                return y0
            t = (x - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return pts[-1][1]


def calibrate(rows: list[dict], thr: dict) -> dict:
    """score → P(up), confidence bins vs hit rate (BUY/SELL direction)."""
    scored = []
    for r in rows:
        d = decide_with_thresholds(r["features"], thr)
        ret = r["outcome"].get("return_10d")
        if ret is None:
            continue
        scored.append({"score": d["score"], "signal": d["signal"], "ret": float(ret), "up": float(ret) > 0})

    # score bins for P(up)
    edges = [-100, -50, -20, 0, 20, 50, 100]
    score_points = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        bucket = [s for s in scored if lo <= s["score"] < hi] if hi < 100 else [s for s in scored if lo <= s["score"] <= hi]
        if len(bucket) < 3:
            continue
        mid = (lo + hi) / 2
        p_up = sum(1 for s in bucket if s["up"]) / len(bucket)
        score_points.append([mid, round(p_up, 3)])

    # confidence proxy = 0.5 + |score|/200 ; bin vs actual directional hit for BUY/SELL only
    conf_bins = []
    for lo, hi in [(0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]:
        hits = total = 0
        for s in scored:
            if s["signal"] not in ("BUY", "SELL"):
                continue
            conf = min(0.95, 0.5 + abs(s["score"]) / 200)
            if not (lo <= conf < hi):
                continue
            total += 1
            if s["signal"] == "BUY" and s["ret"] > 0:
                hits += 1
            if s["signal"] == "SELL" and s["ret"] < 0:
                hits += 1
        if total:
            conf_bins.append({
                "lo": lo,
                "hi": hi if hi <= 1 else 1.0,
                "n": total,
                "hit_rate": round(hits / total, 3),
            })

    return {
        "score_to_p_up": score_points,
        "confidence_bins": conf_bins,
        "n_scored": len(scored),
    }


def empirical_horizons(rows: list[dict], thr: dict) -> dict:
    """시그널별 기대수익/낙폭 경험치 (10D 중심, 5/20도 있으면)."""
    buckets: dict[str, list] = defaultdict(list)
    for r in rows:
        d = decide_with_thresholds(r["features"], thr)
        buckets[d["signal"]].append(r["outcome"])

    out = {}
    for sig, outs in buckets.items():
        def avg(key):
            vals = [float(o[key]) for o in outs if o.get(key) is not None]
            return _safe_float(_mean(vals), 6) if vals else None

        out[sig] = {
            "n": len(outs),
            "expected_return": {
                "5d": avg("return_5d"),
                "10d": avg("return_10d"),
                "20d": avg("return_20d"),
            },
            "expected_drawdown": {
                "5d": avg("mae_5d"),
                "10d": avg("mae_10d"),
                "20d": avg("mae_20d"),
            },
            "mfe": {
                "5d": avg("mfe_5d"),
                "10d": avg("mfe_10d"),
                "20d": avg("mfe_20d"),
            },
        }
    return out


def train_engine(
    features: list,
    outcomes: list,
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> dict:
    rows = join_feature_outcomes(features, outcomes)
    wf = walk_forward(rows)
    thr = wf["chosen_thresholds"]
    cal = calibrate(rows, thr)
    emp = empirical_horizons(rows, thr)
    # ── Deploy safety (적응형): 데이터 많아질수록 BUY 해제 문턱 완화
    LLM_BUY_PREC = 28.6
    LLM_SELL_PREC = 64.1
    oos = wf.get("oos") or {}
    deploy_flags = {
        "buy_enabled": False,
        "sell_enabled": True,
        "reason": [],
        "daily_retrain": True,
    }
    oos_bp = oos.get("buy_precision_pct")
    oos_sp = oos.get("sell_precision_pct")
    oos_nb = oos.get("n_buy") or 0
    oos_ns = oos.get("n_sell") or 0
    oos_br = oos.get("buy_avg_return_pct")
    n_rows = len(rows)

    # 표본 규모에 따라 요구 Precision 상향폭 축소
    if n_rows >= 400:
        buy_lift = 2.0
        min_buy_n = 12
    elif n_rows >= 200:
        buy_lift = 3.0
        min_buy_n = 8
    else:
        buy_lift = 5.0
        min_buy_n = 5

    buy_ok = (
        oos_bp is not None
        and oos_nb >= min_buy_n
        and oos_bp >= LLM_BUY_PREC + buy_lift
        and (oos_br is None or oos_br > 0)
    )
    if buy_ok:
        deploy_flags["buy_enabled"] = True
        deploy_flags["reason"].append(
            f"OOS BUY OK prec={oos_bp}% avgR={oos_br}% n={oos_nb} "
            f"(need ≥{LLM_BUY_PREC + buy_lift}% , n≥{min_buy_n})"
        )
    else:
        # Paper BUY 품질이 매우 좋으면 제한적 완화
        paper_m = eval_rows(rows, thr)
        pp, pn, pr = (
            paper_m.get("paper_buy_precision_pct"),
            paper_m.get("n_paper_buy") or 0,
            paper_m.get("paper_buy_avg_return_pct"),
        )
        if pp is not None and pn >= 15 and pp >= 55 and (pr or 0) > 0:
            thr["buy_min"] = max(62.0, float(thr.get("buy_min", 75)) - 5)
            deploy_flags["reason"].append(
                f"Paper BUY strong (prec={pp}% n={pn} avgR={pr}%) → soft buy_min={thr['buy_min']}"
            )
        else:
            thr["buy_min"] = max(float(thr.get("buy_min", 58)), 75)
            deploy_flags["reason"].append(
                f"OOS BUY hold (prec={oos_bp}, n={oos_nb}, avgR={oos_br}) "
                f"need ≥{LLM_BUY_PREC + buy_lift}% n≥{min_buy_n} → buy_min={thr['buy_min']}"
            )

    if oos_sp is not None and oos_ns >= 5 and oos_sp + 3 < LLM_SELL_PREC:
        thr["sell_max"] = min(float(thr.get("sell_max", -38)), -48)
        deploy_flags["reason"].append(
            f"OOS SELL {oos_sp}% < LLM {LLM_SELL_PREC}% → tighter sell_max={thr['sell_max']}"
        )
    elif oos_sp is not None and oos_ns >= 5:
        deploy_flags["reason"].append(
            f"OOS SELL precision {oos_sp}% (n={oos_ns}) kept"
        )

    thr.setdefault("paper_buy_min", 55)
    in_sample = eval_rows(rows, thr)

    config = {
        "engine_version": "signal_engine_v2",
        "trained_at": datetime.utcnow().isoformat(),
        "n_rows": len(rows),
        "thresholds": thr,
        "deploy_flags": deploy_flags,
        "llm_baseline": {
            "buy_precision_10d": LLM_BUY_PREC,
            "sell_precision_10d": LLM_SELL_PREC,
        },
        "buy_quality_targets": {
            "min_oos_precision_pct": LLM_BUY_PREC + buy_lift,
            "min_oos_n": min_buy_n,
            "require_positive_avg_return": True,
        },
        "calibration": cal,
        "empirical": emp,
        "walk_forward": {
            "mode": wf.get("mode"),
            "months": wf.get("months"),
            "oos": wf.get("oos"),
            "n_folds": len(wf.get("folds") or []),
            "folds": [
                {
                    "test_month": f.get("test_month") or f.get("test"),
                    "thresholds": f.get("thresholds"),
                    "test": f.get("test"),
                }
                for f in (wf.get("folds") or [])
            ],
        },
        "in_sample": in_sample,
        "baseline_compare_note": (
            "매일 자동 재학습. BUY는 OOS Precision·양의 평균수익·최소 표본을 충족할 때만 활성화. "
            "미충족 시 buy_min을 높여 WATCH_UP(Abstention) 우선."
        ),
        "parent_score_engine": DEFAULT_ENGINE_VERSION,
    }

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2))
    return config


def load_engine_config(path: Path = DEFAULT_CONFIG_PATH) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def calibrated_p_up(score: float, config: Optional[dict]) -> float:
    import math
    raw = 1 / (1 + math.exp(-score / 25))
    if not config:
        return raw
    pts = (config.get("calibration") or {}).get("score_to_p_up") or []
    if len(pts) < 2:
        return raw
    return float(_interp(score, pts))


def calibrated_confidence(score: float, signal: str, config: Optional[dict]) -> float:
    raw = min(0.95, 0.5 + abs(score) / 200)
    if not config or signal not in ("BUY", "SELL"):
        return round(raw, 3)
    bins = (config.get("calibration") or {}).get("confidence_bins") or []
    for b in bins:
        if b["lo"] <= raw < b["hi"] or (b["hi"] >= 1 and raw >= b["lo"]):
            # map claimed confidence toward observed hit_rate (shrinkage)
            hr = b.get("hit_rate")
            if hr is None:
                break
            # partial shrink to avoid overfit on tiny n
            n = b.get("n") or 0
            w = min(1.0, n / 15)
            return round((1 - w) * raw + w * hr, 3)
    return round(raw, 3)
