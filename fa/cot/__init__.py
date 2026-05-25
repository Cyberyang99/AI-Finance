"""CoT (Chain of Thought) 模块 — 思维链加载/评分/联合投票.

设计来源：国金证券《主观投资框架验证与个股决策 Agent》
- CoT 三段式: trigger / COT / signal (1-10)
- 单 CoT 对单股: 不符合 / 较符合 / 完全符合 + 置信度
- 联合投票: 多 CoT 等权 / 加权选股
"""

from .loader import load_cots, list_cot_files
from .scorer import score_cot_against_stock, CotScorer, MATCH_LEVELS, MATCH_VALUE
from .voter import vote, weighted_vote, score_all_cots
from .merger import merge_sector, list_sectors_with_cots

__all__ = [
    "load_cots", "list_cot_files",
    "score_cot_against_stock", "CotScorer", "MATCH_LEVELS", "MATCH_VALUE",
    "vote", "weighted_vote", "score_all_cots",
    "merge_sector", "list_sectors_with_cots",
]
