"""Build a Python prelude that injects schema / metric tool functions into the
sandbox so the agent can call ``describe_table()``, ``list_related()``,
``find_metrics()`` and ``describe_metric()`` directly — no regex interception.

The generated code embeds a *copy* of the schema and metric data as plain Python
literals, then defines the four helper functions on top of that data. This keeps
the sandbox fully isolated (no IPC back to the host process) while still giving
the agent rich, structured access to table and metric metadata.
"""

from __future__ import annotations

import json
from typing import Optional

from insightforge.data.metrics import MetricRegistry
from insightforge.data.registry import SchemaRegistry


def build_tool_prelude(
    schema_registry: Optional[SchemaRegistry],
    metric_registry: Optional[MetricRegistry],
) -> str:
    """Return Python source that defines the four tool functions.

    Safe to prepend to any user code block.  The functions are intentionally
    self-contained: they read from module-level constants populated from the
    registries, so they work regardless of how the agent invokes them
    (assignment, keyword args, inside try/except, …).
    """
    schema_payload = _serialize_schema(schema_registry)
    metric_payload = _serialize_metrics(metric_registry)

    return f"""# ── InsightForge built-in tools (auto-injected) ──────────────────────────
import json as _json

_SCHEMA_DATA = _json.loads({repr(schema_payload)})
_METRIC_DATA = _json.loads({repr(metric_payload)})

def describe_table(name):
    \"\"\"Return full column detail for *name*.\"\"\"
    t = _SCHEMA_DATA.get(name)
    if t is None:
        return f"Table '{{name}}' not found."
    lines = [f"### {{name}}"]
    if t.get("description"):
        lines.append(f"Description: {{t['description']}}")
    lines.append(f"Rows: {{t.get('row_count', 0)}}")
    if t.get("primary_key"):
        lines.append(f"Primary Key: {{t['primary_key']}}")
    if t.get("foreign_keys"):
        lines.append("Foreign Keys:")
        for fk in t["foreign_keys"]:
            lines.append(f"  - {{fk}}")
    lines.append("Columns:")
    for c in t.get("columns", []):
        parts = [f"  - {{c['name']}} ({{c['type']}})"]
        if c.get("description"):
            parts.append(f"-- {{c['description']}}")
        if not c.get("nullable", True):
            parts.append("[NOT NULL]")
        lines.append(" ".join(parts))
    return "\\n".join(lines)

def list_related(query, top_k=5):
    \"\"\"Return the *top_k* tables most related to *query*.\"\"\"
    if not _SCHEMA_DATA or not str(query).strip():
        return "No tables available."
    q = str(query).strip().lower()
    q_ngrams = _ngram_set(q)
    q_kw = _extract_keywords(q)
    scored = []
    for name, t in _SCHEMA_DATA.items():
        text_parts = [name, t.get("description", "")]
        text_parts.extend(c["name"] for c in t.get("columns", []))
        text_parts.extend(c.get("description", "") for c in t.get("columns", []))
        text = " ".join(text_parts).lower()
        t_ngrams = _ngram_set(text)
        ngram_sim = _jaccard(q_ngrams, t_ngrams)
        kw_score = 0.0
        if q_kw:
            hits = sum(1 for kw in q_kw if kw in text)
            kw_score = hits / len(q_kw)
        score = ngram_sim * 0.6 + kw_score * 0.4
        if score > 0:
            scored.append((name, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    lines = [f"Tables related to '{{query}}':"]
    for name, score in scored[:top_k]:
        lines.append(f"  - {{name}} (similarity: {{score:.3f}})")
    return "\\n".join(lines) if len(lines) > 1 else f"No tables related to '{{query}}'."

def find_metrics(query, top_k=3):
    \"\"\"Return the *top_k* metrics related to *query*.\"\"\"
    if not _METRIC_DATA or not str(query).strip():
        return "No metrics available."
    q = str(query).strip().lower()
    q_kw = _extract_keywords(q)
    scored = []
    for key, m in _METRIC_DATA.items():
        score = 0.0
        all_aliases = [a.lower() for a in [m.get("name", ""), key] + m.get("aliases", [])]
        if q in all_aliases:
            score = 1.0
        else:
            for alias in all_aliases:
                if alias and (alias in q or q in alias):
                    score = max(score, 0.8)
                    break
        if score < 1.0:
            text = " ".join([m.get("name",""), m.get("description",""),
                             m.get("formula",""), m.get("grain",""),
                             " ".join(m.get("aliases",[])),
                             " ".join(m.get("tables",[]))]).lower()
            if q_kw:
                hits = sum(1 for kw in q_kw if kw in text)
                score = max(score, (hits / len(q_kw)) * 0.5)
        if score > 0:
            scored.append((key, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    lines = [f"Metrics related to '{{query}}':"]
    for key, score in scored[:top_k]:
        m = _METRIC_DATA[key]
        tables = ", ".join(m.get("tables", [])) if m.get("tables") else "—"
        lines.append(f"  - {{m.get('name', key)}} ({{key}}) (score: {{score:.3f}}) [tables: {{tables}}]")
        if m.get("definition"):
            lines.append(f"      def: {{m['definition']}}")
    return "\\n".join(lines) if len(lines) > 1 else f"No metrics related to '{{query}}'."

def describe_metric(key_or_alias):
    \"\"\"Return the full definition of a metric by key or alias.\"\"\"
    m = _METRIC_DATA.get(key_or_alias)
    if m is None:
        for candidate in _METRIC_DATA.values():
            all_names = [candidate.get("name","")] + candidate.get("aliases", [])
            if key_or_alias in all_names:
                m = candidate
                break
    if m is None:
        return f"Metric '{{key_or_alias}}' not found."
    lines = [f"### {{m.get('name', key_or_alias)}} ({{key_or_alias}})"]
    if m.get("description"):
        lines.append(f"Description: {{m['description']}}")
    if m.get("aliases"):
        lines.append(f"Aliases: {{', '.join(m['aliases'])}}")
    if m.get("definition"):
        lines.append(f"SQL Definition: {{m['definition']}}")
    if m.get("formula"):
        lines.append(f"Formula: {{m['formula']}}")
    if m.get("tables"):
        lines.append(f"Tables: {{', '.join(m['tables'])}}")
    if m.get("grain"):
        lines.append(f"Grain: {{m['grain']}}")
    return "\\n".join(lines)

# ── helpers ────────────────────────────────────────────────────────────────
def _ngram_set(text, n=3):
    text = text.lower().strip()
    if len(text) < n:
        return {{text}} if text else set()
    return {{text[i:i+n] for i in range(len(text) - n + 1)}}

def _jaccard(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0

def _extract_keywords(text):
    kws = set()
    for i in range(len(text) - 1):
        kw = text[i:i+2].strip()
        if len(kw) == 2 and not kw.isspace():
            kws.add(kw)
    import re as _re
    for w in _re.findall(r"[a-zA-Z]{{3,}}", text):
        kws.add(w.lower())
    return list(kws)
"""


def _serialize_schema(registry: Optional[SchemaRegistry]) -> str:
    if registry is None:
        return "null"
    data = {}
    for name, t in registry.tables.items():
        data[name] = {
            "description": t.description,
            "row_count": t.row_count,
            "primary_key": t.primary_key,
            "foreign_keys": list(t.foreign_keys),
            "columns": [
                {
                    "name": c.name,
                    "type": c.type,
                    "nullable": c.nullable,
                    "description": c.description,
                }
                for c in t.columns
            ],
        }
    return json.dumps(data, ensure_ascii=False)


def _serialize_metrics(registry: Optional[MetricRegistry]) -> str:
    if registry is None:
        return "null"
    data = {}
    for key, m in registry.metrics.items():
        data[key] = {
            "name": m.name,
            "aliases": list(m.aliases),
            "definition": m.definition,
            "formula": m.formula,
            "tables": list(m.tables),
            "grain": m.grain,
            "description": m.description,
        }
    return json.dumps(data, ensure_ascii=False)