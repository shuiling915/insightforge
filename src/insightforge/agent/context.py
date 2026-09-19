"""Context window management for the agent loop.

Production feature from the enterprise agent guide: keep the message
history within the model's context window by tracking token counts,
pruning low-value messages, and summarizing old rounds when needed.

Strategy:
  1. Always keep system prompt + last user message.
  2. Keep recent N rounds intact (configurable).
  3. For older rounds, replace the full message pair with a one-line
     summary extracted from the assistant's "thinking" field.
  4. If still over budget, drop images / large outputs first.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)

# Rough token estimate: ~4 chars per token for English text
CHARS_PER_TOKEN = 4


@dataclass
class ContextStats:
    total_tokens: int = 0
    message_count: int = 0
    system_tokens: int = 0
    recent_tokens: int = 0
    summary_tokens: int = 0
    pruned: bool = False


class ContextManager:
    """Manages the agent's message history within a token budget."""

    def __init__(
        self,
        max_context_tokens: int = 128000,
        reserve_tokens: int = 4096,
        keep_recent_rounds: int = 6,
    ) -> None:
        self.max_context_tokens = max_context_tokens
        self.reserve_tokens = reserve_tokens
        self.keep_recent_rounds = keep_recent_rounds
        self.stats = ContextStats()

    @property
    def budget(self) -> int:
        return self.max_context_tokens - self.reserve_tokens

    def estimate_tokens(self, messages: List[dict]) -> int:
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                total += sum(len(str(c.get("text", ""))) for c in content) // CHARS_PER_TOKEN
            else:
                total += len(str(content)) // CHARS_PER_TOKEN
        return total

    def manage(self, messages: List[dict]) -> tuple[List[dict], ContextStats]:
        """Prune messages if they exceed the budget.

        Returns the (possibly modified) message list and stats.
        """
        total = self.estimate_tokens(messages)
        self.stats = ContextStats(total_tokens=total, message_count=len(messages))

        if total <= self.budget:
            return messages, self.stats

        logger.info(
            "Context overflow: %d tokens > budget %d, pruning...",
            total,
            self.budget,
        )
        self.stats.pruned = True

        # Always preserve system message (index 0) and the original task (index 1)
        if len(messages) < 3:
            return messages, self.stats

        system_msg = messages[0]
        task_msg = messages[1]
        rest = messages[2:]

        # Count rounds: each round is (assistant, user) pair
        rounds = self._group_into_rounds(rest)

        # Keep the last N rounds intact
        recent_rounds = rounds[-self.keep_recent_rounds:] if self.keep_recent_rounds > 0 else []
        old_rounds = rounds[:-self.keep_recent_rounds] if self.keep_recent_rounds > 0 else rounds

        # Summarize old rounds into compact one-liners
        summaries = []
        for rnd in old_rounds:
            summary = self._summarize_round(rnd)
            if summary:
                summaries.append(summary)

        # Build pruned message list
        pruned = [system_msg, task_msg]
        if summaries:
            summary_text = (
                "## Summary of previous rounds (condensed)\n\n"
                + "\n".join(summaries)
            )
            pruned.append({"role": "system", "content": summary_text})
            self.stats.summary_tokens = len(summary_text) // CHARS_PER_TOKEN

        for rnd in recent_rounds:
            pruned.extend(rnd)

        self.stats.message_count = len(pruned)
        self.stats.total_tokens = self.estimate_tokens(pruned)
        self.stats.system_tokens = len(system_msg.get("content", "")) // CHARS_PER_TOKEN
        self.stats.recent_tokens = self.stats.total_tokens - self.stats.system_tokens - self.stats.summary_tokens

        logger.info(
            "Pruned context: %d -> %d tokens (%d messages)",
            total,
            self.stats.total_tokens,
            self.stats.message_count,
        )

        # If still over budget, truncate long outputs in recent rounds
        if self.stats.total_tokens > self.budget:
            pruned = self._truncate_outputs(pruned)
            self.stats.total_tokens = self.estimate_tokens(pruned)

        return pruned, self.stats

    def _group_into_rounds(self, messages: List[dict]) -> List[List[dict]]:
        """Group assistant+user message pairs into rounds."""
        rounds = []
        current = []
        for msg in messages:
            current.append(msg)
            if msg.get("role") == "user":
                rounds.append(current)
                current = []
        if current:
            rounds.append(current)
        return rounds

    def _summarize_round(self, round_msgs: List[dict]) -> Optional[str]:
        """Create a one-line summary of a round from the thinking field."""
        thinking = ""
        for msg in round_msgs:
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                # Try to extract thinking from JSON action
                m = re.search(r'"thinking"\s*:\s*"([^"]{0,200})', content)
                if m:
                    thinking = m.group(1)
                    break
                # If not JSON, take first line
                thinking = content.split("\n")[0][:150]
                break

        result_summary = ""
        for msg in round_msgs:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if "Output:" in content:
                    result_summary = content.split("Output:")[-1].strip()[:100]
                break

        if thinking:
            line = f"- {thinking}"
            if result_summary:
                line += f" → {result_summary}"
            return line
        return None

    def _truncate_outputs(self, messages: List[dict]) -> List[dict]:
        """Truncate long code outputs to fit budget."""
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if len(content) > 2000:
                    msg["content"] = content[:2000] + "\n... (output truncated to fit context)"
        return messages