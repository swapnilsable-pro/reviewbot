"""Tests for reviewbot.fetcher — repo/PR resolution from flags, env, and the
GitHub Actions event payload. No real API calls."""

import json

import pytest

from reviewbot.fetcher import FetchError, resolve_repo_and_pr


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("GITHUB_REPOSITORY", "REVIEWBOT_PR_NUMBER", "GITHUB_EVENT_PATH"):
        monkeypatch.delenv(var, raising=False)


class TestResolveRepoAndPr:
    def test_explicit_args_win(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOSITORY", "env/repo")
        monkeypatch.setenv("REVIEWBOT_PR_NUMBER", "99")
        assert resolve_repo_and_pr("cli/repo", 7) == ("cli/repo", 7)

    def test_env_vars_used_as_fallback(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
        monkeypatch.setenv("REVIEWBOT_PR_NUMBER", "42")
        assert resolve_repo_and_pr() == ("acme/widgets", 42)

    def test_event_payload_provides_pr_number(self, monkeypatch, tmp_path):
        event = tmp_path / "event.json"
        event.write_text(json.dumps({"pull_request": {"number": 123}}))
        monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
        monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
        assert resolve_repo_and_pr() == ("acme/widgets", 123)

    def test_event_payload_top_level_number(self, monkeypatch, tmp_path):
        event = tmp_path / "event.json"
        event.write_text(json.dumps({"number": 55}))
        monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
        monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
        assert resolve_repo_and_pr() == ("acme/widgets", 55)

    def test_missing_repo_raises(self):
        with pytest.raises(FetchError, match="repository"):
            resolve_repo_and_pr(pr_number=1)

    def test_missing_pr_raises(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
        with pytest.raises(FetchError, match="PR number"):
            resolve_repo_and_pr()

    def test_bad_env_pr_number_raises(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
        monkeypatch.setenv("REVIEWBOT_PR_NUMBER", "abc")
        with pytest.raises(FetchError, match="not a number"):
            resolve_repo_and_pr()

    def test_corrupt_event_payload_ignored(self, monkeypatch, tmp_path):
        event = tmp_path / "event.json"
        event.write_text("{not json")
        monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
        monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
        with pytest.raises(FetchError, match="PR number"):
            resolve_repo_and_pr()
