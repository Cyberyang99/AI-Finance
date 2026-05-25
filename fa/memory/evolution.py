"""进化引擎 — 从回顾中提取模式，建议框架更新。

三个核心功能:
  1. 偏差分析: 找出预测中最常出错的维度
  2. 模式提取: 从多次回顾中发现重复的误判模式
  3. 框架建议: 基于偏差和模式，建议修改 L1 硬框架或 L2 软知识
"""

from datetime import datetime

from .store import MemoryStore


class EvolutionEngine:
    def __init__(self, store: MemoryStore):
        self.store = store

    def analyze_biases(self) -> dict:
        """分析预测偏差模式。

        扫描所有回顾记录，找出:
        - 哪个维度的预测准确率最低
        - 是否系统性高估/低估
        """
        with self.store._conn() as c:
            # theses 表没有 sector 字段，sector 信息在分析时从基本面数据动态取
            # 这里改成从 key_metrics JSON 里取，没有就归"未知"
            rows = c.execute("""
                SELECT r.prediction_results, t.key_metrics, t.ticker
                FROM reviews r
                JOIN theses t ON r.thesis_id = t.id
                ORDER BY r.reviewed_at DESC
                LIMIT 100
            """).fetchall()

        import json

        dimension_stats = {}  # {metric: {correct, partial, wrong, total}}
        sector_stats = {}     # {sector: accuracy}

        for row in rows:
            results = json.loads(row["prediction_results"]) if row["prediction_results"] else []
            try:
                km = json.loads(row["key_metrics"]) if row["key_metrics"] else {}
            except Exception:
                km = {}
            sector = km.get("sector") or "未知"

            if sector not in sector_stats:
                sector_stats[sector] = {"correct": 0, "total": 0}

            for p in results:
                metric = p.get("metric", "未分类")
                if metric not in dimension_stats:
                    dimension_stats[metric] = {"correct": 0, "partial": 0, "wrong": 0, "total": 0}

                dimension_stats[metric]["total"] += 1
                sector_stats[sector]["total"] += 1

                verdict = p.get("result", "")
                if verdict == "正确":
                    dimension_stats[metric]["correct"] += 1
                    sector_stats[sector]["correct"] += 1
                elif verdict == "部分正确":
                    dimension_stats[metric]["partial"] += 1
                else:
                    dimension_stats[metric]["wrong"] += 1

        # 计算准确率
        for m in dimension_stats:
            s = dimension_stats[m]
            s["accuracy"] = round(s["correct"] / s["total"] * 100, 1) if s["total"] > 0 else 0

        for s in sector_stats:
            st = sector_stats[s]
            st["accuracy"] = round(st["correct"] / st["total"] * 100, 1) if st["total"] > 0 else 0

        return {
            "dimensions": dimension_stats,
            "sectors": sector_stats,
            "weakest": sorted(
                [(m, s["accuracy"]) for m, s in dimension_stats.items() if s["total"] >= 3],
                key=lambda x: x[1]
            )[:5],  # 最弱的5个维度
        }

    def extract_patterns(self) -> list[dict]:
        """从回顾中提取可复用的误判模式。

        模式类别:
          - overoptimistic: 增长预测系统性偏高
          - cyclically_blind: 忽视了周期因素
          - margin_misjudge: 毛利率判断反复出错
          - management_misread: 管理层信号解读有偏差
        """
        biases = self.analyze_biases()
        patterns = []

        # 模式1: 增长预测偏差
        for metric in ["营收增速", "revenue_cagr_3y"]:
            if metric in biases["dimensions"]:
                s = biases["dimensions"][metric]
                if s["accuracy"] < 50 and s["total"] >= 3:
                    patterns.append({
                        "name": "增长预测乐观偏差",
                        "description": f"对增速的预测准确率仅 {s['accuracy']}%（{s['total']}次），存在系统性高估倾向",
                        "category": "mistake",
                        "suggested_fix": "在估值章节增加'增速敏感性分析'，明确标注高/中/低三种情景",
                    })

        # 模式2: 行业系统性低准确率
        for sector, stats in biases["sectors"].items():
            if stats["accuracy"] < 60 and stats["total"] >= 5:
                patterns.append({
                    "name": f"{sector}行业判断信心过高",
                    "description": f"在{sector}行业的预测准确率仅 {stats['accuracy']}%，建议增加行业特定检查项",
                    "category": "mistake",
                    "suggested_fix": f"在板块知识库中补充{sector}的行业特性，并在分析时加载",
                })

        # 模式3: 估值判断困境
        pe_metrics = [m for m in biases["dimensions"] if "pe" in m.lower() or "估值" in m]
        if pe_metrics:
            total_pe = sum(biases["dimensions"][m]["total"] for m in pe_metrics)
            correct_pe = sum(biases["dimensions"][m]["correct"] for m in pe_metrics)
            if total_pe >= 3:
                acc = round(correct_pe / total_pe * 100, 1)
                if acc < 50:
                    patterns.append({
                        "name": "估值判断准确率低",
                        "description": f"估值相关预测准确率仅 {acc}%，纯定量估值可能不是有效工具",
                        "category": "insight",
                        "suggested_fix": "弱化对精确估值数字的依赖，更多采用'反推法'和情境分析",
                    })

        return patterns

    def suggest_framework_updates(self) -> list[dict]:
        """基于偏差分析和模式提取，建议框架更新。

        返回列表，每项是一个具体的修改建议。
        用户确认后才执行。
        """
        biases = self.analyze_biases()
        patterns = self.extract_patterns()
        suggestions = []

        # 从偏差点生成建议
        for metric, acc in biases.get("weakest", []):
            if acc < 50:
                suggestions.append({
                    "target": "framework/checklist.md",
                    "type": "add",
                    "reason": f"'{metric}'维度预测准确率仅 {acc}%，当前检查清单可能未充分覆盖此维度的常见陷阱",
                    "suggested_content": f"### {metric} 专项检查\n- [ ] 历史数据中是否有类似的{metric}改善/恶化周期？\n- [ ] 当前{metric}水平是否可持续，还是受一次性因素影响？",
                    "confidence": "高" if acc < 30 else "中",
                })

        # 从模式生成建议
        for p in patterns:
            suggestions.append({
                "target": f"knowledge/patterns/{p['name']}.md",
                "type": "create",
                "reason": f"发现重复模式: {p['description']}",
                "suggested_content": p.get("suggested_fix", ""),
                "confidence": "中",
            })

        return suggestions

    def execute_update(self, suggestion: dict):
        """执行框架更新（用户确认后调用）。"""
        target = suggestion["target"]
        content = suggestion["suggested_content"]

        # 记录变更历史
        self.store.log_framework_change(
            file_name=target,
            change_type=suggestion["type"],
            old_text="",
            new_text=content,
            reason=suggestion["reason"],
        )

        # 如果是 pattern，保存到数据库
        if "patterns/" in target:
            name = target.replace("knowledge/patterns/", "").replace(".md", "")
            self.store.save_pattern(
                name=name,
                description=suggestion["reason"],
                category="mistake",
            )

        return {"status": "已执行", "target": target}
