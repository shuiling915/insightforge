"""Schema registry: pre-scan database schemas and provide on-demand detail.

Avoids the agent having to explore sqlite_master at runtime. Provides:
  - Compact table summary (for system prompt)
  - Full table detail (columns, types, descriptions, enum values)
  - Semantic table search (n-gram Jaccard similarity, no external deps)
  - Sample row retrieval
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

N_GRAM = 3
ENUM_DISTINCT_THRESHOLD = 30
SAMPLE_ROWS_DEFAULT = 3


@dataclass
class ColumnInfo:
    name: str
    type: str
    nullable: bool
    description: str = ""
    enum_values: List[str] = field(default_factory=list)


@dataclass
class TableSchema:
    name: str
    db_path: str
    columns: List[ColumnInfo] = field(default_factory=list)
    row_count: int = 0
    primary_key: Optional[str] = None
    foreign_keys: List[str] = field(default_factory=list)
    description: str = ""

    @property
    def column_names(self) -> List[str]:
        return [c.name for c in self.columns]


class SchemaRegistry:
    """Scans SQLite databases in a workspace and caches their schemas."""

    def __init__(
        self,
        workspace: str,
        descriptions: Optional[Dict] = None,
        enum_threshold: int = ENUM_DISTINCT_THRESHOLD,
    ) -> None:
        self.workspace = Path(workspace)
        self.descriptions = descriptions or {}
        self.enum_threshold = enum_threshold
        self.tables: Dict[str, TableSchema] = {}
        self.scan()

    # ── scanning ────────────────────────────────────────────────────────────

    def scan(self) -> None:
        """Scan all .db files in the workspace and cache their schemas."""
        self.tables.clear()
        if not self.workspace.exists():
            logger.warning("Workspace does not exist: %s", self.workspace)
            return

        for db_file in sorted(self.workspace.glob("*.db")):
            try:
                self._scan_database(str(db_file))
            except Exception:
                logger.exception("Failed to scan database %s", db_file)

        logger.info(
            "SchemaRegistry scanned %d tables from %s",
            len(self.tables),
            self.workspace,
        )

    def _scan_database(self, db_path: str) -> None:
        conn = sqlite3.connect(db_path)
        try:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            for (table_name,) in tables:
                schema = self._scan_table(conn, table_name, db_path)
                self.tables[schema.name] = schema
        finally:
            conn.close()

    def _scan_table(
        self, conn: sqlite3.Connection, table_name: str, db_path: str
    ) -> TableSchema:
        columns_info = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        columns: List[ColumnInfo] = []
        primary_key: Optional[str] = None

        for col in columns_info:
            cid, name, ctype, notnull, dflt, pk = col
            if pk:
                primary_key = name
            desc = self._column_description(table_name, name)
            columns.append(
                ColumnInfo(
                    name=name,
                    type=ctype,
                    nullable=not notnull,
                    description=desc,
                )
            )

        # Row count
        try:
            row_count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        except sqlite3.OperationalError:
            row_count = 0

        # Foreign keys
        fk_rows = conn.execute(f"PRAGMA foreign_key_list({table_name})").fetchall()
        foreign_keys = [
            f"{row[3]} -> {row[2]}.{row[4]}" for row in fk_rows
        ]

        # Distinct values for low-cardinality columns
        for col in columns:
            if row_count > 0:
                col.enum_values = self._get_distinct_values(
                    conn, table_name, col.name
                )

        table_desc = self._table_description(table_name)

        return TableSchema(
            name=table_name,
            db_path=db_path,
            columns=columns,
            row_count=row_count,
            primary_key=primary_key,
            foreign_keys=foreign_keys,
            description=table_desc,
        )

    def _get_distinct_values(
        self, conn: sqlite3.Connection, table: str, column: str
    ) -> List[str]:
        try:
            cur = conn.execute(
                f"SELECT DISTINCT {column} FROM {table} LIMIT {self.enum_threshold + 1}"
            )
            values = [str(r[0]) for r in cur.fetchall() if r[0] is not None]
            if len(values) <= self.enum_threshold:
                return values
        except sqlite3.OperationalError:
            pass
        return []

    def _table_description(self, table_name: str) -> str:
        tbl = self.descriptions.get("tables", {}).get(table_name, {})
        if isinstance(tbl, dict):
            return tbl.get("description", "")
        return str(tbl)

    def _column_description(self, table_name: str, column_name: str) -> str:
        tbl = self.descriptions.get("tables", {}).get(table_name, {})
        if isinstance(tbl, dict):
            cols = tbl.get("columns", {})
            if isinstance(cols, dict):
                return cols.get(column_name, "")
        return ""

    # ── public API ──────────────────────────────────────────────────────────

    def get_summary(self) -> str:
        """Compact table summary for the system prompt.

        Format: table_name (row_count): description — col1, col2, col3
        """
        if not self.tables:
            return "(No databases found in workspace)"

        lines = [f"## Available Tables ({len(self.tables)})"]
        for name in sorted(self.tables.keys()):
            t = self.tables[name]
            col_list = ", ".join(t.column_names[:12])
            if len(t.columns) > 12:
                col_list += f", ... (+{len(t.columns) - 12} more)"
            desc = t.description or "(no description)"
            lines.append(f"- {name} ({t.row_count} rows): {desc} — {col_list}")
        return "\n".join(lines)

    def describe_table(self, table_name: str) -> str:
        """Full detail for a single table: columns, types, descriptions, enums, FKs."""
        t = self.tables.get(table_name)
        if t is None:
            return f"Table '{table_name}' not found."

        lines = [f"### {table_name}"]
        if t.description:
            lines.append(f"Description: {t.description}")
        lines.append(f"Rows: {t.row_count}")
        if t.primary_key:
            lines.append(f"Primary Key: {t.primary_key}")
        if t.foreign_keys:
            lines.append("Foreign Keys:")
            for fk in t.foreign_keys:
                lines.append(f"  - {fk}")
        lines.append("Columns:")
        for c in t.columns:
            parts = [f"  - {c.name} ({c.type})"]
            if c.description:
                parts.append(f"-- {c.description}")
            if c.enum_values:
                parts.append(f"[values: {', '.join(c.enum_values[:15])}]")
            if not c.nullable:
                parts.append("[NOT NULL]")
            lines.append(" ".join(parts))
        return "\n".join(lines)

    def list_related(self, query: str, top_k: int = 5) -> List[Tuple[str, float]]:
        """Find tables most semantically related to the query.

        Uses a hybrid similarity:
          - Character n-gram Jaccard (works well for English table names)
          - Keyword substring overlap (works well for Chinese queries)
        """
        if not self.tables:
            return []

        query = query.strip().lower()
        if not query:
            return []

        query_ngrams = self._ngram_set(query)
        # Extract 2-char keywords from the query (good for Chinese)
        query_keywords = self._extract_keywords(query)

        scored = []
        for name, t in self.tables.items():
            text_parts = [t.name, t.description]
            text_parts.extend(c.name for c in t.columns)
            text_parts.extend(c.description for c in t.columns if c.description)
            table_text = " ".join(text_parts).lower()

            # 1. n-gram Jaccard
            table_ngrams = self._ngram_set(table_text)
            ngram_sim = self._jaccard(query_ngrams, table_ngrams)

            # 2. Keyword overlap (for Chinese queries)
            keyword_score = 0.0
            if query_keywords:
                hits = sum(1 for kw in query_keywords if kw in table_text)
                keyword_score = hits / len(query_keywords)

            # Combined score: weight n-gram higher but include keyword signal
            score = ngram_sim * 0.6 + keyword_score * 0.4
            if score > 0:
                scored.append((name, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def get_sample(self, table_name: str, n: int = SAMPLE_ROWS_DEFAULT) -> str:
        """Return first N rows of a table as a readable string."""
        t = self.tables.get(table_name)
        if t is None:
            return f"Table '{table_name}' not found."

        conn = sqlite3.connect(t.db_path)
        try:
            rows = conn.execute(f"SELECT * FROM {table_name} LIMIT {n}").fetchall()
            col_names = [c.name for c in t.columns]
            header = " | ".join(col_names)
            sep = "-+-".join("-" * max(8, len(c)) for c in col_names)
            lines = [header, sep]
            for row in rows:
                lines.append(" | ".join(str(v)[:20] for v in row))
            return "\n".join(lines)
        except sqlite3.OperationalError as e:
            return f"Error reading {table_name}: {e}"
        finally:
            conn.close()

    @staticmethod
    def _ngram_set(text: str, n: int = N_GRAM) -> set:
        text = text.lower().strip()
        if len(text) < n:
            return {text} if text else set()
        return {text[i : i + n] for i in range(len(text) - n + 1)}

    @staticmethod
    def _extract_keywords(text: str) -> List[str]:
        """Extract 2-character substrings as keywords for Chinese matching.

        For English words, also keep the whole word.
        """
        keywords = set()
        # 2-char substrings (works for Chinese where 2 chars = a word)
        for i in range(len(text) - 1):
            kw = text[i : i + 2].strip()
            if len(kw) == 2 and not kw.isspace():
                keywords.add(kw)
        # Also include English words (split by non-alphanumeric)
        import re as _re

        for word in _re.findall(r"[a-zA-Z]{3,}", text):
            keywords.add(word.lower())
        return list(keywords)

    @staticmethod
    def _jaccard(a: set, b: set) -> float:
        if not a or not b:
            return 0.0
        intersection = len(a & b)
        union = len(a | b)
        return intersection / union if union else 0.0