"""로컬 시황 흐름형 생성 스모크 테스트 (Mongo 저장 없음)."""
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv(override=True)

from market_brief import generate_market_brief


async def run(brief_type: str, as_of: str):
    print(f"=== generating {brief_type} as_of={as_of} ===", flush=True)
    brief = await generate_market_brief(brief_type, as_of=as_of)
    analysis = brief.get("analysis") or ""
    out = Path(f"data/test_{brief_type}_{as_of}.md")
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        f"# SIGNAL: {brief.get('signal')}\n# date: {brief.get('date')}\n"
        f"# type: {brief.get('type')}\n\n{analysis}\n",
        encoding="utf-8",
    )
    print(f"saved → {out} ({len(analysis)} chars)", flush=True)
    print(f"SIGNAL={brief.get('signal')} date={brief.get('date')}", flush=True)

    checks = {
        "###1 흐름": bool(re.search(r"###\s*1\.", analysis)),
        "특징주 섹션": "특징주" in analysis,
        "대응": "대응" in analysis,
        "크립토": bool(re.search(r"BTC|ETH|IBIT|비트코인|이더|코인", analysis, re.I)),
        "반도체 축": bool(re.search(r"SMH|삼성|하이닉스|반도체|NVDA", analysis)),
        "전망:": "전망:" in analysis[:1500],
    }
    for k, v in checks.items():
        print(f"  [{'OK' if v else 'MISS'}] {k}", flush=True)

    m = re.search(r"###\s*1\.[\s\S]{0,1600}", analysis)
    if m:
        print("\n--- ###1 preview ---\n", flush=True)
        print(m.group(0)[:1600], flush=True)
    return brief


if __name__ == "__main__":
    btype = sys.argv[1] if len(sys.argv) > 1 else "us_close"
    as_of = sys.argv[2] if len(sys.argv) > 2 else "2026-08-19"
    asyncio.run(run(btype, as_of))
