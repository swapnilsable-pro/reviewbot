"""ReviewRunner — orchestrates fetch → filter → parse → review → post.

Exit codes:
    0 — review completed, nothing blocking (including "nothing to review")
    1 — blocking findings (severity/category in `block_merge_on`) → CI fails
    2 — usage/config error (cannot determine PR, bad credentials, ...)

A single file failing to parse or review never aborts the run; it is skipped
and reported in the summary.
"""

from __future__ import annotations

import sys

from reviewbot.config import ReviewBotConfig
from reviewbot.fetcher import FetchError, PRFetcher, resolve_repo_and_pr
from reviewbot.models import FileHunk, FileReview, ReviewResult
from reviewbot.parser import DiffParseError, build_file_hunk
from reviewbot.poster import CommentPoster, PostError, build_summary
from reviewbot.reviewer import LLMReviewer


def _log(message: str) -> None:
    print(f"[reviewbot] {message}", file=sys.stderr, flush=True)


class ReviewRunner:
    def __init__(
        self,
        config: ReviewBotConfig,
        github_token: str,
        openrouter_api_key: str,
    ) -> None:
        self.config = config
        self._github_token = github_token
        self._openrouter_api_key = openrouter_api_key

    def run(
        self,
        repo: str | None = None,
        pr_number: int | None = None,
        dry_run: bool = False,
    ) -> int:
        try:
            repo, pr_number = resolve_repo_and_pr(repo, pr_number)
        except FetchError as exc:
            _log(f"error: {exc}")
            return 2

        fetcher = PRFetcher(self._github_token)
        try:
            pull = fetcher.get_pull(repo, pr_number)
            pr_data = fetcher.fetch_data(pull)
        except FetchError as exc:
            _log(f"error: {exc}")
            return 2

        _log(f"Reviewing {repo}#{pr_number}: {pr_data.title!r} "
             f"({len(pr_data.files)} changed files)")

        hunks, skipped_files = self._select_hunks(pr_data.files)
        if not hunks:
            _log("Nothing to review (all files ignored, deleted, or binary). Exiting 0.")
            return 0

        result, review_skips = self._review_all(hunks)
        skipped_files += review_skips

        blocking = result.blocking_findings(self.config.review.block_merge_on)
        summary = build_summary(result, blocking, skipped_files or None)
        commentable_map = {h.path: h.commentable_lines for h in hunks}

        _log(f"Findings: {len(result.findings)} total, {len(blocking)} blocking")

        if dry_run:
            print(summary)
            for finding in result.findings:
                print(f"\n--- {finding.path}:{finding.line} ---")
                print(finding.comment_body())
        else:
            try:
                event = CommentPoster(pull).post_review(
                    summary=summary,
                    findings=result.findings,
                    commentable_map=commentable_map,
                    blocking=bool(blocking),
                )
                _log(f"Posted review ({event}) with "
                     f"{len(result.findings)} findings.")
            except PostError as exc:
                # Don't crash the workflow over a posting failure — surface
                # the findings in the logs instead.
                _log(f"warning: could not post review to GitHub: {exc}")
                print(summary)

        return 1 if blocking else 0

    # -- internals -----------------------------------------------------------

    def _select_hunks(
        self, files: list
    ) -> tuple[list[FileHunk], list[tuple[str, str]]]:
        """Filter changed files down to reviewable hunks + skip reasons."""
        hunks: list[FileHunk] = []
        skipped: list[tuple[str, str]] = []
        max_files = self.config.review.max_files_per_pr

        for changed in files:
            if changed.status == "removed":
                continue  # nothing to review on a deleted file
            if self.config.is_ignored(changed.path):
                _log(f"skip {changed.path} (ignored by config)")
                continue
            if len(hunks) >= max_files:
                skipped.append(
                    (changed.path, f"PR exceeds max_files_per_pr ({max_files})")
                )
                continue
            try:
                hunk = build_file_hunk(
                    changed.path,
                    changed.patch,
                    max_lines=self.config.review.max_lines_per_file,
                    is_new_file=changed.status == "added",
                )
            except DiffParseError as exc:
                _log(f"skip {changed.path} (diff parse failed: {exc})")
                skipped.append((changed.path, "could not parse diff"))
                continue
            if hunk is None:
                continue  # binary or no added lines
            hunks.append(hunk)

        return hunks, skipped

    def _review_all(
        self, hunks: list[FileHunk]
    ) -> tuple[ReviewResult, list[tuple[str, str]]]:
        reviewer = LLMReviewer(
            api_key=self._openrouter_api_key,
            model=self.config.model,
            categories=self.config.review.categories,
        )
        file_reviews: list[FileReview] = []
        skipped: list[tuple[str, str]] = []
        try:
            for i, hunk in enumerate(hunks, 1):
                _log(f"reviewing {hunk.path} ({i}/{len(hunks)}) ...")
                try:
                    review = reviewer.review_file(hunk)
                except Exception as exc:  # noqa: BLE001 — belt and braces:
                    # review_file shouldn't raise, but one file must never
                    # kill the whole run.
                    review = FileReview(
                        path=hunk.path, skipped=True, skip_reason=str(exc)
                    )
                if review.skipped:
                    _log(f"skip {hunk.path} ({review.skip_reason})")
                    skipped.append((hunk.path, review.skip_reason or "LLM error"))
                else:
                    file_reviews.append(review)
        finally:
            reviewer.close()

        result = ReviewResult(
            file_reviews=file_reviews,
            files_reviewed=len(file_reviews),
            files_skipped=len(skipped),
            model=self.config.model,
        )
        return result, skipped
