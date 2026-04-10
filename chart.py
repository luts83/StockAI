import io
import base64
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle
import pandas as pd

DARK_BG  = "#0d1117"
PANEL_BG = "#161b22"
GREEN    = "#26a641"
RED      = "#f85149"
BLUE     = "#58a6ff"
YELLOW   = "#e3b341"
WHITE    = "#e6edf3"
GRAY     = "#8b949e"
ORANGE   = "#f0883e"

def generate_chart(df: pd.DataFrame, ticker: str) -> str:
    """모바일 최적화 차트 생성 — 폰트 크게, 고DPI"""

    # 모바일에서도 읽히도록 세로형 비율 + 큰 폰트
    fig = plt.figure(figsize=(12, 14), facecolor=DARK_BG)
    gs  = gridspec.GridSpec(4, 1, height_ratios=[4, 1.5, 1.5, 1], hspace=0.06)

    ax_main = fig.add_subplot(gs[0])
    ax_rsi  = fig.add_subplot(gs[1], sharex=ax_main)
    ax_macd = fig.add_subplot(gs[2], sharex=ax_main)
    ax_vol  = fig.add_subplot(gs[3], sharex=ax_main)

    TICK_SIZE  = 11  # 기존 8 → 11
    LABEL_SIZE = 12
    TITLE_SIZE = 14

    for ax in [ax_main, ax_rsi, ax_macd, ax_vol]:
        ax.set_facecolor(PANEL_BG)
        ax.tick_params(colors=GRAY, labelsize=TICK_SIZE, length=4)
        ax.spines[:].set_color("#30363d")
        ax.yaxis.set_label_position("right")
        ax.yaxis.tick_right()

    dates = mdates.date2num(df.index.to_pydatetime())
    width = 0.6

    # ── 캔들스틱 ──
    for i, (date, row) in enumerate(df.iterrows()):
        o, h, l, c = row["Open"], row["High"], row["Low"], row["Close"]
        color = GREEN if c >= o else RED
        ax_main.plot([dates[i], dates[i]], [l, h], color=color, linewidth=0.9)
        ax_main.add_patch(Rectangle(
            (dates[i] - width/2, min(o, c)), width, abs(c - o),
            facecolor=color, edgecolor=color, linewidth=0.5
        ))

    # ── 이동평균 ──
    ax_main.plot(dates, df["MA20"],  color=YELLOW, linewidth=1.5, label="MA20",  alpha=0.9)
    ax_main.plot(dates, df["MA60"],  color=BLUE,   linewidth=1.5, label="MA60",  alpha=0.9)
    ax_main.plot(dates, df["MA200"], color=WHITE,  linewidth=1.5, label="MA200", alpha=0.9)

    # ── 볼린저밴드 ──
    ax_main.fill_between(dates, df["BB_Upper"], df["BB_Lower"], alpha=0.06, color=BLUE)
    ax_main.plot(dates, df["BB_Upper"], color=BLUE, linewidth=0.7, linestyle="--", alpha=0.5)
    ax_main.plot(dates, df["BB_Lower"], color=BLUE, linewidth=0.7, linestyle="--", alpha=0.5)

    ax_main.legend(loc="upper left", fontsize=LABEL_SIZE, facecolor=PANEL_BG,
                   labelcolor=WHITE, framealpha=0.8)
    ax_main.set_title(f"{ticker}  |  Technical Analysis", color=WHITE,
                      fontsize=TITLE_SIZE, fontweight="bold", pad=12, loc="left")

    # ── RSI ──
    ax_rsi.plot(dates, df["RSI"], color=BLUE, linewidth=1.5)
    ax_rsi.axhline(70, color=RED,   linewidth=1.0, linestyle="--", alpha=0.7)
    ax_rsi.axhline(30, color=GREEN, linewidth=1.0, linestyle="--", alpha=0.7)
    ax_rsi.fill_between(dates, df["RSI"], 70, where=(df["RSI"] >= 70), alpha=0.2, color=RED)
    ax_rsi.fill_between(dates, df["RSI"], 30, where=(df["RSI"] <= 30), alpha=0.2, color=GREEN)
    ax_rsi.set_ylim(0, 100)
    ax_rsi.set_ylabel("RSI", color=GRAY, fontsize=LABEL_SIZE)
    rsi_val = df["RSI"].iloc[-1]
    ax_rsi.text(0.01, 0.80, f"RSI {rsi_val:.1f}", transform=ax_rsi.transAxes,
                color=BLUE, fontsize=LABEL_SIZE, fontweight="bold")

    # ── MACD ──
    ax_macd.plot(dates, df["MACD"],        color=BLUE,   linewidth=1.5, label="MACD")
    ax_macd.plot(dates, df["MACD_Signal"], color=ORANGE, linewidth=1.2, label="Signal")
    hist_colors = [GREEN if v >= 0 else RED for v in df["MACD_Hist"]]
    ax_macd.bar(dates, df["MACD_Hist"], width=width, color=hist_colors, alpha=0.7)
    ax_macd.axhline(0, color=GRAY, linewidth=0.6)
    ax_macd.set_ylabel("MACD", color=GRAY, fontsize=LABEL_SIZE)
    ax_macd.legend(loc="upper left", fontsize=TICK_SIZE, facecolor=PANEL_BG,
                   labelcolor=WHITE, framealpha=0.8)

    # ── Volume ──
    vol_colors = [GREEN if df["Close"].iloc[i] >= df["Open"].iloc[i] else RED
                  for i in range(len(df))]
    ax_vol.bar(dates, df["Volume"], width=width, color=vol_colors, alpha=0.8)
    ax_vol.axhline(df["Volume"].mean(), color=YELLOW, linewidth=1.0, linestyle="--", alpha=0.7)
    ax_vol.set_ylabel("VOL", color=GRAY, fontsize=LABEL_SIZE)

    # ── X축 날짜 ──
    ax_vol.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
    ax_vol.xaxis.set_major_locator(mdates.MonthLocator(interval=1))
    plt.setp(ax_vol.xaxis.get_majorticklabels(), rotation=0, ha="center",
             fontsize=TICK_SIZE, color=GRAY)
    plt.setp(ax_main.get_xticklabels(), visible=False)
    plt.setp(ax_rsi.get_xticklabels(),  visible=False)
    plt.setp(ax_macd.get_xticklabels(), visible=False)

    plt.tight_layout(pad=1.2)

    buf = io.BytesIO()
    # dpi=120 → 모바일에서 충분히 선명하면서 용량 적당
    plt.savefig(buf, format="png", dpi=120, facecolor=DARK_BG, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8")
