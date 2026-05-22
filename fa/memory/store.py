"""SQLite 持久化存储 — 三层记忆的底层引擎.

三层记忆:
  L1 硬框架 — memory/framework/*.md (人工编辑，不存SQLite)
  L2 软知识 — memory/knowledge/ (SQLite + 文件双写)
  L3 情景记忆 — theses/scans/reviews (SQLite 主存储 + Markdown 导出)
"""

import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
DB_PATH = PROJECT_DIR / "memory" / "agent.db"


class MemoryStore:
    def __init__(self, db_path: Path = None):
        self.db_path = db_path or DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self):
        with self._conn() as c:
            c.executescript("""
                -- 情景记忆: 投资论点
                CREATE TABLE IF NOT EXISTS theses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    thesis TEXT NOT NULL,           -- 核心论点
                    assumptions TEXT,                -- JSON: [{"assumption","validation","deadline"}]
                    predictions TEXT,                -- JSON: [{"prediction","metric","deadline","status"}]
                    risk_flags TEXT,                 -- JSON: [flag1, flag2]
                    key_metrics TEXT,                -- JSON: {"pe":15,"roe":21,...} 记录时的快照
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now')),
                    review_due TEXT,                 -- 下次回顾日期
                    status TEXT DEFAULT 'active'     -- active / archived / falsified
                );

                -- 情景记忆: 回顾记录
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    thesis_id INTEGER REFERENCES theses(id),
                    prediction_results TEXT,          -- JSON: [{"pred_id":0,"result":"正确","actual":"...","deviation":"..."}]
                    framework_feedback TEXT,          -- 框架是否需要调整
                    learnings TEXT,                   -- 提取的经验教训
                    reviewed_at TEXT DEFAULT (datetime('now'))
                );

                -- 软知识: 板块知识
                CREATE TABLE IF NOT EXISTS sector_knowledge (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sector TEXT NOT NULL UNIQUE,
                    characteristics TEXT,             -- 行业特征描述
                    key_drivers TEXT,                 -- JSON: [driver1, driver2]
                    common_risks TEXT,                -- JSON: [risk1, risk2]
                    valuation_notes TEXT,             -- 估值注意事项
                    last_scan_at TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                );

                -- 软知识: 模式库
                CREATE TABLE IF NOT EXISTS patterns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT,
                    category TEXT,                    -- success / mistake / insight
                    evidence_count INTEGER DEFAULT 1,
                    examples TEXT,                    -- JSON: [ticker, ticker]
                    created_at TEXT DEFAULT (datetime('now'))
                );

                -- 进化: 框架变更历史
                CREATE TABLE IF NOT EXISTS framework_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_name TEXT NOT NULL,           -- checklist / red-flags / valuation
                    change_type TEXT,                  -- add / modify / remove
                    old_text TEXT,
                    new_text TEXT,
                    reason TEXT,
                    review_result TEXT,                -- 之后验证此变更的效果
                    created_at TEXT DEFAULT (datetime('now'))
                );

                -- 索引
                CREATE INDEX IF NOT EXISTS idx_theses_ticker ON theses(ticker);
                CREATE INDEX IF NOT EXISTS idx_theses_status ON theses(status);
                CREATE INDEX IF NOT EXISTS idx_theses_review ON theses(review_due);
                CREATE INDEX IF NOT EXISTS idx_reviews_ticker ON reviews(ticker);
                CREATE INDEX IF NOT EXISTS idx_sector_name ON sector_knowledge(sector);
            """)

    # ── 论点操作 ──

    def save_thesis(self, ticker: str, thesis: str, assumptions: list = None,
                    predictions: list = None, risk_flags: list = None,
                    key_metrics: dict = None, review_due: str = None):
        """保存/更新个股论点。"""
        if review_due is None:
            review_due = (datetime.now() + timedelta(days=90)).strftime("%Y-%m-%d")

        with self._conn() as c:
            existing = c.execute(
                "SELECT id FROM theses WHERE ticker=? AND status='active'", (ticker,)
            ).fetchone()

            if existing:
                c.execute("""
                    UPDATE theses SET thesis=?, assumptions=?, predictions=?,
                    risk_flags=?, key_metrics=?, updated_at=datetime('now'),
                    review_due=?
                    WHERE id=?
                """, (thesis, json.dumps(assumptions or [], ensure_ascii=False),
                      json.dumps(predictions or [], ensure_ascii=False),
                      json.dumps(risk_flags or [], ensure_ascii=False),
                      json.dumps(key_metrics or {}, ensure_ascii=False),
                      review_due, existing["id"]))
            else:
                c.execute("""
                    INSERT INTO theses (ticker, thesis, assumptions, predictions,
                    risk_flags, key_metrics, review_due)
                    VALUES (?,?,?,?,?,?,?)
                """, (ticker, thesis, json.dumps(assumptions or [], ensure_ascii=False),
                      json.dumps(predictions or [], ensure_ascii=False),
                      json.dumps(risk_flags or [], ensure_ascii=False),
                      json.dumps(key_metrics or {}, ensure_ascii=False),
                      review_due))

    def get_thesis(self, ticker: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM theses WHERE ticker=? AND status='active' ORDER BY updated_at DESC LIMIT 1",
                (ticker,)
            ).fetchone()
            if row:
                return dict(row)
        return None

    def list_due_reviews(self, days: int = None) -> list:
        """列出需要回顾的股票。"""
        if days is None:
            # 默认: review_due 在今天之前
            cutoff = datetime.now().strftime("%Y-%m-%d")
            query = "SELECT ticker, review_due, updated_at FROM theses WHERE status='active' AND review_due <= ?"
        else:
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            query = "SELECT ticker, review_due, updated_at FROM theses WHERE status='active' AND updated_at <= ?"
        with self._conn() as c:
            return [dict(r) for r in c.execute(query, (cutoff,)).fetchall()]

    def archive_thesis(self, ticker: str):
        with self._conn() as c:
            c.execute("UPDATE theses SET status='falsified' WHERE ticker=?", (ticker,))

    # ── 回顾操作 ──

    def save_review(self, ticker: str, thesis_id: int, prediction_results: list,
                    framework_feedback: str = None, learnings: str = None):
        with self._conn() as c:
            c.execute("""
                INSERT INTO reviews (ticker, thesis_id, prediction_results,
                framework_feedback, learnings)
                VALUES (?,?,?,?,?)
            """, (ticker, thesis_id, json.dumps(prediction_results, ensure_ascii=False),
                  framework_feedback, learnings))

    def get_reviews(self, ticker: str) -> list:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM reviews WHERE ticker=? ORDER BY reviewed_at DESC", (ticker,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_prediction_accuracy(self, ticker: str = None) -> dict:
        """计算预测准确率统计。"""
        with self._conn() as c:
            if ticker:
                rows = c.execute(
                    "SELECT prediction_results FROM reviews WHERE ticker=?", (ticker,)
                ).fetchall()
            else:
                rows = c.execute("SELECT prediction_results FROM reviews").fetchall()

        total, correct, partial, wrong = 0, 0, 0, 0
        for r in rows:
            results = json.loads(r["prediction_results"]) if r["prediction_results"] else []
            for p in results:
                total += 1
                if p.get("result") == "正确":
                    correct += 1
                elif p.get("result") == "部分正确":
                    partial += 1
                else:
                    wrong += 1
        return {
            "total": total,
            "correct": correct,
            "partial": partial,
            "wrong": wrong,
            "accuracy": round(correct / total * 100, 1) if total > 0 else 0
        }

    # ── 板块知识 ──

    def get_sector_knowledge(self, sector: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM sector_knowledge WHERE sector=?", (sector,)
            ).fetchone()
            return dict(row) if row else None

    def save_sector_knowledge(self, sector: str, characteristics: str,
                              key_drivers: list = None, common_risks: list = None,
                              valuation_notes: str = None):
        with self._conn() as c:
            c.execute("""
                INSERT INTO sector_knowledge (sector, characteristics, key_drivers,
                common_risks, valuation_notes, last_scan_at, updated_at)
                VALUES (?,?,?,?,?,datetime('now'),datetime('now'))
                ON CONFLICT(sector) DO UPDATE SET
                    characteristics=excluded.characteristics,
                    key_drivers=excluded.key_drivers,
                    common_risks=excluded.common_risks,
                    valuation_notes=excluded.valuation_notes,
                    last_scan_at=datetime('now'),
                    updated_at=datetime('now')
            """, (sector, characteristics,
                  json.dumps(key_drivers or [], ensure_ascii=False),
                  json.dumps(common_risks or [], ensure_ascii=False),
                  valuation_notes))

    def list_sectors(self) -> list:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT sector, last_scan_at FROM sector_knowledge ORDER BY sector"
            ).fetchall()]

    # ── 模式操作 ──

    def save_pattern(self, name: str, description: str, category: str,
                     examples: list = None):
        with self._conn() as c:
            existing = c.execute(
                "SELECT id, evidence_count FROM patterns WHERE name=?", (name,)
            ).fetchone()
            if existing:
                c.execute("""
                    UPDATE patterns SET description=?, evidence_count=?,
                    examples=? WHERE id=?
                """, (description, existing["evidence_count"] + 1,
                      json.dumps(examples or [], ensure_ascii=False), existing["id"]))
            else:
                c.execute("""
                    INSERT INTO patterns (name, description, category, examples)
                    VALUES (?,?,?,?)
                """, (name, description, category,
                      json.dumps(examples or [], ensure_ascii=False)))

    def get_patterns(self, category: str = None) -> list:
        with self._conn() as c:
            if category:
                rows = c.execute(
                    "SELECT * FROM patterns WHERE category=? ORDER BY evidence_count DESC",
                    (category,)
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM patterns ORDER BY category, evidence_count DESC"
                ).fetchall()
            return [dict(r) for r in rows]

    # ── 框架变更 ──

    def log_framework_change(self, file_name: str, change_type: str,
                             old_text: str, new_text: str, reason: str):
        with self._conn() as c:
            c.execute("""
                INSERT INTO framework_changes (file_name, change_type, old_text, new_text, reason)
                VALUES (?,?,?,?,?)
            """, (file_name, change_type, old_text, new_text, reason))

    def get_framework_changes(self, limit: int = 20) -> list:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM framework_changes ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()]

    # ── 仪表盘 ──

    def dashboard(self) -> dict:
        """一目了然的状态概览。"""
        with self._conn() as c:
            active = c.execute(
                "SELECT COUNT(*) FROM theses WHERE status='active'"
            ).fetchone()[0]
            due = c.execute(
                "SELECT COUNT(*) FROM theses WHERE status='active' AND review_due <= date('now')"
            ).fetchone()[0]
            sectors = c.execute(
                "SELECT COUNT(*) FROM sector_knowledge"
            ).fetchone()[0]
            patterns = c.execute(
                "SELECT COUNT(*) FROM patterns"
            ).fetchone()[0]
            recent_review = c.execute(
                "SELECT MAX(reviewed_at) FROM reviews"
            ).fetchone()[0]
            acc = self.get_prediction_accuracy()
        return {
            "active_theses": active,
            "reviews_due": due,
            "sectors_known": sectors,
            "patterns_found": patterns,
            "last_review": recent_review or "从未",
            "prediction_accuracy": f"{acc['accuracy']}% ({acc['correct']}/{acc['total']})" if acc['total'] > 0 else "尚无数据",
        }
