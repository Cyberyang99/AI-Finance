"""Ticker 模糊解析 — 中文/英文/拼音/代码 → 标准 ticker.

策略：
  ASCII 输入  → EODHD search（代码、英文名、拼音都能查）
  含中文输入  → akshare 本地缓存（首次冷启动拉网络，存到 memory/cache/symbols.json）
                兜底再试 EODHD

返回统一格式: list[{"ticker": "600519.SHG", "name": "贵州茅台", "exchange": "SHG", "country": "China"}]
"""

import json
import re
import ssl
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import certifi

from ..memory.store import PROJECT_DIR

CACHE_FILE = PROJECT_DIR / "memory" / "cache" / "symbols.json"
CACHE_TTL_DAYS = 14


def _is_chinese(s: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in s)


def _ssl_ctx() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())


def _eodhd_search(query: str, limit: int = 10) -> list[dict]:
    """调 EODHD search REST 接口。失败返回空 list。"""
    import os
    key = os.environ.get("EODHD_API_KEY", "")
    if not key:
        return []
    url = f"https://eodhd.com/api/search/{urllib.parse.quote(query)}?api_token={key}&fmt=json&limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=8, context=_ssl_ctx()) as r:
            data = json.loads(r.read())
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for h in data:
        code = h.get("Code", "")
        exch = h.get("Exchange", "")
        if not code or not exch:
            continue
        # 过滤非股票
        type_ = (h.get("Type") or "").lower()
        if type_ and type_ not in ("common stock", "preferred stock", "etf", "fund", "index"):
            continue
        out.append({
            "ticker": f"{code}.{exch}",
            "name": h.get("Name", ""),
            "exchange": exch,
            "country": h.get("Country", ""),
            "type": h.get("Type", ""),
            "source": "eodhd",
        })
    return out


# ── akshare 缓存（覆盖中文搜索） ──

def _ashare_code_to_exchange(code: str) -> str:
    """A 股 6 位代码 → SHG (沪) / SHE (深)。北交所暂归 SHG（akshare 没明确分）。"""
    if not code or len(code) != 6:
        return ""
    prefix = code[0]
    if prefix in ("6", "9"):
        return "SHG"
    if prefix in ("0", "3"):
        return "SHE"
    if prefix in ("4", "8"):
        return "SHG"  # 北交所，EODHD 也用 SHG，保持一致
    return ""


def _refresh_cache() -> dict:
    """从 akshare 拉 A 股 + 港股全表，缓存到 CACHE_FILE。

    会临时清空 HTTP_PROXY/HTTPS_PROXY（akshare 走的是中国站点，走代理会被拦）。
    """
    import os
    saved_proxy = {k: os.environ.pop(k, None) for k in
                   ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy")}
    symbols: list[dict] = []
    try:
        import akshare as ak
        print("  [resolver] 首次/过期，从 akshare 拉股票代码表（~30 秒）...")

        # A 股
        try:
            df = ak.stock_info_a_code_name()
            for _, row in df.iterrows():
                code = str(row.get("code", "")).zfill(6)
                name = str(row.get("name", "")).strip()
                exch = _ashare_code_to_exchange(code)
                if not exch or not name:
                    continue
                symbols.append({
                    "ticker": f"{code}.{exch}",
                    "name": name,
                    "exchange": exch,
                    "country": "China",
                })
            print(f"  [resolver] A 股 {sum(1 for s in symbols if s['exchange'] in ('SHG','SHE'))} 只")
        except Exception as e:
            print(f"  [resolver] akshare A 股拉取失败: {e}")

        # 港股 — 优先新浪接口 (稳)，失败再退东方财富 famous
        hk_ok = False
        try:
            df = ak.stock_hk_spot()  # 新浪
            for _, row in df.iterrows():
                code_raw = str(row.get("代码", "")).strip()
                code = code_raw.lstrip("0") or "0"  # 去前导 0：00700 → 700，与现有数据兼容
                name_cn = str(row.get("中文名称", "")).strip()
                name_en = str(row.get("英文名称", "")).strip()
                if not code:
                    continue
                if name_cn:
                    symbols.append({
                        "ticker": f"{code}.HK",
                        "name": name_cn,
                        "exchange": "HK",
                        "country": "HK",
                    })
                if name_en and name_en != name_cn:
                    symbols.append({
                        "ticker": f"{code}.HK",
                        "name": name_en,
                        "exchange": "HK",
                        "country": "HK",
                    })
            n_hk = sum(1 for s in symbols if s["exchange"] == "HK")
            print(f"  [resolver] 港股 {n_hk} 条 (含中英文别名)")
            hk_ok = True
        except Exception as e:
            print(f"  [resolver] akshare 港股 (新浪) 拉取失败: {e}")

        if not hk_ok:
            try:
                df = ak.stock_hk_famous_spot_em()
                for _, row in df.iterrows():
                    code = str(row.get("代码", "")).lstrip("0") or "0"
                    name = str(row.get("名称", "")).strip()
                    if not code or not name:
                        continue
                    symbols.append({
                        "ticker": f"{code}.HK", "name": name,
                        "exchange": "HK", "country": "HK",
                    })
                print(f"  [resolver] 港股 (famous 兜底) {sum(1 for s in symbols if s['exchange']=='HK')} 只")
            except Exception as e:
                print(f"  [resolver] akshare 港股 famous 也失败: {e}")
    finally:
        # 恢复代理变量
        for k, v in saved_proxy.items():
            if v is not None:
                os.environ[k] = v

    payload = {
        "updated_at": date.today().isoformat(),
        "count": len(symbols),
        "symbols": symbols,
    }
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"  [resolver] 已缓存 {len(symbols)} 只到 {CACHE_FILE.name}")
    return payload


def _load_cache(force_refresh: bool = False) -> list[dict]:
    """读 cache。过期或不存在则刷新。"""
    if force_refresh or not CACHE_FILE.exists():
        return _refresh_cache()["symbols"]
    try:
        payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        updated = date.fromisoformat(payload.get("updated_at", "2000-01-01"))
        if (date.today() - updated).days > CACHE_TTL_DAYS:
            return _refresh_cache()["symbols"]
        return payload.get("symbols", [])
    except Exception:
        return _refresh_cache()["symbols"]


def _akshare_search(query: str, limit: int = 10) -> list[dict]:
    """在缓存里子串匹配。

    排序：精确名 > 名字越短的（最像主公司） > 子串匹配。
    例：'阿里' 优先 '阿里巴巴' 不是 '阿里健康'/'阿里影业'。
    """
    symbols = _load_cache()
    q = query.strip()
    if not q:
        return []

    matches: list[tuple[int, dict]] = []  # (score, sym)
    for s in symbols:
        name = s["name"]
        if name == q:
            score = 0  # 最高优先
        elif name.startswith(q):
            # 越短越优（差距小）。例 '阿里巴巴' (4 字) 优先于 '阿里巴巴大文娱' (8 字)
            score = 10 + (len(name) - len(q))
        elif q in name:
            # 子串包含，再按长度差
            score = 100 + (len(name) - len(q))
        else:
            continue
        matches.append((score, s))

    matches.sort(key=lambda x: x[0])
    out = []
    seen = set()
    for _, s in matches:
        key = (s["ticker"], s["name"])
        if key in seen:
            continue
        seen.add(key)
        out.append({**s, "source": "akshare-cache"})
        if len(out) >= limit:
            break
    return out


# ── 公共入口 ──

TICKER_RE = re.compile(r"^(\d{1,6}\.(?:HK|SHG|SHE|US|SZ|SH)|[A-Z]{1,5}\.US)$", re.IGNORECASE)


def _normalize_ticker(t: str) -> str:
    """标准化 ticker。

    A 股 6 位 zfill；港股去前导 0（与现有数据格式 2513.HK / 700.HK 保持一致）。
    """
    if "." not in t:
        return t.upper()
    code, _, exch = t.upper().rpartition(".")
    if exch == "HK" and code.isdigit():
        code = code.lstrip("0") or "0"
    elif exch in ("SHG", "SHE", "SH", "SZ") and code.isdigit():
        code = code.zfill(6)
    return f"{code}.{exch}"


def resolve(query: str, limit: int = 10, refresh: bool = False) -> list[dict]:
    """模糊解析 query，返回候选列表。

    refresh=True 时强制刷新本地 akshare 缓存。
    """
    q = (query or "").strip()
    if not q:
        return []

    # 0. 如果输入本身就是标准 ticker 格式，直接返回
    if TICKER_RE.match(q):
        normalized = _normalize_ticker(q)
        return [{"ticker": normalized, "name": "(直接输入)", "exchange": normalized.split(".")[-1],
                 "country": "", "source": "direct"}]

    if refresh:
        _refresh_cache()

    # 1. 中文：先 akshare 缓存，没命中再 EODHD
    if _is_chinese(q):
        res = _akshare_search(q, limit=limit)
        if res:
            return res
        return _eodhd_search(q, limit=limit)

    # 2. ASCII：先 EODHD，没命中再 akshare（罕见，比如英文别名）
    res = _eodhd_search(q, limit=limit)
    if res:
        return res
    return _akshare_search(q, limit=limit)


def resolve_one(query: str) -> Optional[dict]:
    """返回最相关的一个匹配，无匹配返回 None。"""
    res = resolve(query, limit=1)
    return res[0] if res else None


# ── ticker → 简称 反查（读本地缓存，绝不触发联网刷新）──

_TICKER_NAME_MAP: Optional[dict] = None


def name_for_ticker(ticker: str) -> Optional[str]:
    """ticker → 公司简称（优先中文名）。仅读已有缓存文件，不刷新、不联网。

    缓存缺失或 ticker 不在表里则返回 None（调用方自行兜底显示代码）。
    """
    global _TICKER_NAME_MAP
    if _TICKER_NAME_MAP is None:
        _TICKER_NAME_MAP = {}
        try:
            if CACHE_FILE.exists():
                payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
                for s in payload.get("symbols", []):
                    t, n = s.get("ticker"), (s.get("name") or "").strip()
                    if not t or not n or n == "(直接输入)":
                        continue
                    cur = _TICKER_NAME_MAP.get(t)
                    # 优先保留中文名；已有中文则不被英文覆盖
                    if cur is None or (_is_chinese(n) and not _is_chinese(cur)):
                        _TICKER_NAME_MAP[t] = n
        except Exception:
            _TICKER_NAME_MAP = {}
    return _TICKER_NAME_MAP.get(_normalize_ticker(ticker))
