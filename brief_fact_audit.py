"""시황 Phase 1 — 본문 수치 vs market_data(yfinance 수집) 팩트 검증."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

# market_brief.TICKERS와 동기화 — 티커·한글명·관용 별칭
_EXTRA_ALIASES: dict[str, tuple[str, ...]] = {
    "SPY": ("S&P500", "S&P 500", "S&P500", "SP500"),
    "RSP": ("S&P 동일가중", "동일가중"),
    "QQQ": ("NASDAQ100", "NASDAQ 100", "나스닥100", "나스닥 100", "NASDAQ"),
    "DIA": ("DOW", "다우", "다우존스", "Dow"),
    "IWM": ("러셀2000", "Russell 2000", "Russell2000"),
    "SMH": ("반도체", "반도체ETF", "반도체 ETF"),
    "XLK": ("기술", "기술섹터"),
    "XLF": ("금융", "금융섹터"),
    "XLE": ("에너지", "에너지섹터"),
    "XLV": ("헬스케어", "헬스케어섹터"),
    "^KS11": ("KOSPI", "코스피", "KS11"),
    "^KQ11": ("KOSDAQ", "코스닥", "KQ11"),
    "005930.KS": ("삼성전자", "삼성"),
    "000660.KS": ("SK하이닉스", "하이닉스", "SK hynix"),
    "^VIX": ("VIX", "공포지수"),
    "KRW=X": ("원달러", "원/달러", "USD/KRW", "USDKRW"),
    "DX-Y.NYB": ("DXY", "달러인덱스", "달러 인덱스"),
    "^TNX": ("10년물", "10년 국채", "10Y"),
    "2YY=F": ("2년물", "2년 국채", "2Y"),
    "BTC-USD": ("비트코인", "BTC"),
    "ETH-USD": ("이더리움", "ETH"),
    "IBIT": ("비트코인 ETF", "Bitcoin ETF"),
}

_PCT_NEAR = re.compile(
    r"([▼▲↑↓+\-−–—]?\s*\d+(?:\.\d+)?)\s*%",
    re.UNICODE,
)
_VOL_NEAR = re.compile(r"(?:거래량|RVOL|vol)\s*(\d+(?:\.\d+)?)\s*%", re.I)
_DATA_FAIL = re.compile(
    r"(데이터\s*(?:수집\s*)?(?:실패|없음|미수집)|수집\s*실패|미수집|확인\s*불가)",
    re.I,
)


def _is_finite(v) -> bool:
    try:
        if v is None or v != v:
            return False
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _parse_cited_pct(raw: str) -> float | None:
    if not raw:
        return None
    s = raw.strip()
    neg = any(c in s for c in ("▼", "↓", "-", "−", "–", "—"))
    pos = any(c in s for c in ("▲", "↑", "+"))
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if not m:
        return None
    val = float(m.group(1))
    if neg and not pos:
        return -abs(val)
    if pos and not neg:
        return abs(val)
    return val


def _build_aliases() -> dict[str, list[str]]:
    try:
        from market_brief import TICKERS
    except ImportError:
        TICKERS = {}

    out: dict[str, set[str]] = {}
    for region_data in TICKERS.values():
        for ticker, name in region_data.items():
            out.setdefault(ticker, set())
            out[ticker].add(ticker)
            if name:
                out[ticker].add(name)
    for ticker, extras in _EXTRA_ALIASES.items():
        out.setdefault(ticker, set())
        out[ticker].add(ticker)
        out[ticker].update(extras)

    # 긴 별칭 우선 (오매칭 방지)
    return {t: sorted(aliases, key=len, reverse=True) for t, aliases in out.items()}


def _flatten_market_data(market_data: dict | None) -> dict[str, dict]:
    flat: dict[str, dict] = {}
    for region_data in (market_data or {}).values():
        if not isinstance(region_data, dict):
            continue
        for ticker, d in region_data.items():
            if isinstance(d, dict):
                flat[ticker] = d
    return flat


def _find_pct_citations(text: str, alias: str) -> list[dict]:
    """별칭 등장 직후 window 내 % 인용 추출."""
    hits: list[dict] = []
    if not text or not alias:
        return hits
    try:
        pattern = re.compile(re.escape(alias), re.I)
    except re.error:
        return hits
    for m in pattern.finditer(text):
        window = text[m.end() : m.end() + 55]
        for pm in _PCT_NEAR.finditer(window):
            raw = pm.group(1)
            val = _parse_cited_pct(raw)
            if val is None:
                continue
            start = m.start()
            snippet = text[max(0, start - 8) : m.end() + pm.end()].replace("\n", " ")
            hits.append({"raw": raw + "%", "value": val, "snippet": snippet.strip()[:120]})
            break
    return hits


def _find_vol_citations(text: str, alias: str) -> list[dict]:
    hits: list[dict] = []
    try:
        pattern = re.compile(re.escape(alias), re.I)
    except re.error:
        return hits
    for m in pattern.finditer(text):
        window = text[m.end() : m.end() + 40]
        vm = _VOL_NEAR.search(window)
        if not vm:
            continue
        try:
            val = float(vm.group(1))
        except (TypeError, ValueError):
            continue
        snippet = text[max(0, m.start() - 8) : m.end() + vm.end()].replace("\n", " ")
        hits.append({"raw": vm.group(0), "value": val, "snippet": snippet.strip()[:120]})
    return hits


def _check_change_pct(
    ticker: str,
    label: str,
    expected: dict,
    citations: list[dict],
    *,
    tol: float,
) -> list[dict]:
    exp = expected.get("change_pct")
    if not _is_finite(exp) or not citations:
        return []
    exp_f = round(float(exp), 2)
    out: list[dict] = []
    for c in citations:
        cited = round(float(c["value"]), 2)
        diff = abs(cited - exp_f)
        ok = diff <= tol
        out.append({
            "ticker": ticker,
            "label": label,
            "field": "change_pct",
            "expected": exp_f,
            "cited": cited,
            "diff": round(diff, 2),
            "status": "pass" if ok else "fail",
            "snippet": c.get("snippet", ""),
            "message": (
                f"등락률 일치 ({exp_f:+.2f}%)"
                if ok
                else f"등락률 불일치 — DB {exp_f:+.2f}% vs 본문 {cited:+.2f}%"
            ),
        })
    return out


def _check_volume_ratio(
    ticker: str,
    label: str,
    expected: dict,
    citations: list[dict],
    *,
    tol: float = 8.0,
) -> list[dict]:
    exp = expected.get("volume_ratio")
    if not _is_finite(exp) or not citations:
        return []
    exp_f = round(float(exp), 1)
    out = []
    for c in citations:
        cited = round(float(c["value"]), 1)
        diff = abs(cited - exp_f)
        ok = diff <= tol
        out.append({
            "ticker": ticker,
            "label": label,
            "field": "volume_ratio",
            "expected": exp_f,
            "cited": cited,
            "diff": round(diff, 1),
            "status": "pass" if ok else "warn",
            "snippet": c.get("snippet", ""),
            "message": (
                f"거래량 비율 일치 ({exp_f}%)"
                if ok
                else f"거래량 비율 차이 — DB {exp_f}% vs 본문 {cited}%"
            ),
        })
    return out


def _check_stale_cited_as_fresh(
    ticker: str,
    label: str,
    expected: dict,
    citations: list[dict],
) -> dict | None:
    if not expected.get("stale") or not citations:
        return None
    return {
        "ticker": ticker,
        "label": label,
        "field": "stale",
        "expected": expected.get("last_date"),
        "cited": citations[0].get("raw"),
        "status": "warn",
        "snippet": citations[0].get("snippet", ""),
        "message": f"지연 데이터({expected.get('last_date')})인데 본문에서 확정 등락률로 인용",
    }


def _check_data_availability_claims(analysis: str, market_data: dict | None) -> list[dict]:
    """'미수집/수집 실패' 주장 vs 실제 market_data 보유."""
    if not analysis:
        return []
    checks: list[dict] = []
    flat = _flatten_market_data(market_data)

    region_keywords = {
        "미국": ("미국", "US", "S&P", "SPY", "NASDAQ", "나스닥", "다우"),
        "한국": ("한국", "KOSPI", "코스피", "KOSDAQ", "코스닥"),
    }
    for region, keys in region_keywords.items():
        block = (market_data or {}).get(region) or {}
        has_fresh = any(
            isinstance(d, dict) and _is_finite(d.get("change_pct")) and not d.get("stale")
            for d in block.values()
        )
        if not has_fresh:
            continue
        for kw in keys:
            idx = analysis.find(kw)
            if idx < 0:
                continue
            window = analysis[max(0, idx - 20) : idx + 80]
            if _DATA_FAIL.search(window):
                checks.append({
                    "ticker": region,
                    "label": region,
                    "field": "data_availability",
                    "expected": "collected",
                    "cited": "missing_claim",
                    "status": "warn",
                    "snippet": window.replace("\n", " ")[:120],
                    "message": f"{region} 데이터가 있는데 본문에 미수집/실패 서술",
                })
                break
    return checks


def audit_brief_facts(
    analysis: str,
    market_data: dict | None,
    *,
    change_tol: float = 0.25,
    vol_tol: float = 8.0,
) -> dict[str, Any]:
    """시황 본문 vs market_data 팩트 검증 결과."""
    text = analysis or ""
    flat = _flatten_market_data(market_data)
    aliases = _build_aliases()
    checks: list[dict] = []

    for ticker, alias_list in aliases.items():
        expected = flat.get(ticker)
        if not expected:
            continue

        pct_cites: list[dict] = []
        vol_cites: list[dict] = []
        for alias in alias_list:
            if alias.lower() not in text.lower() and alias not in text:
                continue
            pct_cites.extend(_find_pct_citations(text, alias))
            vol_cites.extend(_find_vol_citations(text, alias))

        label = expected.get("name") or ticker

        stale_warn = _check_stale_cited_as_fresh(ticker, label, expected, pct_cites)
        if stale_warn:
            checks.append(stale_warn)

        chg_results = _check_change_pct(
            ticker, label, expected, pct_cites, tol=change_tol
        )
        checks.extend(chg_results)

        checks.extend(
            _check_volume_ratio(ticker, label, expected, vol_cites, tol=vol_tol)
        )

    checks.extend(_check_data_availability_claims(text, market_data))

    pass_n = sum(1 for c in checks if c["status"] == "pass")
    fail_n = sum(1 for c in checks if c["status"] == "fail")
    warn_n = sum(1 for c in checks if c["status"] == "warn")

    if fail_n:
        status = "fail"
    elif warn_n:
        status = "warn"
    elif pass_n:
        status = "pass"
    else:
        status = "skip"

    fails = [c for c in checks if c["status"] == "fail"][:5]
    warns = [c for c in checks if c["status"] == "warn"][:3]

    return {
        "status": status,
        "pass_count": pass_n,
        "fail_count": fail_n,
        "warn_count": warn_n,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "summary": _audit_summary(status, pass_n, fail_n, warn_n, fails),
        "failures": fails,
        "warnings": warns,
        "checks": checks,
    }


def _audit_summary(
    status: str,
    pass_n: int,
    fail_n: int,
    warn_n: int,
    fails: list[dict],
) -> str:
    if status == "skip":
        return "본문에서 검증 가능한 지표 인용 없음"
    if status == "pass":
        return f"수치 {pass_n}건 yfinance·market_data와 일치"
    if status == "fail" and fails:
        first = fails[0]
        return f"불일치 {fail_n}건 — {first.get('label')}: {first.get('message', '')[:80]}"
    if warn_n:
        return f"주의 {warn_n}건 (지연 데이터·거래량·미수집 문구)"
    return "팩트 검증 완료"
