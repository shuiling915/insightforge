"""Context window management with semantic retrieval.

Hybrid strategy:
  1. Always keep system prompt + current task.
  2. Keep the last N rounds intact (guarantees reasoning chain continuity).
  3. For older rounds, retrieve the top-K most semantically relevant rounds
     using character n-gram Jaccard similarity (no external embedding deps).
  4. The rest are condensed into one-line summaries.
  5. If still over budget, truncate long outputs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = 4
N_GRAM = 3


@dataclass
class ContextStats:
    total_tokens: int = 0
    message_count: int = 0
    system_tokens: int = 0
    recent_tokens: int = 0
    retrieved_tokens: int = 0
    summary_tokens: int = 0
    pruned: bool = False
    retrieved_rounds: int = 0


class ContextManager:
    """Manages the agent's message history within a token budget.

    Combines a sliding time window (recent rounds) with semantic retrieval
    from older history so relevant earlier context is not lost.
    """

    def __init__(
        self,
        max_context_tokens: int = 128000,
        reserve_tokens: int = 4096,
        keep_recent_rounds: int = 6,
        retrieve_top_k: int = 4,
        similarity_threshold: float = 0.15,
    ) -> None:
        self.max_context_tokens = max_context_tokens
        self.reserve_tokens = reserve_tokens
        self.keep_recent_rounds = keep_recent_rounds
        self.retrieve_top_k = retrieve_top_k
        self.similarity_threshold = similarity_threshold
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

    # ── public entry point ──────────────────────────────────────────────────

    def manage(self, messages: List[dict]) -> tuple[List[dict], ContextStats]:
        total = self.estimate_tokens(messages)
        self.stats = ContextStats(total_tokens=total, message_count=len(messages))

        if total <= self.budget:
            return messages, self.stats

        logger.info(
            "Context overflow: %d tokens > budget %d, pruning with semantic retrieval...",
            total,
            self.budget,
        )
        self.stats.pruned = True

        if len(messages) < 3:
            return messages, self.stats

        system_msg = messages[0]
        task_msg = messages[1]
        rest = messages[2:]

        rounds = self._group_into_rounds(rest)
        if not rounds:
            return messages, self.stats

        # Split: recent rounds (keep intact) + old rounds (retrieve relevant)
        recent_rounds = rounds[-self.keep_recent_rounds:] if self.keep_recent_rounds > 0 else []
        old_rounds = rounds[:-self.keep_recent_rounds] if self.keep_recent_rounds > 0 else rounds

        # Build the query text from the current task + recent rounds
        query_text = self._build_query_text(task_msg, recent_rounds)

        # Semantically retrieve relevant old rounds
        retrieved = self._retrieve_relevant(old_rounds, query_text)
        retrieved_indices = {r[1] for r in retrieved}

        # Remaining old rounds → one-line summaries
        summaries: List[str] = []
        for idx, rnd in enumerate(old_rounds):
            if idx in retrieved_indices:
                continue
            summary = self._summarize_round(rnd)
            if summary:
                summaries.append(summary)

        # Assemble final message list
        pruned: List[dict] = [system_msg, task_msg]

        if summaries:
            summary_text = (
                "## Summary of previous rounds (condensed)\n\n" + "\n".join(summaries)
            )
            pruned.append({"role": "system", "content": summary_text})
            self.stats.summary_tokens = len(summary_text) // CHARS_PER_TOKEN

        # Insert retrieved old rounds (keep original order)
        retrieved_rounds_sorted = sorted(retrieved, key=lambda r: r[1])
        if retrieved_rounds_sorted:
            block_header = {
                "role": "system",
                "content": "## Relevant earlier context (retrieved)\n",
            }
            pruned.append(block_header)
            for rnd_msgs, _idx in retrieved_rounds_sorted:
                pruned.extend(rnd_msgs)

        # Recent rounds at the end (closest to the model's attention)
        for rnd in recent_rounds:
            pruned.extend(rnd)

        self.stats.message_count = len(pruned)
        self.stats.total_tokens = self.estimate_tokens(pruned)
        self.stats.system_tokens = len(system_msg.get("content", "")) // CHARS_PER_TOKEN
        self.stats.retrieved_rounds = len(retrieved)
        self.stats.retrieved_tokens = sum(
            self.estimate_tokens(r[0]) for r in retrieved
        )
        self.stats.recent_tokens = (
            self.stats.total_tokens
            - self.stats.system_tokens
            - self.stats.summary_tokens
            - self.stats.retrieved_tokens
        )

        logger.info(
            "Pruned: %d -> %d tokens (%d msgs, retrieved %d rounds)",
            total,
            self.stats.total_tokens,
            self.stats.message_count,
            self.stats.retrieved_rounds,
        )

        if self.stats.total_tokens > self.budget:
            pruned = self._truncate_outputs(pruned)
            self.stats.total_tokens = self.estimate_tokens(pruned)

        return pruned, self.stats

    # ── round grouping ──────────────────────────────────────────────────────

    def _group_into_rounds(self, messages: List[dict]) -> List[List[dict]]:
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

    # ── semantic retrieval ──────────────────────────────────────────────────

    def _build_query_text(self, task_msg: dict, recent_rounds: List[List[dict]]) -> str:
        """Build query text from the current task and last couple of rounds."""
        parts = [str(task_msg.get("content", ""))]
        # Include the last 2 recent rounds for richer query context
        for rnd in recent_rounds[-2:]:
            for msg in rnd:
                content = msg.get("content", "")
                if isinstance(content, str):
                    parts.append(content[:500])
        return "\n".join(parts)

    def _retrieve_relevant(
        self,
        old_rounds: List[List[dict]],
        query_text: str,
    ) -> List[Tuple[List[dict], int]]:
        """Return top-K old rounds most similar to the query.

        Each element is (round_messages, original_index).
        """
        if not old_rounds or self.retrieve_top_k <= 0:
            return []

        query_ngrams = self._ngram_set(query_text)
        if not query_ngrams:
            return []

        scored: List[Tuple[float, int]] = []
        for idx, rnd in enumerate(old_rounds):
            round_text = self._round_to_text(rnd)
            round_ngrams = self._ngram_set(round_text)
            if not round_ngrams:
                continue
            sim = self._jaccard(query_ngrams, round_ngrams)
            if sim >= self.similarity_threshold:
                scored.append((sim, idx))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[: self.retrieve_top_k]

        if top:
            logger.debug(
                "Retrieved %d relevant rounds (top sim: %.3f, threshold %.2f)",
                len(top),
                top[0][0],
                self.similarity_threshold,
            )

        return [(old_rounds[idx], idx) for _, idx in top]

    @staticmethod
    def _ngram_set(text: str, n: int = N_GRAM) -> set:
        """Character n-gram set for fuzzy matching."""
        text = text.lower().strip()
        if len(text) < n:
            return {text} if text else set()
        return {text[i : i + n] for i in range(len(text) - n + 1)}

    @staticmethod
    def _jaccard(a: set, b: set) -> float:
        if not a or not b:
            return 0.0
        intersection = len(a & b)
        union = len(a | b)
        return intersection / union if union else 0.0

    def _round_to_text(self, round_msgs: List[dict]) -> str:
        """Concatenate a round's messages into a single text for embedding."""
        parts = []
        for msg in round_msgs:
            content = msg.get("content", "")
            if isinstance(content, str):
                parts.append(content[:1000])
        return "\n".join(parts)

    # ── summarization ───────────────────────────────────────────────────────

    def _summarize_round(self, round_msgs: List[dict]) -> Optional[str]:
        thinking = ""
        for msg in round_msgs:
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                m = re.search(r'"thinking"\s*:\s*"([^"]{0,200})', content)
                if m:
                    thinking = m.group(1)
                    break
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
                line += f" -> {result_summary}"
            return line
        return None

    # ── truncation fallback ─────────────────────────────────────────────────

    def _truncate_outputs(self, messages: List[dict]) -> List[dict]:
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > 2000:
                    msg["content"] = content[:2000] + "\n... (output truncated to fit context)"
        return messages