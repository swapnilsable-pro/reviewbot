"""Tests for reviewbot.config — defaults, YAML loading, validation, ignore globs."""

import pytest

from reviewbot.config import (
    DEFAULT_MODEL,
    ConfigError,
    ReviewBotConfig,
    load_config,
)


class TestDefaults:
    def test_missing_file_returns_defaults(self, tmp_path):
        config = load_config(tmp_path / "reviewbot.yml")
        assert config.model == DEFAULT_MODEL
        assert config.review.max_files_per_pr == 20
        assert config.review.max_lines_per_file == 400
        assert "bugs" in config.review.categories
        assert config.review.block_merge_on == ["bug", "security"]

    def test_empty_file_returns_defaults(self, tmp_path):
        path = tmp_path / "reviewbot.yml"
        path.write_text("")
        config = load_config(path)
        assert config.model == DEFAULT_MODEL


class TestLoading:
    def test_full_config_parses(self, tmp_path):
        path = tmp_path / "reviewbot.yml"
        path.write_text(
            """
model: some/other-model
review:
  categories: [bugs, security]
  block_merge_on: [bug]
  ignore: ["*.lock"]
  max_files_per_pr: 5
  max_lines_per_file: 100
"""
        )
        config = load_config(path)
        assert config.model == "some/other-model"
        assert config.review.categories == ["bugs", "security"]
        assert config.review.block_merge_on == ["bug"]
        assert config.review.max_files_per_pr == 5

    def test_partial_config_keeps_other_defaults(self, tmp_path):
        path = tmp_path / "reviewbot.yml"
        path.write_text("review:\n  max_files_per_pr: 3\n")
        config = load_config(path)
        assert config.review.max_files_per_pr == 3
        assert config.model == DEFAULT_MODEL
        assert config.review.max_lines_per_file == 400

    def test_invalid_yaml_raises_config_error(self, tmp_path):
        path = tmp_path / "reviewbot.yml"
        path.write_text("model: [unclosed")
        with pytest.raises(ConfigError, match="Invalid YAML"):
            load_config(path)

    def test_wrong_type_raises_config_error(self, tmp_path):
        path = tmp_path / "reviewbot.yml"
        path.write_text("review:\n  max_files_per_pr: not_a_number\n")
        with pytest.raises(ConfigError, match="max_files_per_pr"):
            load_config(path)

    def test_non_mapping_top_level_raises(self, tmp_path):
        path = tmp_path / "reviewbot.yml"
        path.write_text("- just\n- a\n- list\n")
        with pytest.raises(ConfigError, match="must be a mapping"):
            load_config(path)


class TestIgnoreGlobs:
    @pytest.fixture
    def config(self):
        return ReviewBotConfig()

    @pytest.mark.parametrize(
        "path",
        [
            "README.md",
            "docs/guide.md",
            "tests/test_app.py",
            "tests/unit/test_deep.py",
            "migrations/0001_initial.py",
        ],
    )
    def test_default_ignores_match(self, config, path):
        assert config.is_ignored(path) is True

    @pytest.mark.parametrize(
        "path",
        ["app/auth.py", "src/main.js", "reviewbot/parser.py", "testsuite/run.py"],
    )
    def test_source_files_not_ignored(self, config, path):
        assert config.is_ignored(path) is False

    def test_custom_ignore(self):
        config = ReviewBotConfig.model_validate(
            {"review": {"ignore": ["vendor/**", "*.gen.go"]}}
        )
        assert config.is_ignored("vendor/lib/x.go")
        assert config.is_ignored("api/types.gen.go")
        assert not config.is_ignored("api/types.go")
