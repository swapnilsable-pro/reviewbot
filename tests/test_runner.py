"""End-to-end runner tests with GitHub and the LLM fully mocked."""

import pytest

from reviewbot.config import ReviewBotConfig
from reviewbot.fetcher import ChangedFile, FetchError, PRData
from reviewbot.models import FileReview, Finding
from reviewbot.poster import PostError
from reviewbot.runner import ReviewRunner

SOURCE_PATCH = (
    "@@ -1,2 +1,3 @@\n"
    " def divide(a, b):\n"
    "+    return a / b\n"
    "     # end\n"
)


def make_pr_data(files) -> PRData:
    return PRData(
        repo_full_name="acme/widgets",
        number=7,
        title="Add divide",
        head_sha="abc123",
        files=files,
    )


@pytest.fixture
def runner():
    return ReviewRunner(
        config=ReviewBotConfig(),
        github_token="gh-token",
        openrouter_api_key="or-key",
    )


@pytest.fixture
def mock_fetcher(mocker):
    fetcher_cls = mocker.patch("reviewbot.runner.PRFetcher")
    instance = fetcher_cls.return_value
    instance.get_pull.return_value = mocker.MagicMock()
    return instance


@pytest.fixture
def mock_reviewer(mocker):
    reviewer_cls = mocker.patch("reviewbot.runner.LLMReviewer")
    return reviewer_cls


def reviewer_returning(mock_reviewer, findings_by_path):
    def review_file(hunk):
        return FileReview(
            path=hunk.path, findings=findings_by_path.get(hunk.path, [])
        )

    mock_reviewer.return_value.review_file.side_effect = review_file
    return mock_reviewer


class TestExitCodes:
    def test_blocking_bug_exits_1(self, runner, mock_fetcher, mock_reviewer):
        mock_fetcher.fetch_data.return_value = make_pr_data(
            [ChangedFile(path="app/math.py", patch=SOURCE_PATCH)]
        )
        reviewer_returning(
            mock_reviewer,
            {
                "app/math.py": [
                    Finding(
                        path="app/math.py", line=2, severity="bug",
                        category="bugs", message="division by zero when b == 0",
                    )
                ]
            },
        )
        assert runner.run(repo="acme/widgets", pr_number=7, dry_run=True) == 1

    def test_only_suggestions_exit_0(self, runner, mock_fetcher, mock_reviewer):
        mock_fetcher.fetch_data.return_value = make_pr_data(
            [ChangedFile(path="app/math.py", patch=SOURCE_PATCH)]
        )
        reviewer_returning(
            mock_reviewer,
            {
                "app/math.py": [
                    Finding(
                        path="app/math.py", line=2, severity="suggestion",
                        category="code_quality", message="add a docstring",
                    )
                ]
            },
        )
        assert runner.run(repo="acme/widgets", pr_number=7, dry_run=True) == 0

    def test_security_category_blocks(self, runner, mock_fetcher, mock_reviewer):
        mock_fetcher.fetch_data.return_value = make_pr_data(
            [ChangedFile(path="app/math.py", patch=SOURCE_PATCH)]
        )
        reviewer_returning(
            mock_reviewer,
            {
                "app/math.py": [
                    Finding(
                        path="app/math.py", line=2, severity="warning",
                        category="security", message="possible injection",
                    )
                ]
            },
        )
        assert runner.run(repo="acme/widgets", pr_number=7, dry_run=True) == 1

    def test_fetch_error_exits_2(self, runner, mock_fetcher):
        mock_fetcher.get_pull.side_effect = FetchError("PR not found")
        assert runner.run(repo="acme/widgets", pr_number=7) == 2

    def test_unresolvable_pr_exits_2(self, runner, monkeypatch):
        for var in ("GITHUB_REPOSITORY", "REVIEWBOT_PR_NUMBER", "GITHUB_EVENT_PATH"):
            monkeypatch.delenv(var, raising=False)
        assert runner.run() == 2


class TestFiltering:
    def test_markdown_only_pr_skips_llm_and_exits_0(
        self, runner, mock_fetcher, mock_reviewer, capsys
    ):
        mock_fetcher.fetch_data.return_value = make_pr_data(
            [
                ChangedFile(path="README.md", patch=SOURCE_PATCH),
                ChangedFile(path="docs/guide.md", patch=SOURCE_PATCH),
                ChangedFile(path="tests/test_x.py", patch=SOURCE_PATCH),
            ]
        )
        assert runner.run(repo="acme/widgets", pr_number=7) == 0
        mock_reviewer.assert_not_called()

    def test_removed_and_binary_files_skipped(
        self, runner, mock_fetcher, mock_reviewer
    ):
        mock_fetcher.fetch_data.return_value = make_pr_data(
            [
                ChangedFile(path="old.py", patch="@@ -1 +0,0 @@\n-gone\n",
                            status="removed"),
                ChangedFile(path="logo.png", patch=None),
            ]
        )
        assert runner.run(repo="acme/widgets", pr_number=7) == 0
        mock_reviewer.assert_not_called()

    def test_max_files_per_pr_enforced(self, runner, mock_fetcher, mock_reviewer):
        runner.config = ReviewBotConfig.model_validate(
            {"review": {"max_files_per_pr": 2}}
        )
        files = [
            ChangedFile(path=f"app/m{i}.py", patch=SOURCE_PATCH) for i in range(5)
        ]
        mock_fetcher.fetch_data.return_value = make_pr_data(files)
        reviewer_returning(mock_reviewer, {})
        runner.run(repo="acme/widgets", pr_number=7, dry_run=True)
        assert mock_reviewer.return_value.review_file.call_count == 2


class TestResilience:
    def test_all_files_skipped_by_llm_exits_0(
        self, runner, mock_fetcher, mock_reviewer, capsys
    ):
        mock_fetcher.fetch_data.return_value = make_pr_data(
            [ChangedFile(path="app/math.py", patch=SOURCE_PATCH)]
        )
        mock_reviewer.return_value.review_file.return_value = FileReview(
            path="app/math.py", skipped=True, skip_reason="LLM unavailable"
        )
        assert runner.run(repo="acme/widgets", pr_number=7, dry_run=True) == 0
        assert "Skipped files" in capsys.readouterr().out

    def test_reviewer_exception_does_not_crash_run(
        self, runner, mock_fetcher, mock_reviewer
    ):
        mock_fetcher.fetch_data.return_value = make_pr_data(
            [ChangedFile(path="app/math.py", patch=SOURCE_PATCH)]
        )
        mock_reviewer.return_value.review_file.side_effect = RuntimeError("boom")
        assert runner.run(repo="acme/widgets", pr_number=7, dry_run=True) == 0

    def test_post_error_logged_not_fatal(
        self, runner, mock_fetcher, mock_reviewer, mocker, capsys
    ):
        mock_fetcher.fetch_data.return_value = make_pr_data(
            [ChangedFile(path="app/math.py", patch=SOURCE_PATCH)]
        )
        reviewer_returning(
            mock_reviewer,
            {
                "app/math.py": [
                    Finding(
                        path="app/math.py", line=2, severity="bug",
                        category="bugs", message="bad",
                    )
                ]
            },
        )
        mocker.patch(
            "reviewbot.runner.CommentPoster"
        ).return_value.post_review.side_effect = PostError("403")
        # Posting failed but findings exist → still exit 1, summary in stdout
        assert runner.run(repo="acme/widgets", pr_number=7) == 1
        assert "ReviewBot Summary" in capsys.readouterr().out


class TestDryRun:
    def test_dry_run_prints_summary_and_comments(
        self, runner, mock_fetcher, mock_reviewer, capsys
    ):
        mock_fetcher.fetch_data.return_value = make_pr_data(
            [ChangedFile(path="app/math.py", patch=SOURCE_PATCH)]
        )
        reviewer_returning(
            mock_reviewer,
            {
                "app/math.py": [
                    Finding(
                        path="app/math.py", line=2, severity="bug",
                        category="bugs", message="division by zero",
                        suggestion="guard b == 0",
                    )
                ]
            },
        )
        runner.run(repo="acme/widgets", pr_number=7, dry_run=True)
        out = capsys.readouterr().out
        assert "ReviewBot Summary" in out
        assert "app/math.py:2 — division by zero" in out
        assert "🔴 **Bug** — division by zero" in out
