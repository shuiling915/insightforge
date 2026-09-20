"""Metric registry: business metric definitions with aliases and formulas.

Provides a semantic layer on top of the schema registry so the agent can map
user queries (e.g. "销售额", "GMV") to canonical metric definitions, their
computing formulas, and the underlying tables. This avoids LLM-driven metric
drift and reduces table-lookup errors.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

N_GRAM = 3


@dataclass
class Metric:
    """A single business metric definition."""

    key: str
    name: str
    aliases: List[str] = field(default_factory=list)
    definition: str = ""
    formula: str = ""
    tables: List[str] = field(default_factory=list)
    grain: str = ""
    description: str = ""

    @property
    def searchable_text(self) -> str:
        parts = [self.key, self.name, self.description, self.formula, self.grain]
        parts.extend(self.aliases)
        parts.extend(self.tables)
        return " ".join(parts).lower()


class MetricRegistry:
    """Loads metric definitions from YAML and provides alias-based lookup."""

    def __init__(self, metrics_data: Optional[Dict] = None) -> None:
        self.metrics: Dict[str, Metric] = {}
        if metrics_data:
            self.load(metrics_data)

    def load(self, data: Dict) -> None:
        """Load metrics from a parsed YAML dict (keyed by 'metrics')."""
        raw = data.get("metrics", {}) if isinstance(data, dict) else {}
        for key, info in raw.items():
            if not isinstance(info, dict):
                continue
            aliases = info.get("aliases", [])
            if isinstance(aliases, str):
                aliases = [a.strip() for a in aliases.split(",") if a.strip()]
            tables = info.get("tables", [])
            if isinstance(tables, str):
                tables = [t.strip() for t in tables.split(",") if t.strip()]
            self.metrics[key] = Metric(
                key=key,
                name=info.get("name", key),
                aliases=aliases,
                definition=info.get("definition", ""),
                formula=info.get("formula", ""),
                tables=tables,
                grain=info.get("grain", ""),
                description=info.get("description", ""),
            )
        logger.info("MetricRegistry loaded %d metrics", len(self.metrics))

    # ── public API ──────────────────────────────────────────────────────────

    def get_summary(self) -> str:
        """Compact metric summary for the system prompt."""
        if not self.metrics:
            return ""
        lines = [f"## Available Metrics ({len(self.metrics)})"]
        for key in sorted(self.metrics.keys()):
            m = self.metrics[key]
            alias_str = ", ".join(m.aliases[:6]) if m.aliases else "—"
            table_str = ", ".join(m.tables) if m.tables else "—"
            lines.append(
                f"- {m.name} ({key}): {m.description or m.formula or '(no description)'} "
                f"| aliases: {alias_str} | tables: {table_str}"
            )
        return "\n".join(lines)

    def find_metrics(self, query: str, top_k: int = 3) -> List[Tuple[Metric, float]]:
        """Find metrics related to the query using alias + keyword matching.

        Scoring:
          - Exact alias match → 1.0
          - Alias substring match → 0.8
          - Keyword overlap (2-char n-grams) → 0.0~0.5
        """
        if not self.metrics or not query.strip():
            return []

        q = query.strip().lower()
        q_keywords = self._extract_keywords(q)

        scored: List[Tuple[Metric, float]] = []
        for m in self.metrics.values():
            score = 0.0

            # 1. Exact alias match (highest priority)
            all_aliases = [a.lower() for a in [m.name, m.key] + m.aliases]
            if q in all_aliases:
                score = 1.0
            else:
                # 2. Alias substring match
                for alias in all_aliases:
                    if alias and (alias in q or q in alias):
                        score = max(score, 0.8)
                        break

            # 3. Keyword overlap (for multi-word or partial queries)
            if score < 1.0:
                text = m.searchable_text
                if q_keywords:
                    hits = sum(1 for kw in q_keywords if kw in text)
                    overlap = hits / len(q_keywords)
                    score = max(score, overlap * 0.5)

            if score > 0:
                scored.append((m, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def get_metric(self, key: str) -> Optional[Metric]:
        return self.metrics.get(key)

    def validate(self, schema_registry) -> List[str]:
        """Check every metric against the live schema and return warnings.

        Validates that:
          * every table listed in ``tables`` exists in the scanned schema
          * column identifiers referenced in the SQL ``definition`` actually
            exist on the referenced tables (best-effort, table-qualified only)

        Returns a list of human-readable warning strings (empty if all good).
        """
        warnings: List[str] = []
        if not self.metrics or schema_registry is None:
            return warnings

        available_tables = set(schema_registry.tables.keys())

        for key, m in self.metrics.items():
            # 1. referenced tables must exist
            for tbl in m.tables:
                if tbl not in available_tables:
                    warnings.append(
                        f"metric '{key}' references table '{tbl}' which does "
                        f"not exist in any scanned database"
                    )

            # 2. table-qualified columns in the SQL definition must exist
            if m.definition:
                for tbl in m.tables:
                    schema = schema_registry.tables.get(tbl)
                    if schema is None:
                        continue
                    col_names = set(schema.column_names)
                    # match patterns like "tbl.column" or "alias.column"
                    for match in re.finditer(
                        r"\b" + re.escape(tbl) + r"\.(\w+)", m.definition
                    ):
                        col = match.group(1)
                        if col not in col_names and col.upper() not in (
                            "ID",
                        ):
                            warnings.append(
                                f"metric '{key}' references "
                                f"'{tbl}.{col}' but column '{col}' does not "
                                f"exist on table '{tbl}'"
                            )
        return warnings

    def describe_metric(self, key_or_alias: str) -> str:
        """Return a detailed description of a metric by key or alias."""
        m = self.metrics.get(key_or_alias)
        if m is None:
            for candidate in self.metrics.values():
                all_names = [candidate.name, candidate.key] + candidate.aliases
                if key_or_alias in all_names:
                    m = candidate
                    break
        if m is None:
            return f"Metric '{key_or_alias}' not found."

        lines = [f"### {m.name} ({m.key})"]
        if m.description:
            lines.append(f"Description: {m.description}")
        if m.aliases:
            lines.append(f"Aliases: {', '.join(m.aliases)}")
        if m.definition:
            lines.append(f"SQL Definition: {m.definition}")
        if m.formula:
            lines.append(f"Formula: {m.formula}")
        if m.tables:
            lines.append(f"Tables: {', '.join(m.tables)}")
        if m.grain:
            lines.append(f"Grain: {m.grain}")
        return "\n".join(lines)

    # ── helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_keywords(text: str) -> List[str]:
        keywords = set()
        for i in range(len(text) - 1):
            kw = text[i : i + 2].strip()
            if len(kw) == 2 and not kw.isspace():
                keywords.add(kw)
        for word in re.findall(r"[a-zA-Z]{3,}", text):
            keywords.add(word.lower())
        return list(keywords)