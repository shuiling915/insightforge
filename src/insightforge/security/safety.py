"""Static code safety analysis.

Before any generated code reaches the executor, we run a lightweight
AST check to block obvious dangerous patterns (subprocess, socket,
os.system, file writes outside workspace, etc.).

This is defense-in-depth: the Docker sandbox provides runtime isolation,
but rejecting known-bad patterns early gives faster feedback and reduces
attack surface.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import List, Optional

# Module names that are blocked regardless of context
_BLOCKED_MODULES = {
    "subprocess",
    "socket",
    "ctypes",
    "multiprocessing",
    "os",
    "shutil",
    "sys",
    "importlib",
    "builtins",
    "http",
    "urllib",
    "ftplib",
    "smtplib",
    "telnetlib",
    "signal",
    "resource",
    "fcntl",
    "pty",
    "tty",
    "pickle",
    "marshal",
    "code",
    "codeop",
    "platform",
    "webbrowser",
}

# Attribute chains that are blocked (e.g. "os.system", "shutil.rmtree")
_BLOCKED_ATTRS = {
    "os.system",
    "os.popen",
    "os.execv",
    "os.execve",
    "os.execl",
    "os.fork",
    "os.kill",
    "os.remove",
    "os.rmdir",
    "os.unlink",
    "os.rename",
    "os.replace",
    "os.chmod",
    "os.chown",
    "os.link",
    "os.symlink",
    "os.mknod",
    "shutil.rmtree",
    "shutil.move",
    "shutil.copy",
    "shutil.copy2",
    "shutil.copytree",
}

# Builtins that can execute arbitrary code or access dangerous state
_BLOCKED_BUILTINS = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "input",
    "open",
    "globals",
    "locals",
    "vars",
    "dir",
    "getattr",
    "setattr",
    "delattr",
    "breakpoint",
    "memoryview",
}

# Dangerous dunder attributes that expose internals
_BLOCKED_DUNDERS = {
    "__builtins__",
    "__globals__",
    "__code__",
    "__subclasses__",
    "__mro__",
    "__bases__",
    "__class__",
    "__init__",
    "__import__",
    "__loader__",
    "__spec__",
}

# Regex for shell-like patterns anywhere in the source (including strings)
_SHELL_PATTERN = re.compile(
    r"(?:os\.system|os\.popen|subprocess\.|popen\(|eval\(|exec\(|__import__|"
    r"__builtins__|__globals__|getattr\(\s*__builtins__|globals\(\)\[)"
)

# SQL dangerous keywords that mutate data or schema
_SQL_DANGEROUS = re.compile(
    r"\b(DROP|DELETE\s+FROM|ALTER\s+TABLE|TRUNCATE|ATTACH\s+DATABASE|"
    r"DETACH\s+DATABASE|REINDEX|VACUUM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|"
    r"CREATE\s+TABLE|CREATE\s+INDEX|CREATE\s+VIEW|CREATE\s+TRIGGER)\b",
    re.IGNORECASE,
)


@dataclass
class SafetyReport:
    safe: bool
    violations: List[str]

    @property
    def message(self) -> str:
        if self.safe:
            return "OK"
        return "Code rejected: " + "; ".join(self.violations)


class CodeSafetyChecker:
    def __init__(self) -> None:
        self._blocked_modules = _BLOCKED_MODULES
        self._blocked_attrs = _BLOCKED_ATTRS
        self._blocked_builtins = _BLOCKED_BUILTINS
        self._blocked_dunders = _BLOCKED_DUNDERS

    def check(self, code: str) -> SafetyReport:
        violations: List[str] = []

        # 1. Regex pre-check for shell/escape patterns (catches string-based bypasses)
        shell_hits = _SHELL_PATTERN.findall(code)
        if shell_hits:
            violations.append(f"contains shell-execution pattern: {shell_hits[0]}")

        # 2. SQL dangerous operation detection (scans string literals too)
        if _SQL_DANGEROUS.search(code):
            violations.append("SQL contains data/schema mutation (only SELECT allowed)")

        # 3. AST analysis
        try:
            tree = ast.parse(code)
        except SyntaxError:
            # Let the executor report the syntax error
            return SafetyReport(safe=True, violations=[])

        for node in ast.walk(tree):
            # Block dangerous imports
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in self._blocked_modules:
                        violations.append(f"import of '{alias.name}' is not allowed")
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top = node.module.split(".")[0]
                    if top in self._blocked_modules:
                        violations.append(f"import from '{node.module}' is not allowed")

            # Block dangerous builtin calls
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in self._blocked_builtins:
                    violations.append(f"call to '{func.id}' is not allowed")
                elif isinstance(func, ast.Attribute):
                    full = self._attr_chain(func)
                    if full in self._blocked_attrs:
                        violations.append(f"call to '{full}' is not allowed")
                    # Block method calls on dangerous dunders
                    root = self._attr_root(func)
                    if root in self._blocked_dunders:
                        violations.append(f"access to '{root}' is not allowed")

            # Block attribute access to dangerous dunders
            if isinstance(node, ast.Attribute):
                if node.attr in self._blocked_dunders:
                    violations.append(f"access to dunder '{node.attr}' is not allowed")

            # Block subscript access to __builtins__ / globals via string keys
            if isinstance(node, ast.Subscript):
                if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                    if node.slice.value in self._blocked_dunders or node.slice.value in self._blocked_builtins:
                        violations.append(
                            f"subscript access to '{node.slice.value}' is not allowed"
                        )

        return SafetyReport(safe=len(violations) == 0, violations=violations)

    @staticmethod
    def _attr_chain(node: ast.Attribute) -> str:
        parts: List[str] = []
        current: ast.AST = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        return ".".join(reversed(parts))

    @staticmethod
    def _attr_root(node: ast.Attribute) -> str:
        current: ast.AST = node
        while isinstance(current, ast.Attribute):
            current = current.value
        if isinstance(current, ast.Name):
            return current.id
        return ""


_SQL_SELECT_RE = re.compile(
    r"""(?:execute|read_sql|executescript)\s*\(\s*[\"'](.*?)[\"']""",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class QualityIssue:
    level: str
    message: str


@dataclass
class QualityReport:
    issues: List[QualityIssue]

    @property
    def has_errors(self) -> bool:
        return any(i.level == "error" for i in self.issues)

    @property
    def message(self) -> str:
        if not self.issues:
            return "OK"
        lines = [f"[{i.level}] {i.message}" for i in self.issues]
        return "Code review:\n" + "\n".join(lines)


class CodeQualityChecker:
    def __init__(self, max_result_rows: int = 1000) -> None:
        self.max_result_rows = max_result_rows

    def check(self, code: str) -> QualityReport:
        issues: List[QualityIssue] = []
        try:
            ast.parse(code)
        except SyntaxError as e:
            issues.append(QualityIssue("error", f"Syntax error: {e.msg} (line {e.lineno})"))
            return QualityReport(issues)

        for sql in [m.group(1) for m in _SQL_SELECT_RE.finditer(code)]:
            s = sql.strip()
            if not re.match(r"^\s*SELECT\b", s, re.IGNORECASE):
                continue
            if not re.search(r"\bLIMIT\s+\d+", s, re.IGNORECASE):
                issues.append(QualityIssue("warning", f"SELECT has no LIMIT — add LIMIT {self.max_result_rows}."))
            if re.search(r"\bSELECT\s+\*", s, re.IGNORECASE):
                issues.append(QualityIssue("warning", "SELECT * returns all columns; specify columns you need."))

        if "sqlite3.connect" in code and ".close()" not in code:
            issues.append(QualityIssue("warning", "sqlite3 connection not closed — call conn.close()."))

        return QualityReport(issues)