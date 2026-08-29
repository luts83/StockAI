import yfinance as yf
import feedparser
import anthropic
import json
import os
import re
import time
from dotenv import load_dotenv
from typing import List, Dict

load_dotenv(override=True)


def _get_client():
    return anthropic.Anthropic()


def _claude_with_retry(client, max_retries=3, **kwargs):
    """Claude API 호출 + 과부하(529) 시 지수 백오프 재시도"""
    for attempt in range(max_retries):
        try:
            return client.messages.create(**kwargs)
        except Exception as e:
            err = str(e)
            if "529" in err or "overloaded" in err.lower():
                wait = 2 ** attempt  # 1s → 2s → 4s
                print(f"Claude 과부하(529), {wait}초 후 재시도 ({attempt+1}/{max_retries})...")
                time.sleep(wait)
                continue
            raise e
    print("Claude 최종 실패 — 원문 반환")
    return None


def fetch_news(ticker: str, translate: bool = True) -> List[Dict]:
    """yfinance 뉴스 + Google News RSS 수집.
    translate=False 이면 제목 한글 번역 생략(채팅 실시간 조회용)."""
    news_items = []

    # 1. yfinance 내장 뉴스
    try:
        stock = yf.Ticker(ticker)
        yf_news = stock.news or []
        for item in yf_news[:8]:
            content = item.get("content", {})
            news_items.append({
                "title":     content.get("title", item.get("title", "")),
                "summary":   content.get("summary", ""),
                "url":       content.get("canonicalUrl", {}).get("url", ""),
                "published": content.get("pubDate", ""),
                "source":    content.get("provider", {}).get("displayName", "Yahoo Finance"),
                "title_ko":  "",
            })
    except Exception as e:
        print(f"yfinance 뉴스 오류: {e}")

    # 2. Google News RSS
    try:
        rss_url = f"https://news.google.com/rss/search?q={ticker}+stock&hl=en-US&gl=US&ceid=US:en"
        feed = feedparser.parse(rss_url)
        for entry in feed.entries[:5]:
            news_items.append({
                "title":     entry.get("title", ""),
                "summary":   entry.get("summary", "")[:300],
                "url":       entry.get("link", ""),
                "published": entry.get("published", ""),
                "source":    "Google News",
                "title_ko":  "",
            })
    except Exception as e:
        print(f"Google News RSS 오류: {e}")

    # 중복 제거
    seen, unique = set(), []
    for item in news_items:
        t = item["title"]
        if t and t not in seen:
            seen.add(t)
            unique.append(item)

    unique = unique[:8]

    if translate:
        unique = translate_titles(unique)

    return unique


MACRO_RSS_SOURCES = [
    {
        "url": "https://feeds.reuters.com/reuters/businessNews",
        "source": "Reuters",
        "category": "매크로",
    },
    {
        "url": "https://news.google.com/rss/search?q=Federal+Reserve+interest+rate&hl=en-US&gl=US&ceid=US:en",
        "source": "Google News",
        "category": "연준/금리",
    },
    {
        "url": "https://news.google.com/rss/search?q=oil+price+WTI+crude&hl=en-US&gl=US&ceid=US:en",
        "source": "Google News",
        "category": "유가",
    },
    {
        "url": "https://news.google.com/rss/search?q=dollar+index+DXY+forex&hl=en-US&gl=US&ceid=US:en",
        "source": "Google News",
        "category": "달러/환율",
    },
    {
        "url": "https://news.google.com/rss/search?q=S%26P500+stock+market+today&hl=en-US&gl=US&ceid=US:en",
        "source": "Google News",
        "category": "증시",
    },
    {
        "url": "https://news.google.com/rss/search?q=KOSPI+Korea+stock+market&hl=en-US&gl=US&ceid=US:en",
        "source": "Google News",
        "category": "한국증시",
    },
    {
        "url": "https://news.google.com/rss/search?q=US+Treasury+buyback+bonds+yield&hl=en-US&gl=US&ceid=US:en",
        "source": "Google News",
        "category": "국채/금리",
    },
    {
        "url": "https://news.google.com/rss/search?q=bitcoin+crypto+market+today&hl=en-US&gl=US&ceid=US:en",
        "source": "Google News",
        "category": "크립토",
    },
    {
        "url": "https://news.google.com/rss/search?q=biotech+clinical+trial+FDA+stock&hl=en-US&gl=US&ceid=US:en",
        "source": "Google News",
        "category": "바이오/헬스케어",
    },
    {
        "url": "https://news.google.com/rss/search?q=semiconductor+chip+stock+NVIDIA&hl=en-US&gl=US&ceid=US:en",
        "source": "Google News",
        "category": "반도체",
    },
]


def fetch_macro_news(max_per_source: int = 5) -> List[Dict]:
    """
    시황용 매크로 뉴스 수집
    - 유가/연준/달러/증시/한국 관련 RSS 수집
    - 최근 24시간 이내 뉴스만 필터링
    - 제목 한글 번역 포함
    """
    from datetime import datetime, timezone, timedelta
    import email.utils

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    news_items = []

    for source_info in MACRO_RSS_SOURCES:
        try:
            feed = feedparser.parse(source_info["url"])
            count = 0
            for entry in feed.entries:
                if count >= max_per_source:
                    break

                published_str = entry.get("published", "")
                pub_dt = None
                if published_str:
                    try:
                        parsed = email.utils.parsedate(published_str)
                        if parsed:
                            pub_dt = datetime(*parsed[:6], tzinfo=timezone.utc)
                    except Exception:
                        pass

                if pub_dt and pub_dt < cutoff:
                    continue

                title = entry.get("title", "").strip()
                summary = re.sub(r"<[^>]+>", "", entry.get("summary", "") or "")
                summary = re.sub(r"\s+", " ", summary).strip()[:400]

                if not title:
                    continue

                news_items.append({
                    "title":     title,
                    "summary":   summary,
                    "url":       entry.get("link", ""),
                    "published": published_str,
                    "source":    source_info["source"],
                    "category":  source_info["category"],
                    "title_ko":  "",
                })
                count += 1

        except Exception as e:
            print(f"[macro_news] {source_info['category']} RSS 오류: {e}")

    seen, unique = set(), []
    for item in news_items:
        title = item["title"]
        if title and title not in seen:
            seen.add(title)
            unique.append(item)

    unique = translate_titles(unique)

    print(f"[macro_news] {len(unique)}개 매크로 뉴스 수집 완료")
    return unique


def format_macro_news_for_brief(news_items: List[Dict]) -> str:
    """
    시황 프롬프트용 뉴스 텍스트 포맷
    카테고리별로 묶어서 반환
    """
    if not news_items:
        return "매크로 뉴스 없음 (RSS 수집 실패 또는 최근 24시간 내 뉴스 없음)"

    by_category: dict = {}
    for item in news_items:
        cat = item.get("category", "기타")
        if cat not in by_category:
            by_category[cat] = []
        title = item.get("title_ko") or item.get("title", "")
        summary = (item.get("summary") or "").strip()
        line = f"  - {title}"
        if summary and len(summary) > 20:
            line += f"\n    요약: {summary[:200]}"
        by_category[cat].append(line)

    lines = [
        "[오늘의 촉매 뉴스 — 아래 제목·요약만 근거로 사용, 없는 내용 창작 금지]",
        "작성 시: 촉매 → 연결된 지수/섹터/특징주 순으로 ###1 흐름 섹션에 반영",
    ]
    for cat, entries in by_category.items():
        lines.append(f"\n[{cat}]")
        lines.extend(entries[:3])

    return "\n".join(lines)


def translate_titles(items: List[Dict], max_translate: int = 12) -> List[Dict]:
    """Claude API로 뉴스 제목 일괄 한국어 번역 — 카테고리 골고루, 상한 max_translate."""
    if not items:
        return items
    if not os.getenv("ANTHROPIC_API_KEY"):
        return items

    # 카테고리별 라운드로빈으로 번역 대상 선정 (Haiku 토큰 절감, 촉매 다양성 유지)
    by_cat: dict[str, list[int]] = {}
    for i, item in enumerate(items):
        by_cat.setdefault(item.get("category", "기타"), []).append(i)
    picked: list[int] = []
    cats = list(by_cat.keys())
    ci = 0
    while len(picked) < min(max_translate, len(items)) and cats:
        cat = cats[ci % len(cats)]
        for idx in by_cat[cat]:
            if idx not in picked:
                picked.append(idx)
                break
        ci += 1
        if ci > len(cats) * max(max_translate, 1) + 5:
            break

    subset = [items[i] for i in picked]
    titles = [item["title"] for item in subset]
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles))

    prompt = f"""다음 영문 뉴스 제목들을 한국어로 번역해줘.
규칙:
- 핵심 내용만 담아 간결하게 (25자 이내 권장)
- 주식/금융 용어는 전문용어 그대로 사용 (예: 매출, 어닝, 분기)
- 회사명/인명은 한글 발음으로 (예: Tesla→테슬라, Apple→애플)
- 숫자/% 그대로 유지
- 번호를 붙여서 한 줄에 하나씩 응답 (다른 설명 없이)

입력:
{numbered}

출력 형식 (반드시 이 형식만):
1. 번역된 제목1
2. 번역된 제목2
..."""

    try:
        client = _get_client()
        msg = _claude_with_retry(
            client,
            model="claude-haiku-4-5-20251001",
            max_tokens=min(600, 40 * len(subset) + 80),
            messages=[{"role": "user", "content": prompt}]
        )
        if msg is None:
            return items  # 재시도 모두 실패 → 원문 그대로
        raw = msg.content[0].text.strip()
        translated = []
        for line in raw.splitlines():
            m = re.match(r"^\d+\.\s*(.+)$", line.strip())
            if m:
                translated.append(m.group(1).strip())
        for j, idx in enumerate(picked):
            if j < len(translated) and translated[j]:
                items[idx]["title_ko"] = translated[j]
    except Exception as e:
        print(f"제목 번역 오류: {e}")

    return items
