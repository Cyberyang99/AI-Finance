"""板块成分股发现 — 根据主题词查找相关股票.

当前版本: 基于预设映射表 + 常见主题。
后续进化: Agent 自己搜索/学习新板块的成分股。
"""

# 预设板块 → 成分股列表
PRESET_SECTORS = {
    "固态电池": [
        "300750.SHE", "002074.SHE", "300014.SHE", "688567.SHG",
        "300438.SHE", "002460.SHE", "688116.SHG", "300568.SHE",
        "002340.SHE", "600884.SHG",
    ],
    "HPC散热": [
        "002837.SHE", "300499.SHE", "300602.SHE", "002335.SHE",
        "002518.SHE", "300684.SHE", "688800.SHG",
    ],
    "人形机器人": [
        "300124.SHE", "002747.SHE", "688017.SHG", "300660.SHE",
        "002896.SHE", "300403.SHE", "688160.SHG", "603728.SHG",
    ],
    "AI算力": [
        "688256.SHG", "603019.SHG", "688111.SHG", "002230.SHE",
        "300502.SHE", "688041.SHG", "688012.SHG",
    ],
    "低空经济": [
        "002085.SHE", "300177.SHE", "688070.SHG", "300489.SHE",
        "688297.SHG", "600118.SHG",
    ],
    "创新药": [
        "688180.SHG", "688266.SHG", "300558.SHE", "688192.SHG",
        "688382.SHG", "688331.SHG", "300759.SHE", "688278.SHG",
    ],
    "光伏": [
        "601012.SHG", "688599.SHG", "300274.SHE", "002459.SHE",
        "688390.SHG", "600438.SHG", "300763.SHE",
    ],
    "新能源汽车": [
        "002594.SHE", "300750.SHE", "601127.SHG", "002460.SHE",
        "300014.SHE", "002074.SHE", "688567.SHG",
    ],
    "半导体": [
        "688981.SHG", "688012.SHG", "002371.SHE", "603986.SHG",
        "688396.SHG", "688256.SHG", "688126.SHG",
    ],
    "中概互联": [
        "BABA.US", "JD.US", "PDD.US", "BIDU.US", "NIO.US",
        "BILI.US", "TME.US", "ZTO.US", "TAL.US", "VIPS.US",
        "BEKE.US", "NTES.US",
    ],
    "港股科技": [
        "0700.HK", "9988.HK", "3690.HK", "9618.HK", "9999.HK",
        "1810.HK", "2015.HK", "1024.HK", "9888.HK", "9961.HK",
    ],
    "恒生高股息": [
        "0005.HK", "0011.HK", "0388.HK", "1398.HK", "3988.HK",
        "0939.HK", "1288.HK", "2388.HK", "3328.HK", "2628.HK",
    ],
}


def find_sector_peers(topic: str) -> list[str]:
    """根据主题词查找成分股。精确匹配预设列表，模糊匹配关键词。"""
    # 精确匹配
    if topic in PRESET_SECTORS:
        return PRESET_SECTORS[topic]

    # 模糊匹配
    topic_lower = topic.lower().replace(" ", "")
    for key, tickers in PRESET_SECTORS.items():
        key_lower = key.lower().replace(" ", "")
        if topic_lower in key_lower or key_lower in topic_lower:
            return tickers

    # 无匹配 — Agent 需要自己搜索
    return []


def list_sectors() -> list[str]:
    return sorted(PRESET_SECTORS.keys())
