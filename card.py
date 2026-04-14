"""
Instagram card generator — Playwright HTML→PNG
한글/이모지 완전 지원, Google Fonts (Noto Sans KR + Space Grotesk)
5장 카드: 커버 / 차트 / 트렌드분석 / 시나리오 / 종합
모든 카드 1080×1350 통일
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

W, H = 1080, 1350  # 전 카드 통일 규격

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


def _ul(lines: list, color: str = ACCENT, size: int = 22,
        max_chars: int = 80, max_items: int = 5) -> str:
    if not lines:
        return f'<p style="color:{MUTED};font-size:{size}px">데이터 없음</p>'
    items = "".join(
        f'<div style="display:flex;gap:12px;margin-bottom:14px;align-items:flex-start">'
        f'<span style="color:{color};flex-shrink:0;font-size:{size}px;margin-top:2px">•</span>'
        f'<span style="color:{WHITE};font-size:{size}px;line-height:1.8">{_clean(l)[:max_chars]}</span>'
        f'</div>'
        for l in lines[:max_items] if _clean(l)
    )
    return items or f'<p style="color:{MUTED};font-size:{size}px">데이터 없음</p>'


def _header(ticker: str, date_str: str, subtitle: str = "") -> str:
    return f"""
    <div style="display:flex;justify-content:space-between;align-items:center;
                padding:32px 60px;border-bottom:1px solid {BORDER};
                background:{CARD}">
      <div style="display:flex;align-items:center;gap:14px">
        <span style="font-size:26px">📈</span>
        <span style="font-family:'Space Grotesk',sans-serif;font-size:30px;
                     font-weight:800;color:{ACCENT}">{ticker}</span>
        {f'<span style="font-size:22px;color:{MUTED};margin-left:6px">{subtitle}</span>' if subtitle else ''}
      </div>
      <span style="font-family:'Space Grotesk',sans-serif;font-size:22px;color:{MUTED}">{date_str}</span>
    </div>
    <div style="height:2px;background:linear-gradient(90deg,transparent,{ACCENT}55,transparent)"></div>"""


def _footer(small_text: str = "") -> str:
    return f"""
    <div style="border-top:1px solid {BORDER};padding:22px 60px;
                display:flex;justify-content:space-between;align-items:center;
                background:{CARD}">
      <span style="font-size:18px;color:{MUTED}">{small_text}</span>
      <span style="font-family:'Space Grotesk',sans-serif;font-size:20px;color:{MUTED}">{WATERMARK}</span>
    </div>"""


def _flex_section(icon: str, title: str, body_html: str, color: str = ACCENT) -> str:
    """내용이 카드를 꽉 채우도록 flex:1 사용"""
    return f"""
    <div style="flex:1;background:{CARD};border:1px solid {color}33;
                border-left:4px solid {color};border-radius:12px;
                padding:28px 36px;display:flex;flex-direction:column">
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:18px">
        <span style="font-size:26px">{icon}</span>
        <span style="font-family:'Space Grotesk',sans-serif;font-size:28px;
                     font-weight:700;color:{color}">{title}</span>
      </div>
      <div style="flex:1;display:flex;flex-direction:column;justify-content:center">
        {body_html}
      </div>
    </div>"""


def _wrap_html(content: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
{FONTS}
{RESET}
body {{ width:{W}px; height:{H}px; background:{BG};
        color:{WHITE}; display:flex; flex-direction:column; }}
</style></head><body>
{content}
</body></html>"""


# ── Playwright 스크린샷 ───────────────────────────────────

_CHROMIUM_PATHS = [
    None,                          # Playwright 기본 캐시 경로
    "/usr/bin/chromium",           # apt/nixpkgs 설치 경로
    "/usr/bin/chromium-browser",   # Ubuntu 대안 경로
    "/run/current-system/sw/bin/chromium",  # NixOS 시스템 경로
]

_LAUNCH_ARGS = [
    "--no-sandbox", "--disable-setuid-sandbox",
    "--disable-dev-shm-usage", "--disable-gpu",
    "--disable-software-rasterizer",
]

async def _launch_browser(p):
    """시스템에 설치된 Chromium 경로를 순서대로 시도"""
    for path in _CHROMIUM_PATHS:
        try:
            kwargs = {"args": _LAUNCH_ARGS}
            if path:
                kwargs["executable_path"] = path
            browser = await p.chromium.launch(**kwargs)
            print(f"[Playwright] Chromium launched: {path or 'default'}")
            return browser
        except Exception as e:
            print(f"[Playwright] Failed ({path or 'default'}): {e}")
    raise RuntimeError("Chromium 실행 파일을 찾을 수 없습니다. playwright install chromium 실행 필요")


async def _screenshot(html: str, width: int, height: int) -> bytes:
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await _launch_browser(p)
        page = await browser.new_page(viewport={"width": width, "height": height})
        await page.set_content(html, wait_until="networkidle")
        await page.evaluate("document.fonts.ready")
        data = await page.screenshot(
            clip={"x": 0, "y": 0, "width": width, "height": height}
        )
        await browser.close()
        return data


# ── 카드 1: 커버 (1080×1350) ─────────────────────────────

def _html_cover(ticker: str, signal: str, indicators: dict, created_at: str,
                current_price: float = None, change_pct: float = None) -> str:
    sig_text, sig_color = _signal_info(signal)
    actual_price = current_price or indicators.get("ma20")
    rsi      = indicators.get("rsi")
    macd     = indicators.get("macd", 0) or 0
    macd_sig = indicators.get("macd_signal", 0) or 0

    price_str = f"${actual_price:,.2f}" if actual_price else "—"
    date_str  = (created_at or "")[:10] or datetime.now().strftime("%Y-%m-%d")
    rsi_col   = RED if (rsi or 50) > 70 else GREEN if (rsi or 50) < 30 else MUTED
    rsi_lbl   = "과매수" if (rsi or 50) > 70 else "과매도" if (rsi or 50) < 30 else "중립"
    macd_bull = macd > macd_sig

    # 등락률
    if change_pct is not None:
        arrow    = "▲" if change_pct >= 0 else "▼"
        chg_col  = GREEN if change_pct >= 0 else RED
        chg_str  = f"{arrow} {abs(change_pct):+.2f}%".replace("+-", "+").replace("--", "+")
        chg_str  = f"{arrow} {abs(change_pct):.2f}%"
    else:
        chg_str, chg_col = "", MUTED

    content = f"""
    <!-- 상단 로고바 -->
    <div style="display:flex;justify-content:space-between;align-items:center;
                padding:40px 64px;background:{CARD};border-bottom:1px solid {BORDER}">
      <div style="display:flex;align-items:center;gap:14px">
        <span style="font-size:30px">📈</span>
        <span style="font-family:'Space Grotesk',sans-serif;font-size:32px;
                     font-weight:800;color:{ACCENT}">StockAI</span>
      </div>
      <span style="font-family:'Space Grotesk',sans-serif;font-size:24px;color:{MUTED}">{date_str}</span>
    </div>
    <div style="height:2px;background:linear-gradient(90deg,transparent,{ACCENT}88,transparent)"></div>

    <!-- 메인 콘텐츠 -->
    <div style="flex:1;display:flex;flex-direction:column;align-items:center;
                justify-content:center;padding:60px 64px;gap:40px;
                background:linear-gradient(160deg,{BG} 0%,#1a2030 100%)">

      <!-- 티커 -->
      <div style="font-family:'Space Grotesk',sans-serif;font-size:160px;font-weight:800;
                  color:{WHITE};letter-spacing:-6px;line-height:1">{ticker}</div>

      <!-- 현재가 + 등락률 -->
      <div style="text-align:center">
        <div style="font-family:'Space Grotesk',sans-serif;font-size:72px;font-weight:700;
                    color:{ACCENT};letter-spacing:-2px">{price_str}</div>
        {f'<div style="font-family:\'Space Grotesk\',sans-serif;font-size:40px;font-weight:700;color:{chg_col};margin-top:10px">{chg_str}</div>' if chg_str else ""}
      </div>

      <!-- 구분선 -->
      <div style="width:120px;height:3px;background:linear-gradient(90deg,transparent,{ACCENT},transparent);border-radius:2px"></div>

      <!-- RSI / MACD -->
      <div style="display:flex;gap:72px;align-items:center">
        <div style="text-align:center">
          <div style="font-family:'Space Grotesk',sans-serif;font-size:34px;
                       font-weight:700;color:{rsi_col}">RSI {f"{rsi:.1f}" if rsi else "—"}</div>
          <div style="font-size:20px;color:{MUTED};margin-top:6px">{rsi_lbl}</div>
        </div>
        <span style="width:1px;height:50px;background:{BORDER}"></span>
        <div style="text-align:center">
          <div style="font-family:'Space Grotesk',sans-serif;font-size:34px;
                       font-weight:700;color:{GREEN if macd_bull else RED}">
            MACD {"▲" if macd_bull else "▼"}
          </div>
          <div style="font-size:20px;color:{MUTED};margin-top:6px">{"골든크로스" if macd_bull else "데드크로스"}</div>
        </div>
      </div>

      <!-- 시그널 배지 -->
      <div style="padding:22px 72px;border-radius:70px;
                  background:{_rgba(sig_color, 0.18)};
                  border:2px solid {sig_color};
                  font-size:34px;font-weight:700;color:{sig_color}">
        {sig_text}
      </div>

      <!-- AI 분석 태그 -->
      <div style="display:flex;gap:16px;flex-wrap:wrap;justify-content:center">
        <span style="background:{_rgba(ACCENT,0.12)};border:1px solid {ACCENT}44;border-radius:20px;
                     padding:8px 20px;font-size:18px;color:{MUTED}">기술적 분석</span>
        <span style="background:{_rgba(ACCENT,0.12)};border:1px solid {ACCENT}44;border-radius:20px;
                     padding:8px 20px;font-size:18px;color:{MUTED}">AI 리포트</span>
        <span style="background:{_rgba(ACCENT,0.12)};border:1px solid {ACCENT}44;border-radius:20px;
                     padding:8px 20px;font-size:18px;color:{MUTED}">#StockAI</span>
      </div>
    </div>

    <!-- 하단 워터마크 -->
    <div style="height:2px;background:linear-gradient(90deg,transparent,{ACCENT}44,transparent)"></div>
    <div style="display:flex;justify-content:space-between;align-items:center;
                padding:28px 64px;background:{CARD};border-top:1px solid {BORDER}">
      <span style="font-size:20px;color:{MUTED}">캔들스틱 · RSI · MACD · 볼린저밴드</span>
      <span style="font-family:'Space Grotesk',sans-serif;font-size:22px;color:{MUTED}">{WATERMARK}</span>
    </div>"""

    return _wrap_html(content)


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
      <div style="background:{BG};border-radius:10px;padding:24px 16px;text-align:center;flex:1">
        <div style="font-size:18px;color:{MUTED};margin-bottom:10px">{lbl}</div>
        <div style="font-family:'Space Grotesk',sans-serif;font-size:32px;
                    font-weight:700;color:{col}">{val}</div>
        <div style="font-size:16px;color:{MUTED};margin-top:8px">{sub}</div>
      </div>""" for lbl, val, sub, col in cells)

    content = f"""
    {_header(ticker, date_str, "기술적 차트")}

    <!-- 차트 이미지 -->
    <div style="flex:1;overflow:hidden;background:{BG2};padding:8px">
      {'<img src="' + chart_src + '" style="width:100%;height:100%;object-fit:contain">' if chart_src else f'<div style="display:flex;align-items:center;justify-content:center;height:100%;color:{MUTED};font-size:28px">차트 없음</div>'}
    </div>

    <!-- 지표 그리드 -->
    <div style="display:flex;gap:12px;padding:24px;background:{CARD};
                border-top:1px solid {BORDER}">
      {grid_html}
    </div>

    {_footer("AI 기술적 분석")}"""

    return _wrap_html(content)


# ── 카드 3: 트렌드 + 지표 분석 (1080×1350) ──────────────

def _html_analysis(ticker: str, analysis: str, signal: str,
                   created_at: str, card_data: dict = None,
                   indicators: dict = None) -> str:
    cd   = card_data or {}
    inds = indicators or {}
    date_str = (created_at or "")[:10] or datetime.now().strftime("%Y-%m-%d")

    clean = re.sub(r"\nSIGNAL:(BUY|WATCH|SELL)\s*$", "", analysis,
                   flags=re.MULTILINE).strip()

    # ── 전반적 추세 ─────────────────────────────────────
    trend_summary = _clean(cd.get("trend_summary") or "")
    if not trend_summary:
        lines = _extract_section(clean, ["트렌드", "추세", "전반적", "종합 분석"], 3)
        trend_summary = " ".join(lines) if lines else "분석 중..."

    # ── MA 배열 분석 ────────────────────────────────────
    ma20  = inds.get("ma20")
    ma60  = inds.get("ma60")
    ma200 = inds.get("ma200")
    rsi   = inds.get("rsi")
    macd  = inds.get("macd", 0) or 0
    macd_s = inds.get("macd_signal", 0) or 0

    if ma20 and ma60 and ma200:
        if ma20 > ma60 > ma200:
            ma_arr_txt = "완전 강세 배열 (MA20 > MA60 > MA200)"
            ma_arr_col = GREEN
        elif ma20 < ma60 < ma200:
            ma_arr_txt = "완전 약세 배열 (MA20 < MA60 < MA200)"
            ma_arr_col = RED
        elif ma20 > ma200 and ma60 > ma200:
            ma_arr_txt = "장기 강세, 단기 조정 중"
            ma_arr_col = YELLOW
        else:
            ma_arr_txt = "혼재 배열 — 방향 확인 필요"
            ma_arr_col = YELLOW
    else:
        ma_arr_txt, ma_arr_col = "", MUTED

    if rsi:
        if rsi >= 70:
            rsi_txt, rsi_col_v = f"RSI {rsi:.1f} — 과매수 구간, 단기 조정 가능성", RED
        elif rsi <= 30:
            rsi_txt, rsi_col_v = f"RSI {rsi:.1f} — 과매도 구간, 기술적 반등 가능성", GREEN
        elif rsi >= 50:
            rsi_txt, rsi_col_v = f"RSI {rsi:.1f} — 강세 영역 (50선 위)", GREEN
        else:
            rsi_txt, rsi_col_v = f"RSI {rsi:.1f} — 약세 영역 (50선 아래)", RED
    else:
        rsi_txt, rsi_col_v = "", MUTED

    macd_txt = ("MACD 골든크로스 — 상승 모멘텀 확인" if macd > macd_s
                else "MACD 데드크로스 — 하락 압력 지속")
    macd_col_v = GREEN if macd > macd_s else RED

    # ── 지지 / 저항 ─────────────────────────────────────
    resistance = [r for r in (cd.get("resistance") or []) if r]
    support    = [s for s in (cd.get("support") or []) if s]
    if resistance or support:
        sr_html = '<div style="display:flex;gap:24px">'
        if resistance:
            sr_html += f'<div style="flex:1"><div style="color:{RED};font-size:22px;font-weight:700;margin-bottom:10px">▲ 저항선</div>'
            sr_html += "".join(f'<div style="color:{WHITE};font-size:22px;margin-bottom:8px;font-family:\'Space Grotesk\',sans-serif;line-height:1.8">{_clean(r)}</div>' for r in resistance[:3])
            sr_html += "</div>"
        if support:
            sr_html += f'<div style="flex:1"><div style="color:{GREEN};font-size:22px;font-weight:700;margin-bottom:10px">▼ 지지선</div>'
            sr_html += "".join(f'<div style="color:{WHITE};font-size:22px;margin-bottom:8px;font-family:\'Space Grotesk\',sans-serif;line-height:1.8">{_clean(s)}</div>' for s in support[:3])
            sr_html += "</div>"
        sr_html += "</div>"
    else:
        sr_fallback = _extract_section(clean, ["지지", "저항", "Support", "Resistance"], 4) or \
                      [_clean(l) for l in clean.split("\n") if re.search(r"\$[\d,]+", l)][:3]
        sr_html = _ul(sr_fallback, YELLOW, 22)

    # ── 거래량 & 이슈 ────────────────────────────────────
    vol_note  = _clean(cd.get("volume_note") or "")
    key_event = _clean(cd.get("key_event") or "")

    ma_badges = "".join(
        f'<span style="background:{BG};border-radius:8px;padding:6px 14px;font-size:19px;color:{MUTED}">'
        f'<span style="color:{WHITE};font-family:\'Space Grotesk\',sans-serif">{"MA20" if k=="ma20" else "MA60" if k=="ma60" else "MA200"}</span>'
        f' ${v:,.0f}</span>'
        for k, v in [("ma20", ma20), ("ma60", ma60), ("ma200", ma200)] if v
    )

    content = f"""
    {_header(ticker, date_str, "트렌드 분석")}

    <div style="flex:1;padding:28px 40px;display:flex;flex-direction:column;gap:16px">

      {_flex_section("📈", "전반적 추세",
        f'<p style="font-size:22px;color:{WHITE};line-height:1.8">{trend_summary}</p>',
        ACCENT)}

      {_flex_section("📊", "이동평균선 & 기술지표",
        f'''<div>
          {f'<div style="font-size:22px;font-weight:700;color:{ma_arr_col};margin-bottom:12px">{ma_arr_txt}</div>' if ma_arr_txt else ""}
          <div style="display:flex;gap:12px;margin-bottom:14px;flex-wrap:wrap">{ma_badges}</div>
          {f'<div style="font-size:22px;color:{rsi_col_v};margin-bottom:10px;line-height:1.8">{rsi_txt}</div>' if rsi_txt else ""}
          <div style="font-size:22px;color:{macd_col_v};line-height:1.8">{macd_txt}</div>
        </div>''',
        YELLOW)}

      {_flex_section("🎯", "지지 · 저항선", sr_html, RED)}

      {_flex_section("📰", "거래량 & 주요 이슈",
        f'''<div>
          {f'<div style="font-size:22px;color:{MUTED};margin-bottom:10px;line-height:1.8">📊 {vol_note}</div>' if vol_note else ""}
          {f'<div style="font-size:22px;color:{YELLOW};line-height:1.8">📅 {key_event}</div>' if key_event else f'<p style="font-size:20px;color:{MUTED}">주요 이슈 없음</p>'}
        </div>''',
        MUTED)}
    </div>

    {_footer("기술적 분석 리포트")}"""

    return _wrap_html(content)


# ── 카드 4: 시나리오 (1080×1350) ─────────────────────────

def _html_scenarios(ticker: str, analysis: str, signal: str,
                    created_at: str, card_data: dict = None) -> str:
    cd = card_data or {}
    date_str = (created_at or "")[:10] or datetime.now().strftime("%Y-%m-%d")
    sig_text, sig_color = _signal_info(signal)

    clean = re.sub(r"\nSIGNAL:(BUY|WATCH|SELL)\s*$", "", analysis,
                   flags=re.MULTILINE).strip()

    sig = signal.upper() if signal else "WATCH"
    raw_bull = cd.get("bull_prob")
    raw_bear = cd.get("bear_prob")
    if raw_bull is not None and raw_bear is not None:
        try:
            bull_pct = int(raw_bull)
            bear_pct = int(raw_bear)
        except (ValueError, TypeError):
            bull_pct = 65 if sig == "BUY" else 35 if sig == "SELL" else 50
            bear_pct = 100 - bull_pct
    else:
        bull_pct = 65 if sig == "BUY" else 35 if sig == "SELL" else 50
        bear_pct = 100 - bull_pct

    bull_conditions = [_clean(c) for c in (cd.get("bull_conditions") or []) if c]
    bull_targets    = [_clean(t) for t in (cd.get("bull_targets") or []) if t]
    if not bull_conditions:
        bull_conditions = _extract_section(clean,
            ["강세 시나리오", "상승 시나리오", "Bullish", "매수 조건", "강세"], 5)

    bear_warnings = [_clean(w) for w in (cd.get("bear_warnings") or []) if w]
    stop_loss     = _clean(cd.get("stop_loss") or "")
    if not bear_warnings:
        bear_warnings = _extract_section(clean,
            ["약세 시나리오", "하락 시나리오", "Bearish", "매도 조건", "약세"], 5)

    def progress_bar(pct: int, color: str) -> str:
        return f"""
        <div style="background:{BORDER};border-radius:6px;height:14px;
                    margin-bottom:10px;overflow:hidden">
          <div style="width:{pct}%;height:100%;background:{color};border-radius:6px"></div>
        </div>
        <div style="font-family:'Space Grotesk',sans-serif;font-size:36px;
                    font-weight:800;color:{color};margin-bottom:16px">{pct}%</div>"""

    def targets_html(items: list, color: str) -> str:
        if not items:
            return ""
        labels = ["1차", "2차", "최대"]
        badges = "".join(
            f'<span style="background:{_rgba(color,0.2)};border:1px solid {color}66;'
            f'border-radius:8px;padding:6px 14px;font-size:19px;color:{color};font-weight:700">'
            f'{labels[i] if i < len(labels) else ""} {_clean(t)}</span>'
            for i, t in enumerate(items[:3])
        )
        return f'<div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:12px">{badges}</div>'

    key_event = _clean(cd.get("key_event") or "")

    content = f"""
    {_header(ticker, date_str, "매수/매도 시나리오")}

    <!-- 시그널 배지 -->
    <div style="display:flex;justify-content:center;padding:16px 40px;
                background:{_rgba(sig_color, 0.12)};border-bottom:1px solid {BORDER}">
      <div style="padding:12px 56px;border-radius:50px;background:{_rgba(sig_color, 0.2)};
                  border:2px solid {sig_color};font-size:26px;font-weight:700;color:{sig_color}">
        {sig_text}
      </div>
    </div>

    <div style="flex:1;padding:20px 40px;display:flex;flex-direction:column;gap:14px">

      <!-- 강세 -->
      <div style="flex:1;background:{CARD};border:1px solid {GREEN}33;
                  border-left:4px solid {GREEN};border-radius:14px;padding:24px 32px">
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px">
          <span style="font-size:26px">🟢</span>
          <span style="font-size:28px;font-weight:700;color:{GREEN}">강세 시나리오</span>
        </div>
        {progress_bar(bull_pct, GREEN)}
        {_ul(bull_conditions or ["데이터 없음"], GREEN, 22, 76, 5)}
        {targets_html(bull_targets, GREEN)}
      </div>

      <!-- 약세 -->
      <div style="flex:1;background:{CARD};border:1px solid {RED}33;
                  border-left:4px solid {RED};border-radius:14px;padding:24px 32px">
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px">
          <span style="font-size:26px">🔴</span>
          <span style="font-size:28px;font-weight:700;color:{RED}">약세 시나리오</span>
        </div>
        {progress_bar(bear_pct, RED)}
        {_ul(bear_warnings or ["데이터 없음"], RED, 22, 76, 5)}
        {f'<div style="display:flex;align-items:center;gap:10px;margin-top:12px"><span style="font-size:20px;color:{MUTED}">🛑 손절:</span><span style="background:{_rgba(RED,0.2)};border:1px solid {RED}66;border-radius:8px;padding:5px 16px;font-size:20px;color:{RED};font-weight:700">{stop_loss}</span></div>' if stop_loss else ""}
      </div>

      <!-- 핵심 촉매 -->
      {f'<div style="background:{CARD};border-left:4px solid {YELLOW};border-radius:0 12px 12px 0;padding:16px 24px"><span style="font-size:20px;color:{YELLOW};font-weight:700">📅 핵심 이벤트&nbsp;&nbsp;</span><span style="font-size:22px;color:{WHITE};line-height:1.8">{key_event}</span></div>' if key_event else ""}
    </div>

    {_footer("시나리오 분석")}"""

    return _wrap_html(content)


# ── 카드 5: 종합 (1080×1350) ─────────────────────────────

def _html_summary(ticker: str, analysis: str, signal: str,
                  indicators: dict, created_at: str, card_data: dict = None) -> str:
    cd = card_data or {}
    date_str = (created_at or "")[:10] or datetime.now().strftime("%Y-%m-%d")
    sig_text, sig_color = _signal_info(signal)

    clean = re.sub(r"\nSIGNAL:(BUY|WATCH|SELL)\s*$", "", analysis,
                   flags=re.MULTILINE).strip()

    conclusion = _clean(cd.get("conclusion") or "")
    if not conclusion:
        paras = [p.strip() for p in clean.split("\n\n") if p.strip()]
        conclusion = _clean(paras[-1])[:120] if paras else ""

    cons_txt = _clean(cd.get("strategy_conservative") or "")
    aggr_txt = _clean(cd.get("strategy_aggressive") or "")
    if not cons_txt:
        cons_list = _extract_section(clean, ["보수적", "Conservative", "장기 투자", "안정"], 2)
        cons_txt = " ".join(cons_list) if cons_list else ""
    if not aggr_txt:
        aggr_list = _extract_section(clean, ["공격적", "Aggressive", "단기", "트레이더"], 2)
        aggr_txt = " ".join(aggr_list) if aggr_list else ""

    checkpoints = [_clean(c) for c in (cd.get("checkpoints") or []) if c]
    if not checkpoints:
        checkpoints = _extract_section(clean, ["모니터링", "주목", "확인", "주시", "핵심"], 3)
    if not checkpoints:
        rsi   = indicators.get("rsi")
        ma20  = indicators.get("ma20")
        macd  = indicators.get("macd", 0) or 0
        macd_s = indicators.get("macd_signal", 0) or 0
        if rsi:
            checkpoints.append(f"RSI {rsi:.0f} — {'과매수 구간, 단기 조정 대비' if rsi > 70 else '과매도 구간, 반등 모색' if rsi < 30 else '중립 구간 유지 중'}")
        checkpoints.append("MACD " + ("골든크로스 확인 → 상승 모멘텀 지속 여부 주시" if macd > macd_s else "데드크로스 → 추가 하락 가능성 대비"))
        if ma20:
            checkpoints.append(f"MA20(${ma20:,.0f}) 지지/저항 여부가 단기 방향 결정")

    cp_html = "".join(
        f'<div style="display:flex;gap:14px;margin-bottom:14px;align-items:flex-start">'
        f'<span style="background:{ACCENT};color:{BG};font-size:16px;font-weight:800;'
        f'min-width:26px;height:26px;border-radius:50%;display:flex;align-items:center;'
        f'justify-content:center;flex-shrink:0;margin-top:2px">{i+1}</span>'
        f'<span style="font-size:22px;color:{WHITE};line-height:1.8">{_clean(pt)[:90]}</span>'
        f'</div>'
        for i, pt in enumerate(checkpoints[:3])
    )

    content = f"""
    {_header(ticker, date_str, "종합 의견")}

    <!-- 시그널 -->
    <div style="display:flex;justify-content:center;align-items:center;padding:14px 40px;
                background:{_rgba(sig_color, 0.12)};border-bottom:1px solid {BORDER}">
      <div style="padding:12px 56px;border-radius:50px;border:2px solid {sig_color};
                  background:{_rgba(sig_color, 0.2)};
                  font-size:26px;font-weight:700;color:{sig_color}">{sig_text}</div>
    </div>

    <div style="flex:1;padding:24px 40px;display:flex;flex-direction:column;gap:16px">

      <!-- 종합 결론 -->
      {_flex_section("💬", "종합 결론",
        f'<p style="font-size:22px;color:{WHITE};line-height:1.8">{conclusion or "—"}</p>',
        ACCENT)}

      <!-- 투자자 전략 -->
      <div style="flex:1;background:{CARD};border-radius:12px;padding:28px 32px;display:flex;flex-direction:column">
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:18px">
          <span style="font-size:26px">👤</span>
          <span style="font-size:28px;font-weight:700;color:{ACCENT}">투자자 유형별 전략</span>
        </div>
        <div style="flex:1;display:flex;gap:14px">
          <div style="flex:1;background:{BG};border-radius:10px;padding:18px 20px;display:flex;flex-direction:column">
            <div style="font-size:20px;color:{YELLOW};font-weight:700;margin-bottom:10px">🛡 보수적</div>
            <div style="flex:1;font-size:22px;color:{WHITE};line-height:1.8">{cons_txt or "데이터 없음"}</div>
          </div>
          <div style="flex:1;background:{BG};border-radius:10px;padding:18px 20px;display:flex;flex-direction:column">
            <div style="font-size:20px;color:{GREEN};font-weight:700;margin-bottom:10px">⚡ 공격적</div>
            <div style="flex:1;font-size:22px;color:{WHITE};line-height:1.8">{aggr_txt or "데이터 없음"}</div>
          </div>
        </div>
      </div>

      <!-- 핵심 체크포인트 -->
      <div style="flex:1;background:{CARD};border-radius:12px;padding:28px 32px;display:flex;flex-direction:column">
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:18px">
          <span style="font-size:26px">🔍</span>
          <span style="font-size:28px;font-weight:700;color:{ACCENT}">핵심 체크포인트</span>
        </div>
        <div style="flex:1;display:flex;flex-direction:column;justify-content:space-evenly">
          {cp_html}
        </div>
      </div>

      <!-- 해시태그 + 면책 고지 -->
      <div style="background:{BG};border-radius:10px;padding:16px 20px;border:1px solid {BORDER}">
        <div style="text-align:center;margin-bottom:8px">
          <span style="font-size:18px;color:{MUTED}">#StockAI &nbsp; #기술적분석 &nbsp; #주식 &nbsp; #{ticker}</span>
        </div>
        <p style="font-size:15px;color:{MUTED};line-height:1.6;text-align:center">
          ⚠️ 본 분석은 AI 기반 참고 자료이며 투자 권유가 아닙니다. 모든 투자 결정은 본인의 판단과 책임 하에 이루어져야 합니다.
        </p>
      </div>
    </div>

    {_footer("AI 분석 리포트")}"""

    return _wrap_html(content)


# ── 공개 API ─────────────────────────────────────────────

async def generate_cards(doc: dict, card_data: dict = None) -> list:
    """
    MongoDB 분석 doc + Claude 추출 card_data → 5장 카드 PNG 생성
    Returns: [(filename, bytes), ...]
    """
    ticker        = doc.get("ticker", "TICKER")
    signal        = doc.get("signal", "WATCH")
    indicators    = doc.get("indicators", {})
    analysis      = doc.get("analysis", "")
    chart_b64     = doc.get("chart_b64", "")
    created_at    = doc.get("created_at", datetime.now().isoformat())
    current_price = doc.get("current_price")
    change_pct    = doc.get("change_pct")
    cd            = card_data or {}

    cards_html = [
        (f"{ticker}_1_cover.png",     _html_cover(ticker, signal, indicators, created_at, current_price, change_pct)),
        (f"{ticker}_2_chart.png",     _html_chart(ticker, chart_b64, indicators, created_at)),
        (f"{ticker}_3_analysis.png",  _html_analysis(ticker, analysis, signal, created_at, cd, indicators)),
        (f"{ticker}_4_scenarios.png", _html_scenarios(ticker, analysis, signal, created_at, cd)),
        (f"{ticker}_5_summary.png",   _html_summary(ticker, analysis, signal, indicators, created_at, cd)),
    ]

    from playwright.async_api import async_playwright
    results = []
    async with async_playwright() as p:
        browser = await _launch_browser(p)
        for filename, html in cards_html:
            page = await browser.new_page(viewport={"width": W, "height": H})
            await page.set_content(html, wait_until="networkidle")
            await page.evaluate("document.fonts.ready")
            screenshot = await page.screenshot(
                clip={"x": 0, "y": 0, "width": W, "height": H}
            )
            await page.close()
            results.append((filename, screenshot))
        await browser.close()

    return results
