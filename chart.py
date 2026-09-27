# -*- coding: utf-8 -*-
"""
Vẽ GEX levels thành ảnh PNG (dark theme) để gửi Discord.

Chart: bar chart ngang — net GEX theo strike quanh spot.
  - Xanh: GEX dương (call gamma trội) · Đỏ: GEX âm (put gamma trội)
  - Đường trắng đứt: SPOT · Đường cam chấm: GAMMA FLIP
  - Viền sáng + nhãn: CALL WALL / PUT WALL
Yêu cầu: pip install matplotlib
"""
from __future__ import annotations

from datetime import date

# ------------------------- theme -------------------------
BG = "#131722"        # nền (kiểu TradingView dark)
PANEL = "#1c2030"     # hộp thông tin
FG = "#d1d4dc"        # chữ chính
MUTED = "#787b86"     # chữ phụ
GRID = "#232839"      # lưới
GREEN = "#26a69a"     # call gamma
RED = "#ef5350"       # put gamma
ORANGE = "#ffb74d"    # gamma flip
WHITE = "#ffffff"

MAX_BARS = 18         # số strike hiển thị (lấy theo |GEX| lớn nhất)
WINDOW_PCT = 5.0      # chỉ xét strike trong ±% so với spot


def _fmt(v: float) -> str:
    a = abs(v)
    if a >= 1e9:
        s = f"${a / 1e9:.2f}B"
    elif a >= 1e6:
        s = f"${a / 1e6:.0f}M"
    elif a >= 1e3:
        s = f"${a / 1e3:.0f}K"
    else:
        s = f"${a:.0f}"
    return ("+" if v >= 0 else "-") + s


def _lvl(k: float) -> str:
    """Format strike: thêm dấu phẩy nghìn cho index lớn (VD NDX 30,600)."""
    return f"{k:,.0f}" if k >= 1000 else f"{k:g}"


def render(sym: str, lv: dict, session_date: date, near_days: int | None = None) -> bytes:
    """Trả về bytes PNG của chart GEX cho 1 underlying."""
    import io

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    net, spot, flip, front = (lv["net_by_strike"], lv["spot"],
                              lv["gamma_flip"], lv["front"])

    # ---- chọn strikes: trong cửa sổ quanh spot, top theo |GEX| ----
    ks = [k for k in net if abs(k / spot - 1.0) * 100.0 <= WINDOW_PCT]
    ks = sorted(ks, key=lambda k: abs(net[k]), reverse=True)[:MAX_BARS]
    ks.sort()
    if not ks:
        raise ValueError("không có strike nào trong cửa sổ chart")
    vals = [net[k] for k in ks]

    diffs = [b - a for a, b in zip(ks, ks[1:])] or [1.0]
    step = sorted(diffs)[len(diffs) // 2]
    bar_h = step * 0.72
    pad = step * 1.6

    ylo, yhi = min(ks) - pad, max(ks) + pad
    if flip is not None and abs(flip / spot - 1.0) * 100.0 <= 10.0:
        if flip > spot:
            yhi = max(yhi, flip + pad)
        else:
            ylo = min(ylo, flip - pad)

    vmin, vmax = min(vals + [0.0]), max(vals + [0.0])
    xspan = max(vmax - vmin, 1.0)

    # ---- figure ----
    fig, ax = plt.subplots(figsize=(12.8, 7.6), dpi=100)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    fig.subplots_adjust(left=0.085, right=0.965, top=0.865, bottom=0.105)

    colors = [GREEN if v >= 0 else RED for v in vals]
    bars = ax.barh(ks, vals, height=bar_h, color=colors, zorder=3)

    # nhãn giá trị đầu mỗi bar
    for k, v in zip(ks, vals):
        if v >= 0:
            ax.text(v + xspan * 0.012, k, _fmt(v), ha="left", va="center",
                    fontsize=8, color=GREEN, zorder=4)
        else:
            ax.text(v - xspan * 0.012, k, _fmt(v), ha="right", va="center",
                    fontsize=8, color=RED, zorder=4)

    # ---- nhấn Call Wall / Put Wall ----
    cw, pw = lv["call_wall"], lv["put_wall"]
    for b, k in zip(bars, ks):
        if k == cw:
            b.set_edgecolor("#b2dfdb")
            b.set_linewidth(1.6)
        if k == pw:
            b.set_edgecolor("#ffcdd2")
            b.set_linewidth(1.6)
    if cw in ks:
        ax.annotate(f"CALL WALL {cw:g}", xy=(net[cw], cw),
                    xytext=(14, 6), textcoords="offset points",
                    ha="left", va="center", fontsize=10.5, fontweight="bold",
                    color="#a7ffeb", zorder=6,
                    arrowprops=dict(arrowstyle="->", color="#a7ffeb", lw=1.2))
    if pw in ks:
        ax.annotate(f"PUT WALL {pw:g}", xy=(net[pw], pw),
                    xytext=(-14, -8), textcoords="offset points",
                    ha="right", va="center", fontsize=10.5, fontweight="bold",
                    color="#ffcdd2", zorder=6,
                    arrowprops=dict(arrowstyle="->", color="#ffcdd2", lw=1.2))

    # ---- đường SPOT ----
    if ylo < spot < yhi:
        ax.axhline(spot, color=WHITE, ls="--", lw=1.3, zorder=2)
        ax.text(0.012, spot, f" SPOT {spot:.2f} ",
                transform=ax.get_yaxis_transform(), va="bottom", ha="left",
                fontsize=10, fontweight="bold", color=BG, zorder=6,
                bbox=dict(facecolor=WHITE, edgecolor="none", pad=1.6))

    # ---- đường GAMMA FLIP ----
    if flip is not None and ylo < flip < yhi:
        ax.axhline(flip, color=ORANGE, ls=":", lw=1.8, zorder=2)
        ax.text(0.988, flip, f" GAMMA FLIP {flip:.2f} ",
                transform=ax.get_yaxis_transform(), va="bottom", ha="right",
                fontsize=10, fontweight="bold", color=BG, zorder=6,
                bbox=dict(facecolor=ORANGE, edgecolor="none", pad=1.6))

    # ---- tiêu đề + phụ đề ----
    total = lv["total_gex"]
    regime = ("POSITIVE GAMMA — dealer hedging tends to pin / suppress volatility"
              if total >= 0 else
              "NEGATIVE GAMMA — dealer hedging tends to amplify volatility")
    fig.text(0.055, 0.955, f"{sym} · Gamma Levels — {session_date:%a %d %b %Y}",
             fontsize=16, fontweight="bold", color=WHITE, ha="left", va="center")
    fig.text(0.055, 0.917,
             f"Net GEX {_fmt(total)} per 1% move (all expiries) · {regime}"
             + (f" · walls/chart: expiry ≤ {near_days}d" if near_days else ""),
             fontsize=9.5, color=FG, ha="left", va="center")

    # ---- hộp thông tin front expiry ----
    if front:
        tag = "0DTE" if front["dte"] == 0 else f"{front['dte']}DTE"
        box = (f"Front expiry {front['date']:%d %b} ({tag})\n"
               f"Net {_fmt(front['net'])} · CW {_lvl(front['call_wall'])} · "
               f"PW {_lvl(front['put_wall'])}")
        ax.text(0.99, 0.03, box, transform=ax.transAxes, ha="right", va="bottom",
                fontsize=9, color=FG, zorder=5, linespacing=1.5,
                bbox=dict(boxstyle="round,pad=0.55", facecolor=PANEL,
                          edgecolor="#2a2f45"))

    # ---- trục, lưới ----
    ax.set_xlim(vmin - xspan * 0.28, vmax + xspan * 0.28)
    ax.set_ylim(ylo, yhi)
    ax.set_yticks(ks)
    ax.set_yticklabels([_lvl(k) for k in ks], fontsize=8.5, color=FG)
    ax.tick_params(axis="x", labelsize=9, colors=FG)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _: _fmt(x)))
    ax.set_xlabel("Net Gamma Exposure ($ per 1% move)", fontsize=9.5, color=MUTED)
    ax.set_ylabel("Strike", fontsize=9.5, color=MUTED)
    ax.grid(axis="x", color=GRID, lw=0.7, zorder=0)
    for s in ax.spines.values():
        s.set_visible(False)

    fig.text(0.055, 0.018,
             "Data: CBOE delayed · naive dealer GEX (calls +, puts −) · "
             "educational — not investment advice",
             fontsize=8, color=MUTED, ha="left", va="center")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()
