"""LLM review of a single file's diff via OpenRouter.

Reliability rules (PRD): malformed JSON → retry up to 2 times, then skip the
file. Rate limits / transient HTTP errors → exponential backoff. A file-level
failure must never crash the run — `review_file` always returns a FileReview.
"""

from __future__ import annotations

import json
import re
import time

import httpx
from pydantic import ValidationError

from reviewbot.models import FileHunk, FileReview, Finding, Severity

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Sent as OpenRouter attribution headers (optional but good citizenship).
APP_URL = "https://github.com/swapnilsable-pro/reviewbot"
APP_TITLE = "ReviewBot"

MAX_FINDINGS_PER_FILE = 10

CATEGORY_DESCRIPTIONS = {
    "bugs": "logic errors, None/null dereferences, wrong conditions, off-by-one errors, unhandled edge cases",
    "security": "SQL injection, hardcoded secrets, missing input validation, unsafe deserialization, path traversal",
    "error_handling": "bare except blocks, swallowed exceptions, missing rollback/cleanup on error paths",
    "code_quality": "dead code, copy-pasted duplication, misleading names, unreachable branches",
    "performance": "N+1 queries, work repeated inside loops, unnecessary allocations",
    "style": "naming conventions, formatting, idiomatic usage",
}

LANGUAGE_HINTS = {
    "py": "Python", "js": "JavaScript", "jsx": "JavaScript (React)",
    "ts": "TypeScript", "tsx": "TypeScript (React)", "go": "Go", "rs": "Rust",
    "java": "Java", "kt": "Kotlin", "rb": "Ruby", "php": "PHP", "cs": "C#",
    "c": "C", "h": "C header", "cpp": "C++", "swift": "Swift", "scala": "Scala",
    "sh": "Shell", "bash": "Shell", "sql": "SQL", "html": "HTML", "css": "CSS",
    "yml": "YAML", "yaml": "YAML", "tf": "Terraform", "vue": "Vue",
}


class LLMError(Exception):
    """Raised when the LLM cannot produce a usable response."""


def build_system_prompt(categories: list[str]) -> str:
    enabled = [c for c in categories if c in CATEGORY_DESCRIPTIONS] or ["bugs"]
    category_lines = "\n".join(
        f"- {name}: {CATEGORY_DESCRIPTIONS[name]}" for name in enabled
    )
    return f"""You are ReviewBot, an expert code reviewer. You review pull request diffs \
and report specific, actionable findings.

Report findings ONLY in these categories:
{category_lines}

Severity levels:
- "bug": will or very likely will cause incorrect behavior, a crash, or a vulnerability
- "warning": a risky pattern that should be fixed but may not break immediately
- "suggestion": an improvement that is optional

Rules:
1. Only report issues on ADDED lines — the ones prefixed with a line number and "+".
2. Use the line number shown at the start of the line.
3. Be specific: name the variable/function and say what goes wrong. No vague advice.
4. Do NOT report issues you cannot see evidence for in the diff.
5. If the code is fine, return an empty array. Do not invent findings.
6. At most {MAX_FINDINGS_PER_FILE} findings.

Respond with ONLY a JSON array (no prose, no markdown fences). Each element:
{{"line": <int>, "severity": "bug"|"warning"|"suggestion", "category": "<category>", \
"message": "<what is wrong and why>", "suggestion": "<how to fix it, optional>"}}"""


def build_user_prompt(hunk: FileHunk) -> str:
    ext = hunk.path.rsplit(".", 1)[-1].lower() if "." in hunk.path else ""
    language = LANGUAGE_HINTS.get(ext, "")
    lang_line = f"Language: {language}\n" if language else ""
    new_file_note = "This is a NEW file.\n" if hunk.is_new_file else ""
    truncated_note = (
        "Note: the diff was truncated; review only what is shown.\n"
        if hunk.is_truncated
        else ""
    )
    return (
        f"File: {hunk.path}\n{lang_line}{new_file_note}{truncated_note}\n"
        f"Diff (added lines are marked with +, line numbers refer to the new file):\n\n"
        f"{hunk.annotated_diff}\n\n"
        "Return the JSON array of findings now."
    )


def extract_json_array(text: str) -> list:
    """Pull a JSON array out of an LLM response, tolerating fences and prose."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end <= start:
            raise LLMError("Response contains no JSON array")
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"Response JSON is malformed: {exc}") from exc

    if isinstance(parsed, dict):  # some models wrap: {"findings": [...]}
        for key in ("findings", "issues", "results"):
            if isinstance(parsed.get(key), list):
                return parsed[key]
        raise LLMError("Response JSON is an object without a findings array")
    if not isinstance(parsed, list):
        raise LLMError(f"Response JSON is {type(parsed).__name__}, expected array")
    return parsed


class LLMReviewer:
    """Reviews one FileHunk at a time against the OpenRouter API."""

    def __init__(
        self,
        api_key: str,
        model: str,
        categories: list[str] | None = None,
        timeout: float = 90.0,
        max_json_retries: int = 2,
        max_http_retries: int = 3,
    ) -> None:
        self.model = model
        self.categories = categories or list(CATEGORY_DESCRIPTIONS)
        self.max_json_retries = max_json_retries
        self.max_http_retries = max_http_retries
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": APP_URL,
                "X-Title": APP_TITLE,
            },
        )

    # -- public API ----------------------------------------------------------

    def ping(self) -> str:
        """One tiny request to prove the key + model work."""
        content = self._chat(
            [{"role": "user", "content": "Reply with exactly: OK"}], max_tokens=10
        )
        return content.strip()

    def review_file(self, hunk: FileHunk) -> FileReview:
        """Review one file. Never raises — failures produce a skipped FileReview."""
        messages = [
            {"role": "system", "content": build_system_prompt(self.categories)},
            {"role": "user", "content": build_user_prompt(hunk)},
        ]

        last_error = "unknown error"
        for attempt in range(1 + self.max_json_retries):
            try:
                content = self._chat(messages)
            except LLMError as exc:
                # HTTP-level failure already retried inside _chat — skip the file.
                return FileReview(
                    path=hunk.path, skipped=True, skip_reason=str(exc)
                )

            try:
                raw_findings = extract_json_array(content)
            except LLMError as exc:
                last_error = str(exc)
                # Nudge the model and retry.
                messages = messages[:2] + [
                    {"role": "assistant", "content": content[:2000]},
                    {
                        "role": "user",
                        "content": "That was not a valid JSON array. Respond again "
                        "with ONLY the JSON array of findings, nothing else.",
                    },
                ]
                continue

            findings = self._validate_findings(raw_findings, hunk)
            return FileReview(path=hunk.path, findings=findings)

        return FileReview(
            path=hunk.path,
            skipped=True,
            skip_reason=f"LLM returned malformed JSON after "
            f"{1 + self.max_json_retries} attempts ({last_error})",
        )

    def close(self) -> None:
        self._client.close()

    # -- internals -----------------------------------------------------------

    def _validate_findings(self, raw: list, hunk: FileHunk) -> list[Finding]:
        findings: list[Finding] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                finding = Finding.model_validate({**item, "path": hunk.path})
            except ValidationError:
                continue  # drop individual bad findings, keep the rest
            findings.append(finding)

        # Most severe first, capped to keep PRs readable.
        order = {Severity.BUG: 0, Severity.WARNING: 1, Severity.SUGGESTION: 2}
        findings.sort(key=lambda f: (order[f.severity], f.line))
        return findings[:MAX_FINDINGS_PER_FILE]

    def _chat(self, messages: list[dict], max_tokens: int = 4000) -> str:
        """POST to OpenRouter with backoff on 429/5xx/transport errors."""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }

        last_error = "unknown"
        for attempt in range(self.max_http_retries):
            if attempt > 0:
                time.sleep(min(5 * 2 ** (attempt - 1), 30))
            try:
                response = self._client.post(OPENROUTER_URL, json=payload)
            except httpx.HTTPError as exc:
                last_error = f"network error: {exc}"
                continue

            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                continue
            if response.status_code in (401, 403):
                raise LLMError(
                    f"OpenRouter rejected the API key (HTTP {response.status_code}). "
                    "Check OPENROUTER_API_KEY."
                )
            if response.status_code != 200:
                raise LLMError(
                    f"OpenRouter error HTTP {response.status_code}: {response.text[:300]}"
                )

            try:
                data = response.json()
                content = data["choices"][0]["message"]["content"]
            except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
                raise LLMError(f"Unexpected OpenRouter response shape: {exc}") from exc
            if content is None:
                raise LLMError("OpenRouter returned an empty completion")
            return content

        raise LLMError(
            f"OpenRouter unavailable after {self.max_http_retries} attempts "
            f"(last: {last_error})"
        )
