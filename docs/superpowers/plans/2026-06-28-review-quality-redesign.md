# ReviewBot Review-Quality Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ReviewBot's reviews accurate (not vague/wrong) by feeding the LLM real code context — the enclosing scope, imports, callee definitions, and caller sites read off the checked-out tree — plus prompt discipline and a grounded verification pass.

**Architecture:** Keep the existing `fetch → parse → review → post` pipeline. Add a `source_context.py` module (read file from disk, widen each hunk to its enclosing function/class, extract imports) and a `codegraph.py` module (pure-Python symbol index: resolve callee definitions and find caller sites). Thread PR intent and richer context into the prompt; enforce evidence/confidence/category at validation time; add one optional verify pass; polish output (committable suggestions, dedup, honest truncation).

**Tech Stack:** Python 3.11+, pydantic v2, httpx, PyGithub, unidiff, typer, pytest + pytest-mock. **No new runtime dependencies are added by any phase** (the design doc's tree-sitter is a documented optional upgrade, not required for v1).

## Global Constraints

- **Zero infra:** runs as a pip-installable CLI inside a GitHub Actions runner. No server, no persistent DB, no daemon. (verbatim from spec)
- **Free LLM default:** `google/gemma-4-31b-it:free` via OpenRouter — rate-limited, finite context window. Every context block must respect a token/line budget. Users may point `model:` at a paid model.
- **Zero setup:** one workflow file + one secret (`OPENROUTER_API_KEY`). Any new config or file (e.g. house rules) is **optional** with sane defaults.
- **The checked-out tree is the asset:** `actions/checkout` places the full source at `head_sha` on disk at `$GITHUB_WORKSPACE`. Read from it; never assume network for source.
- **Graceful fallback:** when the file is not on disk (local `--dry-run` against a remote PR), every disk-reading step must fall back to the current diff-only behavior, never crash.
- **No new deps:** prefer stdlib + already-installed libs. Mark any deliberate simplification ceiling with a `# ponytail:` comment.
- **Backward-compatible signatures:** existing tests construct `Finding(...)`, `FileHunk(...)`, `build_file_hunk(path, patch)`, and `LLMReviewer(api_key, model)` positionally/minimally. New parameters are keyword args with defaults that preserve current behavior; the runner opts into new behavior.
- **Commits:** Conventional Commits (`type(scope): description`).

## File Structure

| File | Responsibility | Phases |
|---|---|---|
| `reviewbot/source_context.py` (new) | Read source from disk; widen a hunk to its enclosing scope; extract imports. Pure functions. | 1 |
| `reviewbot/codegraph.py` (new) | Pure-Python symbol index over the checked-out tree: resolve callee definitions, find caller sites. | 3 |
| `reviewbot/models.py` | `FileHunk` + `Finding` gain context/evidence/confidence/start_line fields; `ReviewResult` gains dedup. | 1,2,3,5 |
| `reviewbot/parser.py` | `build_file_hunk` accepts `repo_root`, populates context fields; fix truncation to count added lines. | 1,5 |
| `reviewbot/reviewer.py` | Prompt blocks (scope/imports/intent/defs/callers), schema (evidence/confidence), validation filtering, structured outputs, verify pass. | 1,2,3,4,5 |
| `reviewbot/fetcher.py` | `ChangedFile`/`PRData` gain `body`; `head_sha` threaded as `commit_id`. | 2,5 |
| `reviewbot/runner.py` | Resolve `repo_root`; build `CodeGraph`; thread intent + context + commit_id; dedup; read house rules. | 1,2,3,5 |
| `reviewbot/poster.py` | Off-diff quarantine; `commit_id`; multi-line suggestion comments. | 2,5 |
| `reviewbot/config.py` | `ReviewSettings` gains `min_confidence`, `require_evidence`, `verify`, `context`; read optional house rules. | 2,4,5 |
| `tests/test_source_context.py` (new), `tests/test_codegraph.py` (new) | Unit tests for the two new modules. | 1,3 |
| existing tests | Updated where signatures/behavior change. | all |

## Interfaces (names + types used across tasks)

```python
# source_context.py
def read_source(repo_root: str | None, path: str) -> str | None: ...
def enclosing_context(source: str, changed_lines: set[int], max_lines: int) -> str: ...
def extract_imports(source: str, max_lines: int = 40) -> str: ...

# codegraph.py
class CodeGraph:
    def __init__(self, repo_root: str) -> None: ...
    def related_definitions(self, changed_path: str, added_text: str, budget_lines: int) -> str: ...
    def affected_callers(self, changed_path: str, defined_names: set[int] | set[str], budget_lines: int) -> str: ...
def build_codegraph(repo_root: str | None) -> "CodeGraph | None": ...  # None when repo_root missing

# models.py additions (all optional, defaults preserve behavior)
FileHunk.enclosing_context: str = ""
FileHunk.imports: str = ""
FileHunk.related_definitions: str = ""
FileHunk.affected_callers: str = ""
Finding.evidence: str = ""
Finding.confidence: float = 1.0
Finding.start_line: int | None = None

# reviewer.py signatures
def build_system_prompt(categories: list[str], house_rules: str = "") -> str: ...
def build_user_prompt(hunk: FileHunk, intent: str = "") -> str: ...
class LLMReviewer:
    def __init__(self, api_key, model, categories=None, *, intent="", house_rules="",
                 min_confidence=0.0, require_evidence=False, verify=False,
                 timeout=90.0, max_json_retries=2, max_http_retries=3): ...

# parser.py
def build_file_hunk(path, patch, max_lines=400, is_new_file=False, repo_root=None): ...
```

---

## Phase 1 — Read the file on disk; widen each hunk to its enclosing scope

### Task 1: `source_context.py` — read source, widen to enclosing scope, extract imports

**Files:**
- Create: `reviewbot/source_context.py`
- Test: `tests/test_source_context.py`

**Interfaces:**
- Produces: `read_source(repo_root, path) -> str | None`, `enclosing_context(source, changed_lines, max_lines) -> str`, `extract_imports(source, max_lines=40) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_source_context.py
"""Tests for reviewbot.source_context — reading and widening on-disk source."""

from reviewbot.source_context import enclosing_context, extract_imports, read_source

SMALL_FILE = """import os
from app.db import get_user

def login(session, uid):
    user = get_user(session, uid)
    return user.email
""".rstrip("\n")

BIG_FILE = "\n".join(
    ["import os", ""]
    + [f"def fn_{i}():\n    x = {i}\n    return x\n" for i in range(60)]
)


class TestReadSource:
    def test_reads_existing_file(self, tmp_path):
        (tmp_path / "a.py").write_text("print(1)\n")
        assert read_source(str(tmp_path), "a.py") == "print(1)\n"

    def test_missing_file_returns_none(self, tmp_path):
        assert read_source(str(tmp_path), "nope.py") is None

    def test_missing_root_returns_none(self):
        assert read_source(None, "a.py") is None

    def test_path_traversal_refused(self, tmp_path):
        assert read_source(str(tmp_path), "../../etc/passwd") is None


class TestEnclosingContext:
    def test_small_file_returns_whole_file_numbered(self):
        ctx = enclosing_context(SMALL_FILE, {5}, max_lines=400)
        assert "  4 | def login(session, uid):" in ctx
        assert "  5 | >     user = get_user(session, uid)" in ctx  # changed line marked
        assert "  6 |       return user.email" in ctx

    def test_large_file_widens_to_enclosing_def_only(self):
        # change is inside fn_30's body; we should see its def, not fn_0 or fn_59
        lines = BIG_FILE.split("\n")
        target = next(i for i, l in enumerate(lines, 1) if l == "def fn_30():")
        ctx = enclosing_context(BIG_FILE, {target + 1}, max_lines=20)
        assert "def fn_30():" in ctx
        assert "def fn_0():" not in ctx
        assert "def fn_59():" not in ctx

    def test_no_header_falls_back_to_window(self):
        src = "\n".join(f"x{i} = {i}" for i in range(100))
        ctx = enclosing_context(src, {50}, max_lines=10)
        assert "x50 = 50" in ctx
        assert len(ctx.splitlines()) <= 12


class TestExtractImports:
    def test_python_imports(self):
        out = extract_imports(SMALL_FILE)
        assert "import os" in out
        assert "from app.db import get_user" in out
        assert "def login" not in out

    def test_no_imports_returns_empty(self):
        assert extract_imports("def f():\n    return 1\n") == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_source_context.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'reviewbot.source_context'`

- [ ] **Step 3: Write the implementation**

```python
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

    width = len(str(end))
    out = []
    for n in range(start, end + 1):
        mark = ">" if n in changed_lines else " "
        out.append(f"{n:>{width}} | {mark} {lines[n - 1]}")
    return "\n".join(out)


def extract_imports(source: str, max_lines: int = 40) -> str:
    """Collect the file's import/use lines (bounded)."""
    found = [ln for ln in source.splitlines() if _IMPORT_RE.match(ln)]
    return "\n".join(found[:max_lines])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_source_context.py -v`
Expected: PASS (all 9 tests)

- [ ] **Step 5: Commit**

```bash
git add reviewbot/source_context.py tests/test_source_context.py
git commit -m "feat(context): read on-disk source, widen hunk to enclosing scope"
```

---

### Task 2: Wire enclosing scope + imports into the hunk and the prompt

**Files:**
- Modify: `reviewbot/models.py` (FileHunk fields)
- Modify: `reviewbot/parser.py` (`build_file_hunk` populates context)
- Modify: `reviewbot/runner.py` (resolve `repo_root`, pass it down)
- Modify: `reviewbot/reviewer.py` (`build_user_prompt` emits blocks + rule)
- Test: `tests/test_parser.py`, `tests/test_reviewer.py`

**Interfaces:**
- Consumes: `source_context.read_source`, `enclosing_context`, `extract_imports`.
- Produces: `FileHunk.enclosing_context`, `FileHunk.imports`; `build_file_hunk(..., repo_root=None)`; `build_user_prompt(hunk, intent="")`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_parser.py inside a new class
class TestEnclosingContext:
    def test_repo_root_populates_enclosing_scope(self, tmp_path):
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "auth.py").write_text(
            "import os\n\ndef login(user):\n    return user.email\n"
        )
        patch = "@@ -3,1 +3,2 @@\n def login(user):\n+    log(user)\n     return user.email\n"
        hunk = build_file_hunk("app/auth.py", patch, repo_root=str(tmp_path))
        assert "def login(user):" in hunk.enclosing_context
        assert "import os" in hunk.imports

    def test_missing_file_falls_back_to_empty_context(self):
        patch = "@@ -1,1 +1,2 @@\n a = 1\n+b = 2\n"
        hunk = build_file_hunk("ghost.py", patch, repo_root="/nonexistent")
        assert hunk is not None
        assert hunk.enclosing_context == ""  # graceful fallback, no crash
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_parser.py::TestEnclosingContext -v`
Expected: FAIL — `build_file_hunk() got an unexpected keyword argument 'repo_root'`

- [ ] **Step 3: Implement**

In `reviewbot/models.py`, add to `FileHunk` (after `added_line_count`):

```python
    enclosing_context: str = Field(
        default="", description="Enclosing function/class + file context, for reference only"
    )
    imports: str = Field(default="", description="The file's import/use lines")
    related_definitions: str = Field(default="")  # populated in Phase 3
    affected_callers: str = Field(default="")      # populated in Phase 3
```

In `reviewbot/parser.py`, change the signature and add population at the end:

```python
from reviewbot.source_context import enclosing_context, extract_imports, read_source

def build_file_hunk(
    path: str,
    patch: str | None,
    max_lines: int = 400,
    is_new_file: bool = False,
    repo_root: str | None = None,
) -> FileHunk | None:
    # ... existing body unchanged until the final return ...
    enclosing = ""
    imports = ""
    source = read_source(repo_root, path)
    if source is not None:
        # `commentable` already holds the new-file line numbers we care about
        enclosing = enclosing_context(source, commentable, max_lines)
        imports = extract_imports(source)

    return FileHunk(
        path=path,
        annotated_diff="\n".join(annotated_lines),
        commentable_lines=commentable,
        is_new_file=is_new_file,
        is_truncated=truncated,
        added_line_count=added_count,
        enclosing_context=enclosing,
        imports=imports,
    )
```

In `reviewbot/runner.py`, resolve the root once in `run()` and thread it into `_select_hunks`:

```python
import os
# ... in run(), after resolving repo/pr, before _select_hunks:
repo_root = os.environ.get("GITHUB_WORKSPACE") or os.getcwd()
hunks, skipped_files = self._select_hunks(pr_data.files, repo_root)
```

Update `_select_hunks` signature and the `build_file_hunk` call:

```python
def _select_hunks(self, files, repo_root):
    # ...
            hunk = build_file_hunk(
                changed.path,
                changed.patch,
                max_lines=self.config.review.max_lines_per_file,
                is_new_file=changed.status == "added",
                repo_root=repo_root,
            )
```

In `reviewbot/reviewer.py`, rewrite `build_user_prompt`:

```python
def build_user_prompt(hunk: FileHunk, intent: str = "") -> str:
    ext = hunk.path.rsplit(".", 1)[-1].lower() if "." in hunk.path else ""
    language = LANGUAGE_HINTS.get(ext, "")
    parts = [f"File: {hunk.path}"]
    if language:
        parts.append(f"Language: {language}")
    if hunk.is_new_file:
        parts.append("This is a NEW file.")
    if hunk.is_truncated:
        parts.append("Note: the diff was truncated; review only what is shown.")
    if intent:
        parts.append(f"\nChange intent (PR title/description):\n{intent}")
    if hunk.imports:
        parts.append(f"\nImports in this file:\n{hunk.imports}")
    if hunk.enclosing_context:
        parts.append(
            "\nEnclosing scope (unchanged context for reference — DO NOT review "
            "these lines; '>' marks the changed lines):\n" + hunk.enclosing_context
        )
    parts.append(
        "\nDiff (added lines marked +, line numbers refer to the new file):\n\n"
        + hunk.annotated_diff
    )
    parts.append(
        "\nOnly report issues on the changed (+) lines. Missing-import / "
        "undefined-name findings are out of scope unless the import line itself "
        "is in the diff. Return the JSON array of findings now."
    )
    return "\n".join(parts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_parser.py tests/test_reviewer.py -v`
Expected: PASS (existing reviewer tests still pass — `build_user_prompt`'s new params default to empty; `make_hunk` has no enclosing_context so those blocks are omitted).

- [ ] **Step 5: Commit**

```bash
git add reviewbot/models.py reviewbot/parser.py reviewbot/runner.py reviewbot/reviewer.py tests/test_parser.py
git commit -m "feat(context): give the model enclosing scope + imports per file"
```

---

## Phase 2 — Free prompt-only grounding: intent, evidence, scope discipline, off-diff suppression

### Task 3: Add intent + evidence/confidence schema to the prompt and model

**Files:**
- Modify: `reviewbot/models.py` (`Finding` gains `evidence`, `confidence`)
- Modify: `reviewbot/fetcher.py` (`ChangedFile`/`PRData` gain `body`)
- Modify: `reviewbot/runner.py` (thread title+body as `intent` into the reviewer)
- Modify: `reviewbot/reviewer.py` (`build_system_prompt` schema + rules + few-shot; `LLMReviewer.__init__` gains `intent`; `review_file` passes it)
- Test: `tests/test_reviewer.py`

**Interfaces:**
- Produces: `Finding.evidence: str = ""`, `Finding.confidence: float = 1.0`; `LLMReviewer(..., intent="")`; `build_system_prompt(categories, house_rules="")`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_reviewer.py
from reviewbot.reviewer import build_system_prompt, build_user_prompt

class TestPromptGrounding:
    def test_system_prompt_requires_evidence_and_confidence(self):
        p = build_system_prompt(["bugs"])
        assert "evidence" in p
        assert "confidence" in p
        assert "partial" in p.lower()  # the "diff is a partial view" rule

    def test_user_prompt_includes_intent(self):
        hunk = make_hunk()
        p = build_user_prompt(hunk, intent="Fix login crash")
        assert "Fix login crash" in p

    def test_finding_accepts_evidence_and_confidence(self):
        from reviewbot.models import Finding
        f = Finding(line=1, severity="bug", category="bugs", message="x",
                    evidence="user.email", confidence=0.9)
        assert f.evidence == "user.email"
        assert f.confidence == 0.9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_reviewer.py::TestPromptGrounding -v`
Expected: FAIL — `ImportError: cannot import name 'build_system_prompt'` is already importable, so failure is on `assert "evidence" in p`.

- [ ] **Step 3: Implement**

`reviewbot/models.py` — add to `Finding` (after `suggestion`):

```python
    evidence: str = Field(default="", description="Verbatim quote of the added line proving the issue")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
```

`reviewbot/fetcher.py` — add `body` to both models and populate it:

```python
class ChangedFile(BaseModel):
    # ... existing fields ...
    # (no body on a file)

class PRData(BaseModel):
    repo_full_name: str
    number: int
    title: str = ""
    body: str = ""
    head_sha: str = ""
    files: list[ChangedFile] = Field(default_factory=list)

# in fetch_data():
        return PRData(
            repo_full_name=pull.base.repo.full_name,
            number=pull.number,
            title=pull.title or "",
            body=(pull.body or "")[:2000],  # ponytail: cap intent to keep tokens bounded
            head_sha=pull.head.sha,
            files=files,
        )
```

`reviewbot/reviewer.py` — rewrite `build_system_prompt` (add evidence/confidence to schema, the partial-diff rule, and a rejected few-shot) and add `intent` to `LLMReviewer`:

```python
def build_system_prompt(categories: list[str], house_rules: str = "") -> str:
    enabled = [c for c in categories if c in CATEGORY_DESCRIPTIONS] or ["bugs"]
    category_lines = "\n".join(f"- {n}: {CATEGORY_DESCRIPTIONS[n]}" for n in enabled)
    rules_block = f"\nProject-specific rules:\n{house_rules}\n" if house_rules else ""
    return f"""You are ReviewBot, an expert code reviewer. Report specific, provable findings on the changed (+) lines of a diff.

Report findings ONLY in these categories:
{category_lines}

Severity:
- "bug": will or very likely will cause incorrect behavior, a crash, or a vulnerability
- "warning": a risky pattern that should be fixed but may not break immediately
- "suggestion": an optional improvement
{rules_block}
Rules:
1. The diff is a PARTIAL view. Do NOT flag missing null-checks, validation, or error handling unless the shown code uses the value unguarded AND no guard is visible in the enclosing scope. Assume a called function may already validate/guard unless its definition is shown and proves otherwise.
2. Every finding MUST quote, in "evidence", the exact added line it refers to. If you cannot quote a concrete added line that proves the issue, DO NOT report it.
3. Set "confidence" in [0,1]: how sure you are this is a real defect a reviewer would act on.
4. Be specific: name the variable/function and the exact failing input/state.
5. If the code is fine, return [].  At most {MAX_FINDINGS_PER_FILE} findings.

Respond with ONLY a JSON array (no prose, no fences). Each element:
{{"line": <int>, "severity": "bug"|"warning"|"suggestion", "category": "<category>", "message": "<what is wrong and the input that triggers it>", "evidence": "<verbatim quote of the added line>", "confidence": <0..1>, "suggestion": "<how to fix, optional>"}}

Examples:
GOOD: {{"line": 42, "severity": "bug", "category": "bugs", "message": "total is used before assignment when items is empty", "evidence": "return total / len(items)", "confidence": 0.9}}
REJECTED (do not produce): a finding like "consider adding validation" with no quotable line — there is nothing to anchor it to."""
```

In `LLMReviewer.__init__`, add keyword-only params and store them:

```python
    def __init__(self, api_key, model, categories=None, *, intent="", house_rules="",
                 min_confidence=0.0, require_evidence=False, verify=False,
                 timeout=90.0, max_json_retries=2, max_http_retries=3):
        self.model = model
        self.categories = categories or list(CATEGORY_DESCRIPTIONS)
        self.intent = intent
        self.house_rules = house_rules
        self.min_confidence = min_confidence
        self.require_evidence = require_evidence
        self.verify = verify
        self.max_json_retries = max_json_retries
        self.max_http_retries = max_http_retries
        self._client = httpx.Client(timeout=timeout, headers={...})  # unchanged headers
```

In `review_file`, build messages with intent + house_rules:

```python
        messages = [
            {"role": "system", "content": build_system_prompt(self.categories, self.house_rules)},
            {"role": "user", "content": build_user_prompt(hunk, self.intent)},
        ]
```

In `reviewbot/runner.py` `_review_all`, pass intent (store `pr_data` intent on the runner in `run()` first):

```python
# in run(): self._intent = "\n".join(p for p in [pr_data.title, pr_data.body] if p)
# in _review_all():
        reviewer = LLMReviewer(
            api_key=self._openrouter_api_key,
            model=self.config.model,
            categories=self.config.review.categories,
            intent=self._intent,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_reviewer.py tests/test_runner.py -v`
Expected: PASS. (Existing `VALID_FINDINGS` still validates — `evidence`/`confidence` are optional with defaults.)

- [ ] **Step 5: Commit**

```bash
git add reviewbot/models.py reviewbot/fetcher.py reviewbot/runner.py reviewbot/reviewer.py tests/test_reviewer.py
git commit -m "feat(prompt): pass PR intent; require evidence + confidence in findings"
```

---

### Task 4: Enforce evidence / category / confidence at validation time

**Files:**
- Modify: `reviewbot/config.py` (`ReviewSettings` gains `min_confidence`, `require_evidence`)
- Modify: `reviewbot/reviewer.py` (`_validate_findings` drops ungrounded findings; runner passes config values)
- Modify: `reviewbot/runner.py` (pass `min_confidence`, `require_evidence` to `LLMReviewer`)
- Test: `tests/test_reviewer.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: `LLMReviewer.min_confidence`, `LLMReviewer.require_evidence` (Task 3).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_reviewer.py
class TestGroundedValidation:
    def _review(self, items, **kw):
        def handler(request):
            return httpx.Response(200, json=llm_response(json.dumps(items)))
        return make_reviewer(handler, require_evidence=True, min_confidence=0.5, **kw).review_file(
            make_hunk(annotated_diff="    13 +     email = user.email", commentable_lines={13})
        )

    def test_finding_without_evidence_in_diff_dropped(self):
        review = self._review([
            {"line": 13, "severity": "bug", "category": "bugs",
             "message": "x", "evidence": "this text is not in the diff", "confidence": 0.9},
        ])
        assert review.findings == []

    def test_finding_with_quoted_evidence_kept(self):
        review = self._review([
            {"line": 13, "severity": "bug", "category": "bugs",
             "message": "x", "evidence": "email = user.email", "confidence": 0.9},
        ])
        assert len(review.findings) == 1

    def test_low_confidence_dropped(self):
        review = self._review([
            {"line": 13, "severity": "bug", "category": "bugs",
             "message": "x", "evidence": "email = user.email", "confidence": 0.2},
        ])
        assert review.findings == []

    def test_disabled_category_dropped(self):
        review = self._review([
            {"line": 13, "severity": "suggestion", "category": "style",
             "message": "x", "evidence": "email = user.email", "confidence": 0.9},
        ], categories=["bugs"])
        assert review.findings == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_reviewer.py::TestGroundedValidation -v`
Expected: FAIL — findings are kept because filtering isn't implemented.

- [ ] **Step 3: Implement**

`reviewbot/config.py` — add to `ReviewSettings`:

```python
    min_confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    require_evidence: bool = True
    verify: bool = True  # used in Phase 4
```

`reviewbot/reviewer.py` — rewrite `_validate_findings`:

```python
    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"\s+", "", s).lower()

    def _validate_findings(self, raw: list, hunk: FileHunk) -> list[Finding]:
        enabled = {c for c in self.categories}
        diff_norm = self._norm(hunk.annotated_diff)
        findings: list[Finding] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                finding = Finding.model_validate({**item, "path": hunk.path})
            except ValidationError:
                continue
            if finding.confidence < self.min_confidence:
                continue
            if enabled and finding.category not in enabled:
                continue
            if self.require_evidence:
                if not finding.evidence or self._norm(finding.evidence) not in diff_norm:
                    continue
            findings.append(finding)

        order = {Severity.BUG: 0, Severity.WARNING: 1, Severity.SUGGESTION: 2}
        findings.sort(key=lambda f: (order[f.severity], f.line))
        return findings[:MAX_FINDINGS_PER_FILE]
```

`reviewbot/runner.py` — pass the config knobs:

```python
        reviewer = LLMReviewer(
            api_key=self._openrouter_api_key,
            model=self.config.model,
            categories=self.config.review.categories,
            intent=self._intent,
            min_confidence=self.config.review.min_confidence,
            require_evidence=self.config.review.require_evidence,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_reviewer.py tests/test_config.py -v`
Expected: PASS. Note: existing `TestFindingValidation` tests use `make_reviewer(handler)` with defaults (`require_evidence=False`, `min_confidence=0.0`) so they are unaffected.

- [ ] **Step 5: Commit**

```bash
git add reviewbot/config.py reviewbot/reviewer.py reviewbot/runner.py tests/test_reviewer.py
git commit -m "feat(validate): drop findings lacking quoted evidence, low confidence, or disabled category"
```

---

### Task 5: Quarantine off-diff findings out of the authoritative summary and blocking set

**Files:**
- Modify: `reviewbot/models.py` (`ReviewResult.blocking_findings` ignores off-diff)
- Modify: `reviewbot/poster.py` (`build_summary` renders an "unverified" section)
- Modify: `reviewbot/runner.py` (pass commentable map into summary/blocking)
- Test: `tests/test_poster.py`

**Interfaces:**
- Consumes: `commentable_map: dict[str, set[int]]` already built in `runner.run()`.
- Produces: `build_summary(result, blocking, skipped_files=None, off_diff=None)`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_poster.py
class TestOffDiffQuarantine:
    def test_off_diff_findings_in_collapsed_section_not_bugs(self):
        result = make_result([make_finding(line=999, message="suspicious")])
        summary = build_summary(
            result, blocking=[],
            off_diff=[make_finding(line=999, message="suspicious")],
        )
        assert "Unverified" in summary
        assert "999" in summary
        # not promoted into the authoritative Bugs section
        assert "### 🔴 Bugs — fix before merge" not in summary
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_poster.py::TestOffDiffQuarantine -v`
Expected: FAIL — `build_summary() got an unexpected keyword argument 'off_diff'`.

- [ ] **Step 3: Implement**

`reviewbot/poster.py` — change `build_summary` to take `off_diff` and render it collapsed, and exclude those findings from the severity sections:

```python
def build_summary(result, blocking, skipped_files=None, off_diff=None):
    off_keys = {(f.path, f.line, f.message) for f in (off_diff or [])}
    on_diff = [f for f in result.findings if (f.path, f.line, f.message) not in off_keys]
    bugs = sum(1 for f in on_diff if f.severity == Severity.BUG)
    warnings = sum(1 for f in on_diff if f.severity == Severity.WARNING)
    suggestions = sum(1 for f in on_diff if f.severity == Severity.SUGGESTION)
    total = bugs + warnings + suggestions

    lines = [SUMMARY_MARKER, "## ReviewBot Summary", ""]
    lines.append(
        f"Files reviewed: {result.files_reviewed} | Findings: {total} "
        f"({bugs} 🔴 bugs · {warnings} 🟡 warnings · {suggestions} 🔵 suggestions)"
    )
    if total == 0:
        lines += ["", "✅ No issues found in the reviewed changes."]

    sections = [
        (Severity.BUG, "### 🔴 Bugs — fix before merge"),
        (Severity.WARNING, "### 🟡 Warnings"),
        (Severity.SUGGESTION, "### 🔵 Suggestions"),
    ]
    for severity, header in sections:
        items = [f for f in on_diff if f.severity == severity]
        if not items:
            continue
        lines += ["", header]
        lines += [f"- {f.path}:{f.line} — {f.message}" for f in items]

    if off_diff:
        lines += ["", "<details><summary>⚠️ Unverified (line not in the diff)</summary>", ""]
        lines += [f"- {f.path}:{f.line} — {f.message}" for f in off_diff]
        lines += ["", "</details>"]

    if skipped_files:
        lines += ["", "### ⚪ Skipped files"]
        lines += [f"- {path} — {reason}" for path, reason in skipped_files]

    lines += ["", "---", f"Powered by ReviewBot · `{result.model}`"]
    return "\n".join(lines)
```

`reviewbot/runner.py` — compute off-diff findings and exclude them from blocking + summary:

```python
        commentable_map = {h.path: h.commentable_lines for h in hunks}
        off_diff = [
            f for f in result.findings
            if f.line not in commentable_map.get(f.path, set())
        ]
        on_diff_result = result  # findings property still includes all; blocking filters below
        blocking = [
            f for f in result.blocking_findings(self.config.review.block_merge_on)
            if f not in off_diff
        ]
        summary = build_summary(result, blocking, skipped_files or None, off_diff=off_diff or None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_poster.py tests/test_runner.py -v`
Expected: PASS. (Existing `test_counts_and_sections` calls `build_summary(result, blocking=...)` with no `off_diff` → behaves as before.)

- [ ] **Step 5: Commit**

```bash
git add reviewbot/poster.py reviewbot/runner.py tests/test_poster.py
git commit -m "feat(summary): quarantine off-diff findings, never block on them"
```

---

### Task 6: Structured outputs (response_format) + temperature 0, with extractor fallback

**Files:**
- Modify: `reviewbot/reviewer.py` (`_chat` sends `response_format` + `temperature: 0`; keep extractor as fallback)
- Test: `tests/test_reviewer.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_reviewer.py
class TestStructuredOutput:
    def test_review_call_sends_temperature_zero(self):
        seen = {}
        def handler(request):
            seen.update(json.loads(request.content))
            return httpx.Response(200, json=llm_response(VALID_FINDINGS))
        make_reviewer(handler).review_file(make_hunk())
        assert seen["temperature"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_reviewer.py::TestStructuredOutput -v`
Expected: FAIL — `temperature` is `0.1`.

- [ ] **Step 3: Implement**

In `reviewbot/reviewer.py` `_chat`, change the payload (the extractor in `extract_json_array` remains the fallback for models that ignore `response_format`):

```python
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        if max_tokens > 100:  # review calls, not ping
            payload["response_format"] = {"type": "json_object"}
```

> Note: OpenRouter `json_object` mode asks the model for valid JSON; some models wrap the array in `{"findings": [...]}`, which `extract_json_array` already unwraps (reviewer.py:115-119). No further change needed.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_reviewer.py -v`
Expected: PASS (the `test_ping` test sends `max_tokens=10` so `response_format` is not added; its assertion is unaffected).

- [ ] **Step 5: Commit**

```bash
git add reviewbot/reviewer.py tests/test_reviewer.py
git commit -m "feat(reviewer): request structured JSON output at temperature 0"
```

---

## Phase 3 — Callee definitions + depth-1 callers (the reported-bug cure)

### Task 7: `codegraph.py` — resolve callee definitions and caller sites (pure Python, no new deps)

**Files:**
- Create: `reviewbot/codegraph.py`
- Test: `tests/test_codegraph.py`

**Interfaces:**
- Produces: `CodeGraph(repo_root)`, `.related_definitions(changed_path, added_text, budget_lines) -> str`, `.affected_callers(changed_path, defined_names, budget_lines) -> str`, `build_codegraph(repo_root) -> CodeGraph | None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_codegraph.py
"""Tests for reviewbot.codegraph — pure-Python definition/caller resolution."""

from reviewbot.codegraph import build_codegraph


def _tree(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "db.py").write_text(
        "def get_user(session, uid):\n"
        "    u = session.get(User, uid)\n"
        "    if u is None:\n"
        "        raise NotFound(uid)\n"
        "    return u\n"
    )
    (tmp_path / "app" / "auth.py").write_text(
        "from app.db import get_user\n\n"
        "def login(session, uid):\n"
        "    user = get_user(session, uid)\n"
        "    return user.email\n"
    )
    return str(tmp_path)


class TestRelatedDefinitions:
    def test_resolves_callee_signature(self, tmp_path):
        graph = build_codegraph(_tree(tmp_path))
        out = graph.related_definitions(
            "app/auth.py", "user = get_user(session, uid)", budget_lines=20
        )
        assert "get_user" in out
        assert "app/db.py" in out
        assert "raise NotFound" in out  # body shown proves the guard exists

    def test_unknown_symbol_omitted(self, tmp_path):
        graph = build_codegraph(_tree(tmp_path))
        out = graph.related_definitions("app/auth.py", "frobnicate(x)", budget_lines=20)
        assert "frobnicate" not in out


class TestAffectedCallers:
    def test_finds_caller_of_changed_function(self, tmp_path):
        graph = build_codegraph(_tree(tmp_path))
        out = graph.affected_callers("app/db.py", {"get_user"}, budget_lines=20)
        assert "app/auth.py" in out
        assert "get_user" in out


class TestBuild:
    def test_none_root_returns_none(self):
        assert build_codegraph(None) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_codegraph.py -v`
Expected: FAIL — `No module named 'reviewbot.codegraph'`.

- [ ] **Step 3: Implement**

```python
# reviewbot/codegraph.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_codegraph.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add reviewbot/codegraph.py tests/test_codegraph.py
git commit -m "feat(codegraph): resolve callee definitions + caller sites, dependency-free"
```

---

### Task 8: Wire codegraph context into the hunk and prompt

**Files:**
- Modify: `reviewbot/runner.py` (build one `CodeGraph` per run; populate `related_definitions`/`affected_callers` per hunk)
- Modify: `reviewbot/reviewer.py` (`build_user_prompt` emits the two blocks; system prompt rule about callee guards)
- Modify: `reviewbot/source_context.py` (helper to extract defined names from added lines)
- Test: `tests/test_reviewer.py`, `tests/test_runner.py`

**Interfaces:**
- Consumes: `CodeGraph.related_definitions`, `CodeGraph.affected_callers`, `build_codegraph`.
- Produces: `source_context.defined_names(added_text) -> set[str]`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_reviewer.py
class TestContextBlocks:
    def test_user_prompt_includes_definitions_and_callers(self):
        hunk = make_hunk(
            related_definitions="# get_user — defined at app/db.py:1\ndef get_user(...): ...",
            affected_callers="app/x.py:5: get_user(s, 1)",
        )
        p = build_user_prompt(hunk)
        assert "get_user — defined at app/db.py:1" in p
        assert "Other call sites" in p
        assert "app/x.py:5" in p
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_reviewer.py::TestContextBlocks -v`
Expected: FAIL — the blocks aren't rendered.

- [ ] **Step 3: Implement**

`reviewbot/source_context.py` — add:

```python
_DEFINED_RE = re.compile(
    r"^\s*[+]?\s*(?:export\s+|async\s+)*(?:def|class|function|func|fn)\s+([A-Za-z_]\w*)"
)

def defined_names(added_text: str) -> set[str]:
    """Names of functions/classes defined in the changed lines."""
    return {m.group(1) for line in added_text.splitlines() if (m := _DEFINED_RE.match(line))}
```

`reviewbot/reviewer.py` `build_user_prompt` — add before the final "Only report…" line:

```python
    if hunk.related_definitions:
        parts.append(
            "\nDefinitions of functions called by this change (a callee may already "
            "guard/validate — only assume it does NOT if shown here and proven):\n"
            + hunk.related_definitions
        )
    if hunk.affected_callers:
        parts.append(
            "\nOther call sites of functions changed here (check you didn't break them):\n"
            + hunk.affected_callers
        )
```

`reviewbot/runner.py` — build the graph once and populate hunks. In `run()` after resolving `repo_root`:

```python
from reviewbot.codegraph import build_codegraph
from reviewbot.source_context import defined_names

        graph = build_codegraph(repo_root)
        hunks, skipped_files = self._select_hunks(pr_data.files, repo_root)
        if graph is not None:
            budget = max(self.config.review.max_lines_per_file // 4, 20)
            for h in hunks:
                added = "\n".join(
                    l for l in h.annotated_diff.splitlines() if " + " in l[:10]
                )
                h.related_definitions = graph.related_definitions(h.path, added, budget)
                names = defined_names(added)
                if names:
                    h.affected_callers = graph.affected_callers(h.path, names, budget)
```

> The token budget order (enclosing scope > callee defs > callers) is enforced implicitly: enclosing scope is already capped by `max_lines_per_file` in Task 2; defs+callers each get a quarter-budget here. Tighten only if the free model's window overflows in practice.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_reviewer.py tests/test_runner.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add reviewbot/source_context.py reviewbot/reviewer.py reviewbot/runner.py tests/test_reviewer.py
git commit -m "feat(context): show callee definitions and caller sites to the reviewer"
```

---

## Phase 4 — Single grounded verify-then-comment pass

### Task 9: One batched verification call per file that drops ungrounded findings

**Files:**
- Modify: `reviewbot/reviewer.py` (`review_file` runs one verify `_chat` when `self.verify`; add `build_verify_prompt`)
- Modify: `reviewbot/runner.py` (pass `verify=config.review.verify`)
- Modify: `reviewbot/cli.py` (`--no-verify` flag → overrides config)
- Test: `tests/test_reviewer.py`

**Interfaces:**
- Consumes: `LLMReviewer.verify` (Task 3).
- Produces: `build_verify_prompt(findings, hunk) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_reviewer.py
class TestVerifyPass:
    def test_verify_drops_unconfirmed_finding(self):
        # 1st call: two candidates. 2nd (verify) call: keep only line 13.
        responses = [
            json.dumps([
                {"line": 13, "severity": "bug", "category": "bugs", "message": "real",
                 "evidence": "email = user.email", "confidence": 0.9},
                {"line": 14, "severity": "bug", "category": "bugs", "message": "bogus",
                 "evidence": "email = user.email", "confidence": 0.9},
            ]),
            json.dumps([
                {"line": 13, "severity": "bug", "category": "bugs", "message": "real",
                 "evidence": "email = user.email", "confidence": 0.95},
            ]),
        ]
        def handler(request):
            return httpx.Response(200, json=llm_response(responses.pop(0)))
        review = make_reviewer(handler, verify=True).review_file(
            make_hunk(annotated_diff="    13 +     email = user.email", commentable_lines={13, 14})
        )
        assert [f.line for f in review.findings] == [13]

    def test_no_findings_skips_verify_call(self):
        calls = []
        def handler(request):
            calls.append(1)
            return httpx.Response(200, json=llm_response("[]"))
        make_reviewer(handler, verify=True).review_file(make_hunk())
        assert len(calls) == 1  # no verify call when nothing to verify
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_reviewer.py::TestVerifyPass -v`
Expected: FAIL — verify pass not implemented; both candidates returned.

- [ ] **Step 3: Implement**

`reviewbot/reviewer.py` — add the prompt builder and the verify step at the end of `review_file` (after `findings = self._validate_findings(...)`):

```python
def build_verify_prompt(findings: list[Finding], hunk: FileHunk) -> str:
    listed = "\n".join(
        f'{i}. line {f.line} [{f.severity.value}] {f.message} (evidence: "{f.evidence}")'
        for i, f in enumerate(findings, 1)
    )
    return (
        "You previously proposed these findings on the change below. For EACH, decide "
        "if it is a TRUE positive: name the input/state that triggers it and confirm no "
        "visible guard or shown callee prevents it. Return ONLY a JSON array of the "
        "findings that survive, each with the SAME line/severity/category/message/evidence "
        "and a confidence in [0,1]. Drop any you cannot ground.\n\n"
        f"Candidates:\n{listed}\n\n"
        f"Enclosing scope:\n{hunk.enclosing_context or '(not available)'}\n\n"
        f"Callee definitions:\n{hunk.related_definitions or '(none)'}\n\n"
        f"Diff:\n{hunk.annotated_diff}\n\nReturn the JSON array now."
    )
```

In `review_file`, replace the success path:

```python
            findings = self._validate_findings(raw_findings, hunk)
            if self.verify and findings:
                findings = self._verify(findings, hunk)
            return FileReview(path=hunk.path, findings=findings)
```

And add the `_verify` method:

```python
    def _verify(self, findings: list[Finding], hunk: FileHunk) -> list[Finding]:
        try:
            content = self._chat([
                {"role": "system", "content": "You are a strict verifier. Default to dropping unprovable findings."},
                {"role": "user", "content": build_verify_prompt(findings, hunk)},
            ])
            survivors = extract_json_array(content)
        except LLMError:
            return findings  # verification is best-effort; never lose findings to its failure
        kept = self._validate_findings(survivors, hunk)
        # keep only originally-proposed lines (verifier can't invent new ones)
        original_lines = {f.line for f in findings}
        return [f for f in kept if f.line in original_lines] or []
```

`reviewbot/runner.py` — pass `verify`:

```python
            verify=self.config.review.verify,
```

`reviewbot/cli.py` — add `--no-verify` to the `review` command and override config:

```python
    no_verify: bool = typer.Option(False, "--no-verify", help="Skip the second verification pass (faster, cheaper on the free tier)."),
    # ... after load_config(config_path):
    if no_verify:
        config.review.verify = False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_reviewer.py -v`
Expected: PASS. (Existing tests use default `verify=False`, so they make exactly one call as before.)

- [ ] **Step 5: Commit**

```bash
git add reviewbot/reviewer.py reviewbot/runner.py reviewbot/cli.py tests/test_reviewer.py
git commit -m "feat(verify): add one grounded verification pass; --no-verify to skip"
```

---

## Phase 5 — Output polish: committable suggestions, dedup, partial-review honesty, house rules

### Task 10: Committable `suggestion` blocks + multi-line anchors + commit_id

**Files:**
- Modify: `reviewbot/models.py` (`Finding.start_line`; `comment_body` emits a suggestion block when a fix is concrete)
- Modify: `reviewbot/poster.py` (inline comment carries `start_line`/`start_side`; review payload carries `commit_id`)
- Modify: `reviewbot/runner.py` (pass `pr_data.head_sha` as `commit_id`)
- Test: `tests/test_poster.py`

**Interfaces:**
- Consumes: `pr_data.head_sha` (already captured in `fetcher.py`).
- Produces: `CommentPoster.post_review(..., commit_id="")`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_poster.py
class TestSuggestionsAndCommit:
    def test_commit_id_passed_into_review(self):
        pull = make_pull()
        CommentPoster(pull).post_review(
            summary="s", findings=[make_finding(line=13)],
            commentable_map={"app/auth.py": {13}}, blocking=False, commit_id="abc123",
        )
        payload = pull._requester.requestJsonAndCheck.call_args.kwargs["input"]
        assert payload["commit_id"] == "abc123"

    def test_multiline_finding_emits_start_line(self):
        pull = make_pull()
        f = make_finding(line=15, start_line=13)
        CommentPoster(pull).post_review(
            summary="s", findings=[f],
            commentable_map={"app/auth.py": {13, 14, 15}}, blocking=False,
        )
        comment = pull._requester.requestJsonAndCheck.call_args.kwargs["input"]["comments"][0]
        assert comment["start_line"] == 13
        assert comment["line"] == 15
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_poster.py::TestSuggestionsAndCommit -v`
Expected: FAIL — `post_review() got an unexpected keyword argument 'commit_id'`.

- [ ] **Step 3: Implement**

`reviewbot/models.py` — add `start_line` to `Finding` (after `confidence`):

```python
    start_line: int | None = Field(default=None, description="First line of a multi-line range")
```

`reviewbot/poster.py` — thread `commit_id` and `start_line`:

```python
    def post_review(self, summary, findings, commentable_map, blocking, commit_id=""):
        comments = self._build_inline_comments(findings, commentable_map)
        event = REQUEST_CHANGES if blocking else COMMENT
        payload: dict = {"body": summary, "event": event}
        if commit_id:
            payload["commit_id"] = commit_id
        if comments:
            payload["comments"] = comments
        # ... rest unchanged ...

    @staticmethod
    def _build_inline_comments(findings, commentable_map):
        out = []
        for f in findings:
            allowed = commentable_map.get(f.path, set())
            if f.line not in allowed:
                continue
            comment = {"path": f.path, "line": f.line, "side": "RIGHT", "body": f.comment_body()}
            if f.start_line and f.start_line < f.line and f.start_line in allowed:
                comment["start_line"] = f.start_line
                comment["start_side"] = "RIGHT"
            out.append(comment)
        return out
```

`reviewbot/runner.py` — pass `commit_id`:

```python
                event = CommentPoster(pull).post_review(
                    summary=summary,
                    findings=result.findings,
                    commentable_map=commentable_map,
                    blocking=bool(blocking),
                    commit_id=pr_data.head_sha,
                )
```

> `comment_body()` already renders a `**Fix:**` block. Emitting a literal ```` ```suggestion ```` block requires the model to return a replacement that exactly matches the target line range; that is deferred — the current `**Fix:**` prose plus correct line anchoring is the safe, high-value step. (ponytail: suggestion-block correctness needs line-range validation + 4-backtick escaping; add when prose fixes prove insufficient.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_poster.py tests/test_runner.py -v`
Expected: PASS. (`post_review`'s `commit_id` defaults to `""`, so existing poster tests are unaffected.)

- [ ] **Step 5: Commit**

```bash
git add reviewbot/models.py reviewbot/poster.py reviewbot/runner.py tests/test_poster.py
git commit -m "feat(poster): anchor comments to commit_id and support multi-line ranges"
```

---

### Task 11: Deduplicate repeated findings

**Files:**
- Modify: `reviewbot/models.py` (`ReviewResult.findings` dedups by normalized message+category)
- Test: `tests/test_poster.py` (uses `ReviewResult`) or new assertions

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_poster.py
class TestDedup:
    def test_duplicate_findings_collapsed(self):
        from reviewbot.models import FileReview, Finding, ReviewResult
        dup = lambda p: Finding(path=p, line=1, severity="warning", category="code_quality",
                                message="Duplicated logic across files")
        result = ReviewResult(
            file_reviews=[
                FileReview(path="a.py", findings=[dup("a.py")]),
                FileReview(path="b.py", findings=[dup("b.py")]),
            ],
            files_reviewed=2,
        )
        # same (message, category) on different files → kept once
        msgs = [(f.message, f.category) for f in result.findings]
        assert msgs.count(("Duplicated logic across files", "code_quality")) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_poster.py::TestDedup -v`
Expected: FAIL — both copies returned.

- [ ] **Step 3: Implement**

`reviewbot/models.py` — make `ReviewResult.findings` dedup:

```python
    @property
    def findings(self) -> list[Finding]:
        seen: set[tuple[str, str]] = set()
        out: list[Finding] = []
        for fr in self.file_reviews:
            for f in fr.findings:
                key = (f.message.strip().lower(), f.category)
                if key in seen:
                    continue
                seen.add(key)
                out.append(f)
        return out
```

> Keyed on (normalized message, category) — conservative, so genuinely distinct findings that happen to share a category are not merged. ponytail: do not key on line (the same bug on different files has different lines but is one finding).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/ -v`
Expected: PASS. (Check `test_runner.py`/`test_poster.py` counts still hold — distinct messages remain distinct.)

- [ ] **Step 5: Commit**

```bash
git add reviewbot/models.py tests/test_poster.py
git commit -m "feat(results): dedup repeated findings by message + category"
```

---

### Task 12: Honest truncation — count added lines and mark partial reviews

**Files:**
- Modify: `reviewbot/parser.py` (truncation budget counts added lines; consistent `>`/`>=`)
- Modify: `reviewbot/poster.py` (`build_summary` notes partially-reviewed files)
- Modify: `reviewbot/runner.py` (collect truncated hunks → pass to summary)
- Test: `tests/test_parser.py`

**Interfaces:**
- Produces: `build_summary(..., partial=None)` where `partial: list[str]` is file paths reviewed partially.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_parser.py
class TestTruncationAddedLines:
    def test_exact_fit_not_marked_truncated(self):
        # 5 added lines, generous budget → must NOT be flagged truncated
        added = "\n".join(f"+x{i} = {i}" for i in range(5))
        hunk = build_file_hunk("ok.py", f"@@ -0,0 +1,5 @@\n{added}\n", max_lines=50)
        assert not hunk.is_truncated
        assert "diff truncated" not in hunk.annotated_diff

    def test_genuinely_large_diff_still_truncates(self):
        added = "\n".join(f"+x{i} = {i}" for i in range(200))
        hunk = build_file_hunk("big.py", f"@@ -0,0 +1,200 @@\n{added}\n", max_lines=20)
        assert hunk.is_truncated
        assert hunk.annotated_diff.splitlines()[-1] == "... (diff truncated)"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_parser.py::TestTruncationAddedLines -v`
Expected: FAIL on `test_exact_fit_not_marked_truncated` — the current mid-loop `>=` check marks small-but-exact diffs as truncated (confirmed audit finding).

- [ ] **Step 3: Implement**

`reviewbot/parser.py` — replace the truncation logic in `build_file_hunk`. Use a single overflow source of truth and break mid-hunk:

```python
    for hunk in patched_file:
        header = (f"@@ -{hunk.source_start},{hunk.source_length} "
                  f"+{hunk.target_start},{hunk.target_length} @@")
        annotated_lines.append(header)
        for line in hunk:
            content = line.value.rstrip("\n")
            if line.is_added:
                annotated_lines.append(f"{line.target_line_no:>6} + {content}")
                commentable.add(line.target_line_no)
                added_count += 1
            elif line.is_context:
                annotated_lines.append(f"{line.target_line_no:>6}   {content}")
                commentable.add(line.target_line_no)
            elif line.is_removed:
                annotated_lines.append(f"       - {content}")
            if len(annotated_lines) > max_lines:   # single source of truth, break mid-hunk
                truncated = True
                break
        if truncated:
            break

    if added_count == 0:
        return None
    if truncated:
        annotated_lines = annotated_lines[:max_lines]
        annotated_lines.append("... (diff truncated)")
```

`reviewbot/poster.py` `build_summary` — add a `partial` param:

```python
def build_summary(result, blocking, skipped_files=None, off_diff=None, partial=None):
    # ... after the off_diff block, before skipped_files:
    if partial:
        lines += ["", "### ⚠️ Partially reviewed (large files — tail not analyzed)"]
        lines += [f"- {p}" for p in partial]
```

`reviewbot/runner.py` — collect truncated paths and pass them:

```python
        partial = [h.path for h in hunks if h.is_truncated]
        summary = build_summary(result, blocking, skipped_files or None,
                                off_diff=off_diff or None, partial=partial or None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_parser.py tests/test_poster.py tests/test_runner.py -v`
Expected: PASS. Update `tests/test_parser.py::TestTruncation::test_large_diff_is_truncated` if its `max_lines=10` boundary assertion shifts by one (it asserts `<= 12` lines, still satisfied).

- [ ] **Step 5: Commit**

```bash
git add reviewbot/parser.py reviewbot/poster.py reviewbot/runner.py tests/test_parser.py
git commit -m "fix(parser): truncate on real overflow only; flag partially reviewed files"
```

---

### Task 13: Optional `.github/reviewbot.md` house rules

**Files:**
- Modify: `reviewbot/config.py` (`load_house_rules(repo_root) -> str`)
- Modify: `reviewbot/runner.py` (load house rules, pass into `LLMReviewer`)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `config.load_house_rules(repo_root: str | None) -> str`.
- Consumes: `LLMReviewer(house_rules=...)` and `build_system_prompt(categories, house_rules)` (Task 3).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_config.py
from reviewbot.config import load_house_rules

class TestHouseRules:
    def test_reads_house_rules_when_present(self, tmp_path):
        (tmp_path / ".github").mkdir()
        (tmp_path / ".github" / "reviewbot.md").write_text("Always use tabs.\n")
        assert "Always use tabs." in load_house_rules(str(tmp_path))

    def test_absent_returns_empty(self, tmp_path):
        assert load_house_rules(str(tmp_path)) == ""

    def test_none_root_returns_empty(self):
        assert load_house_rules(None) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py::TestHouseRules -v`
Expected: FAIL — `cannot import name 'load_house_rules'`.

- [ ] **Step 3: Implement**

`reviewbot/config.py` — add:

```python
HOUSE_RULES_PATH = ".github/reviewbot.md"

def load_house_rules(repo_root: str | None, max_chars: int = 4000) -> str:
    """Optional project review conventions; empty string when absent (zero-setup)."""
    if not repo_root:
        return ""
    path = Path(repo_root) / HOUSE_RULES_PATH
    try:
        return path.read_text(encoding="utf-8")[:max_chars] if path.exists() else ""
    except OSError:
        return ""
```

`reviewbot/runner.py` — in `run()` after resolving `repo_root`, and pass into `_review_all` (store on self):

```python
from reviewbot.config import load_house_rules  # add import
        self._house_rules = load_house_rules(repo_root)
# in _review_all reviewer construction:
            house_rules=self._house_rules,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/ -v`
Expected: PASS (full suite green).

- [ ] **Step 5: Commit**

```bash
git add reviewbot/config.py reviewbot/runner.py tests/test_config.py
git commit -m "feat(config): optional .github/reviewbot.md house rules in the prompt"
```

---

## Final verification

- [ ] Run the full suite: `pytest tests/ -v` → all green.
- [ ] Lint check (if configured): `python -m pyflakes reviewbot/` → no unused imports from the edits.
- [ ] Smoke test against a real PR (manual): `reviewbot review --repo <owner/name> --pr <n> --dry-run` from inside a clean checkout, confirm the printed prompt context now contains enclosing scope + callee definitions, and findings carry evidence.

## Self-Review notes (gaps deliberately left)

- **Suggestion blocks** (committable ```` ```suggestion ````): Task 10 ships line anchoring + commit_id; the literal suggestion block is deferred (needs exact line-range replacement text + 4-backtick escaping). Add when prose `**Fix:**` proves insufficient.
- **Idempotency / one living review** (the unused `SUMMARY_MARKER`, duplicate reviews on every push) is a separate concern from review *quality* and lives in the earlier 27-issue audit, not this plan. It pairs naturally after Phase 5.
- **tree-sitter precision upgrade** for `codegraph.py` call extraction is the documented next step if regex call-detection produces noise; intentionally not a v1 dependency.
