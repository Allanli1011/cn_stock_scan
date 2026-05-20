"""严格的 ICT Order Block (OB) 与 Fair Value Gap (FVG) 识别。

定义（机械化）：

Order Block
  • 看涨 OB：在一次强上推 (后续 ≥ displacement_atr × ATR) 之前的 **最后一根
    阴线**。
  • 看跌 OB：镜像。
  Zone = 该 K 线的完整 High–Low 区间。

Fair Value Gap (3 根 K 线缺口)
  • 看涨 FVG：K1.High < K3.Low (K2 为推动 K 线)。
  • 看跌 FVG：K1.Low > K3.High。
  最小尺寸：min_size_atr × ATR。

Zone 状态：
  • invalidated（破坏）：后续有 K 线收盘穿越 zone（看涨 zone 跌破/看跌 zone
    站上） → 失效。
  • mitigated（被触发）：只触碰未收破 → 仍可作为入场区。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

import numpy as np
import pandas as pd

from ..config import load_config

ZoneKind = Literal["ob", "fvg"]
ZoneDirection = Literal["bullish", "bearish"]


@dataclass(frozen=True)
class Zone:
    kind: ZoneKind
    direction: ZoneDirection
    formation_idx: int
    zone_high: float
    zone_low: float
    mitigated: bool = False
    invalidated: bool = False

    @property
    def mid(self) -> float:
        return (self.zone_high + self.zone_low) / 2

    @property
    def height(self) -> float:
        return self.zone_high - self.zone_low

    def contains(self, price: float) -> bool:
        return self.zone_low <= price <= self.zone_high

    def overlaps(self, other: "Zone") -> bool:
        return not (self.zone_high < other.zone_low or self.zone_low > other.zone_high)


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder 风格 ATR（用 EMA 平滑 True Range）。"""
    high_low = df["High"] - df["Low"]
    high_close = (df["High"] - df["Close"].shift()).abs()
    low_close = (df["Low"] - df["Close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def _zone_status(df: pd.DataFrame, zone: Zone, end_idx: int | None = None) -> Zone:
    if end_idx is None:
        end_idx = len(df) - 1
    if end_idx <= zone.formation_idx:
        return zone
    after_high = df["High"].iloc[zone.formation_idx + 1 : end_idx + 1].to_numpy()
    after_low = df["Low"].iloc[zone.formation_idx + 1 : end_idx + 1].to_numpy()
    after_close = df["Close"].iloc[zone.formation_idx + 1 : end_idx + 1].to_numpy()
    if len(after_high) == 0:
        return zone

    touched = ((after_low <= zone.zone_high) & (after_high >= zone.zone_low)).any()
    if zone.direction == "bullish":
        broken = (after_close < zone.zone_low).any()
    else:
        broken = (after_close > zone.zone_high).any()
    return replace(zone, mitigated=bool(touched), invalidated=bool(broken))


def find_order_blocks(
    df: pd.DataFrame,
    atr_period: int | None = None,
    displacement_atr: float | None = None,
    lookforward: int = 3,
) -> list[Zone]:
    cfg = load_config()["ob_fvg"]
    atr_period = atr_period if atr_period is not None else cfg["atr_period"]
    displacement_atr = displacement_atr if displacement_atr is not None else cfg["ob_displacement_atr"]

    atr_series = compute_atr(df, period=atr_period)
    close = df["Close"].to_numpy()
    open_ = df["Open"].to_numpy()
    high = df["High"].to_numpy()
    low = df["Low"].to_numpy()
    atr_arr = atr_series.to_numpy()
    n = len(df)
    obs: list[Zone] = []

    for i in range(atr_period, n - lookforward):
        if np.isnan(atr_arr[i]) or atr_arr[i] == 0:
            continue
        baseline_close = close[i]
        threshold = displacement_atr * atr_arr[i]

        future_high_window = high[i + 1 : i + 1 + lookforward]
        if future_high_window.size and future_high_window.max() - baseline_close >= threshold:
            if close[i] < open_[i]:
                obs.append(Zone(
                    kind="ob", direction="bullish", formation_idx=i,
                    zone_high=float(high[i]), zone_low=float(low[i]),
                ))

        future_low_window = low[i + 1 : i + 1 + lookforward]
        if future_low_window.size and baseline_close - future_low_window.min() >= threshold:
            if close[i] > open_[i]:
                obs.append(Zone(
                    kind="ob", direction="bearish", formation_idx=i,
                    zone_high=float(high[i]), zone_low=float(low[i]),
                ))
    return obs


def find_fvgs(
    df: pd.DataFrame,
    atr_period: int | None = None,
    min_size_atr: float | None = None,
) -> list[Zone]:
    cfg = load_config()["ob_fvg"]
    atr_period = atr_period if atr_period is not None else cfg["atr_period"]
    min_size_atr = min_size_atr if min_size_atr is not None else cfg["fvg_min_size_atr"]

    atr_series = compute_atr(df, period=atr_period)
    high = df["High"].to_numpy()
    low = df["Low"].to_numpy()
    atr_arr = atr_series.to_numpy()
    n = len(df)
    fvgs: list[Zone] = []

    for i in range(atr_period + 2, n):
        a = atr_arr[i]
        if np.isnan(a) or a == 0:
            continue
        min_size = min_size_atr * a
        k1_high, k1_low = high[i - 2], low[i - 2]
        k3_high, k3_low = high[i], low[i]

        if k1_high < k3_low and (k3_low - k1_high) >= min_size:
            fvgs.append(Zone(
                kind="fvg", direction="bullish", formation_idx=i - 1,
                zone_high=float(k3_low), zone_low=float(k1_high),
            ))
        if k1_low > k3_high and (k1_low - k3_high) >= min_size:
            fvgs.append(Zone(
                kind="fvg", direction="bearish", formation_idx=i - 1,
                zone_high=float(k1_low), zone_low=float(k3_high),
            ))
    return fvgs


def find_active_zones(
    df: pd.DataFrame,
    end_idx: int | None = None,
    include_obs: bool = True,
    include_fvgs: bool = True,
) -> list[Zone]:
    """所有未被破坏（non-invalidated）的 OB / FVG zone。已被触发但未破的仍保留。"""
    zones: list[Zone] = []
    if include_obs:
        zones.extend(find_order_blocks(df))
    if include_fvgs:
        zones.extend(find_fvgs(df))
    zones = [_zone_status(df, z, end_idx=end_idx) for z in zones]
    return [z for z in zones if not z.invalidated]


def find_overlap_zones(zones: list[Zone]) -> list[tuple[Zone, Zone]]:
    """同方向且区间相交的 (OB, FVG) 对 —— ICT 最优入场区。"""
    obs = [z for z in zones if z.kind == "ob"]
    fvgs = [z for z in zones if z.kind == "fvg"]
    pairs: list[tuple[Zone, Zone]] = []
    for ob in obs:
        for fvg in fvgs:
            if ob.direction == fvg.direction and ob.overlaps(fvg):
                pairs.append((ob, fvg))
    return pairs


if __name__ == "__main__":
    import sys
    from ..data_fetcher import load_prices

    ticker = sys.argv[1] if len(sys.argv) > 1 else "600519"
    df = load_prices(ticker)
    if df is None:
        print(f"No cached data for {ticker}")
        sys.exit(1)

    obs = find_order_blocks(df)
    fvgs = find_fvgs(df)
    active = find_active_zones(df)
    overlaps = find_overlap_zones(active)
    last_price = float(df["Close"].iloc[-1])

    print(f"{ticker} last_close={last_price:.2f}")
    print(f"  Total: {len(obs)} OBs + {len(fvgs)} FVGs")
    print(f"  Active (not invalidated): {len(active)}")
    print(f"  OB+FVG overlap zones: {len(overlaps)}")
