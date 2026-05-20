"""MACD 严格三重背离检测 —— 同时支持顶背离（做空）和底背离（做多）。

顶背离（看跌）：
  1. 三个 swing 高点递增：p1 < p2 < p3
  2. 三次金叉的 DIF 值严格递减
  3. 两次回调中 DIF 自上方逼近 0，但不破 0
  4. 三段红柱面积严格递减，且每次减少 ≥ min_area_reduction

底背离（看涨）—— 镜像：
  1. 三个 swing 低点递减：p1 > p2 > p3
  2. 三次死叉的 DIF 值严格递增（越来越接近 0）
  3. 两次反弹中 DIF 自下方逼近 0，但不破 0
  4. 三段绿柱面积（|hist|）严格递减
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd

from ..config import load_config

Direction = Literal["top", "bottom"]


@dataclass
class Wave:
    direction: Literal["up", "down"]
    start_cross_idx: int
    end_cross_idx: int
    cross_value: float
    extreme_idx: int
    extreme_price: float
    hist_area: float

    def __repr__(self) -> str:
        return (
            f"Wave({self.direction}, cross@{self.start_cross_idx}={self.cross_value:+.4f}, "
            f"ext@{self.extreme_idx}={self.extreme_price:.2f}, area={self.hist_area:.2f})"
        )


UpWave = Wave  # backwards-compat alias


@dataclass(frozen=True)
class RuleCheck:
    """一条 MACD 背离规则的检查结果。"""
    code: str           # R1..R5
    name: str           # 中文规则名
    passed: bool
    detail: str = ""    # 数值或失败原因

    def label(self) -> str:
        mark = "✓" if self.passed else "✗"
        return f"{mark} {self.code} {self.name}"

    def full(self) -> str:
        s = self.label()
        return f"{s}: {self.detail}" if self.detail else s


HitKind = Literal["strict", "loose", "miss"]


@dataclass
class DivergenceResult:
    hit: bool                                     # True 仅当严格命中（5/5 规则）
    hit_kind: HitKind = "miss"                    # strict | loose (差 1 条) | miss
    direction: Direction = "top"
    waves: list[Wave] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    rule_checks: list[RuleCheck] = field(default_factory=list)
    strength: float = 0.0

    @property
    def n_passed(self) -> int:
        return sum(1 for c in self.rule_checks if c.passed)

    @property
    def n_total(self) -> int:
        return len(self.rule_checks)

    @property
    def failed_rules(self) -> list[RuleCheck]:
        return [c for c in self.rule_checks if not c.passed]

    def summary(self) -> str:
        if self.hit_kind == "miss":
            return f"NO HIT ({self.direction}) — " + (
                "; ".join(self.reasons) if self.reasons else "unknown"
            )
        kind_word = "严格" if self.hit_kind == "strict" else "宽松"
        score = f"{self.n_passed}/{self.n_total}"
        if self.waves:
            peaks = " → ".join(f"{w.extreme_price:.2f}" for w in self.waves)
            crosses = " → ".join(f"{w.cross_value:+.3f}" for w in self.waves)
            areas = " → ".join(f"{w.hist_area:.1f}" for w in self.waves)
            return (
                f"{kind_word}命中 {self.direction} [{score}] (strength {self.strength:.2f}) | "
                f"extremes {peaks} | crossovers {crosses} | areas {areas}"
            )
        return f"{kind_word}命中 {self.direction} [{score}]"


def compute_macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    """标准 MACD；柱状图按国内常见的 hist = (dif - dea) × 2 约定。"""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = (dif - dea) * 2
    return pd.DataFrame({"dif": dif, "dea": dea, "hist": hist}, index=close.index)


def find_crossovers(dif: pd.Series, dea: pd.Series) -> pd.DataFrame:
    delta = dif - dea
    prev_delta = delta.shift(1)
    up_cross = (prev_delta <= 0) & (delta > 0)
    down_cross = (prev_delta >= 0) & (delta < 0)

    records: list[dict] = []
    for pos, is_up in enumerate(up_cross.values):
        if is_up:
            records.append({"idx": pos, "type": "up", "value": float(dif.iloc[pos])})
    for pos, is_down in enumerate(down_cross.values):
        if is_down:
            records.append({"idx": pos, "type": "down", "value": float(dif.iloc[pos])})

    if not records:
        return pd.DataFrame(columns=["idx", "type", "value"]).astype(
            {"idx": int, "type": object, "value": float}
        )
    return pd.DataFrame(records).sort_values("idx").reset_index(drop=True)


def _build_waves(
    price: pd.Series,
    hist: pd.Series,
    crossovers: pd.DataFrame,
    start_type: Literal["up", "down"],
    end_type: Literal["up", "down"],
    extreme_op: Literal["max", "min"],
) -> list[Wave]:
    if crossovers.empty:
        return []

    last_bar = len(price) - 1
    starts = crossovers[crossovers["type"] == start_type]
    ends = crossovers[crossovers["type"] == end_type]
    waves: list[Wave] = []

    for _, row in starts.iterrows():
        start_idx = int(row["idx"])
        nxt = ends[ends["idx"] > start_idx]
        end_idx = int(nxt["idx"].iloc[0]) if len(nxt) else last_bar
        if end_idx < start_idx:
            continue

        seg_price = price.iloc[start_idx : end_idx + 1].values
        seg_hist = hist.iloc[start_idx : end_idx + 1].values
        if len(seg_price) == 0:
            continue

        ext_off = int(np.argmax(seg_price)) if extreme_op == "max" else int(np.argmin(seg_price))
        hist_area = float(np.abs(seg_hist).sum())

        waves.append(Wave(
            direction="up" if start_type == "up" else "down",
            start_cross_idx=start_idx,
            end_cross_idx=end_idx,
            cross_value=float(row["value"]),
            extreme_idx=start_idx + ext_off,
            extreme_price=float(seg_price[ext_off]),
            hist_area=hist_area,
        ))
    return waves


def build_up_waves(high: pd.Series, hist: pd.Series, crossovers: pd.DataFrame) -> list[Wave]:
    return _build_waves(high, hist, crossovers, "up", "down", "max")


def build_down_waves(low: pd.Series, hist: pd.Series, crossovers: pd.DataFrame) -> list[Wave]:
    return _build_waves(low, hist, crossovers, "down", "up", "min")


def check_divergence_rules(
    w1: Wave, w2: Wave, w3: Wave,
    dif: pd.Series,
    *,
    direction: Direction,
    bars_since_last_peak: int,
    min_area_reduction: float,
    dif_zero_tolerance: float,
    dif_approach_zero_ratio: float,
    min_price_increase_pct: float,
    recency_bars: int,
) -> tuple[list[RuleCheck], float]:
    """检查 5 条规则，返回逐条结果 + 形态强度（强度无论是否完美命中都会计算）。"""
    checks: list[RuleCheck] = []
    p1, p2, p3 = w1.extreme_price, w2.extreme_price, w3.extreme_price
    c1, c2, c3 = w1.cross_value, w2.cross_value, w3.cross_value
    a1, a2, a3 = w1.hist_area, w2.hist_area, w3.hist_area

    # ── R1: 价格三推创新极值 ──
    if direction == "top":
        inc_12 = (p2 - p1) / p1 if p1 > 0 else 0
        inc_23 = (p3 - p2) / p2 if p2 > 0 else 0
        r1_ok = inc_12 >= min_price_increase_pct and inc_23 >= min_price_increase_pct
        r1_detail = (
            f"{p1:.2f}→{p2:.2f}→{p3:.2f} (+{inc_12*100:.2f}%/+{inc_23*100:.2f}%)"
            if r1_ok else
            f"价格未创新高 {p1:.2f}→{p2:.2f}→{p3:.2f} (+{inc_12*100:.2f}%/+{inc_23*100:.2f}%)"
        )
        r1_name = "价格三推创新高"
    else:
        dec_12 = (p1 - p2) / p1 if p1 > 0 else 0
        dec_23 = (p2 - p3) / p2 if p2 > 0 else 0
        r1_ok = dec_12 >= min_price_increase_pct and dec_23 >= min_price_increase_pct
        r1_detail = (
            f"{p1:.2f}→{p2:.2f}→{p3:.2f} (-{dec_12*100:.2f}%/-{dec_23*100:.2f}%)"
            if r1_ok else
            f"价格未创新低 {p1:.2f}→{p2:.2f}→{p3:.2f} (-{dec_12*100:.2f}%/-{dec_23*100:.2f}%)"
        )
        r1_name = "价格三推创新低"
    checks.append(RuleCheck("R1", r1_name, r1_ok, r1_detail))

    # ── R2: DIF 交叉值单调收敛（逼近零轴）──
    if direction == "top":
        r2_ok = c1 > c2 > c3
        r2_name = "DIF 金叉值递减"
    else:
        r2_ok = c1 < c2 < c3
        r2_name = "DIF 死叉值递增"
    r2_detail = f"{c1:+.3f}→{c2:+.3f}→{c3:+.3f}"
    checks.append(RuleCheck("R2", r2_name, r2_ok, r2_detail))

    # ── R3: DIF 回调逼近零轴（不破零 + 充分逼近）──
    r3_problems: list[str] = []
    for k, (wA, wB) in enumerate([(w1, w2), (w2, w3)], start=1):
        seg = dif.iloc[wA.end_cross_idx : wB.start_cross_idx + 1]
        if len(seg) == 0:
            continue
        if direction == "top":
            seg_min = float(seg.min())
            if seg_min < -dif_zero_tolerance:
                r3_problems.append(f"回调{k}破零(min={seg_min:.3f})")
            prev_cross = wA.cross_value
            if prev_cross > 0:
                thr = prev_cross * dif_approach_zero_ratio
                if seg_min > thr:
                    r3_problems.append(f"回调{k}未逼近(min={seg_min:.3f}需≤{thr:.3f})")
        else:
            seg_max = float(seg.max())
            if seg_max > dif_zero_tolerance:
                r3_problems.append(f"反弹{k}破零(max={seg_max:.3f})")
            prev_cross = wA.cross_value
            if prev_cross < 0:
                thr = prev_cross * dif_approach_zero_ratio
                if seg_max < thr:
                    r3_problems.append(f"反弹{k}未逼近(max={seg_max:.3f}需≥{thr:.3f})")
    r3_ok = len(r3_problems) == 0
    r3_detail = "两次回调均逼近零未破" if r3_ok else "; ".join(r3_problems)
    checks.append(RuleCheck("R3", "DIF 回调逼近零轴", r3_ok, r3_detail))

    # ── R4: 柱面积严格衰减（递减 + 达到最低衰减幅度）──
    red_12 = (a1 - a2) / a1 if a1 > 0 else 0
    red_23 = (a2 - a3) / a2 if a2 > 0 else 0
    r4_monotonic = a1 > a2 > a3
    r4_enough = red_12 >= min_area_reduction and red_23 >= min_area_reduction
    r4_ok = r4_monotonic and r4_enough
    if r4_ok:
        r4_detail = f"{a1:.2f}→{a2:.2f}→{a3:.2f} 衰减{red_12*100:.0f}%/{red_23*100:.0f}%"
    elif not r4_monotonic:
        r4_detail = f"非严格递减 {a1:.2f}/{a2:.2f}/{a3:.2f}"
    else:
        r4_detail = (
            f"衰减不足 {red_12*100:.0f}%/{red_23*100:.0f}% "
            f"(需≥{min_area_reduction*100:.0f}%)"
        )
    checks.append(RuleCheck("R4", "柱面积严格衰减", r4_ok, r4_detail))

    # ── R5: 第三推时效 ──
    r5_ok = bars_since_last_peak <= recency_bars
    which = "顶" if direction == "top" else "底"
    r5_detail = f"第三{which}距今 {bars_since_last_peak} 根 (≤{recency_bars})"
    checks.append(RuleCheck("R5", "第三推时效", r5_ok, r5_detail))

    # ── 强度（永远计算）──
    if direction == "top":
        price_score = min(max((p3 - p1) / max(p1, 1e-6), 0), 0.5) / 0.5
        cross_score = min(max((c1 - c3) / max(c1, 1e-6), 0), 0.9) / 0.9
    else:
        price_score = min(max((p1 - p3) / max(p1, 1e-6), 0), 0.5) / 0.5
        cross_score = min(max((c3 - c1) / max(abs(c1), 1e-6), 0), 0.9) / 0.9
    area_score = min(max((a1 - a3) / max(a1, 1e-6), 0), 0.9) / 0.9
    strength = float(np.clip((price_score + cross_score + area_score) / 3, 0, 1))
    return checks, strength


def detect_triple_divergence(
    df: pd.DataFrame,
    *,
    direction: Direction = "top",
    fast: int | None = None,
    slow: int | None = None,
    signal: int | None = None,
    min_area_reduction: float | None = None,
    dif_zero_tolerance: float | None = None,
    dif_approach_zero_ratio: float | None = None,
    min_price_increase_pct: float | None = None,
    recency_bars: int | None = None,
) -> DivergenceResult:
    cfg = load_config()["macd"]
    dcfg = cfg["divergence"]

    fast = fast if fast is not None else cfg["fast"]
    slow = slow if slow is not None else cfg["slow"]
    signal = signal if signal is not None else cfg["signal"]
    min_area_reduction = min_area_reduction if min_area_reduction is not None else dcfg["min_area_reduction"]
    dif_zero_tolerance = dif_zero_tolerance if dif_zero_tolerance is not None else dcfg["dif_zero_tolerance"]
    dif_approach_zero_ratio = dif_approach_zero_ratio if dif_approach_zero_ratio is not None else dcfg["dif_approach_zero_ratio"]
    min_price_increase_pct = min_price_increase_pct if min_price_increase_pct is not None else dcfg["min_price_increase_pct"]
    recency_bars = recency_bars if recency_bars is not None else dcfg["recency_bars"]

    needed_col = "High" if direction == "top" else "Low"
    if "Close" not in df.columns or needed_col not in df.columns:
        return DivergenceResult(
            hit=False, hit_kind="miss", direction=direction,
            reasons=[f"df 缺失 Close 或 {needed_col} 列"],
        )
    if len(df) < slow * 2:
        return DivergenceResult(
            hit=False, hit_kind="miss", direction=direction,
            reasons=[f"仅 {len(df)} 根 K 线, 需 ≥ {slow * 2}"],
        )

    macd_df = compute_macd(df["Close"], fast, slow, signal)
    crossovers = find_crossovers(macd_df["dif"], macd_df["dea"])

    if direction == "top":
        waves = build_up_waves(df["High"], macd_df["hist"], crossovers)
    else:
        waves = build_down_waves(df["Low"], macd_df["hist"], crossovers)

    if len(waves) < 3:
        return DivergenceResult(
            hit=False, hit_kind="miss", direction=direction,
            reasons=[f"仅 {len(waves)} 段 {direction}-wave; 需 ≥ 3"],
        )

    w1, w2, w3 = waves[-3:]
    bars_since_last = len(df) - 1 - w3.extreme_idx
    checks, raw_strength = check_divergence_rules(
        w1, w2, w3, macd_df["dif"],
        direction=direction,
        bars_since_last_peak=bars_since_last,
        min_area_reduction=min_area_reduction,
        dif_zero_tolerance=dif_zero_tolerance,
        dif_approach_zero_ratio=dif_approach_zero_ratio,
        min_price_increase_pct=min_price_increase_pct,
        recency_bars=recency_bars,
    )
    failed = [c for c in checks if not c.passed]
    reasons = [c.detail for c in failed]

    if len(failed) == 0:
        return DivergenceResult(
            hit=True, hit_kind="strict", direction=direction,
            waves=[w1, w2, w3], rule_checks=checks, strength=raw_strength,
        )
    if len(failed) == 1:
        # 宽松命中：差 1 条规则，强度打 5 折以反映质量折扣
        return DivergenceResult(
            hit=False, hit_kind="loose", direction=direction,
            waves=[w1, w2, w3], rule_checks=checks,
            reasons=reasons, strength=raw_strength * 0.5,
        )
    return DivergenceResult(
        hit=False, hit_kind="miss", direction=direction,
        waves=[w1, w2, w3], rule_checks=checks,
        reasons=reasons, strength=0.0,
    )


def detect_both_directions(df: pd.DataFrame, **kwargs) -> dict[Direction, DivergenceResult]:
    return {
        "top": detect_triple_divergence(df, direction="top", **kwargs),
        "bottom": detect_triple_divergence(df, direction="bottom", **kwargs),
    }


if __name__ == "__main__":
    import sys
    from ..data_fetcher import load_prices

    ticker = sys.argv[1] if len(sys.argv) > 1 else "600519"
    df = load_prices(ticker)
    if df is None:
        print(f"No cached prices for {ticker}. Run data_fetcher first.")
        sys.exit(1)

    results = detect_both_directions(df)
    for direction, res in results.items():
        print(f"{ticker} [{direction:>6}]: {res.summary()}")
