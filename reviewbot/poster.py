"""Post review results back to GitHub: one review submission containing the
summary body plus all inline comments (path + line + side).

Uses the raw REST endpoint via PyGithub's requester because the modern
`line`/`side` comment parameters are what make inline comments land on the
right lines (the legacy `position` parameter is diff-offset based and fragile).
"""

from __future__ import annotations

from github import GithubException
from github.PullRequest import PullRequest

from reviewbot.models import Finding, ReviewResult, Severity

REQUEST_CHANGES = "REQUEST_CHANGES"
COMMENT = "COMMENT"

SUMMARY_MARKER = "<!-- reviewbot-summary -->"


class PostError(Exception):
    """Raised when the review cannot be posted to GitHub at all."""


def build_summary(
    result: ReviewResult,
    blocking: list[Finding],
    skipped_files: list[tuple[str, str]] | None = None,
) -> str:
    """Render the PR summary comment (PRD format)."""
    bugs = result.count(Severity.BUG)
    warnings = result.count(Severity.WARNING)
    suggestions = result.count(Severity.SUGGESTION)
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
        items = [f for f in result.findings if f.severity == severity]
        if not items:
            continue
        lines += ["", header]
        lines += [f"- {f.path}:{f.line} — {f.message}" for f in items]

    if skipped_files:
        lines += ["", "### ⚪ Skipped files"]
        lines += [f"- {path} — {reason}" for path, reason in skipped_files]

    lines += ["", "---", f"Powered by ReviewBot · `{result.model}`"]
    return "\n".join(lines)


class CommentPoster:
    """Submits one PR review with inline comments and a summary body."""

    def __init__(self, pull: PullRequest) -> None:
        self._pull = pull

    def post_review(
        self,
        summary: str,
        findings: list[Finding],
        commentable_map: dict[str, set[int]],
        blocking: bool,
    ) -> str:
        """Post the review; returns the review event that was actually used.

        Degrades gracefully:
        - REQUEST_CHANGES rejected (e.g. own PR) → retry as COMMENT
        - inline comments rejected (422)        → retry summary-only
        """
        comments = self._build_inline_comments(findings, commentable_map)
        event = REQUEST_CHANGES if blocking else COMMENT

        payload: dict = {"body": summary, "event": event}
        if comments:
            payload["comments"] = comments

        attempts = 0
        while True:
            attempts += 1
            try:
                self._pull._requester.requestJsonAndCheck(
                    "POST", f"{self._pull.url}/reviews", input=payload
                )
                return payload["event"]
            except GithubException as exc:
                if exc.status == 422 and attempts <= 2:
                    message = str(exc.data).lower() if exc.data else ""
                    if payload["event"] == REQUEST_CHANGES and "request changes" in message:
                        payload["event"] = COMMENT
                        continue
                    if "comments" in payload:
                        # One or more inline comments rejected — findings are
                        # still all listed in the summary body.
                        payload.pop("comments")
                        continue
                raise PostError(
                    f"GitHub rejected the review (HTTP {exc.status}): {exc.data}"
                ) from exc

    @staticmethod
    def _build_inline_comments(
        findings: list[Finding], commentable_map: dict[str, set[int]]
    ) -> list[dict]:
        """Inline comment payloads for findings on lines GitHub will accept.

        Findings on lines outside the diff are not dropped — they appear in
        the summary body — they just can't be anchored inline.
        """
        return [
            {
                "path": f.path,
                "line": f.line,
                "side": "RIGHT",
                "body": f.comment_body(),
            }
            for f in findings
            if f.line in commentable_map.get(f.path, set())
        ]
