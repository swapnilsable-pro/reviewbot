# reviewbot/source_context.py
"""Read a changed file from the checked-out tree and build a context block that
gives the LLM the enclosing function/class + imports, not just the diff hunk.

Everything here is best-effort and bounded: if the file isn't on disk (local
runs) the caller falls back to diff-only review, and every block is capped by a
line budget so the free-model context window is never blown.
"""

from __future__ import annotations

import os
import re

# Definition headers across the common languages reviewbot sees. Matching is a
# heuristic, not a parser — bounded by the line cap so a miss is cheap.
# ponytail: regex headers, not an AST. Upgrade to tree-sitter only if mis-detection
# on exotic layouts (multi-line signatures, decorators) becomes a real complaint.
_HEADER_RE = re.compile(
    r"^\s*(?:export\s+|default\s+|public\s+|private\s+|protected\s+|static\s+|"
    r"async\s+)*"
    r"(?:def|class|function|func|fn|interface|struct|impl|module|"
    r"[A-Za-z_][\w<>]*\s+[A-Za-z_]\w*\s*\()"
    r"|^\s*[A-Za-z_]\w*\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>)"
)

_IMPORT_RE = re.compile(
    r"^\s*(?:import\b|from\s+\S+\s+import\b|#include\b|use\s+\S|require\s*\(|"
    r"using\s+\S|package\b)"
)


def read_source(repo_root: str | None, path: str) -> str | None:
    """Return the file's text from the checked-out tree, or None if unavailable.

    Refuses paths that escape repo_root (defensive — `path` comes from the API).
    """
    if not repo_root:
        return None
    root = os.path.realpath(repo_root)
    full = os.path.realpath(os.path.join(root, path))
    if not (full == root or full.startswith(root + os.sep)):
        return None  # path traversal
    try:
        with open(full, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except (OSError, ValueError):
        return None


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def enclosing_context(source: str, changed_lines: set[int], max_lines: int) -> str:
    """A line-numbered context block. Whole file if small; else the enclosing
    def/class around the change; else a fixed window. Changed lines marked '>'."""
    lines = source.splitlines()
    if not lines or not changed_lines:
        return ""

    if len(lines) <= max_lines:
        start, end = 1, len(lines)
    else:
        first = max(min(changed_lines), 1)
        last = min(max(changed_lines), len(lines))
        body_indent = _indent(lines[first - 1])
        # scan up for the nearest header at a lower indent
        header = None
        for i in range(first - 1, 0, -1):
            ln = lines[i - 1]
            if ln.strip() and _indent(ln) < body_indent and _HEADER_RE.match(ln):
                header = i
                break
        if header is None:
            half = max_lines // 2
            start, end = max(first - half, 1), min(last + half, len(lines))
        else:
            start = header
            # extend down through the block (indent > header) until budget hit
            head_indent = _indent(lines[header - 1])
            end = last
            for i in range(last + 1, len(lines) + 1):
                if i - start >= max_lines:
                    break
                ln = lines[i - 1]
                if ln.strip() and _indent(ln) <= head_indent:
                    break
                end = i
        if end - start + 1 > max_lines:
            end = start + max_lines - 1

    width = max(len(str(end)), 3)
    out = []
    for n in range(start, end + 1):
        mark = ">" if n in changed_lines else " "
        out.append(f"{n:>{width}} | {mark} {lines[n - 1]}")
    return "\n".join(out)


_DEFINED_RE = re.compile(
    r"^\s*[+]?\s*(?:export\s+|async\s+)*(?:def|class|function|func|fn)\s+([A-Za-z_]\w*)"
)


def defined_names(added_text: str) -> set[str]:
    """Names of functions/classes defined in the changed lines."""
    return {m.group(1) for line in added_text.splitlines() if (m := _DEFINED_RE.match(line))}


def extract_imports(source: str, max_lines: int = 40) -> str:
    """Collect the file's import/use lines (bounded)."""
    found = [ln for ln in source.splitlines() if _IMPORT_RE.match(ln)]
    return "\n".join(found[:max_lines])
