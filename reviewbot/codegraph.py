"""A pure-Python, daemon-free symbol index over the checked-out tree.

It answers two questions for the reviewer, deterministically and with no new
dependencies and no LLM round-trips:
  1. "What does a function this hunk CALLS look like?"  -> related_definitions
  2. "Who CALLS a function this hunk CHANGED?"          -> affected_callers

# ponytail: name-based resolution. It cannot resolve overloads, dynamic dispatch,
# or same-name collisions across files (it drops on ambiguity rather than guess).
# A true call graph needs an LSP daemon, which the zero-infra constraint forbids.
# Upgrade path: swap the regex scan for tree-sitter `tags.scm` queries if precision
# becomes a measured problem.
"""

from __future__ import annotations

import os
import re

# Directories never worth scanning.
_SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", ".venv",
             "venv", "__pycache__", ".tox", "target"}
_MAX_FILE_BYTES = 400_000
_SOURCE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rb", ".java",
              ".kt", ".php", ".cs", ".c", ".h", ".cpp", ".rs", ".swift", ".scala"}

# Definition headers: capture the defined name. Union across languages.
_DEF_RE = re.compile(
    r"^\s*(?:export\s+|default\s+|public\s+|private\s+|protected\s+|static\s+|async\s+)*"
    r"(?:def|class|function|func|fn|interface|struct)\s+([A-Za-z_]\w*)"
    r"|^\s*([A-Za-z_]\w*)\s*[:=]\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>)"
)
_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_KEYWORDS = {
    "if", "for", "while", "switch", "return", "and", "or", "not", "in", "is",
    "with", "elif", "else", "def", "class", "function", "func", "fn", "print",
    "len", "range", "super", "self", "new", "await", "yield", "assert", "raise",
    "import", "from", "as", "try", "except", "finally", "lambda", "case", "match",
}


class CodeGraph:
    def __init__(self, repo_root: str) -> None:
        self._root = repo_root
        # name -> list of (relpath, lineno, header_line)
        self._defs: dict[str, list[tuple[str, int, str]]] = {}
        self._files: list[str] = []
        self._build_index()

    def _build_index(self) -> None:
        for dirpath, dirnames, filenames in os.walk(self._root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for name in filenames:
                if os.path.splitext(name)[1] not in _SOURCE_EXT:
                    continue
                full = os.path.join(dirpath, name)
                try:
                    if os.path.getsize(full) > _MAX_FILE_BYTES:
                        continue
                    with open(full, encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                except OSError:
                    continue
                rel = os.path.relpath(full, self._root)
                self._files.append(rel)
                for i, line in enumerate(text.splitlines(), 1):
                    m = _DEF_RE.match(line)
                    if m:
                        defname = m.group(1) or m.group(2)
                        self._defs.setdefault(defname, []).append((rel, i, line.strip()))

    def _file_lines(self, rel: str) -> list[str]:
        try:
            with open(os.path.join(self._root, rel), encoding="utf-8", errors="replace") as fh:
                return fh.read().splitlines()
        except OSError:
            return []

    def related_definitions(self, changed_path: str, added_text: str, budget_lines: int) -> str:
        called = {n for n in _CALL_RE.findall(added_text) if n not in _KEYWORDS}
        blocks: list[str] = []
        used = 0
        for name in sorted(called):
            defs = self._defs.get(name)
            if not defs:
                continue
            # Prefer same file, then same top-level dir, then a unique global match.
            same_file = [d for d in defs if d[0] == changed_path]
            same_dir = [d for d in defs if d[0].split(os.sep)[0] == changed_path.split(os.sep)[0]]
            if same_file:
                chosen = same_file[0]
            elif len(defs) == 1:
                chosen = defs[0]
            elif len(same_dir) == 1:
                chosen = same_dir[0]
            else:
                continue  # ambiguous — drop rather than guess
            rel, lineno, _ = chosen
            lines = self._file_lines(rel)
            snippet = lines[lineno - 1 : lineno - 1 + 12]  # signature + short body
            block = f"# {name} — defined at {rel}:{lineno}\n" + "\n".join(snippet)
            cost = block.count("\n") + 2
            if used + cost > budget_lines:
                break
            blocks.append(block)
            used += cost
        return "\n\n".join(blocks)

    def affected_callers(self, changed_path: str, defined_names, budget_lines: int) -> str:
        names = {str(n) for n in defined_names if isinstance(n, str)}
        if not names:
            return ""
        pat = re.compile(r"\b(" + "|".join(re.escape(n) for n in names) + r")\s*\(")
        hits: list[str] = []
        for rel in self._files:
            if rel == changed_path:
                continue
            for i, line in enumerate(self._file_lines(rel), 1):
                if pat.search(line):
                    hits.append(f"{rel}:{i}: {line.strip()}")
                    if len(hits) >= budget_lines:
                        return "\n".join(hits)
        return "\n".join(hits)


def build_codegraph(repo_root: str | None) -> "CodeGraph | None":
    if not repo_root or not os.path.isdir(repo_root):
        return None
    return CodeGraph(repo_root)
