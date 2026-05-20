"""全市场 A 股扫描脚本。

流程：
  1. 加载 (或刷新) 股票池 + 增量更新日线缓存
  2. 对每只股票同时跑 MACD 三重背离 / PA 三推 / HTF PDA 三个检测器
  3. 三项合成得分 (max 3.0)，按得分 + 市值排序
  4. 输出 CSV 到 output/full_signals_<date>.csv，并可选地为 top N 自动出图

用法：
  python scripts/scan_full.py --min-score 1.5 --plot-top 5
  python scripts/scan_full.py --direction bottom --refresh-data
"""
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
from src.data_fetcher import load_prices, refresh_all, update_prices
from src.indicators.macd import DivergenceResult, detect_triple_divergence as detect_macd
from src.indicators.pda import PDAResult, detect_htf_pda_hit
from src.indicators.swing import find_swing_points
from src.indicators.three_push import ThreePushResult, detect_three_push
from src.universe import load_universe

logger = logging.getLogger(__name__)
Direction = Literal["top", "bottom"]


def _stop_buffer_pct() -> float:
    return 0.01


def _currency() -> str:
    return load_config().get("runtime", {}).get("currency_symbol", "¥")


def build_notes(
    direction: Direction,
    macd_res: DivergenceResult,
    tp_res: ThreePushResult,
    pda_res: PDAResult,
    target_price: float,
) -> str:
    cur = _currency()
    parts: list[str] = []
    label = "顶" if direction == "top" else "底"

    if tp_res.hit and tp_res.pullbacks:
        e1, e2, e3 = tp_res.extremes
        p1, p2 = tp_res.pullbacks
        pull_word = "回撤" if direction == "top" else "反弹"
        parts.append(
            f"三推{label} {e1.price:.2f}→{e2.price:.2f}→{e3.price:.2f} "
            f"{pull_word} {p1*100:.0f}%/{p2*100:.0f}%"
        )

    if macd_res.hit_kind == "strict":
        parts.append(f"MACD严格{label}背离 ({macd_res.n_passed}/{macd_res.n_total}, 强度 {macd_res.strength:.2f})")
    elif macd_res.hit_kind == "loose":
        failed = macd_res.failed_rules
        miss_word = failed[0].code if failed else ""
        parts.append(
            f"MACD宽松{label}背离 ({macd_res.n_passed}/{macd_res.n_total}, 差{miss_word}, 强度 {macd_res.strength:.2f})"
        )

    if pda_res.hit:
        first = pda_res.hits[0]
        tf_word = "周线" if first.timeframe == "W" else "月线"
        parts.append(
            f"{tf_word}{pda_res.best_quality} [{first.zone.zone_low:.2f}-{first.zone.zone_high:.2f}]"
        )

    if pda_res.hit and (tp_res.hit or macd_res.hit_kind != "miss"):
        zone = pda_res.hits[0].zone
        origin_price = tp_res.origin.price if (tp_res.hit and tp_res.origin) else None
        buffer = _stop_buffer_pct()

        if direction == "bottom":
            stop = zone.zone_low * (1 - buffer)
            entry = target_price
            target = origin_price
            if target is not None and target > entry > stop:
                rr = (target - entry) / (entry - stop)
                parts.append(
                    f"多: 入{cur}{entry:.2f} 止{cur}{stop:.2f} "
                    f"标{cur}{target:.2f} R:R {rr:.1f}"
                )
            elif target is None:
                parts.append(f"多: 入{cur}{entry:.2f} 止{cur}{stop:.2f}（无明确目标，仅PDA命中）")
        else:
            stop = zone.zone_high * (1 + buffer)
            entry = target_price
            target = origin_price
            if target is not None and stop > entry > target:
                rr = (entry - target) / (stop - entry)
                parts.append(
                    f"空: 入{cur}{entry:.2f} 止{cur}{stop:.2f} "
                    f"标{cur}{target:.2f} R:R {rr:.1f}"
                )
            elif target is None:
                parts.append(f"空: 入{cur}{entry:.2f} 止{cur}{stop:.2f}（无明确目标，仅PDA命中）")

    return " | ".join(parts) if parts else "(无显著信号)"


def _list_cached_tickers() -> list[str]:
    return sorted(f.stem for f in project_path("data/prices").glob("*.parquet"))


def _load_metadata() -> tuple[dict[str, float], dict[str, str]]:
    """从 universe.csv 读取 market_cap 与 name 映射。"""
    uni_path = project_path("data/universe.csv")
    caps: dict[str, float] = {}
    names: dict[str, str] = {}
    if uni_path.exists():
        df = pd.read_csv(uni_path, dtype={"ticker": str})
        df["ticker"] = df["ticker"].str.zfill(6)
        if "market_cap" in df.columns:
            caps = dict(zip(df["ticker"], df["market_cap"]))
        if "name" in df.columns:
            names = dict(zip(df["ticker"], df["name"]))
    return caps, names


def _target_price_for(df: pd.DataFrame, direction: Direction) -> tuple[float, int]:
    swings = find_swing_points(df)
    target_kind = "high" if direction == "top" else "low"
    candidates = [s for s in swings if s.kind == target_kind]
    if candidates:
        last = candidates[-1]
        return last.price, last.idx
    return float(df["Close"].iloc[-1]), len(df) - 1


def _pda_score(quality: str) -> float:
    if quality == "OB+FVG":
        return 1.0
    if quality in ("OB", "FVG"):
        return 0.5
    return 0.0


def _macd_score(kind: str) -> float:
    """严格命中 +1.0, 宽松命中 +0.5, miss 0。"""
    return {"strict": 1.0, "loose": 0.5}.get(kind, 0.0)


def scan_one(
    ticker: str, df: pd.DataFrame,
    market_cap: float | None, name: str, direction: Direction,
) -> dict:
    target_price, target_idx = _target_price_for(df, direction)
    target_date = df.index[target_idx].date()

    macd_res = detect_macd(df, direction=direction)
    tp_res = detect_three_push(df, direction=direction)
    pda_res = detect_htf_pda_hit(df, target_price=target_price, direction=direction)

    score = 0.0
    score += _macd_score(macd_res.hit_kind)
    score += 1.0 if tp_res.hit else 0.0
    score += _pda_score(pda_res.best_quality)

    failed_codes = ",".join(c.code for c in macd_res.failed_rules)
    row: dict = {
        "ticker": ticker,
        "name": name,
        "direction": direction,
        "signal": "SHORT" if direction == "top" else "LONG",
        "score": round(score, 2),
        # 市值单位：亿元
        "market_cap_yi": round(market_cap / 1e8, 2) if market_cap else None,
        "last_close": round(float(df["Close"].iloc[-1]), 2),
        "last_date": df.index[-1].date(),
        "target_price": round(target_price, 2),
        "target_date": target_date,
        "macd_kind": macd_res.hit_kind,                 # strict | loose | miss
        "macd_passed": f"{macd_res.n_passed}/{macd_res.n_total}" if macd_res.n_total else None,
        "macd_failed_rules": failed_codes or None,
        "macd_strength": round(macd_res.strength, 3) if macd_res.hit_kind != "miss" else None,
        "three_push_hit": tp_res.hit,
        "three_push_quality": round(tp_res.quality, 3) if tp_res.hit else None,
        "pda_hit": pda_res.hit,
        "pda_quality": pda_res.best_quality if pda_res.hit else None,
    }
    if pda_res.hit:
        first = pda_res.hits[0]
        row.update({
            "pda_timeframe": first.timeframe,
            "pda_zone_low": round(first.zone.zone_low, 2),
            "pda_zone_high": round(first.zone.zone_high, 2),
        })

    row["notes"] = build_notes(direction, macd_res, tp_res, pda_res, target_price)
    return row


def scan_all(direction: Literal["both", "top", "bottom"], min_score: float) -> pd.DataFrame:
    tickers = _list_cached_tickers()
    caps, names = _load_metadata()
    directions: list[Direction] = ["top", "bottom"] if direction == "both" else [direction]

    rows: list[dict] = []
    for t in tqdm(tickers, desc="scanning"):
        df = load_prices(t)
        if df is None or df.empty:
            continue
        for d in directions:
            row = scan_one(t, df, caps.get(t), names.get(t, ""), d)
            if row["score"] >= min_score:
                rows.append(row)

    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows).sort_values(
        ["score", "market_cap_yi"], ascending=[False, False],
    )
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--direction", choices=["both", "top", "bottom"], default="both")
    parser.add_argument("--min-score", type=float, default=1.0,
                        help="入选 CSV 的最低合成得分 (默认 1.0)")
    parser.add_argument("--plot-top", type=int, default=1,
                        help="自动渲染 top N 信号的 K 线图 (0=不渲染)")
    parser.add_argument("--refresh-data", action="store_true",
                        help="扫描前先刷新股票池+日线数据")
    parser.add_argument("--update-prices", action="store_true",
                        help="仅做增量价格更新，不刷新股票池")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    out_dir = project_path(cfg["output"]["csv_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.refresh_data:
        refresh_all()
    elif args.update_prices:
        universe = load_universe()
        update_prices(universe["ticker"].tolist())

    df = scan_all(args.direction, args.min_score)
    today = date.today().isoformat()
    path = out_dir / f"full_signals_{today}.csv"
    df.to_csv(path, index=False, encoding="utf-8-sig")
    logger.info("Combined signals: %d rows → %s", len(df), path)

    if len(df):
        print(f"\n── 扫描结果 (score ≥ {args.min_score}) ──")
        show_cols = [
            "ticker", "name", "direction", "signal", "score",
            "market_cap_yi", "last_close",
            "macd_kind", "macd_passed", "three_push_hit", "pda_hit", "pda_quality",
        ]
        show_cols = [c for c in show_cols if c in df.columns]
        print(df[show_cols].head(25).to_string(index=False))

        print("\n── 得分分布 ──")
        print(df["score"].value_counts().sort_index(ascending=False).to_string())

        buff_stacked = df[df["score"] >= 2.5]
        if len(buff_stacked):
            print(f"\n🎯 三重共振 (score ≥ 2.5): {len(buff_stacked)}")
            print(buff_stacked[show_cols].to_string(index=False))

        if args.plot_top > 0:
            from src.visualization import render_signal_chart
            charts_dir = project_path("output/charts")
            top_rows = df.head(args.plot_top)
            print(f"\n── 渲染 {len(top_rows)} 张 K 线图 ──")
            for _, row in top_rows.iterrows():
                price_df = load_prices(row["ticker"])
                if price_df is None:
                    continue
                chart_path = charts_dir / f"{today}_{row['ticker']}_{row['direction']}.png"
                render_signal_chart(
                    ticker=row["ticker"], name=row.get("name", ""),
                    df=price_df, direction=row["direction"],
                    score=float(row["score"]), notes=row.get("notes", ""),
                    output_path=chart_path,
                )
                print(f"  → {chart_path}")
    else:
        print(f"\n在 score ≥ {args.min_score} 阈值下无信号。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
