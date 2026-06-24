"""Fetch PR metadata and changed files from the GitHub API.

Also resolves which repo/PR to review: explicit CLI flags first, then
environment variables, then the GitHub Actions event payload — so the bot
is zero-config inside Actions but still runnable locally.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from github import Auth, Github
from github.PullRequest import PullRequest
from pydantic import BaseModel, Field


class FetchError(Exception):
    """Raised when the PR to review cannot be determined or fetched."""


class ChangedFile(BaseModel):
    path: str
    patch: str | None = None  # None for binary files
    status: str = "modified"  # added | modified | removed | renamed
    additions: int = 0
    deletions: int = 0


class PRData(BaseModel):
    repo_full_name: str
    number: int
    title: str = ""
    head_sha: str = ""
    files: list[ChangedFile] = Field(default_factory=list)


def resolve_repo_and_pr(
    repo: str | None = None, pr_number: int | None = None
) -> tuple[str, int]:
    """Figure out which repo + PR to review.

    Order: explicit args → env vars → GitHub Actions event payload.
    """
    repo = repo or os.environ.get("GITHUB_REPOSITORY", "").strip() or None
    if repo is None:
        raise FetchError(
            "Cannot determine repository. Pass --repo owner/name or set "
            "GITHUB_REPOSITORY (set automatically in GitHub Actions)."
        )

    if pr_number is None:
        env_pr = os.environ.get("REVIEWBOT_PR_NUMBER", "").strip()
        if env_pr:
            try:
                pr_number = int(env_pr)
            except ValueError:
                raise FetchError(f"REVIEWBOT_PR_NUMBER is not a number: {env_pr!r}")

    if pr_number is None:
        pr_number = _pr_number_from_event_payload()

    if pr_number is None:
        raise FetchError(
            "Cannot determine PR number. Pass --pr N, set REVIEWBOT_PR_NUMBER, "
            "or run inside a GitHub Actions `pull_request` event."
        )
    return repo, pr_number


def _pr_number_from_event_payload() -> int | None:
    """Read the PR number from the GitHub Actions event payload, if present."""
    event_path = os.environ.get("GITHUB_EVENT_PATH", "").strip()
    if not event_path or not Path(event_path).exists():
        return None
    try:
        payload = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    pull_request = payload.get("pull_request")
    if isinstance(pull_request, dict) and isinstance(pull_request.get("number"), int):
        return pull_request["number"]
    # `number` is set on pull_request events at the top level too
    if isinstance(payload.get("number"), int):
        return payload["number"]
    return None


class PRFetcher:
    """Thin wrapper around PyGithub for fetching PR diffs and metadata."""

    def __init__(self, github_token: str) -> None:
        self._github = Github(auth=Auth.Token(github_token))

    def get_pull(self, repo_full_name: str, pr_number: int) -> PullRequest:
        try:
            return self._github.get_repo(repo_full_name).get_pull(pr_number)
        except Exception as exc:  # noqa: BLE001 — surface a single clean error type
            raise FetchError(
                f"Failed to fetch PR #{pr_number} from {repo_full_name}: {exc}"
            ) from exc

    def fetch_data(self, pull: PullRequest) -> PRData:
        files = [
            ChangedFile(
                path=f.filename,
                patch=f.patch,
                status=f.status,
                additions=f.additions,
                deletions=f.deletions,
            )
            for f in pull.get_files()
        ]
        return PRData(
            repo_full_name=pull.base.repo.full_name,
            number=pull.number,
            title=pull.title or "",
            head_sha=pull.head.sha,
            files=files,
        )
