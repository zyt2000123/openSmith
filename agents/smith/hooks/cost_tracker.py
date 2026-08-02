"""成本跟踪 Hook

跟踪每个会话的 token 使用和成本。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
from common.paths import get_data_root
from engine.execution.hooks import StopHook

# 成本记录含会话 ID 与 token 统计，必须私有（0600）且不被 umask 影响。
_COST_FILE_MODE = 0o600


class CostTrackerHook(StopHook):
    """成本跟踪 Hook

    在每轮响应结束时记录：
    - session_id
    - timestamp
    - input_tokens
    - output_tokens
    - estimated_cost_usd
    - model_used
    """

    # 模型定价（美元/百万 token）
    MODEL_PRICING = {
        "claude-opus-4": {"input": 15.0, "output": 75.0},
        "claude-sonnet-4": {"input": 3.0, "output": 15.0},
        "claude-haiku-4": {"input": 0.25, "output": 1.25},
        # 向后兼容旧模型名
        "claude-3-opus": {"input": 15.0, "output": 75.0},
        "claude-3-sonnet": {"input": 3.0, "output": 15.0},
        "claude-3-haiku": {"input": 0.25, "output": 1.25},
    }

    @property
    def id(self) -> str:
        return "cost-tracker"

    @property
    def async_execution(self) -> bool:
        return True  # 异步执行，不阻塞响应

    async def run(
        self,
        session_id: str,
        session_context: dict[str, Any]
    ) -> None:
        """记录会话成本"""
        session_stats = session_context.get("session_stats", {})
        if not session_stats:
            return

        # 提取 token 统计
        input_tokens = session_stats.get("input_tokens", 0)
        output_tokens = session_stats.get("output_tokens", 0)
        model_used = session_stats.get("model", "unknown")

        # 计算成本
        cost_usd = self._calculate_cost(model_used, input_tokens, output_tokens)

        # 构建成本记录
        cost_record = {
            "session_id": session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "model": model_used,
            "estimated_cost_usd": round(cost_usd, 6),
        }

        # 保存到 ~/.agent-smith/metrics/costs.jsonl
        try:
            await self._save_cost_record(cost_record)
        except Exception as e:
            # 成本跟踪失败不应影响正常流程，只记录错误
            import logging
            logging.getLogger(__name__).error(
                "Failed to save cost record: %s", e, exc_info=True
            )

    def _calculate_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int
    ) -> float:
        """计算成本（美元）"""
        # 标准化模型名（移除版本号等后缀）
        model_base = model.lower()
        for key in self.MODEL_PRICING:
            if key in model_base:
                pricing = self.MODEL_PRICING[key]
                input_cost = (input_tokens / 1_000_000) * pricing["input"]
                output_cost = (output_tokens / 1_000_000) * pricing["output"]
                return input_cost + output_cost

        # 未知模型，使用 Sonnet 定价作为默认
        default_pricing = self.MODEL_PRICING["claude-sonnet-4"]
        input_cost = (input_tokens / 1_000_000) * default_pricing["input"]
        output_cost = (output_tokens / 1_000_000) * default_pricing["output"]
        return input_cost + output_cost

    async def _save_cost_record(self, record: dict[str, Any]) -> None:
        """保存成本记录到 JSONL 文件"""
        data_root = get_data_root()
        metrics_dir = data_root / "metrics"
        metrics_dir.mkdir(parents=True, exist_ok=True)

        cost_file = metrics_dir / "costs.jsonl"

        # 追加写入 JSONL：显式 0600（不受 umask 影响），单次写入保证原子性。
        fd = os.open(
            cost_file,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            _COST_FILE_MODE,
        )
        try:
            os.write(fd, (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        if cost_file.stat().st_mode & 0o777 != _COST_FILE_MODE:
            cost_file.chmod(_COST_FILE_MODE)
