"""
Instagram card generator — Playwright HTML→PNG
한글/이모지 완전 지원, Google Fonts (Noto Sans KR + Space Grotesk)
5장 카드: 커버 / 차트 / 트렌드분석 / 시나리오 / 종합
"""
import asyncio
import io
import os
import re
import base64
import textwrap
from datetime import datetime

_HANDLE = os.getenv("INSTAGRAM_HANDLE", "@stockai_kr")
WATERMARK = f"StockAI · {_HANDLE}"

# ── 팔레트 ────────────────────────────────────────────────
BG     = "#0a0e14"
BG2    = "#0d1117"
CARD   = "#161b22"
BORDER = "#21262d"
ACCENT = "#4d9fff"
GREEN  = "#34d399"
RED    = "#f87171"
YELLOW = "#fbbf24"
MUTED  = "#8b949e"
WHITE  = "#e6edf3"

FONTS = "@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@300;400;500;700;900&family=Space+Grotesk:wght@400;500;600;700;800&display=swap');"
RESET = "* { margin:0; padding:0; box-sizing:border-box; } body { font-family:'Noto Sans KR','Space Grotesk',-apple-system,sans-serif; -webkit-font-smoothing:antialiased; overflow:hidden; }"


# ── 헬퍼 ─────────────────────────────────────────────────

def _rgba(hex_color: str, alpha: float = 0.15) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def _signal_info(signal: str):
    s = (signal or "WATCH").upper()
    if s == "BUY":  return "🟢 매수 검토 구간", GREEN
    if s == "SELL": return "🔴 매수 자제 구간", RED
    return "👀 관망 구간", YELLOW


def _clean(t: str) -> str:
    return re.sub(r"[*_`#>]", "", str(t)).strip()


def _extract_section(text: str, keywords: list, max_lines: int = 4) -> list:
    lines, result, capturing = text.split("\n"), [], False
    for line in lines:
        s = line.strip().lstrip("#*").strip()
        if not s:
            continue
        if any(kw in s for kw in keywords):
            capturing = True
            continue
        if capturing:
            raw = line.strip()
            if raw.startswith("##") or (raw.startswith("**") and raw.endswith("**") and len(raw) < 40):
                break
            c = _clean(s)
            if c and len(c) > 4:
                result.append(c)
            if len(result) >= max_lines:
                break
    return result


def _ul(lines: list, color: str = ACCENT, size: int = 26) -> str:
    if not lines:
        return f'<p style="color:{MUTED};font-size:{size}px">데이터 없음</p>'
    items = "".join(
        f'<div style="display:flex;gap:10px;margin-bottom:10px;align-items:flex-start">'
        f'<span style="color:{color};flex-shrink:0;font-size:{size}px">•</span>'
        f'<span style="color:{WHITE};font-size:{size}px;line-height:1.55">{_clean(l)[:64]}</span>'
        f'</div>'
        for l in lines[:4] if _clean(l)
    )
    return items or f'<p style="color:{MUTED};font-size:{size}px">데이터 없음</p>'


def _section_card(icon: str, title: str, body_html: str,
                  border_color: str = ACCENT) -> str:
    return f"""
    <div style="background:{CARD};border:1px solid {border_color}33;
                border-left:3px solid {border_color};border-radius:12px;
                padding:32px 36px;margin-bottom:24px">
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:18px">
        <span style="font-size:26px">{icon}</span>
        <span style="font-family:'Space Grotesk',sans-serif;font-size:22px;
                     font-weight:700;color:{border_color}">{title}</span>
      </div>
      {body_html}
    </div>"""


def _header(ticker: str, date_str: str, subtitle: str = "") -> str:
    return f"""
    <div style="display:flex;justify-content:space-between;align-items:center;
                padding:30px 60px;border-bottom:1px solid {BORDER};
                background:{CARD}">
      <div style="display:flex;align-items:center;gap:14px">
        <span style="font-size:24px">📈</span>
        <span style="font-family:'Space Grotesk',sans-serif;font-size:28px;
                     font-weight:800;color:{ACCENT}">{ticker}</span>
        {f'<span style="font-size:20px;color:{MUTED};margin-left:4px">{subtitle}</span>' if subtitle else ''}
      </div>
      <span style="font-family:'Space Grotesk',sans-serif;font-size:20px;color:{MUTED}">{date_str}</span>
    </div>
    <div style="height:2px;background:linear-gradient(90deg,transparent,{ACCENT}55,transparent)"></div>"""


def _footer(small_text: str = "") -> str:
    return f"""
    <div style="border-top:1px solid {BORDER};padding:20px 60px;
                display:flex;justify-content:space-between;align-items:center;
                background:{CARD}">
      <span style="font-size:16px;color:{MUTED}">{small_text}</span>
      <span style="font-family:'Space Grotesk',sans-serif;font-size:18px;color:{MUTED}">{WATERMARK}</span>
    </div>"""


def _wrap_html(content: str, width: int, height: int) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
{FONTS}
{RESET}
body {{ width:{width}px; height:{height}px; background:{BG};
        color:{WHITE}; display:flex; flex-direction:column; }}
</style></head><body>
{content}
</body></html>"""


# ── Playwright 스크린샷 ───────────────────────────────────

async def _screenshot(html: str, width: int, height: int) -> bytes:
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage", "--disable-gpu"]
        )
        page = await browser.new_page(viewport={"width": width, "height": height})
        await page.set_content(html, wait_until="networkidle")
        # 폰트 완전 로드 대기
        await page.evaluate("document.fonts.ready")
        data = await page.screenshot(
            clip={"x": 0, "y": 0, "width": width, "height": height}
        )
        await browser.close()
        return data


# ── 카드 1: 커버 (1080×1080) ─────────────────────────────

def _html_cover(ticker: str, signal: str, indicators: dict, created_at: str) -> str:
    sig_text, sig_color = _signal_info(signal)
    ma20 = indicators.get("ma20")
    rsi  = indicators.get("rsi")
    macd = indicators.get("macd", 0) or 0
    macd_sig = indicators.get("macd_signal", 0) or 0
    ma60  = indicators.get("ma60")
    ma200 = indicators.get("ma200")

    price_str = f"${ma20:,.2f}" if ma20 else "—"
    date_str  = (created_at or "")[:10] or datetime.now().strftime("%Y-%m-%d")
    rsi_col   = RED if (rsi or 50) > 70 else GREEN if (rsi or 50) < 30 else MUTED
    macd_bull = macd > macd_sig

    content = f"""
    <!-- 상단 로고바 -->
    <div style="display:flex;justify-content:space-between;align-items:center;
                padding:36px 64px;background:{CARD};border-bottom:1px solid {BORDER}">
      <div style="display:flex;align-items:center;gap:12px">
        <span style="font-size:28px">📈</span>
        <span style="font-family:'Space Grotesk',sans-serif;font-size:28px;
                     font-weight:800;color:{ACCENT}">StockAI</span>
      </div>
      <span style="font-family:'Space Grotesk',sans-serif;font-size:22px;color:{MUTED}">{date_str}</span>
    </div>
    <div style="height:2px;background:linear-gradient(90deg,transparent,{ACCENT}88,transparent)"></div>

    <!-- 메인 콘텐츠 -->
    <div style="flex:1;display:flex;flex-direction:column;align-items:center;
                justify-content:center;padding:40px 64px;gap:28px;
                background:linear-gradient(160deg,{BG} 0%,#1a2030 100%)">

      <!-- 티커 -->
      <div style="font-family:'Space Grotesk',sans-serif;font-size:168px;font-weight:800;
                  color:{WHITE};letter-spacing:-8px;line-height:1">{ticker}</div>

      <!-- 가격 -->
      <div style="text-align:center">
        <div style="font-family:'Space Grotesk',sans-serif;font-size:60px;font-weight:700;
                    color:{ACCENT};letter-spacing:-2px">{price_str}</div>
        <div style="font-size:18px;color:{MUTED};margin-top:6px">기준가 (MA20)</div>
      </div>

      <!-- 구분선 -->
      <div style="width:100px;height:2px;background:{BORDER};border-radius:1px"></div>

      <!-- RSI / MACD -->
      <div style="display:flex;gap:56px;align-items:center">
        <span style="font-family:'Space Grotesk',sans-serif;font-size:24px;
                     font-weight:600;color:{rsi_col}">
          RSI&nbsp;&nbsp;{f"{rsi:.1f}" if rsi else "—"}
        </span>
        <span style="width:1px;height:32px;background:{BORDER}"></span>
        <span style="font-family:'Space Grotesk',sans-serif;font-size:24px;
                     font-weight:600;color:{GREEN if macd_bull else RED}">
          MACD&nbsp;&nbsp;{"▲ 강세" if macd_bull else "▼ 약세"}
        </span>
      </div>

      <!-- 시그널 배지 -->
      <div style="padding:18px 56px;border-radius:60px;
                  background:{_rgba(sig_color, 0.18)};
                  border:2px solid {sig_color};
                  font-size:30px;font-weight:700;color:{sig_color}">
        {sig_text}
      </div>

      <!-- MA 요약 -->
      <div style="display:flex;gap:32px;flex-wrap:wrap;justify-content:center">
        <span style="font-size:18px;color:{MUTED}">MA60&nbsp;
          <span style="color:{WHITE};font-family:'Space Grotesk',sans-serif">
            ${f"{ma60:,.0f}" if ma60 else "—"}
          </span>
        </span>
        <span style="font-size:18px;color:{MUTED}">MA200&nbsp;
          <span style="color:{YELLOW};font-family:'Space Grotesk',sans-serif">
            ${f"{ma200:,.0f}" if ma200 else "—"}
          </span>
        </span>
      </div>
    </div>

    <!-- 하단 워터마크 -->
    <div style="height:2px;background:linear-gradient(90deg,transparent,{ACCENT}44,transparent)"></div>
    <div style="display:flex;justify-content:space-between;align-items:center;
                padding:24px 64px;background:{CARD};border-top:1px solid {BORDER}">
      <span style="font-size:18px;color:{MUTED}">캔들스틱 · 볼린저밴드 · RSI · MACD · MA</span>
      <span style="font-family:'Space Grotesk',sans-serif;font-size:20px;color:{MUTED}">{WATERMARK}</span>
    </div>"""

    return _wrap_html(content, 1080, 1080)


# ── 카드 2: 차트 (1080×1350) ─────────────────────────────

def _html_chart(ticker: str, chart_b64: str, indicators: dict, created_at: str) -> str:
    date_str = (created_at or "")[:10] or datetime.now().strftime("%Y-%m-%d")
    rsi      = indicators.get("rsi")
    macd     = indicators.get("macd", 0) or 0
    macd_sig = indicators.get("macd_signal", 0) or 0
    ma20     = indicators.get("ma20")
    ma200    = indicators.get("ma200")
    rsi_col  = RED if (rsi or 50) > 70 else GREEN if (rsi or 50) < 30 else MUTED
    rsi_lbl  = "과매수" if (rsi or 50) > 70 else "과매도" if (rsi or 50) < 30 else "중립"
    macd_bull = macd > macd_sig

    chart_src = f"data:image/png;base64,{chart_b64}" if chart_b64 else ""

    cells = [
        ("RSI 14",  f"{rsi:.1f}" if rsi else "—",           rsi_lbl,    rsi_col),
        ("MACD",    "▲ 강세" if macd_bull else "▼ 약세",     "모멘텀",   GREEN if macd_bull else RED),
        ("MA 20",   f"${ma20:,.0f}" if ma20 else "—",        "단기 추세", ACCENT),
        ("MA 200",  f"${ma200:,.0f}" if ma200 else "—",      "장기 추세", YELLOW),
    ]
    grid_html = "".join(f"""
      <div style="background:{BG};border-radius:10px;padding:24px 20px;text-align:center;flex:1">
        <div style="font-size:16px;color:{MUTED};margin-bottom:10px">{lbl}</div>
        <div style="font-family:'Space Grotesk',sans-serif;font-size:30px;
                    font-weight:700;color:{col}">{val}</div>
        <div style="font-size:14px;color:{MUTED};margin-top:6px">{sub}</div>
      </div>""" for lbl, val, sub, col in cells)

    content = f"""
    {_header(ticker, date_str, "기술적 차트")}

    <!-- 차트 이미지 -->
    <div style="flex:1;overflow:hidden;background:{BG2};padding:8px">
      {'<img src="' + chart_src + '" style="width:100%;height:100%;object-fit:contain">' if chart_src else f'<div style="display:flex;align-items:center;justify-content:center;height:100%;color:{MUTED};font-size:24px">차트 없음</div>'}
    </div>

    <!-- 지표 그리드 -->
    <div style="display:flex;gap:12px;padding:20px 24px;background:{CARD};
                border-top:1px solid {BORDER}">
      {grid_html}
    </div>

    {_footer("AI 기술적 분석")}"""

    return _wrap_html(content, 1080, 1350)


# ── 카드 3: 트렌드 + 지표 분석 (1080×1350) ──────────────

def _html_analysis(ticker: str, analysis: str, signal: str, created_at: str) -> str:
    date_str = (created_at or "")[:10] or datetime.now().strftime("%Y-%m-%d")
    _, sig_color = _signal_info(signal)

    clean = re.sub(r"\nSIGNAL:(BUY|WATCH|SELL)\s*$", "", analysis,
                   flags=re.MULTILINE).strip()

    trend  = _extract_section(clean, ["트렌드", "추세", "전반적", "종합 분석"], 3)
    if not trend:
        trend = [_clean(l) for l in clean.split("\n")
                 if l.strip() and not l.strip().startswith("#")][:3]

    sr     = _extract_section(clean, ["지지", "저항", "Support", "Resistance"], 4)
    if not sr:
        sr = [_clean(l) for l in clean.split("\n") if re.search(r"\$[\d,]+", l)][:3]

    news   = _extract_section(clean, ["뉴스", "이슈", "News", "catalyst", "모멘텀"], 3)

    content = f"""
    {_header(ticker, date_str, "트렌드 분석")}

    <div style="flex:1;overflow:hidden;padding:28px 40px;display:flex;flex-direction:column;gap:0">
      {_section_card("📈", "전반적 추세", _ul(trend, ACCENT, 26), ACCENT)}
      {_section_card("🎯", "지지 · 저항선", _ul(sr, YELLOW, 26), YELLOW)}
      {_section_card("📰", "핵심 이슈 / 모멘텀",
                     _ul(news, MUTED, 24) if news else f'<p style="color:{MUTED};font-size:24px">최근 주요 이슈 없음</p>',
                     MUTED)}
    </div>

    {_footer("기술적 분석 리포트")}"""

    return _wrap_html(content, 1080, 1350)


# ── 카드 4: 시나리오 (1080×1350) ─────────────────────────

def _html_scenarios(ticker: str, analysis: str, signal: str, created_at: str) -> str:
    date_str = (created_at or "")[:10] or datetime.now().strftime("%Y-%m-%d")
    sig_text, sig_color = _signal_info(signal)

    clean = re.sub(r"\nSIGNAL:(BUY|WATCH|SELL)\s*$", "", analysis,
                   flags=re.MULTILINE).strip()

    bull = _extract_section(clean,
        ["강세 시나리오", "상승 시나리오", "Bullish", "매수 조건", "상승 조건", "강세"], 4)
    bear = _extract_section(clean,
        ["약세 시나리오", "하락 시나리오", "Bearish", "매도 조건", "하락 조건", "약세"], 4)

    sig = signal.upper() if signal else "WATCH"
    bull_pct = 65 if sig == "BUY" else 35 if sig == "SELL" else 50
    bear_pct = 100 - bull_pct

    def progress_bar(pct: int, color: str) -> str:
        return f"""
        <div style="background:{BORDER};border-radius:4px;height:10px;margin-bottom:16px;overflow:hidden">
          <div style="width:{pct}%;height:100%;background:{color};border-radius:4px;
                      transition:width 0.3s"></div>
        </div>
        <div style="font-family:'Space Grotesk',sans-serif;font-size:22px;
                    font-weight:700;color:{color};margin-bottom:16px">{pct}%</div>"""

    content = f"""
    {_header(ticker, date_str, "매수/매도 시나리오")}

    <!-- 시그널 배지 -->
    <div style="display:flex;justify-content:center;padding:20px 40px;
                background:{_rgba(sig_color, 0.12)};border-bottom:1px solid {BORDER}">
      <div style="padding:12px 44px;border-radius:50px;background:{_rgba(sig_color, 0.2)};
                  border:2px solid {sig_color};font-size:26px;font-weight:700;color:{sig_color}">
        {sig_text}
      </div>
    </div>

    <div style="flex:1;overflow:hidden;padding:24px 40px;display:flex;flex-direction:column;gap:16px">

      <!-- 강세 시나리오 -->
      <div style="flex:1;background:{CARD};border:1px solid {GREEN}33;
                  border-left:4px solid {GREEN};border-radius:14px;padding:28px 32px">
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">
          <span style="font-size:26px">🟢</span>
          <span style="font-family:'Space Grotesk',sans-serif;font-size:24px;
                       font-weight:700;color:{GREEN}">강세 시나리오</span>
        </div>
        {progress_bar(bull_pct, GREEN)}
        {_ul(bull or ["데이터 없음"], GREEN, 24)}
      </div>

      <!-- 약세 시나리오 -->
      <div style="flex:1;background:{CARD};border:1px solid {RED}33;
                  border-left:4px solid {RED};border-radius:14px;padding:28px 32px">
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">
          <span style="font-size:26px">🔴</span>
          <span style="font-family:'Space Grotesk',sans-serif;font-size:24px;
                       font-weight:700;color:{RED}">약세 시나리오</span>
        </div>
        {progress_bar(bear_pct, RED)}
        {_ul(bear or ["데이터 없음"], RED, 24)}
      </div>
    </div>

    {_footer("시나리오 분석")}"""

    return _wrap_html(content, 1080, 1350)


# ── 카드 5: 종합 (1080×1080) ─────────────────────────────

def _html_summary(ticker: str, analysis: str, signal: str,
                  indicators: dict, created_at: str) -> str:
    date_str = (created_at or "")[:10] or datetime.now().strftime("%Y-%m-%d")
    sig_text, sig_color = _signal_info(signal)

    clean = re.sub(r"\nSIGNAL:(BUY|WATCH|SELL)\s*$", "", analysis,
                   flags=re.MULTILINE).strip()

    summary = _extract_section(clean, ["종합 의견", "종합", "결론", "총평", "Conclusion"], 2)
    if not summary:
        paras = [p.strip() for p in clean.split("\n\n") if p.strip()]
        last = _clean(paras[-1]) if paras else _clean(clean)
        summary = textwrap.wrap(last[:100], 38)[:2]

    cons  = _extract_section(clean, ["보수적", "Conservative", "장기 투자", "안정"], 2)
    aggr  = _extract_section(clean, ["공격적", "Aggressive", "단기", "단기 투자"], 2)
    check = _extract_section(clean, ["모니터링", "주목", "확인", "주시", "핵심"], 3)

    rsi       = indicators.get("rsi")
    ma20      = indicators.get("ma20")
    macd      = indicators.get("macd", 0) or 0
    macd_s    = indicators.get("macd_signal", 0) or 0
    macd_bull = macd > macd_s

    if not check:
        check = []
        if rsi:
            check.append(f"RSI {rsi:.0f} — {'과매수 주의' if rsi > 70 else '과매도 반등 가능' if rsi < 30 else '중립 구간'}")
        check.append("MACD " + ("골든크로스 → 상승 모멘텀" if macd_bull else "데드크로스 → 하락 주의"))
        if ma20:
            check.append(f"MA20(${ma20:,.0f}) 지지 여부 확인")

    strategy_html = ""
    if cons or aggr:
        strategy_html = f"""
        <div style="display:flex;gap:14px;margin-bottom:0">
          <div style="flex:1;background:{BG};border-radius:10px;padding:18px 20px">
            <div style="font-size:16px;color:{YELLOW};font-weight:700;margin-bottom:10px">🛡 보수적 투자자</div>
            {_ul(cons or ["데이터 없음"], YELLOW, 20)}
          </div>
          <div style="flex:1;background:{BG};border-radius:10px;padding:18px 20px">
            <div style="font-size:16px;color:{GREEN};font-weight:700;margin-bottom:10px">⚡ 공격적 투자자</div>
            {_ul(aggr or ["데이터 없음"], GREEN, 20)}
          </div>
        </div>"""
    else:
        strategy_html = _ul(check[:2], ACCENT, 24)

    content = f"""
    {_header(ticker, date_str, "종합 의견")}

    <!-- 시그널 -->
    <div style="display:flex;justify-content:center;align-items:center;padding:20px 40px;
                background:{_rgba(sig_color, 0.12)};border-bottom:1px solid {BORDER}">
      <div style="padding:14px 50px;border-radius:50px;border:2px solid {sig_color};
                  background:{_rgba(sig_color, 0.2)};
                  font-size:28px;font-weight:700;color:{sig_color}">{sig_text}</div>
    </div>

    <div style="flex:1;overflow:hidden;padding:24px 40px;display:flex;flex-direction:column;gap:16px">

      <!-- 종합 의견 -->
      <div style="background:{CARD};border-left:4px solid {ACCENT};border-radius:0 12px 12px 0;padding:24px 28px">
        <div style="font-size:18px;color:{ACCENT};font-weight:700;margin-bottom:12px">💬 종합 의견</div>
        {"".join(f'<p style="font-size:24px;color:{WHITE};line-height:1.6;margin-bottom:6px">{_clean(l)[:64]}</p>' for l in summary)}
      </div>

      <!-- 투자자 전략 -->
      <div style="background:{CARD};border-radius:12px;padding:22px 28px">
        <div style="font-size:18px;color:{ACCENT};font-weight:700;margin-bottom:14px">👤 투자자 유형별 전략</div>
        {strategy_html}
      </div>

      <!-- 핵심 체크포인트 -->
      <div style="background:{CARD};border-radius:12px;padding:22px 28px">
        <div style="font-size:18px;color:{ACCENT};font-weight:700;margin-bottom:14px">🔍 핵심 체크포인트</div>
        {"".join(f'<div style="display:flex;gap:10px;margin-bottom:8px"><span style="color:{ACCENT};font-size:20px;font-weight:700;min-width:24px">{i+1}.</span><span style="font-size:20px;color:{WHITE};line-height:1.5">{_clean(pt)[:58]}</span></div>' for i, pt in enumerate(check[:3]))}
      </div>

      <!-- 해시태그 -->
      <div style="text-align:center;padding:8px 0">
        <span style="font-size:18px;color:{MUTED}">#StockAI &nbsp; #기술적분석 &nbsp; #주식 &nbsp; #{ticker}</span>
      </div>
    </div>

    {_footer("AI 분석 리포트")}"""

    return _wrap_html(content, 1080, 1080)


# ── 공개 API ─────────────────────────────────────────────

async def generate_cards(doc: dict) -> list:
    """
    MongoDB 분석 doc → 5장 카드 PNG 생성
    Returns: [(filename, bytes), ...]
    """
    ticker     = doc.get("ticker", "TICKER")
    signal     = doc.get("signal", "WATCH")
    indicators = doc.get("indicators", {})
    analysis   = doc.get("analysis", "")
    chart_b64  = doc.get("chart_b64", "")
    created_at = doc.get("created_at", datetime.now().isoformat())

    cards_html = [
        (f"{ticker}_1_cover.png",    _html_cover(ticker, signal, indicators, created_at),    1080, 1080),
        (f"{ticker}_2_chart.png",    _html_chart(ticker, chart_b64, indicators, created_at), 1080, 1350),
        (f"{ticker}_3_analysis.png", _html_analysis(ticker, analysis, signal, created_at),   1080, 1350),
        (f"{ticker}_4_scenarios.png",_html_scenarios(ticker, analysis, signal, created_at),  1080, 1350),
        (f"{ticker}_5_summary.png",  _html_summary(ticker, analysis, signal, indicators, created_at), 1080, 1080),
    ]

    from playwright.async_api import async_playwright
    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage", "--disable-gpu"]
        )
        for filename, html, w, h in cards_html:
            page = await browser.new_page(viewport={"width": w, "height": h})
            await page.set_content(html, wait_until="networkidle")
            await page.evaluate("document.fonts.ready")
            screenshot = await page.screenshot(
                clip={"x": 0, "y": 0, "width": w, "height": h}
            )
            await page.close()
            results.append((filename, screenshot))
        await browser.close()

    return results
