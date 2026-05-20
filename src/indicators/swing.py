"""ZigZag 摆动点检测。

沿 Close 序列前进，当价格自最近的极值反向超过 `pct_threshold` 时确认一个
swing；高点价格取当日 High，低点价格取当日 Low，确保对齐真实价格极值。

最后一个 swing 可能尚未被反向确认（confirmed=False），用于检测进行中的形态
（例如正在形成的第三推）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

from ..config import load_config


@dataclass(frozen=True)
class SwingPoint:
    idx: int
    kind: Literal["high", "low"]
    price: float
    confirmed: bool = True

    def __repr__(self) -> str:
        marker = "" if self.confirmed else "?"
        return f"Sw{marker}({self.kind}@{self.idx}={self.price:.2f})"


def find_swing_points(
    df: pd.DataFrame,
    pct_threshold: float | None = None,
    include_tentative_last: bool = True,
) -> list[SwingPoint]:
    """返回交替排列的高/低 swing 点。"""
    if pct_threshold is None:
        pct_threshold = load_config()["swing"]["pct_threshold"]

    needed = ("Close", "High", "Low")
    if not all(c in df.columns for c in needed):
        raise ValueError(f"df missing one of {needed}")

    close = df["Close"].to_numpy()
    high = df["High"].to_numpy()
    low = df["Low"].to_numpy()
    n = len(close)
    if n < 3:
        return []

    cand_idx = 0
    cand_close = float(close[0])
    direction: str | None = None
    swings: list[SwingPoint] = []

    for i in range(1, n):
        c = float(close[i])

        if direction is None:
            if c > cand_close * (1 + pct_threshold):
                direction = "up"
                swings.append(SwingPoint(cand_idx, "low", float(low[cand_idx])))
                cand_idx, cand_close = i, c
            elif c < cand_close * (1 - pct_threshold):
                direction = "down"
                swings.append(SwingPoint(cand_idx, "high", float(high[cand_idx])))
                cand_idx, cand_close = i, c
        elif direction == "up":
            if c > cand_close:
                cand_idx, cand_close = i, c
            elif c < cand_close * (1 - pct_threshold):
                swings.append(SwingPoint(cand_idx, "high", float(high[cand_idx])))
                direction = "down"
                cand_idx, cand_close = i, c
        else:  # down
            if c < cand_close:
                cand_idx, cand_close = i, c
            elif c > cand_close * (1 + pct_threshold):
                swings.append(SwingPoint(cand_idx, "low", float(low[cand_idx])))
                direction = "up"
                cand_idx, cand_close = i, c

    if include_tentative_last and direction is not None:
        kind = "high" if direction == "up" else "low"
        price = float(high[cand_idx]) if kind == "high" else float(low[cand_idx])
        swings.append(SwingPoint(cand_idx, kind, price, confirmed=False))

    return swings


if __name__ == "__main__":
    import sys
    from ..data_fetcher import load_prices

    ticker = sys.argv[1] if len(sys.argv) > 1 else "600519"
    df = load_prices(ticker)
    if df is None:
        print(f"No cached data for {ticker}")
        sys.exit(1)
    swings = find_swing_points(df)
    print(f"{ticker}: {len(swings)} swing points (last 10):")
    for s in swings[-10:]:
        print(f"  {df.index[s.idx].date()} {s!r}")
