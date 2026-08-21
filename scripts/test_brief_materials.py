"""Mongo/Claude 없이 시황 입력 재료만 스모크 테스트."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv

load_dotenv(override=True)

from market_brief import (
    _AS_OF_LOCAL,
    _clocks_for_as_of,
    build_featured_context,
    collect_movers,
    format_macro_news_for_brief,
    get_market_data,
)
from news import fetch_macro_news


async def main(brief_type: str, as_of: str):
    now, now_et, now_kst = _clocks_for_as_of(brief_type, as_of)
    print(f"=== materials {brief_type} as_of={as_of} ===", flush=True)
    print(f"ET={now_et} KST={now_kst}", flush=True)

    market_data = get_market_data(now_et=now_et, now_kst=now_kst)
    movers = await collect_movers(now_et, now_kst)
    try:
        news = await asyncio.to_thread(fetch_macro_news, 4)
    except Exception as e:
        news = []
        print(f"[news] skip: {e}", flush=True)

    featured = build_featured_context(
        market_data, brief_type=brief_type, fear_greed=None, movers=movers
    )
    news_text = format_macro_news_for_brief(news)

    out = Path(f"data/test_materials_{brief_type}_{as_of}.md")
    out.parent.mkdir(exist_ok=True)
    body = (
        f"# materials {brief_type} {as_of}\n\n"
        f"## featured\n\n{featured}\n\n"
        f"## news (sample)\n\n{news_text[:4000]}\n"
    )
    out.write_text(body, encoding="utf-8")
    print(f"saved → {out}", flush=True)

    print("\n--- US gainers ---", flush=True)
    for t, d in movers.get("us_gainers") or []:
        print(f"  {t}: {d.get('change_pct')}% {d.get('name')}", flush=True)
    print("--- US losers ---", flush=True)
    for t, d in movers.get("us_losers") or []:
        print(f"  {t}: {d.get('change_pct')}% {d.get('name')}", flush=True)
    crypto = market_data.get("크립토") or {}
    print("--- crypto ---", flush=True)
    for t, d in crypto.items():
        print(f"  {t}: {d.get('change_pct')}%", flush=True)


if __name__ == "__main__":
    btype = sys.argv[1] if len(sys.argv) > 1 else "us_close"
    as_of = sys.argv[2] if len(sys.argv) > 2 else "2026-08-19"
    if btype not in _AS_OF_LOCAL:
        raise SystemExit(f"unknown type {btype}")
    asyncio.run(main(btype, as_of))
