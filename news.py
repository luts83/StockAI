import yfinance as yf
import feedparser
import anthropic
import json
import os
import re
from dotenv import load_dotenv
from typing import List, Dict

load_dotenv(override=True)


def _get_client():
    return anthropic.Anthropic()


def fetch_news(ticker: str) -> List[Dict]:
    """yfinance 뉴스 + Google News RSS 수집"""
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

    # Claude로 제목 일괄 한글 번역
    unique = translate_titles(unique)

    return unique


def translate_titles(items: List[Dict]) -> List[Dict]:
    """Claude API로 뉴스 제목들 일괄 한국어 번역 (핵심만, 간결하게)"""
    if not items:
        return items
    if not os.getenv("ANTHROPIC_API_KEY"):
        return items

    titles = [item["title"] for item in items]
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
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        # "숫자. 번역" 형태로 한 줄씩 파싱 (특수문자에도 안전)
        translated = []
        for line in raw.splitlines():
            m = re.match(r"^\d+\.\s*(.+)$", line.strip())
            if m:
                translated.append(m.group(1).strip())
        for i, item in enumerate(items):
            if i < len(translated) and translated[i]:
                item["title_ko"] = translated[i]
    except Exception as e:
        print(f"제목 번역 오류: {e}")

    return items
