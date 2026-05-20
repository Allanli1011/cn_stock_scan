"""PA 三推（Three-Push）形态识别 —— 两次回撤分别约 75%。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

from ..config import load_config
from .swing import SwingPoint, find_swing_points

Direction = Literal["top", "bottom"]


@dataclass(frozen=True)
class ThreePushResult:
    hit: bool
    direction: Direction
    origin: SwingPoint | None = None
    extremes: tuple[SwingPoint, ...] = ()
    intermediates: tuple[SwingPoint, ...] = ()
    pullbacks: tuple[float, float] | None = None
    reasons: tuple[str, ...] = ()
    quality: float = 0.0

    def summary(self) -> str:
        if not self.hit:
            return f"NO HIT ({self.direction}) — " + "; ".join(self.reasons)
        prices = " → ".join(f"{e.price:.2f}" for e in self.extremes)
        pulls = " → ".join(f"{p*100:.0f}%" for p in self.pullbacks)
        origin = f"origin@{self.origin.price:.2f} | " if self.origin else ""
        return (
            f"HIT {self.direction} (quality {self.quality:.2f}) | "
            f"{origin}extremes {prices} | retracements {pulls}"
        )


def _expected_sequence(direction: Direction) -> list[str]:
    if direction == "top":
        return ["low", "high", "low", "high", "low", "high"]
    return ["high", "low", "high", "low", "high", "low"]


def _take_six_ending_with(
    swings: list[SwingPoint], ending_kind: str,
) -> list[SwingPoint] | None:
    for i in range(len(swings) - 1, -1, -1):
        if swings[i].kind == ending_kind:
            if i < 5:
                return None
            return swings[i - 5 : i + 1]
    return None


def _compute_retracements(
    six: list[SwingPoint], direction: Direction,
) -> tuple[float, float] | None:
    s0, e1, s1, e2, s2, e3 = six
    if direction == "top":
        push1 = e1.price - s0.price
        pull1 = e1.price - s1.price
        push2 = e2.price - s1.price
        pull2 = e2.price - s2.price
    else:
        push1 = s0.price - e1.price
        pull1 = s1.price - e1.price
        push2 = s1.price - e2.price
        pull2 = s2.price - e2.price
    if push1 <= 0 or push2 <= 0:
        return None
    return pull1 / push1, pull2 / push2


def detect_three_push(
    df: pd.DataFrame,
    direction: Direction = "top",
    *,
    pct_threshold: float | None = None,
    pullback_target: float | None = None,
    pullback_tolerance: float | None = None,
    recency_bars: int | None = None,
) -> ThreePushResult:
    cfg = load_config()
    pct_threshold = pct_threshold if pct_threshold is not None else cfg["swing"]["pct_threshold"]
    tp = cfg["three_push"]
    pullback_target = pullback_target if pullback_target is not None else tp["pullback_target_pct"]
    pullback_tolerance = pullback_tolerance if pullback_tolerance is not None else tp["pullback_tolerance"]
    recency_bars = recency_bars if recency_bars is not None else cfg["macd"]["divergence"]["recency_bars"]

    swings = find_swing_points(df, pct_threshold=pct_threshold)
    ending_kind = "high" if direction == "top" else "low"

    six = _take_six_ending_with(swings, ending_kind)
    if six is None:
        return ThreePushResult(
            hit=False, direction=direction,
            reasons=(f"以 '{ending_kind}' 结尾的 swing 不足 (共 {len(swings)} 个)",),
        )

    actual = [s.kind for s in six]
    expected = _expected_sequence(direction)
    if actual != expected:
        return ThreePushResult(
            hit=False, direction=direction,
            reasons=(f"swing 高低交替断裂 (得到 {actual})",),
        )

    s0, e1, s1, e2, s2, e3 = six
    extremes = (e1, e2, e3)
    intermediates = (s1, s2)

    if direction == "top":
        if not (e1.price < e2.price < e3.price):
            return ThreePushResult(
                hit=False, direction=direction, extremes=extremes,
                reasons=(f"高点未严格递增 "
                         f"({e1.price:.2f}, {e2.price:.2f}, {e3.price:.2f})",),
            )
    else:
        if not (e1.price > e2.price > e3.price):
            return ThreePushResult(
                hit=False, direction=direction, extremes=extremes,
                reasons=(f"低点未严格递减 "
                         f"({e1.price:.2f}, {e2.price:.2f}, {e3.price:.2f})",),
            )

    ratios = _compute_retracements(list(six), direction)
    if ratios is None:
        return ThreePushResult(
            hit=False, direction=direction, extremes=extremes,
            reasons=("推浪幅度无效 (零或负)",),
        )
    pull1_ratio, pull2_ratio = ratios

    low = pullback_target - pullback_tolerance
    high = pullback_target + pullback_tolerance

    reasons: list[str] = []
    if not (low <= pull1_ratio <= high):
        reasons.append(
            f"回撤 1 不在 [{low*100:.0f}%, {high*100:.0f}%] 区间 (实际 {pull1_ratio*100:.1f}%)"
        )
    if not (low <= pull2_ratio <= high):
        reasons.append(
            f"回撤 2 不在 [{low*100:.0f}%, {high*100:.0f}%] 区间 (实际 {pull2_ratio*100:.1f}%)"
        )

    bars_since = len(df) - 1 - e3.idx
    if bars_since > recency_bars:
        reasons.append(f"第三推距今过远 ({bars_since} 根, 最大 {recency_bars})")

    if reasons:
        return ThreePushResult(
            hit=False, direction=direction, origin=s0, extremes=extremes,
            intermediates=intermediates,
            pullbacks=(pull1_ratio, pull2_ratio), reasons=tuple(reasons),
        )

    deviation_1 = abs(pull1_ratio - pullback_target) / pullback_tolerance
    deviation_2 = abs(pull2_ratio - pullback_target) / pullback_tolerance
    quality = float(max(0.0, 1.0 - (deviation_1 + deviation_2) / 2))

    return ThreePushResult(
        hit=True, direction=direction, origin=s0, extremes=extremes,
        intermediates=intermediates,
        pullbacks=(pull1_ratio, pull2_ratio), quality=quality,
    )


def detect_both_directions(df: pd.DataFrame, **kwargs) -> dict[Direction, ThreePushResult]:
    return {
        "top": detect_three_push(df, direction="top", **kwargs),
        "bottom": detect_three_push(df, direction="bottom", **kwargs),
    }


if __name__ == "__main__":
    import sys
    from ..data_fetcher import load_prices

    ticker = sys.argv[1] if len(sys.argv) > 1 else "600519"
    df = load_prices(ticker)
    if df is None:
        print(f"No cached data for {ticker}")
        sys.exit(1)

    for direction, res in detect_both_directions(df).items():
        print(f"{ticker} [{direction:>6}]: {res.summary()}")
