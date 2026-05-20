"""更高时间框架 (Weekly / Monthly) 的 PDA (Premium-Discount Array) 命中检测。

按优先级：OB+FVG 同向重叠 > 单个 OB > 单个 FVG。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

from .ob_fvg import Zone, find_active_zones, find_overlap_zones

Direction = Literal["top", "bottom"]
Timeframe = Literal["W", "M"]


@dataclass(frozen=True)
class PDAHit:
    timeframe: Timeframe
    zone: Zone
    overlap_partner: Zone | None = None

    @property
    def quality(self) -> str:
        if self.overlap_partner is not None:
            return "OB+FVG"
        return self.zone.kind.upper()


@dataclass(frozen=True)
class PDAResult:
    target_price: float
    direction: Direction
    hits: tuple[PDAHit, ...] = ()

    @property
    def hit(self) -> bool:
        return len(self.hits) > 0

    @property
    def best_quality(self) -> str:
        if not self.hit:
            return "NONE"
        if any(h.overlap_partner is not None for h in self.hits):
            return "OB+FVG"
        kinds = {h.zone.kind for h in self.hits}
        if "ob" in kinds and "fvg" in kinds:
            return "OB+FVG"
        return "OB" if "ob" in kinds else "FVG"

    def summary(self) -> str:
        if not self.hit:
            return f"NO PDA HIT for {self.direction} @ {self.target_price:.2f}"
        parts = []
        for h in self.hits:
            ext = f"+{h.overlap_partner.kind.upper()}" if h.overlap_partner else ""
            parts.append(
                f"{h.timeframe} {h.zone.kind.upper()}{ext} "
                f"[{h.zone.zone_low:.2f},{h.zone.zone_high:.2f}]"
            )
        return f"HIT {self.direction} @ {self.target_price:.2f} → " + " | ".join(parts)


_AGG_OHLCV = {
    "Open": "first",
    "High": "max",
    "Low": "min",
    "Close": "last",
    "Volume": "sum",
}


def resample_to_htf(df: pd.DataFrame, timeframe: Timeframe) -> pd.DataFrame:
    """日线 → 周/月线。A 股周末休市，按 W-FRI 自然对齐周线。"""
    rule = "W-FRI" if timeframe == "W" else "ME"
    htf = df.resample(rule).agg(_AGG_OHLCV).dropna(how="any")
    return htf


def _direction_match(zone_direction: str, trade_direction: Direction) -> bool:
    """做空命中看跌 zone（上方阻力），做多命中看涨 zone（下方支撑）。"""
    if trade_direction == "top":
        return zone_direction == "bearish"
    return zone_direction == "bullish"


def detect_htf_pda_hit(
    df: pd.DataFrame,
    target_price: float,
    direction: Direction,
    timeframes: tuple[Timeframe, ...] = ("W", "M"),
) -> PDAResult:
    hits: list[PDAHit] = []
    for tf in timeframes:
        htf_df = resample_to_htf(df, tf)
        if len(htf_df) < 30:
            continue
        active = find_active_zones(htf_df)
        aligned = [
            z for z in active
            if _direction_match(z.direction, direction) and z.contains(target_price)
        ]
        if not aligned:
            continue

        overlap_pairs = find_overlap_zones(aligned)
        ob_to_fvg: dict[int, Zone] = {}
        fvg_to_ob: dict[int, Zone] = {}
        for ob, fvg in overlap_pairs:
            ob_to_fvg[id(ob)] = fvg
            fvg_to_ob[id(fvg)] = ob

        for z in aligned:
            partner = ob_to_fvg.get(id(z)) if z.kind == "ob" else fvg_to_ob.get(id(z))
            hits.append(PDAHit(timeframe=tf, zone=z, overlap_partner=partner))

    return PDAResult(target_price=target_price, direction=direction, hits=tuple(hits))


if __name__ == "__main__":
    import sys
    from ..data_fetcher import load_prices

    ticker = sys.argv[1] if len(sys.argv) > 1 else "600519"
    df = load_prices(ticker)
    if df is None:
        print(f"No cached data for {ticker}")
        sys.exit(1)

    last_price = float(df["Close"].iloc[-1])
    print(f"\n{ticker} last_close={last_price:.2f}")
    for direction in ("top", "bottom"):
        res = detect_htf_pda_hit(df, target_price=last_price, direction=direction)
        print(f"  [{direction:>6}] {res.summary()}  best={res.best_quality}")
