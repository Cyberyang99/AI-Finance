"""独立 LLM Agent 角色 — Predictor/Critic/Reflector/Evolver.

设计原则（PDF 1 §2.2.2）:
  - 每个 Agent 独立 system prompt，不共享状态
  - JSON 通信，便于程序消费
  - Critic 必须独立于 Predictor，评分有客观锚定
"""

from .critic import CriticAgent
from .recall import RecallAgent
from .reflector import ReflectorAgent
from .conflict import ConflictResolver

__all__ = ["CriticAgent", "RecallAgent", "ReflectorAgent", "ConflictResolver"]
