"""Tests for reviewbot.poster — summary rendering and review submission with
a mocked PyGithub PullRequest. No real GitHub calls."""

from unittest.mock import MagicMock

import pytest
from github import GithubException

from reviewbot.models import FileReview, Finding, ReviewResult, Severity
from reviewbot.poster import (
    COMMENT,
    REQUEST_CHANGES,
    SUMMARY_MARKER,
    CommentPoster,
    PostError,
    build_summary,
)


def make_finding(path="app/auth.py", line=13, severity="bug", **kw) -> Finding:
    defaults = dict(
        path=path, line=line, severity=severity, category="bugs",
        message="user may be None",
    )
    defaults.update(kw)
    return Finding(**defaults)


def make_result(findings, model="test/model") -> ReviewResult:
    by_path: dict[str, list[Finding]] = {}
    for f in findings:
        by_path.setdefault(f.path, []).append(f)
    return ReviewResult(
        file_reviews=[FileReview(path=p, findings=fs) for p, fs in by_path.items()],
        files_reviewed=len(by_path),
        model=model,
    )


def make_pull() -> MagicMock:
    pull = MagicMock()
    pull.url = "https://api.github.com/repos/acme/widgets/pulls/7"
    pull._requester.requestJsonAndCheck.return_value = ({}, {})
    return pull


class TestBuildSummary:
    def test_counts_and_sections(self):
        result = make_result(
            [
                make_finding(line=13, severity="bug"),
                make_finding(path="app/db.py", line=87, severity="bug",
                             message="missing rollback"),
                make_finding(path="app/q.py", line=23, severity="warning",
                             category="error_handling", message="bare except"),
                make_finding(path="static/x.js", line=134, severity="suggestion",
                             category="code_quality", message="duplicated logic"),
            ]
        )
        summary = build_summary(result, blocking=result.findings[:2])
        assert SUMMARY_MARKER in summary
        assert "Findings: 4 (2 🔴 bugs · 1 🟡 warnings · 1 🔵 suggestions)" in summary
        assert "### 🔴 Bugs — fix before merge" in summary
        assert "- app/auth.py:13 — user may be None" in summary
        assert "### 🟡 Warnings" in summary
        assert "### 🔵 Suggestions" in summary
        assert "`test/model`" in summary

    def test_clean_review(self):
        result = make_result([])
        summary = build_summary(result, blocking=[])
        assert "✅ No issues found" in summary
        assert "🔴 Bugs" not in summary

    def test_skipped_files_section(self):
        summary = build_summary(
            make_result([]), blocking=[],
            skipped_files=[("app/big.py", "LLM returned malformed JSON")],
        )
        assert "### ⚪ Skipped files" in summary
        assert "app/big.py — LLM returned malformed JSON" in summary


class TestCommentPoster:
    def test_posts_inline_comments_and_blocking_event(self):
        pull = make_pull()
        finding = make_finding(line=13)
        event = CommentPoster(pull).post_review(
            summary="summary",
            findings=[finding],
            commentable_map={"app/auth.py": {13, 14}},
            blocking=True,
        )
        assert event == REQUEST_CHANGES
        method, url = pull._requester.requestJsonAndCheck.call_args.args
        payload = pull._requester.requestJsonAndCheck.call_args.kwargs["input"]
        assert method == "POST"
        assert url.endswith("/pulls/7/reviews")
        assert payload["event"] == REQUEST_CHANGES
        assert payload["comments"] == [
            {
                "path": "app/auth.py",
                "line": 13,
                "side": "RIGHT",
                "body": finding.comment_body(),
            }
        ]

    def test_non_blocking_uses_comment_event(self):
        pull = make_pull()
        event = CommentPoster(pull).post_review(
            summary="s", findings=[], commentable_map={}, blocking=False
        )
        assert event == COMMENT

    def test_findings_outside_diff_not_inlined(self):
        pull = make_pull()
        CommentPoster(pull).post_review(
            summary="s",
            findings=[make_finding(line=99)],  # 99 not in the diff
            commentable_map={"app/auth.py": {13}},
            blocking=False,
        )
        payload = pull._requester.requestJsonAndCheck.call_args.kwargs["input"]
        assert "comments" not in payload  # demoted to summary only

    def test_own_pr_request_changes_falls_back_to_comment(self):
        pull = make_pull()
        pull._requester.requestJsonAndCheck.side_effect = [
            GithubException(
                422, {"message": "Can not request changes on your own pull request"}
            ),
            ({}, {}),
        ]
        event = CommentPoster(pull).post_review(
            summary="s",
            findings=[make_finding(line=13)],
            commentable_map={"app/auth.py": {13}},
            blocking=True,
        )
        assert event == COMMENT
        assert pull._requester.requestJsonAndCheck.call_count == 2

    def test_invalid_comments_fall_back_to_summary_only(self):
        pull = make_pull()
        pull._requester.requestJsonAndCheck.side_effect = [
            GithubException(422, {"message": "Validation Failed: line not in diff"}),
            ({}, {}),
        ]
        CommentPoster(pull).post_review(
            summary="s",
            findings=[make_finding(line=13)],
            commentable_map={"app/auth.py": {13}},
            blocking=False,
        )
        second_payload = (
            pull._requester.requestJsonAndCheck.call_args_list[1].kwargs["input"]
        )
        assert "comments" not in second_payload
        assert second_payload["body"] == "s"

    def test_forbidden_raises_post_error(self):
        pull = make_pull()
        pull._requester.requestJsonAndCheck.side_effect = GithubException(
            403, {"message": "Resource not accessible by integration"}
        )
        with pytest.raises(PostError, match="403"):
            CommentPoster(pull).post_review(
                summary="s", findings=[], commentable_map={}, blocking=False
            )


class TestFindingCommentBody:
    def test_body_includes_severity_and_fix(self):
        finding = make_finding(suggestion="add a None check")
        body = finding.comment_body()
        assert body.startswith("🔴 **Bug** — user may be None")
        assert "**Fix:** add a None check" in body

    def test_body_without_suggestion(self):
        body = make_finding(severity="warning").comment_body()
        assert body.startswith("🟡 **Warning** —")
        assert "**Fix:**" not in body
