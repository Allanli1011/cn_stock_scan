"""仅跑 MACD 三重背离的轻量扫描脚本。"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Literal

import pandas as pd
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config, project_path
from src.data_fetcher import load_prices
from src.indicators.macd import detect_triple_divergence as detect_macd

logger = logging.getLogger(__name__)
Direction = Literal["top", "bottom"]


def _list_cached_tickers() -> list[str]:
    return sorted(f.stem for f in project_path("data/prices").glob("*.parquet"))


def _load_metadata() -> tuple[dict[str, float], dict[str, str]]:
    uni_path = project_path("data/universe.csv")
    if not uni_path.exists():
        return {}, {}
    df = pd.read_csv(uni_path, dtype={"ticker": str})
    df["ticker"] = df["ticker"].str.zfill(6)
    caps = dict(zip(df["ticker"], df["market_cap"])) if "market_cap" in df.columns else {}
    names = dict(zip(df["ticker"], df["name"])) if "name" in df.columns else {}
    return caps, names


def scan_all(direction: Literal["both", "top", "bottom"], include_near: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    tickers = _list_cached_tickers()
    caps, names = _load_metadata()
    directions: list[Direction] = ["top", "bottom"] if direction == "both" else [direction]

    hits: list[dict] = []
    near: list[dict] = []

    for t in tqdm(tickers, desc="MACD scan"):
        df = load_prices(t)
        if df is None or df.empty:
            continue
        for d in directions:
            res = detect_macd(df, direction=d)
            cap = caps.get(t)
            base = {
                "ticker": t,
                "name": names.get(t, ""),
                "direction": d,
                "signal": "SHORT" if d == "top" else "LONG",
                "kind": res.hit_kind,
                "passed": f"{res.n_passed}/{res.n_total}" if res.n_total else None,
                "market_cap_yi": round(cap / 1e8, 2) if cap else None,
                "last_close": round(float(df["Close"].iloc[-1]), 2),
                "last_date": df.index[-1].date(),
            }
            if res.waves:
                w1, w2, w3 = res.waves
                base.update({
                    "strength": round(res.strength, 3),
                    "p1": round(w1.extreme_price, 2),
                    "p2": round(w2.extreme_price, 2),
                    "p3": round(w3.extreme_price, 2),
                    "cross1": round(w1.cross_value, 4),
                    "cross2": round(w2.cross_value, 4),
                    "cross3": round(w3.cross_value, 4),
                    "area1": round(w1.hist_area, 2),
                    "area2": round(w2.hist_area, 2),
                    "area3": round(w3.hist_area, 2),
                })

            if res.hit_kind in ("strict", "loose"):
                base["failed_rules"] = ",".join(c.code for c in res.failed_rules) or None
                base["failed_detail"] = "; ".join(c.full() for c in res.failed_rules) or None
                hits.append(base)
            elif include_near and len(res.failed_rules) == 2:
                # 历史的 near-misses 输出（差 2 条）
                base["failed_rules"] = ",".join(c.code for c in res.failed_rules)
                base["failed_detail"] = "; ".join(c.full() for c in res.failed_rules)
                near.append(base)

    hits_df = pd.DataFrame(hits)
    near_df = pd.DataFrame(near)
    if not hits_df.empty:
        # 严格优先、再按强度
        hits_df["kind_rank"] = hits_df["kind"].map({"strict": 0, "loose": 1}).fillna(2)
        hits_df = hits_df.sort_values(["kind_rank", "strength", "market_cap_yi"],
                                       ascending=[True, False, False])
        hits_df = hits_df.drop(columns=["kind_rank"])
    return hits_df, near_df


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--direction", choices=["both", "top", "bottom"], default="both")
    parser.add_argument("--include-near-misses", action="store_true",
                        help="把仅缺一条规则的近似命中输出到 *_near_misses.csv")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    out_dir = project_path(load_config()["output"]["csv_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    hits, near = scan_all(args.direction, include_near=args.include_near_misses)
    today = date.today().isoformat()

    hits_path = out_dir / f"macd_signals_{today}.csv"
    hits.to_csv(hits_path, index=False, encoding="utf-8-sig")
    logger.info("MACD hits (strict+loose): %d → %s", len(hits), hits_path)
    if len(hits):
        cols = [c for c in ["ticker", "name", "direction", "signal", "kind", "passed",
                            "failed_rules", "strength", "market_cap_yi", "last_close",
                            "p1", "p2", "p3"] if c in hits.columns]
        n_strict = (hits["kind"] == "strict").sum()
        n_loose = (hits["kind"] == "loose").sum()
        print(f"严格命中 {n_strict} 只 / 宽松命中 {n_loose} 只")
        print(hits[cols].head(30).to_string(index=False))

    if args.include_near_misses and len(near):
        near_path = out_dir / f"macd_near_misses_{today}.csv"
        near.to_csv(near_path, index=False, encoding="utf-8-sig")
        logger.info("MACD near-misses: %d → %s", len(near), near_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
