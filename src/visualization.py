"""为单只股票渲染一张交易方案图。

包含：
  • K 线图（取三推起点之前约 20 根至最新）
  • 三推高/低点 (H1/H2/H3 或 L1/L2/L3) + 三推起点 (s0)
  • 周/月线 PDA 区间（半透明矩形）
  • 入场 / 止损 / 目标 水平线及 R:R
  • MACD 子图，三次金/死叉位置高亮
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import platform

import matplotlib.dates as mdates
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib import font_manager

from .config import load_config
from .indicators.macd import compute_macd, detect_triple_divergence as detect_macd
from .indicators.pda import PDAResult, detect_htf_pda_hit
from .indicators.swing import find_swing_points
from .indicators.three_push import ThreePushResult, detect_three_push

logger = logging.getLogger(__name__)
Direction = Literal["top", "bottom"]

# 中文字体配置：按平台精准设置，**把当前平台的 CJK 字体放在列表最前**。
# 必须这样做的原因：matplotlib 的 findfont 会"模糊匹配"，如果列表第一个找不到，
# 它常会回退到一个相似度高的非 CJK 字体（如 DejaVu Sans），导致中文渲染成方框。

def _pick_cjk_fonts() -> list[str]:
    """探测当前系统已安装的 CJK 字体，按可用性排序返回。"""
    system_fonts = {f.name for f in font_manager.fontManager.ttflist}
    plat = platform.system()
    # 按平台分别准备一份"应该出现"的字体清单
    if plat == "Windows":
        candidates = ["Microsoft YaHei", "SimHei", "Microsoft JhengHei", "DengXian"]
    elif plat == "Darwin":  # macOS
        candidates = ["PingFang SC", "Hiragino Sans GB", "Heiti SC", "STHeiti", "Songti SC"]
    else:  # Linux / GitHub Actions
        candidates = [
            "Noto Sans CJK SC", "Noto Sans CJK JP", "Noto Sans CJK",
            "WenQuanYi Micro Hei", "WenQuanYi Zen Hei",
            "Source Han Sans SC", "Source Han Sans CN",
        ]
    found = [c for c in candidates if c in system_fonts]
    if not found:
        # 兜底：扫一遍所有字体，凡名字里带 CJK/Noto/Han/YaHei/SimHei/WenQuanYi 的都收
        found = sorted({
            n for n in system_fonts
            if any(k in n for k in ("CJK", "Noto Sans", "Han Sans",
                                     "YaHei", "SimHei", "WenQuanYi", "PingFang"))
        })
    return found


_CJK_FONTS = _pick_cjk_fonts()
plt.rcParams["font.family"] = "sans-serif"
plt.rcParams["font.sans-serif"] = _CJK_FONTS + ["Arial Unicode MS", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False
if not _CJK_FONTS:
    logger.warning(
        "未检测到任何 CJK 字体！中文将被渲染为方框。请安装 fonts-noto-cjk "
        "(Linux) / SimHei (Windows) / PingFang SC (macOS)"
    )


@dataclass(frozen=True)
class TradePlan:
    entry: float
    stop: float
    target: float | None
    rr: float | None


def _currency() -> str:
    return load_config().get("runtime", {}).get("currency_symbol", "¥")


def _build_trade_plan(
    direction: Direction, target_price: float,
    pda: PDAResult, three_push: ThreePushResult,
    stop_buffer_pct: float = 0.01,
) -> TradePlan | None:
    if not pda.hit:
        return None
    zone = pda.hits[0].zone
    origin = three_push.origin.price if (three_push.hit and three_push.origin) else None
    if direction == "bottom":
        stop = zone.zone_low * (1 - stop_buffer_pct)
        if origin and origin > target_price > stop:
            rr = (origin - target_price) / (target_price - stop)
            return TradePlan(target_price, stop, origin, rr)
        return TradePlan(target_price, stop, None, None)
    stop = zone.zone_high * (1 + stop_buffer_pct)
    if origin and stop > target_price > origin:
        rr = (target_price - origin) / (stop - target_price)
        return TradePlan(target_price, stop, origin, rr)
    return TradePlan(target_price, stop, None, None)


def _draw_candles(ax, df: pd.DataFrame) -> None:
    """A 股配色：阳红阴绿（与美股相反）。"""
    bar_width = 0.6
    for date, row in df.iterrows():
        x = mdates.date2num(date)
        o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
        up = c >= o
        color = "#d62728" if up else "#2ca02c"   # CN 习惯：红涨绿跌
        ax.plot([x, x], [l, h], color=color, linewidth=0.7, solid_capstyle="butt")
        body_low = min(o, c)
        body_height = max(abs(c - o), h * 0.0005)
        ax.add_patch(mpatches.Rectangle(
            (x - bar_width / 2, body_low), bar_width, body_height,
            facecolor=color, edgecolor=color, linewidth=0.5, alpha=0.85,
        ))


def render_signal_chart(
    ticker: str,
    name: str,
    df: pd.DataFrame,
    direction: Direction,
    score: float,
    notes: str,
    output_path: Path,
) -> Path:
    """重新运行各检测器以获取完整对象，然后输出 PNG。"""
    cur = _currency()
    three_push = detect_three_push(df, direction=direction)
    macd_res = detect_macd(df, direction=direction)
    swings = find_swing_points(df)

    target_kind = "high" if direction == "top" else "low"
    same_kind = [s for s in swings if s.kind == target_kind]
    target_swing = same_kind[-1] if same_kind else None
    target_price = target_swing.price if target_swing else float(df["Close"].iloc[-1])

    pda_res = detect_htf_pda_hit(df, target_price=target_price, direction=direction)
    plan = _build_trade_plan(direction, target_price, pda_res, three_push)

    if three_push.origin is not None:
        start_idx = max(0, three_push.origin.idx - 20)
    else:
        start_idx = max(0, len(df) - 120)
    # 若 MACD 检测出了 3 段 wave 且首段早于当前窗口起点，则扩大窗口
    if macd_res.waves and macd_res.hit_kind in ("strict", "loose"):
        macd_first = min(w.start_cross_idx for w in macd_res.waves)
        start_idx = min(start_idx, max(0, macd_first - 10))
    end_idx = len(df) - 1
    plot_df = df.iloc[start_idx : end_idx + 1]
    macd_df = compute_macd(df["Close"]).iloc[start_idx : end_idx + 1]

    fig, (ax_price, ax_macd) = plt.subplots(
        2, 1, figsize=(16, 10), sharex=True,
        gridspec_kw={"height_ratios": [3, 1], "hspace": 0.08},
    )

    _draw_candles(ax_price, plot_df)
    ax_price.set_ylabel(f"价格 ({cur})", fontsize=11)
    ax_price.grid(True, alpha=0.25, linestyle="--")
    ax_price.set_axisbelow(True)

    # CN 习惯：红涨绿跌；做空对应红 zone，做多对应绿 zone
    color_up, color_down = "#d62728", "#2ca02c"
    is_short = direction == "top"

    if pda_res.hit:
        zone = pda_res.hits[0].zone
        tf = pda_res.hits[0].timeframe
        x_lo = mdates.date2num(plot_df.index[0])
        x_hi = mdates.date2num(plot_df.index[-1]) + 5
        zone_color = "#ffaaaa" if is_short else "#aaffaa"
        ax_price.add_patch(mpatches.Rectangle(
            (x_lo, zone.zone_low), x_hi - x_lo, zone.zone_high - zone.zone_low,
            facecolor=zone_color, alpha=0.30, edgecolor=zone_color,
            linewidth=1.2, linestyle="--",
        ))
        tf_word = "周线" if tf == "W" else "月线"
        ax_price.text(
            x_hi, (zone.zone_high + zone.zone_low) / 2,
            f"  {tf_word}\n  {pda_res.best_quality}",
            va="center", fontsize=10, color="#333", fontweight="bold",
        )

    if three_push.hit:
        for i, ext in enumerate(three_push.extremes, start=1):
            x = mdates.date2num(df.index[ext.idx])
            label = f"H{i}" if direction == "top" else f"L{i}"
            marker_color = color_up if is_short else color_down
            ax_price.scatter(
                x, ext.price, marker="v" if is_short else "^",
                s=180, color=marker_color, zorder=6, edgecolors="black", linewidths=1,
            )
            ax_price.annotate(
                f"{label}\n{cur}{ext.price:.2f}",
                xy=(x, ext.price),
                xytext=(0, 18 if is_short else -28),
                textcoords="offset points", ha="center",
                fontsize=10, fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.3", facecolor="lightyellow",
                          edgecolor=marker_color, linewidth=1.2),
            )
        if three_push.origin is not None:
            s0 = three_push.origin
            x = mdates.date2num(df.index[s0.idx])
            ax_price.scatter(
                x, s0.price, marker="*", s=320, color="gold",
                edgecolors="black", linewidths=1.5, zorder=6,
            )
            ax_price.annotate(
                f"三推起点\n{cur}{s0.price:.2f}",
                xy=(x, s0.price),
                xytext=(0, -28 if is_short else 18),
                textcoords="offset points", ha="center",
                fontsize=9, fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.3", facecolor="#fff8e1",
                          edgecolor="orange", linewidth=1.2),
            )

    if plan is not None:
        x_lo = mdates.date2num(plot_df.index[0])
        x_hi = mdates.date2num(plot_df.index[-1]) + 5
        line_kwargs = dict(linewidth=1.5, alpha=0.9)
        ax_price.hlines(plan.entry, x_lo, x_hi, color="#1f77b4", linestyle="-", **line_kwargs)
        ax_price.text(x_lo, plan.entry, f"入{cur}{plan.entry:.2f}", va="center",
                      ha="left", fontsize=10, color="#1f77b4", fontweight="bold",
                      bbox=dict(facecolor="white", edgecolor="#1f77b4", boxstyle="round,pad=0.2"))
        ax_price.hlines(plan.stop, x_lo, x_hi, color="#ff7f0e", linestyle="--", **line_kwargs)
        ax_price.text(x_lo, plan.stop, f"止{cur}{plan.stop:.2f}", va="center",
                      ha="left", fontsize=10, color="#ff7f0e", fontweight="bold",
                      bbox=dict(facecolor="white", edgecolor="#ff7f0e", boxstyle="round,pad=0.2"))
        if plan.target is not None:
            target_color = color_down if is_short else color_up
            ax_price.hlines(plan.target, x_lo, x_hi, color=target_color, linestyle="--", **line_kwargs)
            label = f"标{cur}{plan.target:.2f}  R:R {plan.rr:.1f}" if plan.rr else f"标{cur}{plan.target:.2f}"
            ax_price.text(x_lo, plan.target, label, va="center",
                          ha="left", fontsize=10, color=target_color, fontweight="bold",
                          bbox=dict(facecolor="white", edgecolor=target_color, boxstyle="round,pad=0.2"))

    # MACD 子图
    ax_macd.plot(macd_df.index, macd_df["dif"], color="#1f77b4", linewidth=1.2, label="DIF")
    ax_macd.plot(macd_df.index, macd_df["dea"], color="#ff7f0e", linewidth=1.2, label="DEA")
    hist_colors = ["#d62728" if v >= 0 else "#2ca02c" for v in macd_df["hist"]]
    ax_macd.bar(macd_df.index, macd_df["hist"], color=hist_colors, width=0.7, alpha=0.7)
    ax_macd.axhline(0, color="black", linewidth=0.5)
    ax_macd.grid(True, alpha=0.25, linestyle="--")
    ax_macd.legend(loc="upper left", fontsize=9)
    ax_macd.set_ylabel("MACD", fontsize=10)

    # ── MACD 背离三点标注 + 连线（在价格图与 MACD 子图上）──
    # 仅当严格/宽松命中时绘制（miss 不画，避免误导）
    if macd_res.waves and macd_res.hit_kind in ("strict", "loose"):
        macd_full = compute_macd(df["Close"])
        waves = macd_res.waves
        is_top_div = direction == "top"
        div_color = "#6a1b9a" if macd_res.hit_kind == "strict" else "#e65100"  # 紫=严格, 橙=宽松

        # 三个背离点：(extreme_idx, extreme_price) 在价格图；
        # 对应同一根 bar 上 DIF 的值 在 MACD 子图
        ext_idxs = [w.extreme_idx for w in waves]
        ext_prices = [w.extreme_price for w in waves]
        xs = [mdates.date2num(df.index[i]) for i in ext_idxs]
        dif_at_ext = [float(macd_full["dif"].iloc[i]) for i in ext_idxs]

        # 价格图：三点 + 虚线 (M1-M2-M3)
        ax_price.plot(xs, ext_prices, color=div_color, linewidth=1.5,
                      linestyle="--", alpha=0.85, zorder=4)
        for i, (x, y) in enumerate(zip(xs, ext_prices), 1):
            ax_price.scatter(x, y, marker="D", s=80, color=div_color,
                             edgecolors="black", linewidths=0.8, zorder=6)
            # 标签放在远离三推 H/L 标签的一侧，避免重叠
            dy = -28 if is_top_div else 22
            ax_price.annotate(
                f"M{i}\n{cur}{y:.2f}",
                xy=(x, y),
                xytext=(0, dy),
                textcoords="offset points", ha="center",
                fontsize=9, color=div_color, fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.25", facecolor="white",
                          edgecolor=div_color, linewidth=1.0, alpha=0.9),
            )

        # MACD 子图：DIF 同位置三点 + 虚线 + cross_value 标注
        ax_macd.plot(xs, dif_at_ext, color=div_color, linewidth=1.5,
                     linestyle="--", alpha=0.85, zorder=4)
        for i, (x, y, w) in enumerate(zip(xs, dif_at_ext, waves), 1):
            ax_macd.scatter(x, y, marker="D", s=70, color=div_color,
                            edgecolors="black", linewidths=0.8, zorder=6)
            ax_macd.annotate(
                f"M{i} {y:+.2f}",
                xy=(x, y),
                xytext=(6, 8 if not is_top_div else -14),
                textcoords="offset points",
                fontsize=8, color=div_color, fontweight="bold",
            )

        # 同时在 MACD 子图标出三次金/死叉位置（细竖虚线）
        for w in waves:
            ax_macd.axvline(df.index[w.start_cross_idx], color=div_color,
                            alpha=0.25, linestyle=":", linewidth=0.8)

    ax_macd.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax_macd.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    plt.setp(ax_macd.get_xticklabels(), rotation=30, ha="right")

    signal_word = "做空 SHORT" if is_short else "做多 LONG"
    signal_color = color_up if is_short else color_down
    macd_tag = ""
    if macd_res.hit_kind == "strict":
        macd_tag = f"  · MACD严格 {macd_res.n_passed}/{macd_res.n_total}"
    elif macd_res.hit_kind == "loose":
        failed = macd_res.failed_rules[0].code if macd_res.failed_rules else "?"
        macd_tag = f"  · MACD宽松 {macd_res.n_passed}/{macd_res.n_total} (差{failed})"
    header = f"{ticker}  ({name})  [得分 {score}]  {signal_word}{macd_tag}"
    fig.suptitle(header, fontsize=16, fontweight="bold", color=signal_color, y=0.97)

    fig.text(
        0.5, 0.01, notes,
        ha="center", va="bottom", fontsize=11,
        bbox=dict(facecolor="#f5f5f5", edgecolor="#999", boxstyle="round,pad=0.6"),
        wrap=True,
    )

    plt.tight_layout(rect=[0, 0.06, 1, 0.95])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=120, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    logger.info("Chart written → %s", output_path)
    return output_path
