"""数据获取层 v2 — EODHD + akshare + 行业基准注入 + 缓存.

改进:
  - 修复 revenue_cagr 计算 bug (EODHD yearly key 排序问题)
  - 注入行业百分位基准 (从 value-screen 移植)
  - pickle 缓存 (24h TTL)
  - 断点续拉
"""

import os
import pickle
import hashlib
import time
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd


def _eodhd_key() -> str:
    """每次调用时现取，避免模块加载时机问题。"""
    return os.environ.get("EODHD_API_KEY", "")


RMB = 100_000_000
CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "cache"
BENCH_DIR = Path(__file__).resolve().parent.parent.parent / "benchmarks"


def _safe(v) -> Optional[float]:
    if v is None:
        return None
    try:
        if isinstance(v, str):
            s = v.strip()
            if s in ("", "-", "N/A", "NA", "None"):
                return None
            return float(s.replace(",", ""))
        if isinstance(v, (int, float)):
            if pd.isna(v):
                return None
            return float(v)
        import numpy as np
        if isinstance(v, (np.integer, np.floating)):
            if pd.isna(v):
                return None
            return float(v)
    except Exception:
        pass
    return None


# ── 缓存 (24h TTL) ──
def _cache_path(ticker, prefix):
    CACHE_DIR.mkdir(exist_ok=True)
    key = hashlib.md5(f"{prefix}:{ticker}".encode()).hexdigest()
    return CACHE_DIR / f"{prefix}_{key}.pkl"


def _cache_get(ticker, prefix):
    p = _cache_path(ticker, prefix)
    if p.exists():
        try:
            d = pickle.load(open(p, "rb"))
            if time.time() - d.get("_ts", 0) < 24 * 3600:
                return d.get("_payload")
        except Exception:
            pass
    return None


def _cache_set(ticker, prefix, payload):
    with open(_cache_path(ticker, prefix), "wb") as f:
        pickle.dump({"_ts": time.time(), "_payload": payload}, f)


# ── CAGR 计算 (修复版: 不假设 sorted 的 key 顺序，取最近N年) ──
def _cagr(series, years):
    """series: [(year_str, value), ...] 按年份升序"""
    if len(series) < years + 1:
        return None
    recent = sorted(series, key=lambda x: str(x[0]))[-(years + 1):]
    s_val, e_val = recent[0][1], recent[-1][1]
    if s_val is None or e_val is None or s_val <= 0 or e_val <= 0:
        return None
    try:
        return round((pow(e_val / s_val, 1 / years) - 1) * 100, 2)
    except Exception:
        return None


def _latest_yr(yearly_dict):
    if not yearly_dict:
        return {}
    keys = sorted(yearly_dict.keys())
    return yearly_dict[keys[-1]]


def _extract_series(yearly_dict, field):
    if not yearly_dict:
        return []
    result = []
    for d, v in yearly_dict.items():
        val = _safe(v.get(field)) if isinstance(v, dict) else None
        if val is not None:
            result.append((d, val))
    return sorted(result, key=lambda x: str(x[0]))


# ── 行业基准加载 ──
def load_benchmarks(market: str) -> dict:
    """加载行业百分位基准。market: US / HK / A"""
    path = BENCH_DIR / f"benchmarks_{market}.json"
    if not path.exists():
        return {}
    import json
    with open(path) as f:
        return json.load(f)


def _get_sector_pct(benchmarks: dict, sector: str, metric: str, pct: int) -> Optional[float]:
    """从基准数据中提取行业百分位值。"""
    sectors = benchmarks.get("sectors", {})
    entry = sectors.get(sector)
    if not entry or entry.get("_fallback"):
        entry = benchmarks.get("market_wide", {})
    if not entry:
        return None
    m = entry.get(metric)
    if not m:
        return None
    return m.get(f"p{pct}")


def inject_benchmarks(data: dict, benchmarks: dict) -> dict:
    """将行业百分位上下文注入数据 dict，供 Agent 参考。

    添加上下文注释，如：
      gross_margin_p50: 行业毛利率中位数
      revenue_cagr_p75: 行业营收增速75分位
    """
    if not benchmarks or not data.get("sector"):
        return data

    sector = data["sector"]
    metrics = [
        ("revenue_cagr_3y", "rev_cagr"),
        ("gross_margin", "gross_margin"),
        ("roe", "roe"),
        ("debt_ratio", "debt_ratio"),
    ]

    for data_key, bm_key in metrics:
        for pct in [25, 50, 75]:
            val = _get_sector_pct(benchmarks, sector, bm_key, pct)
            if val is not None:
                data[f"{data_key}_p{pct}"] = val

    return data


# ── EODHD 数据拉取 ──
def _fetch_eodhd(ticker: str) -> Optional[dict]:
    cached = _cache_get(ticker, "fund")
    if cached:
        return cached

    try:
        from eodhd import APIClient
        api = APIClient(_eodhd_key())
        data = api.get_fundamentals_data(ticker)
    except Exception as e:
        print(f"  [EODHD] {ticker}: {e}")
        return None

    if not data or not isinstance(data, dict):
        return None

    g = data.get("General", {})
    hl = data.get("Highlights", {})
    ss = data.get("SharesStats", {})
    fin = data.get("Financials", {})

    bs_y = fin.get("Balance_Sheet", {}).get("yearly", {})
    cf_y = fin.get("Cash_Flow", {}).get("yearly", {})
    inc_y = fin.get("Income_Statement", {}).get("yearly", {})

    bs_latest = _latest_yr(bs_y)
    cf_latest = _latest_yr(cf_y)
    inc_latest = _latest_yr(inc_y)

    equity = _safe(bs_latest.get("totalStockholderEquity")) or 0
    ocf = _safe(cf_latest.get("totalCashFromOperatingActivities")) or 0
    capex = _safe(cf_latest.get("capitalExpenditures")) or 0
    ni = _safe(inc_latest.get("netIncome")) or 0
    revenue = _safe(inc_latest.get("totalRevenue")) or 0
    gp = _safe(inc_latest.get("grossProfit"))

    # 财务比率
    gross_margin = round(gp / revenue * 100, 2) if gp and revenue > 0 else None
    net_margin = round(ni / revenue * 100, 2) if ni and revenue > 0 else None
    roe = round(ni / equity * 100, 2) if equity > 0 and ni else None
    ta = _safe(bs_latest.get("totalAssets"))
    tl = _safe(bs_latest.get("totalLiab")) or _safe(bs_latest.get("totalLiabilities"))
    debt_ratio = round(tl / ta * 100, 2) if ta and ta > 0 and tl is not None else None

    # 毛利率趋势
    gm_series = _extract_series(inc_y, "grossProfit")
    rev_series = dict(_extract_series(inc_y, "totalRevenue"))
    margins = []
    for d, gv in gm_series[-3:]:
        rv = rev_series.get(d)
        if rv and rv > 0:
            margins.append(round(gv / rv * 100, 2))
    gm_change_pp = round(margins[-1] - margins[0], 2) if len(margins) >= 3 else None
    gm_trend = (
        "up" if gm_change_pp and gm_change_pp > 2
        else "down" if gm_change_pp and gm_change_pp < -2
        else "flat"
    )

    # 连负检查
    cf_vals = [v for _, v in _extract_series(cf_y, "totalCashFromOperatingActivities")[-3:]]
    ni_vals = [v for _, v in _extract_series(inc_y, "netIncome")[-2:]]
    ocf_neg_3yr = all(v is not None and v < 0 for v in cf_vals) if len(cf_vals) >= 3 else False
    ni_neg_2yr = all(v is not None and v < 0 for v in ni_vals) if len(ni_vals) >= 2 else False

    # 历史序列
    rev_hist = _extract_series(inc_y, "totalRevenue")
    ni_hist = _extract_series(inc_y, "netIncome")
    cf_hist = _extract_series(cf_y, "totalCashFromOperatingActivities")
    eq_hist = _extract_series(bs_y, "totalStockholderEquity")

    # 10年PE: 从 EODHD annual EPS + 历史价格计算
    avg_pe = None
    try:
        earn_annual = data.get("Earnings", {}).get("Annual", {})
        eps_map = {}
        for d, v in earn_annual.items():
            eps = _safe(v.get("epsActual")) if isinstance(v, dict) else None
            if eps and eps > 0:
                eps_map[d[:4]] = eps
        if eps_map:
            h = api.get_historical_data(ticker, interval='d', results=3650)
            if h is not None and not h.empty:
                h.index = pd.to_datetime(h.index)
                yr_end = h.groupby(h.index.year)['adjusted_close'].last()
                pes = []
                for yr, eps in eps_map.items():
                    try:
                        yr_i = int(yr)
                        if yr_i in yr_end.index:
                            pe_v = yr_end[yr_i] / eps
                            if 0 < pe_v < 200:
                                pes.append(pe_v)
                    except Exception:
                        pass
                if len(pes) >= 3:
                    avg_pe = round(statistics.mean(pes), 2)
    except Exception:
        pass

    result = {
        "ticker": ticker,
        "name": g.get("Name", ticker),
        "sector": g.get("GicSector", ""),
        "industry": g.get("GicIndustry", ""),
        "currency": g.get("CurrencyCode", ""),
        "market_cap": _safe(hl.get("MarketCapitalization")) or 0,
        "shares_outstanding": _safe(ss.get("SharesOutstanding")) or 0,
        "cash": _safe(bs_latest.get("cash")) or 0,
        "cash_pool": (_safe(bs_latest.get("cashAndShortTermInvestments"))
                      or (_safe(bs_latest.get("cash")) or 0) + (_safe(bs_latest.get("shortTermInvestments")) or 0)),
        "st_invest": _safe(bs_latest.get("shortTermInvestments")) or 0,
        "tca": _safe(bs_latest.get("totalCurrentAssets")) or 0,
        "tcl": _safe(bs_latest.get("totalCurrentLiabilities")) or 0,
        "nwc": _safe(bs_latest.get("netWorkingCapital")) or (
            (_safe(bs_latest.get("totalCurrentAssets")) or 0) - (_safe(bs_latest.get("totalCurrentLiabilities")) or 0)
        ),
        "equity": equity,
        "total_assets": ta or 0,
        "debt_ratio": debt_ratio,
        "revenue_cagr_3y": _cagr(rev_hist, 3),
        "gross_margin": gross_margin,
        "net_margin": net_margin,
        "roe": roe,
        "gm_trend": gm_trend,
        "gm_change_pp": gm_change_pp,
        "ocf": ocf,
        "fcf": ocf - capex,
        "capex": capex,
        "ocf_neg_3yr": ocf_neg_3yr,
        "ni_neg_2yr": ni_neg_2yr,
        "pe": _safe(hl.get("PERatio")),
        "pb": None,
        "eps": None,
        "div_yield": round((_safe(hl.get("DividendYield")) or 0) * 100, 2),
        "avg_pe_10y": avg_pe,
        "rev_hist_years": len(rev_hist),
        "ni_hist_years": len(ni_hist),
        "rev_hist": rev_hist,
        "ni_hist": ni_hist,
        "cf_hist": cf_hist,
        "eq_hist": eq_hist,
        "total_revenue": revenue,
        "net_income": ni,
        "capital_expenditure": capex,
    }

    _cache_set(ticker, "fund", result)
    return result


# ── 港股 (akshare) ──
def _fetch_hk(ticker: str) -> Optional[dict]:
    cached = _cache_get(ticker, "fund")
    if cached:
        return cached

    code = ticker.split(".")[0].zfill(5)
    try:
        import akshare as ak
        os.environ["NO_PROXY"] = os.environ.get("NO_PROXY", "") + ",eastmoney.com,eastmoney.com.cn"

        # 概况
        name_cn, sector_cn = "", ""
        try:
            profile = ak.stock_hk_company_profile_em(symbol=code)
            if profile is not None and not profile.empty:
                name_cn = str(profile.iloc[0].get("公司名称", ""))
                sector_cn = str(profile.iloc[0].get("所属行业", ""))
        except Exception:
            pass

        # 财务
        df = ak.stock_financial_hk_analysis_indicator_em(symbol=code, indicator="年度")
        if df is None or df.empty:
            return None
        df = df.sort_values("REPORT_DATE")
        latest = df.iloc[-1]

        rev_vals = df["OPERATE_INCOME"].apply(_safe).dropna()
        rev_cagr = None
        if len(rev_vals) >= 4:
            s, e = rev_vals.iloc[-4], rev_vals.iloc[-1]
            if s and s > 0 and e and e > 0:
                rev_cagr = round((pow(e / s, 1 / 3) - 1) * 100, 2)

        gm_vals = df["GROSS_PROFIT_RATIO"].apply(_safe).dropna().tail(3).tolist()
        gm_change_pp = round(gm_vals[-1] - gm_vals[0], 2) if len(gm_vals) >= 3 else None
        gm_trend = "up" if gm_change_pp and gm_change_pp > 2 else (
            "down" if gm_change_pp and gm_change_pp < -2 else "flat"
        )

        ni_vals = df["HOLDER_PROFIT"].apply(_safe).dropna().tail(2).tolist()
        ocf_series = df["PER_NETCASH_OPERATE"].apply(_safe).dropna().tail(3).tolist()
        ni_neg_2yr = all(v is not None and v < 0 for v in ni_vals) if len(ni_vals) >= 2 else False
        ocf_neg_3yr = all(v is not None and v < 0 for v in ocf_series) if len(ocf_series) >= 3 else False

        # 快照
        snapshot = None
        try:
            sn = ak.stock_hk_financial_indicator_em(symbol=code)
            if sn is not None and not sn.empty:
                snapshot = sn.iloc[0]
        except Exception:
            pass

        mcap = _safe(snapshot.get("总市值(港元)")) if snapshot is not None else None
        shares = _safe(snapshot.get("已发行股本(股)")) if snapshot is not None else None

        result = {
            "ticker": ticker,
            "name": name_cn or ticker,
            "sector": _map_hk_sector(sector_cn),
            "industry": sector_cn,
            "currency": "HKD",
            "market_cap": mcap or 0,
            "shares_outstanding": shares or 0,
            "cash": None, "cash_pool": None, "st_invest": 0,
            "tca": None, "tcl": None, "nwc": None,
            "equity": (_safe(latest.get("BPS")) * shares) if shares and _safe(latest.get("BPS")) else 0,
            "total_assets": 0,
            "debt_ratio": _safe(latest.get("DEBT_ASSET_RATIO")),
            "revenue_cagr_3y": rev_cagr,
            "gross_margin": _safe(latest.get("GROSS_PROFIT_RATIO")),
            "net_margin": _safe(latest.get("NET_PROFIT_RATIO")),
            "roe": _safe(latest.get("ROE_AVG")),
            "gm_trend": gm_trend,
            "gm_change_pp": gm_change_pp,
            "ocf": (_safe(latest.get("PER_NETCASH_OPERATE")) or 0) * (shares or 0),
            "fcf": None,
            "capex": 0,
            "ocf_neg_3yr": ocf_neg_3yr,
            "ni_neg_2yr": ni_neg_2yr,
            "pe": _safe(snapshot.get("市盈率")) if snapshot is not None else None,
            "pb": _safe(snapshot.get("市净率")) if snapshot is not None else None,
            "eps": _safe(latest.get("EPS_TTM")),
            "div_yield": _safe(snapshot.get("股息率TTM(%)")) if snapshot is not None else None,
            "avg_pe_10y": None,
            "rev_hist_years": len(rev_vals),
            "ni_hist_years": len(ni_vals),
            "rev_hist": [(str(i), v) for i, v in enumerate(rev_vals.tolist())],
            "ni_hist": [(str(i), v) for i, v in enumerate(ni_vals)],
            "cf_hist": [],
            "eq_hist": [],
            "total_revenue": _safe(latest.get("OPERATE_INCOME")) or 0,
            "net_income": _safe(latest.get("HOLDER_PROFIT")) or 0,
            "capital_expenditure": 0,
        }
        _cache_set(ticker, "fund", result)
        return result
    except Exception as e:
        print(f"  [HK] {ticker}: {e}")
        return None


def _map_hk_sector(cn_label: str) -> str:
    if not cn_label:
        return "Unknown"
    mapping = {
        "银行": "Financials", "保险": "Financials", "证券": "Financials",
        "软件": "Information Technology", "半导体": "Information Technology",
        "药品": "Health Care", "生物": "Health Care", "医疗": "Health Care",
        "地产": "Real Estate", "物业": "Real Estate",
        "汽车": "Consumer Discretionary", "餐饮": "Consumer Discretionary",
        "食品": "Consumer Staples", "饮料": "Consumer Staples",
        "电力": "Utilities", "燃气": "Utilities",
        "石油": "Energy", "煤炭": "Energy",
        "金属": "Materials", "化工": "Materials",
        "建筑": "Industrials", "运输": "Industrials",
        "电讯": "Communication Services", "媒体": "Communication Services",
    }
    for k, v in mapping.items():
        if k in str(cn_label):
            return v
    return "Unknown"


# ── 公共 API ──
def fetch_fundamentals(ticker: str, market: str = "auto", with_benchmarks: bool = True) -> Optional[dict]:
    """拉取单只股票基本面数据，可选注入行业基准。"""
    if ticker.endswith(".HK"):
        result = _fetch_hk(ticker)
        bm_market = "HK"
    else:
        result = _fetch_eodhd(ticker)
        bm_market = "A" if ticker.endswith((".SHG", ".SHE")) else "US"

    if result and with_benchmarks:
        bm = load_benchmarks(bm_market)
        if bm:
            result = inject_benchmarks(result, bm)
            result["_has_benchmarks"] = True
        else:
            result["_has_benchmarks"] = False

    return result


def fetch_batch(tickers: list, max_workers: int = 4, with_benchmarks: bool = True) -> list[dict]:
    """并发拉取多只股票。"""
    results = []
    total = len(tickers)
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        fmap = {ex.submit(fetch_fundamentals, t, "auto", with_benchmarks): t for t in tickers}
        for fut in as_completed(fmap):
            t = fmap[fut]
            try:
                d = fut.result(timeout=120)
                if d:
                    results.append(d)
            except Exception:
                pass
            done += 1
            if done % 10 == 0:
                print(f"  数据: {done}/{total}")
            time.sleep(0.1)
    return results


# ─────────────────────────────────────────────────────────────
# 历史价格 + 指数价格 (Phase 1: 客观评分闭环用)
# ─────────────────────────────────────────────────────────────

# 市场基准映射 (全部用 EODHD，统一且稳定)
MARKET_INDEX = {
    "A":  {"code": "000300.SHG", "name": "沪深300"},
    "HK": {"code": "HSI.INDX",   "name": "恒生指数"},
    "US": {"code": "GSPC.INDX",  "name": "标普500"},
}


def detect_market(ticker: str) -> str:
    """从 ticker 后缀推断市场。"""
    if ticker.endswith(".HK"):
        return "HK"
    if ticker.endswith((".SHG", ".SHE", ".SZ", ".SH")):
        return "A"
    return "US"


def _nearest_trading_day(df: "pd.DataFrame", target_date: str) -> Optional[dict]:
    """从历史 DataFrame 中找到 ≤ target_date 的最近一个交易日的收盘。

    df 需要有 'date' 列（或 DatetimeIndex）和 'close'/'adjusted_close' 列。
    返回 {date, close} 或 None。
    """
    if df is None or df.empty:
        return None

    target = pd.to_datetime(target_date)

    # 统一时间索引
    if "date" in df.columns:
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
    elif not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
        df = df.sort_index()

    # 取 ≤ target 的最后一行
    sub = df[df.index <= target]
    if sub.empty:
        return None

    row = sub.iloc[-1]
    price = _safe(row.get("adjusted_close")) or _safe(row.get("close"))
    if price is None:
        return None
    return {"date": sub.index[-1].strftime("%Y-%m-%d"), "close": price}


def _retry(fn, *args, tries: int = 3, delay: float = 1.0, **kwargs):
    """通用网络重试。akshare 经常 RemoteDisconnected，重试 1-2 次基本能成。"""
    last = None
    for i in range(tries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last = e
            if i < tries - 1:
                time.sleep(delay * (i + 1))
    if last:
        raise last


def _setup_akshare_env():
    os.environ.setdefault("NO_PROXY", "")
    if "eastmoney.com" not in os.environ["NO_PROXY"]:
        os.environ["NO_PROXY"] = os.environ["NO_PROXY"] + ",eastmoney.com,push2his.eastmoney.com"


def _eodhd_history(code: str) -> Optional["pd.DataFrame"]:
    """统一 EODHD 历史价拉取（带重试）。"""
    from eodhd import APIClient
    api = APIClient(_eodhd_key())
    return _retry(api.get_historical_data, code, interval="d", results=3650)


def fetch_price_at(ticker: str, date: str = None) -> Optional[dict]:
    """拉取股票在某个日期（或最近）的收盘价。

    date: "YYYY-MM-DD"。None = 最新。
    返回 {ticker, date, close} 或 None。
    遇到非交易日自动取 ≤date 的最近一个交易日。
    全部走 EODHD（A/HK/US 统一）。
    """
    target_date = date or datetime.now().strftime("%Y-%m-%d")
    try:
        df = _eodhd_history(ticker)
        hit = _nearest_trading_day(df, target_date)
        if hit:
            return {"ticker": ticker, **hit}
    except Exception as e:
        print(f"  [PRICE] {ticker} @ {target_date}: {e}")
    return None


def fetch_index_at(market: str, date: str = None) -> Optional[dict]:
    """拉取大盘基准指数在某个日期（或最近）的收盘价。

    market: 'A' / 'HK' / 'US'
    """
    cfg = MARKET_INDEX.get(market)
    if not cfg:
        return None
    target_date = date or datetime.now().strftime("%Y-%m-%d")
    try:
        df = _eodhd_history(cfg["code"])
        hit = _nearest_trading_day(df, target_date)
        if hit:
            return {"market": market, "index": cfg["code"], "name": cfg["name"], **hit}
    except Exception as e:
        print(f"  [INDEX] {market} @ {target_date}: {e}")
    return None
