"""
Instagram card generator — 4-card series from StockAI analysis data
각 카드는 matplotlib Agg 백엔드로 생성 (Pillow 불필요)
"""
import io
import os
import re
import base64
import textwrap
import platform
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.image as mpimg
import matplotlib.font_manager as fm

# ── 색상 팔레트 ──────────────────────────────────────────
BG       = "#0d1117"
PANEL    = "#161b22"
BORDER   = "#21262d"
ACCENT   = "#4d9fff"
WHITE    = "#e6edf3"
MUTED    = "#8b949e"
C_GREEN  = "#3fb950"
C_RED    = "#f85149"
C_YELLOW = "#d29922"
BRAND    = "StockAI · @success_eunjin"


def _hex(h: str) -> tuple:
    """hex → (r, g, b) 0~1 matplotlib 튜플"""
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) / 255 for i in (0, 2, 4))


_FONT_SETUP_DONE = False

def _setup_font():
    global _FONT_SETUP_DONE
    if _FONT_SETUP_DONE:
        return
    candidates = []
    if platform.system() == "Darwin":
        candidates = [
            "/System/Library/Fonts/AppleSDGothicNeo.ttc",
            "/Library/Fonts/NanumGothic.ttf",
        ]
    else:  # Linux (Railway)
        candidates = [
            "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
            "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    for p in candidates:
        if os.path.exists(p):
            fm.fontManager.addfont(p)
            plt.rcParams["font.family"] = fm.FontProperties(fname=p).get_name()
            break
    plt.rcParams["axes.unicode_minus"] = False
    _FONT_SETUP_DONE = True


# ── 공통 그리기 헬퍼 ─────────────────────────────────────

def _fig(w_px: int, h_px: int, dpi: int = 150):
    fig = plt.figure(figsize=(w_px / dpi, h_px / dpi), dpi=dpi)
    fig.patch.set_facecolor(_hex(BG))
    return fig


def _ax_full(fig):
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor(_hex(BG))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    return ax


def _t(ax, x, y, txt, size=18, color=WHITE, ha="center", va="center", bold=False):
    ax.text(x, y, str(txt),
            fontsize=size, color=color if isinstance(color, str) else color,
            ha=ha, va=va, fontweight="bold" if bold else "normal",
            transform=ax.transAxes, clip_on=False)


def _divider(ax, y, color=BORDER, xmin=0.05, xmax=0.95):
    ax.axhline(y=y, xmin=xmin, xmax=xmax, color=color, linewidth=0.8, alpha=0.7)


def _panel(ax, x, y, w, h, color=PANEL, alpha=1.0, edge=None):
    from matplotlib.patches import FancyBboxPatch
    rect = FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0,rounding_size=0.015",
        facecolor=_hex(color), alpha=alpha,
        edgecolor=_hex(edge) if edge else "none",
        linewidth=1.5,
        transform=ax.transAxes, zorder=2
    )
    ax.add_patch(rect)


def _fig_to_bytes(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=fig.dpi, bbox_inches=None, pad_inches=0)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _signal_info(signal: str):
    s = (signal or "WATCH").upper()
    if s == "BUY":
        return "🟢 매수 검토 구간", C_GREEN
    if s == "SELL":
        return "🔴 매수 자제 구간", C_RED
    return "👀 관망 구간", C_YELLOW


def _extract_section(text: str, keywords: list, max_lines: int = 4) -> list:
    """분석 텍스트에서 키워드 섹션 추출"""
    lines = text.split("\n")
    result, capturing = [], False
    for line in lines:
        stripped = line.strip().lstrip("#*").strip()
        if not stripped:
            continue
        if any(kw in stripped for kw in keywords):
            capturing = True
            continue
        if capturing:
            if len(stripped) < 40 and (stripped.startswith("##") or
               (stripped.startswith("**") and stripped.endswith("**"))):
                break
            clean = re.sub(r"[*_`#]", "", stripped)
            if clean and len(clean) > 5:
                result.append(clean)
            if len(result) >= max_lines:
                break
    return result


def _bullet_points(text: str, n: int = 3) -> list:
    bullets = re.findall(r"[-•*]\s+(.+)", text)
    cleaned = [re.sub(r"[*_`]", "", b).strip() for b in bullets if len(b.strip()) > 5]
    if cleaned:
        return cleaned[:n]
    sentences = re.split(r"(?<=[.。])\s+", text)
    return [re.sub(r"[*_`#]", "", s).strip() for s in sentences if len(s.strip()) > 10][:n]


# ── 카드 1: 커버 (1080×1080) ─────────────────────────────

def card_cover(ticker: str, signal: str, indicators: dict, created_at: str) -> bytes:
    _setup_font()
    fig = _fig(1080, 1080)
    ax = _ax_full(fig)

    date_str = (created_at or "")[:10] or datetime.now().strftime("%Y-%m-%d")
    sig_text, sig_color = _signal_info(signal)
    ma20 = indicators.get("ma20")
    rsi = indicators.get("rsi")
    macd = indicators.get("macd", 0) or 0
    macd_sig = indicators.get("macd_signal", 0) or 0

    # 상단 로고 바
    _panel(ax, 0.05, 0.90, 0.90, 0.07)
    _t(ax, 0.20, 0.935, "📈 StockAI", size=17, color=ACCENT, ha="center", bold=True)
    _t(ax, 0.75, 0.935, date_str, size=14, color=MUTED, ha="center")

    # 구분 라인 (ACCENT 컬러)
    ax.axhline(y=0.885, xmin=0.05, xmax=0.95, color=ACCENT, linewidth=1.5, alpha=0.6)

    # 티커 이름 (메인)
    _t(ax, 0.5, 0.73, ticker, size=92, color=WHITE, ha="center", bold=True)

    # 기준가
    price_str = f"${ma20:,.2f}" if ma20 else "—"
    _t(ax, 0.5, 0.635, price_str, size=34, color=ACCENT, ha="center", bold=True)
    _t(ax, 0.5, 0.595, "기준가 (MA20)", size=14, color=MUTED, ha="center")

    # RSI / MACD 미니 지표
    _divider(ax, 0.565, BORDER)
    rsi_str = f"RSI  {rsi:.1f}" if rsi else "RSI  —"
    rsi_col = C_RED if (rsi or 50) > 70 else C_GREEN if (rsi or 50) < 30 else MUTED
    macd_bull = macd > macd_sig
    macd_str = "MACD  ▲ 강세" if macd_bull else "MACD  ▼ 약세"
    macd_col = C_GREEN if macd_bull else C_RED
    _t(ax, 0.28, 0.525, rsi_str, size=17, color=rsi_col, ha="center")
    _t(ax, 0.72, 0.525, macd_str, size=17, color=macd_col, ha="center")
    _divider(ax, 0.495, BORDER)

    # 시그널 배지
    _panel(ax, 0.12, 0.395, 0.76, 0.082, sig_color, alpha=0.18, edge=sig_color)
    _t(ax, 0.5, 0.436, sig_text, size=24, color=sig_color, ha="center", bold=True)

    # 서브 텍스트
    _t(ax, 0.5, 0.33, "AI 기술적 분석 리포트", size=16, color=MUTED, ha="center")
    _t(ax, 0.5, 0.29, "캔들스틱 · 볼린저밴드 · RSI · MACD · MA", size=13, color=MUTED, ha="center")

    # 하단 워터마크
    _divider(ax, 0.10)
    _t(ax, 0.5, 0.060, BRAND, size=14, color=MUTED, ha="center")

    return _fig_to_bytes(fig)


# ── 카드 2: 차트 (1080×1350) ─────────────────────────────

def card_chart(ticker: str, chart_b64: str, indicators: dict, created_at: str) -> bytes:
    _setup_font()
    dpi = 150
    fig = plt.figure(figsize=(1080 / dpi, 1350 / dpi), dpi=dpi)
    fig.patch.set_facecolor(_hex(BG))

    date_str = (created_at or "")[:10] or datetime.now().strftime("%Y-%m-%d")

    # 레이아웃
    ax_hdr   = fig.add_axes([0.00, 0.935, 1.00, 0.065])
    ax_chart = fig.add_axes([0.02, 0.125, 0.96, 0.805])
    ax_ind   = fig.add_axes([0.00, 0.000, 1.00, 0.118])

    for ax in (ax_hdr, ax_chart, ax_ind):
        ax.set_facecolor(_hex(BG))
        ax.axis("off")

    # 헤더
    ax_hdr.set_xlim(0, 1); ax_hdr.set_ylim(0, 1)
    ax_hdr.text(0.06, 0.5, f"📈  {ticker}", fontsize=20, color=_hex(ACCENT),
                fontweight="bold", va="center")
    ax_hdr.text(0.94, 0.5, date_str, fontsize=14, color=_hex(MUTED),
                ha="right", va="center")
    ax_hdr.axhline(y=0.05, xmin=0.02, xmax=0.98, color=ACCENT, linewidth=1.2, alpha=0.5)

    # 차트 이미지 삽입
    if chart_b64:
        try:
            img = mpimg.imread(io.BytesIO(base64.b64decode(chart_b64)))
            ax_chart.imshow(img, aspect="auto", origin="upper")
        except Exception:
            ax_chart.text(0.5, 0.5, "차트 로드 실패", fontsize=18,
                          color="gray", ha="center", va="center")
    else:
        ax_chart.text(0.5, 0.5, "차트 없음", fontsize=18,
                      color="gray", ha="center", va="center")

    # 하단 지표 바
    ax_ind.set_xlim(0, 1); ax_ind.set_ylim(0, 1)
    ax_ind.axhline(y=0.95, xmin=0.02, xmax=0.98, color=BORDER, linewidth=0.8, alpha=0.7)

    rsi = indicators.get("rsi")
    macd = indicators.get("macd", 0) or 0
    macd_sig = indicators.get("macd_signal", 0) or 0
    ma20 = indicators.get("ma20")
    ma200 = indicators.get("ma200")
    rsi_col = C_RED if (rsi or 50) > 70 else C_GREEN if (rsi or 50) < 30 else MUTED
    macd_bull = macd > macd_sig

    cells = [
        ("RSI 14",  f"{rsi:.1f}" if rsi else "—",              rsi_col),
        ("MACD",    "▲ 강세" if macd_bull else "▼ 약세",        C_GREEN if macd_bull else C_RED),
        ("MA 20",   f"${ma20:,.0f}" if ma20 else "—",            ACCENT),
        ("MA 200",  f"${ma200:,.0f}" if ma200 else "—",          C_YELLOW),
    ]
    for i, (lbl, val, col) in enumerate(cells):
        x = 0.125 + i * 0.25
        ax_ind.text(x, 0.73, lbl, fontsize=12, color=_hex(MUTED), ha="center", va="center")
        ax_ind.text(x, 0.38, val, fontsize=15, color=_hex(col),
                    ha="center", va="center", fontweight="bold")
    ax_ind.text(0.5, 0.09, BRAND, fontsize=11, color=_hex(MUTED), ha="center", va="center")

    return _fig_to_bytes(fig)


# ── 카드 3: 분석 (1080×1350) ─────────────────────────────

def card_analysis(ticker: str, analysis: str, signal: str, created_at: str) -> bytes:
    _setup_font()
    fig = _fig(1080, 1350)
    ax = _ax_full(fig)

    date_str = (created_at or "")[:10] or datetime.now().strftime("%Y-%m-%d")
    _, sig_color = _signal_info(signal)

    clean = re.sub(r"\nSIGNAL:(BUY|WATCH|SELL)\s*$", "", analysis,
                   flags=re.MULTILINE).strip()

    # 헤더
    _panel(ax, 0.04, 0.935, 0.92, 0.057)
    _t(ax, 0.5, 0.963, f"📊  {ticker}  기술적 분석", size=19, color=ACCENT,
       ha="center", bold=True)
    _t(ax, 0.88, 0.963, date_str, size=12, color=MUTED, ha="center")

    ax.axhline(y=0.928, xmin=0.04, xmax=0.96, color=ACCENT, linewidth=1.2, alpha=0.5)

    # 콘텐츠 섹션
    trend  = _extract_section(clean, ["트렌드", "추세", "전반적", "종합"], 3) or \
             [re.sub(r"[*_`#]", "", l).strip()
              for l in clean.split("\n") if l.strip() and not l.startswith("#")][:3]
    sr     = _extract_section(clean, ["지지", "저항", "Support", "Resistance"], 3)
    bull   = _extract_section(clean, ["강세", "상승", "Bullish", "매수 시나리오"], 2)
    bear   = _extract_section(clean, ["약세", "하락", "Bearish", "매도", "하락 시나리오"], 2)

    y = 0.895

    def section(icon, title, lines, color):
        nonlocal y
        _t(ax, 0.07, y, f"{icon}  {title}", size=15, color=color, ha="left", bold=True)
        y -= 0.042
        for line in lines:
            clean_line = re.sub(r"[*_`#]", "", str(line)).strip()[:52]
            if clean_line:
                _t(ax, 0.085, y, f"  {clean_line}", size=12, color=WHITE, ha="left")
                y -= 0.033
        y -= 0.008
        _divider(ax, y + 0.004, BORDER)
        y -= 0.012

    section("📈", "트렌드 요약",      trend or ["분석 데이터 없음"], ACCENT)
    section("🎯", "지지 · 저항선",    sr    or ["데이터 없음"],     C_YELLOW)
    section("🟢", "강세 시나리오",    bull  or ["데이터 없음"],     C_GREEN)
    section("🔴", "약세 시나리오",    bear  or ["데이터 없음"],     C_RED)

    # 워터마크
    _divider(ax, 0.07)
    _t(ax, 0.5, 0.042, BRAND, size=13, color=MUTED, ha="center")

    return _fig_to_bytes(fig)


# ── 카드 4: 종합 (1080×1080) ─────────────────────────────

def card_summary(ticker: str, analysis: str, signal: str,
                 indicators: dict, created_at: str) -> bytes:
    _setup_font()
    fig = _fig(1080, 1080)
    ax = _ax_full(fig)

    sig_text, sig_color = _signal_info(signal)
    clean = re.sub(r"\nSIGNAL:(BUY|WATCH|SELL)\s*$", "", analysis,
                   flags=re.MULTILINE).strip()

    summary_lines = _extract_section(clean, ["종합", "결론", "Summary", "Conclusion", "의견"], 2)
    if not summary_lines:
        paras = [p.strip() for p in clean.split("\n\n") if p.strip()]
        last = paras[-1] if paras else clean
        summary_lines = textwrap.wrap(re.sub(r"[*_`#]", "", last)[:100], 38)[:2]

    monitor_lines = _extract_section(clean, ["모니터링", "주목", "확인", "주시", "Watch"], 3)
    if not monitor_lines:
        monitor_lines = _bullet_points(clean, 3)

    # 헤더
    _panel(ax, 0.04, 0.916, 0.92, 0.068)
    _t(ax, 0.5, 0.950, f"⚡  {ticker}  종합 의견", size=21,
       color=WHITE, ha="center", bold=True)
    ax.axhline(y=0.908, xmin=0.04, xmax=0.96, color=ACCENT, linewidth=1.2, alpha=0.5)

    # 시그널 배지
    _panel(ax, 0.10, 0.826, 0.80, 0.072, sig_color, alpha=0.18, edge=sig_color)
    _t(ax, 0.5, 0.862, sig_text, size=23, color=sig_color, ha="center", bold=True)

    _divider(ax, 0.812)

    # 종합 의견
    _t(ax, 0.07, 0.775, "💬  종합 의견", size=15, color=ACCENT, ha="left", bold=True)
    y = 0.735
    for line in summary_lines:
        _t(ax, 0.085, y, re.sub(r"[*_`#]", "", line).strip()[:54],
           size=13, color=WHITE, ha="left")
        y -= 0.038

    _divider(ax, y - 0.012)
    y -= 0.052

    # 핵심 모니터링
    _t(ax, 0.07, y, "🔍  핵심 모니터링 포인트", size=15, color=ACCENT, ha="left", bold=True)
    y -= 0.045
    for i, pt in enumerate(monitor_lines[:3]):
        clean_pt = re.sub(r"[*_`#]", "", str(pt)).strip()[:52]
        _t(ax, 0.085, y, f"{i+1}.  {clean_pt}", size=13, color=WHITE, ha="left")
        y -= 0.038

    _divider(ax, y - 0.012)
    y -= 0.052

    # 액션 플랜
    _t(ax, 0.07, y, "📌  다음 액션 플랜", size=15, color=ACCENT, ha="left", bold=True)
    y -= 0.045
    rsi      = indicators.get("rsi")
    ma20     = indicators.get("ma20")
    macd     = indicators.get("macd", 0) or 0
    macd_sig = indicators.get("macd_signal", 0) or 0
    macd_bull = macd > macd_sig

    actions = []
    if rsi and rsi > 70:
        actions.append(f"RSI {rsi:.0f} 과매수 → 단기 조정 주의, 분할 매도 고려")
    elif rsi and rsi < 30:
        actions.append(f"RSI {rsi:.0f} 과매도 → 반등 가능성, 소량 매수 검토")
    else:
        actions.append(f"RSI {rsi:.0f} 중립 → 방향성 확인 후 진입" if rsi else "RSI 데이터 확인 필요")

    actions.append("MACD " + ("골든크로스 → 상승 모멘텀 지속 확인" if macd_bull
                               else "데드크로스 → 추가 하락 가능성 주의"))
    if ma20:
        actions.append(f"MA20(${ma20:,.0f}) 지지 여부 확인")

    for act in actions[:3]:
        _t(ax, 0.085, y, f"•  {act}", size=12, color=WHITE, ha="left")
        y -= 0.037

    # 워터마크
    _divider(ax, 0.088)
    _t(ax, 0.5, 0.053, BRAND, size=14, color=MUTED, ha="center")
    _t(ax, 0.5, 0.022, "#StockAI  #기술적분석  #주식", size=11, color=MUTED, ha="center")

    return _fig_to_bytes(fig)


# ── 공개 API ─────────────────────────────────────────────

def generate_cards(doc: dict) -> list:
    """
    MongoDB 분석 doc → 4장 카드 생성
    Returns: [(filename, bytes), ...]
    """
    ticker     = doc.get("ticker", "TICKER")
    signal     = doc.get("signal", "WATCH")
    indicators = doc.get("indicators", {})
    analysis   = doc.get("analysis", "")
    chart_b64  = doc.get("chart_b64", "")
    created_at = doc.get("created_at", datetime.now().isoformat())

    return [
        (f"{ticker}_1_cover.png",    card_cover(ticker, signal, indicators, created_at)),
        (f"{ticker}_2_chart.png",    card_chart(ticker, chart_b64, indicators, created_at)),
        (f"{ticker}_3_analysis.png", card_analysis(ticker, analysis, signal, created_at)),
        (f"{ticker}_4_summary.png",  card_summary(ticker, analysis, signal, indicators, created_at)),
    ]
