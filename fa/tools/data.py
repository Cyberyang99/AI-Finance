"""数据获取 — 从 EODHD + akshare 拉取基本面数据.

复用并简化 value-screen 的数据层，去掉缓存机制改为按需拉取。
"""

import os
import statistics
from typing import Optional

import pandas as pd


EODHD_KEY = os.environ.get("EODHD_API_KEY", "")
RMB = 100_000_000  # 亿


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


def _yi(v):
    x = _safe(v)
    return round(x / RMB, 2) if x is not None else None


def _cagr(series, years):
    if len(series) < years + 1:
        return None
    vals = [v for _, v in series[-(years + 1):]]
    s, e = vals[0], vals[-1]
    if s is None or e is None or s <= 0 or e <= 0:
        return None
    try:
        return round((pow(e / s, 1 / years) - 1) * 100, 2)
    except Exception:
        return None


def _latest_yr(yearly_dict):
    if not yearly_dict:
        return {}
    return yearly_dict[sorted(yearly_dict.keys())[-1]]


def _extract_series(yearly_dict, field):
    if not yearly_dict:
        return []
    return sorted(
        [(d, _safe(v.get(field))) for d, v in yearly_dict.items()
         if _safe(v.get(field)) is not None]
    )


def fetch_fundamentals(ticker: str, market: str = "auto") -> dict:
    """拉取单只股票的全部基本面数据.

    返回标准化 dict，供 Agent 使用:
    {
        ticker, name, sector, industry, currency, market_cap,
        cash, cash_pool, equity, debt_ratio,
        revenue_cagr_3y, gross_margin, net_margin, roe,
        gm_trend, gm_change_pp,
        ocf, fcf, ocf_neg_3yr, ni_neg_2yr,
        pe, pb, eps, div_yield, avg_pe_10y,
        rev_hist: [(year, val)], ni_hist: [(year, val)],
        cf_hist: [(year, val)], eq_hist: [(year, val)],
    }
    """
    if ticker.endswith(".HK"):
        return _fetch_hk(ticker)
    else:
        return _fetch_eodhd(ticker)


def _fetch_eodhd(ticker: str) -> Optional[dict]:
    try:
        from eodhd import APIClient
        api = APIClient(EODHD_KEY)
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
    equity = _safe(bs_latest.get("totalStockholderEquity")) or 0
    ocf = _safe(_latest_yr(cf_y).get("totalCashFromOperatingActivities")) or 0
    capex = _safe(_latest_yr(cf_y).get("capitalExpenditures")) or 0
    ni = _safe(_latest_yr(inc_y).get("netIncome")) or 0
    revenue = _safe(_latest_yr(inc_y).get("totalRevenue")) or 0
    gp = _safe(_latest_yr(inc_y).get("grossProfit"))

    # 毛利率/净利率/ROE/负债率
    gross_margin = round(gp / revenue * 100, 2) if gp and revenue > 0 else None
    net_margin = round(ni / revenue * 100, 2) if ni and revenue > 0 else None
    roe = round(ni / equity * 100, 2) if equity > 0 and ni else None
    ta = _safe(bs_latest.get("totalAssets"))
    tl = _safe(bs_latest.get("totalLiab")) or _safe(bs_latest.get("totalLiabilities"))
    debt_ratio = round(tl / ta * 100, 2) if ta and ta > 0 and tl is not None else None

    # 毛利率趋势
    gm_vals = [v for d, v in _extract_series(inc_y, "grossProfit")]
    rev_vals = [v for d, v in _extract_series(inc_y, "totalRevenue")]
    margins = [
        round(g / r * 100, 2) for g, r in zip(gm_vals[-3:], rev_vals[-3:])
        if r and r > 0
    ]
    gm_change_pp = round(margins[-1] - margins[0], 2) if len(margins) >= 3 else None
    gm_trend = (
        "up" if gm_change_pp and gm_change_pp > 2
        else "down" if gm_change_pp and gm_change_pp < -2
        else "flat"
    )

    # OCF/净利润 连负
    cf_vals = [v for _, v in _extract_series(cf_y, "totalCashFromOperatingActivities")[-3:]]
    ni_vals = [v for _, v in _extract_series(inc_y, "netIncome")[-2:]]
    ocf_neg_3yr = all(v is not None and v < 0 for v in cf_vals) if len(cf_vals) >= 3 else False
    ni_neg_2yr = all(v is not None and v < 0 for v in ni_vals) if len(ni_vals) >= 2 else False

    rev_hist = _extract_series(inc_y, "totalRevenue")
    ni_hist = _extract_series(inc_y, "netIncome")
    cf_hist = _extract_series(cf_y, "totalCashFromOperatingActivities")
    eq_hist = _extract_series(bs_y, "totalStockholderEquity")

    return {
        "ticker": ticker,
        "name": g.get("Name", ticker),
        "sector": g.get("GicSector", ""),
        "industry": g.get("GicIndustry", ""),
        "currency": g.get("CurrencyCode", ""),
        "market_cap": _safe(hl.get("MarketCapitalization")) or 0,
        "cash": _safe(bs_latest.get("cash")) or 0,
        "cash_pool": (_safe(bs_latest.get("cashAndShortTermInvestments"))
                      or (_safe(bs_latest.get("cash")) or 0) + (_safe(bs_latest.get("shortTermInvestments")) or 0)),
        "equity": equity,
        "debt_ratio": debt_ratio,
        "revenue_cagr_3y": _cagr(rev_hist, 3),
        "gross_margin": gross_margin,
        "net_margin": net_margin,
        "roe": roe,
        "gm_trend": gm_trend,
        "gm_change_pp": gm_change_pp,
        "ocf": ocf,
        "fcf": ocf - capex,
        "ocf_neg_3yr": ocf_neg_3yr,
        "ni_neg_2yr": ni_neg_2yr,
        "pe": _safe(hl.get("PERatio")),
        "pb": None,  # EODHD highlights 不一定有 PB
        "eps": None,
        "div_yield": round((_safe(hl.get("DividendYield")) or 0) * 100, 2),
        "avg_pe_10y": None,  # 需要单独计算，首次分析时跳过
        "rev_hist": rev_hist,
        "ni_hist": ni_hist,
        "cf_hist": cf_hist,
        "eq_hist": eq_hist,
        "total_revenue": revenue,
        "net_income": ni,
        "capital_expenditure": capex,
        "shares_outstanding": _safe(ss.get("SharesOutstanding")) or 0,
    }


def _fetch_hk(ticker: str) -> Optional[dict]:
    """港股基本面 via akshare."""
    code = ticker.split(".")[0].zfill(5)

    try:
        import akshare as ak
        os.environ["NO_PROXY"] = os.environ.get("NO_PROXY", "") + ",eastmoney.com,eastmoney.com.cn"

        # 公司概况
        try:
            profile = ak.stock_hk_company_profile_em(symbol=code)
            name_cn = str(profile.iloc[0].get("公司名称", "")) if profile is not None and not profile.empty else ""
            sector_cn = str(profile.iloc[0].get("所属行业", "")) if profile is not None and not profile.empty else ""
        except Exception:
            name_cn, sector_cn = "", ""

        # 财务指标
        df = ak.stock_financial_hk_analysis_indicator_em(symbol=code, indicator="年度")
        if df is None or df.empty:
            return None

        df = df.sort_values("REPORT_DATE")
        latest = df.iloc[-1]

        # 3年营收CAGR
        rev_vals = df["OPERATE_INCOME"].apply(_safe).dropna()
        rev_cagr = None
        if len(rev_vals) >= 4:
            s, e = rev_vals.iloc[-4], rev_vals.iloc[-1]
            if s and s > 0 and e and e > 0:
                rev_cagr = round((pow(e / s, 1 / 3) - 1) * 100, 2)

        # 毛利率趋势
        gm_vals = df["GROSS_PROFIT_RATIO"].apply(_safe).dropna().tail(3).tolist()
        gm_change_pp = round(gm_vals[-1] - gm_vals[0], 2) if len(gm_vals) >= 3 else None
        gm_trend = (
            "up" if gm_change_pp and gm_change_pp > 2
            else "down" if gm_change_pp and gm_change_pp < -2
            else "flat"
        )

        ni_vals = df["HOLDER_PROFIT"].apply(_safe).dropna().tail(2).tolist()
        ni_neg_2yr = all(v is not None and v < 0 for v in ni_vals) if len(ni_vals) >= 2 else False

        ocf_series = df["PER_NETCASH_OPERATE"].apply(_safe).dropna().tail(3).tolist()
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
        pe = _safe(snapshot.get("市盈率")) if snapshot is not None else None
        pb = _safe(snapshot.get("市净率")) if snapshot is not None else None
        div_yield = _safe(snapshot.get("股息率TTM(%)")) if snapshot is not None else None

        return {
            "ticker": ticker,
            "name": name_cn or ticker,
            "sector": _map_hk_sector(sector_cn),
            "industry": sector_cn,
            "currency": "HKD",
            "market_cap": mcap or 0,
            "cash": None,
            "cash_pool": None,
            "equity": _safe(latest.get("BPS")) * (_safe(snapshot.get("已发行股本(股)")) or 0)
                       if snapshot is not None and _safe(latest.get("BPS")) else 0,
            "debt_ratio": _safe(latest.get("DEBT_ASSET_RATIO")),
            "revenue_cagr_3y": rev_cagr,
            "gross_margin": _safe(latest.get("GROSS_PROFIT_RATIO")),
            "net_margin": _safe(latest.get("NET_PROFIT_RATIO")),
            "roe": _safe(latest.get("ROE_AVG")),
            "gm_trend": gm_trend,
            "gm_change_pp": gm_change_pp,
            "ocf": (_safe(latest.get("PER_NETCASH_OPERATE")) or 0)
                   * (_safe(snapshot.get("已发行股本(股)")) or 0) if snapshot is not None else 0,
            "fcf": None,
            "ocf_neg_3yr": ocf_neg_3yr,
            "ni_neg_2yr": ni_neg_2yr,
            "pe": pe,
            "pb": pb,
            "eps": _safe(latest.get("EPS_TTM")),
            "div_yield": div_yield,
            "avg_pe_10y": None,
            "rev_hist": [],
            "ni_hist": [],
            "cf_hist": [],
            "eq_hist": [],
            "total_revenue": _safe(latest.get("OPERATE_INCOME")) or 0,
            "net_income": _safe(latest.get("HOLDER_PROFIT")) or 0,
            "capital_expenditure": 0,
            "shares_outstanding": _safe(snapshot.get("已发行股本(股)")) if snapshot is not None else 0,
        }
    except Exception as e:
        print(f"  [HK] {ticker}: {e}")
        return None


def _map_hk_sector(cn_label: str) -> str:
    """简化的港股行业映射，完整版后续从 value-screen 迁移."""
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
        "建筑": "Industrials", "运输": "Industrials", "航空": "Industrials",
        "电讯": "Communication Services", "媒体": "Communication Services",
    }
    label = str(cn_label).strip()
    for k, v in mapping.items():
        if k in label:
            return v
    return "Unknown"


def fetch_batch(tickers: list, max_workers: int = 4) -> list[dict]:
    """并发拉取多只股票数据."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import time

    results = []
    total = len(tickers)
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        fmap = {ex.submit(fetch_fundamentals, t): t for t in tickers}
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
