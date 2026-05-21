"""A 股股票池构建。

数据源 (akshare)：
- 全市场清单 + 实时市值：`ak.stock_zh_a_spot_em()`（一次性返回沪深京全部 A 股
  及 总市值/流通市值/名称 等）
- 沪深 300：`ak.index_stock_cons_sina(symbol="000300")`
- 中证 500：`ak.index_stock_cons_sina(symbol="000905")`

返回 DataFrame: ticker(6位代码) / name / market / market_cap / sources / fetched_at
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Iterable

import pandas as pd

from .config import load_config, project_path

logger = logging.getLogger(__name__)


# ───────────────────────── 工具 ─────────────────────────

def _classify_market(code: str) -> str:
    """根据 6 位代码判断交易所/板块。"""
    if not code or len(code) != 6:
        return "其他"
    head = code[0]
    if code.startswith("60"):
        return "沪市主板"
    if code.startswith("68"):
        return "科创板"
    if code.startswith("000") or code.startswith("001"):
        return "深市主板"
    if code.startswith("002") or code.startswith("003"):
        return "深市中小板"
    if code.startswith("30"):
        return "创业板"
    if head in ("8", "4", "9"):
        return "北交所"
    return "其他"


def _is_st(name: str) -> bool:
    if not isinstance(name, str):
        return False
    if name.startswith("*ST") or name.startswith("ST") or name.startswith("退"):
        return True
    if "ST" in name.upper().replace(" ", ""):
        # 例如 "N 长安 ST"、"长安B*ST" 这类
        return True
    return False


def _is_bj(code: str) -> bool:
    return code.startswith(("8", "4", "9"))


# ───────────────────────── 数据抓取 ─────────────────────────

def fetch_all_a_shares_spot() -> pd.DataFrame | None:
    """一次性拉取全市场 A 股的快照（含市值）。失败返回 None（调用方降级处理）。"""
    import akshare as ak
    logger.info("Fetching A-share spot snapshot via akshare (eastmoney) ...")
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_spot_em()
            if df is None or df.empty:
                raise RuntimeError("stock_zh_a_spot_em returned empty")
            break
        except Exception as e:
            last_err = e
            logger.warning("spot attempt %d failed: %s", attempt + 1, e)
            time.sleep(1.5 * (attempt + 1))
    else:
        logger.error("spot snapshot 全部失败 (%s)，将降级到无市值清单", last_err)
        return None

    rename = {"代码": "ticker", "名称": "name", "总市值": "market_cap",
              "流通市值": "float_market_cap", "最新价": "last_price"}
    cols = [c for c in rename if c in df.columns]
    df = df[cols].rename(columns=rename)
    df["ticker"] = df["ticker"].astype(str).str.zfill(6)
    df["market"] = df["ticker"].map(_classify_market)
    df["source"] = "a_shares_all"
    logger.info("A-share spot: %d rows", len(df))
    return df


def fetch_sina_a_shares_spot() -> pd.DataFrame | None:
    """Sina 全 A 股快照（适用于海外 IP，~5500 只，无市值字段）。带 3 次重试。"""
    import akshare as ak
    logger.info("Fetching A-share spot via sina ...")
    df = None
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_spot()
            if df is not None and not df.empty:
                break
        except Exception as e:
            last_err = e
            wait = 2.0 + attempt * 2.0
            logger.warning("sina spot attempt %d failed: %s; sleep %.1fs",
                           attempt + 1, e, wait)
            time.sleep(wait)
    if df is None or df.empty:
        logger.error("sina spot 3 次重试均失败: %s", last_err)
        return None

    rename = {"代码": "ticker", "名称": "name", "最新价": "last_price"}
    cols = {k: v for k, v in rename.items() if k in df.columns}
    out = df.rename(columns=cols)[list(cols.values())].copy()
    # sina 的代码字段带 sh/sz 前缀（如 "sh600000"），去掉
    out["ticker"] = out["ticker"].astype(str).str.replace(r"^(sh|sz|bj)", "", regex=True).str.zfill(6)
    out = out.drop_duplicates("ticker")
    out["market"] = out["ticker"].map(_classify_market)
    out["market_cap"] = pd.NA
    out["float_market_cap"] = pd.NA
    out["source"] = "a_shares_all"
    logger.info("Sina A-share spot: %d rows", len(out))
    return out


def fetch_a_shares_codes_only() -> pd.DataFrame:
    """最后兜底：从交易所官网直接拉清单（仅代码+名称，无市值，海外 IP 可能 reset）。"""
    import akshare as ak
    logger.info("Fetching A-share code-name list (no market cap) ...")
    df = ak.stock_info_a_code_name()
    df = df.rename(columns={"code": "ticker", "name": "name"})
    df["ticker"] = df["ticker"].astype(str).str.zfill(6)
    df["market"] = df["ticker"].map(_classify_market)
    df["market_cap"] = pd.NA
    df["source"] = "a_shares_all"
    logger.info("A-share code list (fallback): %d rows", len(df))
    return df


def fetch_index_constituents(index_code: str, label: str) -> pd.DataFrame:
    """拉取指数成分股（沪深300=000300, 中证500=000905）。

    优先使用新浪接口：返回英文列名，自带 mktcap (单位:万元) 和 nmc。
    失败时降级到中证指数公司接口，仅有代码 + 名称。
    """
    import akshare as ak
    logger.info("Fetching index %s (%s) constituents ...", index_code, label)
    df = None
    try:
        df = ak.index_stock_cons_sina(symbol=index_code)
    except Exception as e:
        logger.warning("sina 指数接口失败 (%s); 切换到 csindex", e)

    if df is not None and "code" in df.columns:
        # 新浪英文列名
        out = pd.DataFrame({
            "ticker": df["code"].astype(str).str.zfill(6),
            "name": df.get("name", "").astype(str),
        })
        if "mktcap" in df.columns:
            # mktcap 单位是万元 → CNY
            out["market_cap"] = pd.to_numeric(df["mktcap"], errors="coerce") * 1e4
        if "nmc" in df.columns:
            out["float_market_cap"] = pd.to_numeric(df["nmc"], errors="coerce") * 1e4
        out["source"] = label
        return out

    # 降级路径：csindex（中文列）
    if df is None:
        df = ak.index_stock_cons_csindex(symbol=index_code)
    rename = {"代码": "ticker", "品种代码": "ticker", "成分券代码": "ticker",
              "名称": "name", "品种名称": "name", "成分券名称": "name"}
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    if "ticker" not in df.columns:
        raise RuntimeError(f"无法识别指数 {index_code} 成分股的代码列，列={list(df.columns)}")
    out = pd.DataFrame({
        "ticker": df["ticker"].astype(str).str.zfill(6),
        "name": df.get("name", pd.Series([""] * len(df))).astype(str),
    })
    out["source"] = label
    return out


# ───────────────────────── 主入口 ─────────────────────────

def build_universe(force_refresh: bool = False) -> pd.DataFrame:
    """根据 config.yaml 的 universe.source 构建股票池并写缓存。"""
    cfg = load_config()
    ucfg = cfg["universe"]
    cache_path = project_path(ucfg["cache_path"])
    refresh_days = ucfg["refresh_days"]
    source = ucfg["source"]
    exclude_st = bool(ucfg.get("exclude_st", False))
    exclude_bj = bool(ucfg.get("exclude_bj", False))
    min_cap = float(ucfg.get("min_market_cap_cny", 0))

    if not force_refresh and cache_path.exists():
        try:
            cached = pd.read_csv(cache_path, dtype={"ticker": str})
            cached["ticker"] = cached["ticker"].str.zfill(6)
            if "fetched_at" in cached.columns and len(cached) > 0:
                fetched_at = pd.to_datetime(cached["fetched_at"].iloc[0], utc=True)
                age_days = (datetime.now(timezone.utc) - fetched_at).days
                if age_days < refresh_days:
                    logger.info(
                        "Universe cache fresh (%d days old, %d tickers); skipping refresh",
                        age_days, len(cached),
                    )
                    return cached
        except Exception as e:
            logger.warning("Cache read failed (%s); refreshing", e)

    # universe 抓取优先级：eastmoney (国内最快) → sina (海外稳定) → None
    spot = fetch_all_a_shares_spot()
    if spot is None:
        spot = fetch_sina_a_shares_spot()

    if source == "hs300_zz500":
        hs300 = fetch_index_constituents("000300", "hs300")
        zz500 = fetch_index_constituents("000905", "zz500")
        index_df = pd.concat([hs300, zz500], ignore_index=True)
        # 用 groupby 合并 source，但保留第一个非空 market_cap
        agg_cols = {"name": "first", "source": lambda s: ",".join(sorted(set(s)))}
        for c in ("market_cap", "float_market_cap"):
            if c in index_df.columns:
                agg_cols[c] = "first"
        index_df = index_df.groupby("ticker", as_index=False).agg(agg_cols)
        if spot is not None:
            # 用 spot 数据补全市值（更新鲜）
            df = spot[spot["ticker"].isin(set(index_df["ticker"]))].copy()
            df = df.merge(index_df[["ticker", "source"]], on="ticker", how="left", suffixes=("_spot", ""))
            df["source"] = df["source"].fillna("hs300_zz500")
        else:
            df = index_df.copy()
            df["market"] = df["ticker"].map(_classify_market)
    else:
        # a_shares_all 或 by_market_cap 都先取全集
        if spot is None:
            if source == "by_market_cap":
                raise RuntimeError("by_market_cap 需要市值快照，但 spot 抓取失败")
            try:
                df = fetch_a_shares_codes_only()
            except Exception as e:
                raise RuntimeError(
                    f"全市场 universe 抓取全部失败 (eastmoney/sina/exchange-direct): {e}"
                )
        else:
            df = spot.copy()
            if source == "by_market_cap":
                df = df[df["market_cap"].fillna(0) >= min_cap].copy()

    if exclude_st:
        before = len(df)
        df = df[~df["name"].apply(_is_st)].copy()
        logger.info("剔除 ST/退市: %d → %d", before, len(df))

    if exclude_bj:
        before = len(df)
        df = df[~df["ticker"].apply(_is_bj)].copy()
        logger.info("剔除北交所: %d → %d", before, len(df))

    if "market_cap" in df.columns and df["market_cap"].notna().any():
        df = df.sort_values("market_cap", ascending=False).reset_index(drop=True)
    else:
        df = df.reset_index(drop=True)
    df["fetched_at"] = datetime.now(timezone.utc).isoformat()

    keep_cols = ["ticker", "name", "market", "market_cap", "float_market_cap",
                 "source", "fetched_at"]
    keep_cols = [c for c in keep_cols if c in df.columns]
    df = df[keep_cols]

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(cache_path, index=False, encoding="utf-8-sig")
    logger.info("Universe written → %s (%d tickers, source=%s)",
                cache_path, len(df), source)
    return df


def load_universe() -> pd.DataFrame:
    """加载缓存；不存在则自动构建。"""
    cache_path = project_path(load_config()["universe"]["cache_path"])
    if not cache_path.exists():
        return build_universe()
    df = pd.read_csv(cache_path, dtype={"ticker": str})
    df["ticker"] = df["ticker"].str.zfill(6)
    return df


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    df = build_universe(force_refresh=True)
    print(df.head(15))
    print(f"\n合计: {len(df)} 只 A 股")
    if "market" in df.columns:
        print("按板块分布:\n", df["market"].value_counts())
