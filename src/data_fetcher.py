"""A 股日线数据抓取（akshare 后端）。

- 每只股票存 `data/prices/<6位代码>.parquet`，列：Open/High/Low/Close/Volume
- 支持增量更新：仅拉缓存中 last_date 之后的新 K 线
- akshare 单股请求，使用 ThreadPoolExecutor 并发

公开接口：
- update_prices(tickers, full_refresh=False) → {ticker: status}
- load_prices(ticker)                         → DataFrame indexed by Date
- refresh_all()                               → 端到端：universe + 价格全量更新
"""
from __future__ import annotations

import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd
from tqdm import tqdm

from .config import load_config, project_path

logger = logging.getLogger(__name__)


# ───────────────────────── helpers ─────────────────────────

def _price_path(ticker: str) -> Path:
    return project_path("data/prices") / f"{ticker}.parquet"


def load_prices(ticker: str) -> pd.DataFrame | None:
    """加载单股票缓存日线。"""
    p = _price_path(ticker)
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    df.index = pd.to_datetime(df.index)
    return df


def _save_prices(ticker: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    path = _price_path(ticker)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path)


# ───────────────────────── akshare 单股拉取 ─────────────────────────

_EM_COL_MAP = {
    "日期": "Date", "开盘": "Open", "最高": "High",
    "最低": "Low", "收盘": "Close", "成交量": "Volume",
}
_SINA_COL_MAP = {
    "date": "Date", "open": "Open", "high": "High",
    "low": "Low", "close": "Close", "volume": "Volume",
}


def _sina_symbol(ticker: str) -> str:
    """6 位代码 → sina 前缀符号 (sh/sz/bj)。"""
    if not ticker:
        return ticker
    head = ticker[0]
    if ticker.startswith(("60", "68", "9")):
        return "sh" + ticker
    if ticker.startswith(("00", "30", "20")):
        return "sz" + ticker
    if head in ("8", "4"):
        return "bj" + ticker
    return "sh" + ticker if ticker.startswith("6") else "sz" + ticker


def _normalize_frame(raw: pd.DataFrame, col_map: dict[str, str]) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()
    cols = {k: v for k, v in col_map.items() if k in raw.columns}
    df = raw.rename(columns=cols)[list(cols.values())].copy()
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "Volume" not in df.columns:
        df["Volume"] = 0  # 某些端点（如腾讯）不返回 Volume
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    return df


def _fetch_via_eastmoney(ticker: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    import akshare as ak
    raw = ak.stock_zh_a_hist(
        symbol=ticker, period="daily",
        start_date=start, end_date=end, adjust=adjust,
    )
    return _normalize_frame(raw, _EM_COL_MAP)


def _fetch_via_sina(ticker: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    import akshare as ak
    # sina 用 YYYY-MM-DD 格式
    s = f"{start[:4]}-{start[4:6]}-{start[6:]}" if len(start) == 8 else start
    e = f"{end[:4]}-{end[4:6]}-{end[6:]}" if len(end) == 8 else end
    raw = ak.stock_zh_a_daily(
        symbol=_sina_symbol(ticker), start_date=s, end_date=e, adjust=adjust or "",
    )
    return _normalize_frame(raw, _SINA_COL_MAP)


# 抓取源优先级，可在 config.yaml 中通过 prices.source 覆盖
_SOURCES = {
    "sina": _fetch_via_sina,
    "eastmoney": _fetch_via_eastmoney,
}


def _fetch_single(ticker: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    """单股票单次请求，带多源回退 + 重试。"""
    cfg = load_config()
    source_order = cfg["prices"].get("sources") or ["sina", "eastmoney"]
    if isinstance(source_order, str):
        source_order = [source_order]

    last_err: Exception | None = None
    for src in source_order:
        fn = _SOURCES.get(src)
        if fn is None:
            continue
        for attempt in range(2):
            try:
                df = fn(ticker, start, end, adjust)
                if not df.empty:
                    return df
            except Exception as e:
                last_err = e
                wait = 0.3 + attempt * 0.4 + random.random() * 0.3
                logger.debug("[%s] %s try%d: %s; sleep %.1fs",
                             src, ticker, attempt + 1, e, wait)
                time.sleep(wait)
    if last_err:
        logger.debug("All sources failed for %s: %s", ticker, last_err)
    return pd.DataFrame()


# ───────────────────────── 主更新逻辑 ─────────────────────────

def update_prices(
    tickers: list[str],
    full_refresh: bool = False,
) -> dict[str, str]:
    """批量更新缓存。状态 ∈ {ok, up_to_date, no_data, no_new_data}."""
    cfg = load_config()
    lookback_days = cfg["prices"]["lookback_days"]
    adjust = cfg["prices"].get("adjust", "qfq") or ""
    max_workers = int(cfg["prices"].get("max_workers", 8))
    sleep_sec = float(cfg["prices"].get("request_sleep_sec", 0.05))

    today = pd.Timestamp.utcnow().tz_localize(None).normalize()
    full_start_str = (today - pd.Timedelta(days=lookback_days)).strftime("%Y%m%d")
    end_str = today.strftime("%Y%m%d")

    status: dict[str, str] = {}
    plan: list[tuple[str, str]] = []  # (ticker, start_date)

    for t in tickers:
        existing = load_prices(t)
        if full_refresh or existing is None or existing.empty:
            plan.append((t, full_start_str))
            continue
        last_date = existing.index.max().normalize()
        if last_date >= today - pd.Timedelta(days=1):
            status[t] = "up_to_date"
            continue
        start = (last_date + pd.Timedelta(days=1)).strftime("%Y%m%d")
        plan.append((t, start))

    logger.info("Price plan: %d to fetch, %d up-to-date", len(plan), len(status))
    if not plan:
        return status

    def _worker(item: tuple[str, str]) -> tuple[str, pd.DataFrame, str]:
        t, start = item
        time.sleep(sleep_sec)
        new_df = _fetch_single(t, start, end_str, adjust)
        return t, new_df, start

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_worker, item) for item in plan]
        for fut in tqdm(as_completed(futures), total=len(futures), desc="prices"):
            t, new_df, start = fut.result()
            if new_df is None or new_df.empty:
                # 增量没新数据 vs 全新拉空：区分一下
                existing = load_prices(t)
                status[t] = "no_new_data" if existing is not None else "no_data"
                continue
            existing = load_prices(t)
            if existing is None or existing.empty:
                _save_prices(t, new_df)
            else:
                combined = pd.concat([existing, new_df])
                combined = combined[~combined.index.duplicated(keep="last")].sort_index()
                _save_prices(t, combined)
            status[t] = "ok"

    n_ok = sum(1 for s in status.values() if s == "ok")
    n_skip = sum(1 for s in status.values() if s in ("up_to_date", "no_new_data"))
    n_fail = sum(1 for s in status.values() if s == "no_data")
    logger.info("Price update done: %d ok, %d skip, %d no_data", n_ok, n_skip, n_fail)
    return status


# ───────────────────────── orchestration ─────────────────────────

def refresh_all(force_universe: bool = False) -> pd.DataFrame:
    """端到端：构建股票池 → 增量更新所有股票日线。"""
    from .universe import build_universe
    universe = build_universe(force_refresh=force_universe)
    logger.info("Universe loaded: %d tickers", len(universe))
    update_prices(universe["ticker"].tolist())
    return universe


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--force-universe", action="store_true",
                   help="强制刷新股票池清单")
    p.add_argument("--full", action="store_true",
                   help="全量重拉所有日线（忽略已有缓存）")
    p.add_argument("--limit", type=int, default=0,
                   help="只更新前 N 只（用于调试）")
    args = p.parse_args()

    from .universe import build_universe
    universe = build_universe(force_refresh=args.force_universe)
    tickers = universe["ticker"].tolist()
    if args.limit > 0:
        tickers = tickers[: args.limit]
    update_prices(tickers, full_refresh=args.full)
    print(f"\n✅ 更新完成 — {len(tickers)} 只 A 股")
