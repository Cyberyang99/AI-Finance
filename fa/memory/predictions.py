"""预测注册与验证追踪 — Agent 进化闭环的核心引擎.

每个投资论点必须包含可验证的预测。
fa review 自动读取→比对→判定→计算准确率。
"""

import json
from datetime import datetime
from typing import Optional

from .store import MemoryStore


class PredictionRegistry:
    """管理预测的完整生命周期: 注册 → 追踪 → 验证 → 判定."""

    def __init__(self, store: MemoryStore):
        self.store = store

    def extract_from_thesis(self, predictions_str: str) -> list[dict]:
        """从 Agent 输出的 JSON 解析预测列表。"""
        if not predictions_str:
            return []
        try:
            preds = json.loads(predictions_str) if isinstance(predictions_str, str) else predictions_str
            return preds if isinstance(preds, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    def verify(self, ticker: str, thesis_id: int,
               current_data: dict) -> list[dict]:
        """用最新数据验证预测。返回每个预测的判定结果。

        current_data: fetch_fundamentals 返回的最新数据 dict
        """
        thesis = self.store.get_thesis(ticker)
        if not thesis:
            return []

        preds = self.extract_from_thesis(thesis.get("predictions", "[]"))
        results = []

        for i, pred in enumerate(preds):
            metric = pred.get("metric", "")
            expected = pred.get("expected")
            deadline = pred.get("deadline", "")

            actual = self._resolve_metric(current_data, metric)
            result = self._judge(expected, actual)

            results.append({
                "pred_id": i,
                "prediction": pred.get("prediction", ""),
                "metric": metric,
                "expected": expected,
                "actual": actual,
                "result": result["verdict"],
                "deviation": result["deviation"],
                "deadline": deadline,
            })

        return results

    def _resolve_metric(self, data: dict, metric: str) -> Optional[float]:
        """从数据 dict 中解析指标值。支持嵌套路径如 'gross_margin'."""
        # 直接映射
        key_map = {
            "gross_margin": "gross_margin",
            "毛利率": "gross_margin",
            "net_margin": "net_margin",
            "净利率": "net_margin",
            "roe": "roe",
            "revenue_cagr_3y": "revenue_cagr_3y",
            "营收增速": "revenue_cagr_3y",
            "debt_ratio": "debt_ratio",
            "资产负债率": "debt_ratio",
            "pe": "pe",
            "ocf": "ocf",
        }
        key = key_map.get(metric, metric)
        return data.get(key)

    def _judge(self, expected, actual) -> dict:
        """判定预测结果。支持数值比较和定性判断。

        expected 格式:
          - "> 56": 大于56
          - "> 20%": 大于20 (% 可选)
          - "改善": 定性改善
          - "50-60": 区间
        """
        if expected is None or actual is None:
            return {"verdict": "无法验证", "deviation": None}

        expected_str = str(expected).strip()

        # 定性判断
        if expected_str in ("改善", "上升", "增长", "加速", "up", "increase"):
            if isinstance(actual, (int, float)) and actual > 0:
                return {"verdict": "正确", "deviation": None}
            return {"verdict": "部分正确", "deviation": None}

        # 区间: "50-60"
        if "-" in expected_str and not expected_str.startswith(("-", ">")):
            try:
                lo, hi = expected_str.split("-")
                lo, hi = float(lo), float(hi)
                actual_f = float(actual)
                if lo <= actual_f <= hi:
                    return {"verdict": "正确", "deviation": round(actual_f - (lo + hi) / 2, 2)}
                elif actual_f < lo:
                    return {"verdict": "错误", "deviation": round(actual_f - lo, 2)}
                else:
                    return {"verdict": "错误", "deviation": round(actual_f - hi, 2)}
            except ValueError:
                pass

        # 数值比较: "> 56", "> 20%", "< 30"
        import re
        m = re.match(r"([><]=?)\s*(\d+\.?\d*)\s*%?", expected_str)
        if m:
            op, val = m.group(1), float(m.group(2))
            actual_f = float(actual)
            if op == ">" and actual_f > val:
                return {"verdict": "正确", "deviation": round(actual_f - val, 2)}
            elif op == ">=" and actual_f >= val:
                return {"verdict": "正确", "deviation": round(actual_f - val, 2)}
            elif op == "<" and actual_f < val:
                return {"verdict": "正确", "deviation": round(val - actual_f, 2)}
            elif op == "<=" and actual_f <= val:
                return {"verdict": "正确", "deviation": round(val - actual_f, 2)}
            else:
                return {"verdict": "错误", "deviation": round(actual_f - val, 2)}

        # Fallback: 定性比较
        return {"verdict": "无法判定", "deviation": None}

    def accuracy_report(self, ticker: str = None) -> dict:
        """生成预测准确率报告。"""
        return self.store.get_prediction_accuracy(ticker)
