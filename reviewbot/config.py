"""Load and validate reviewbot.yml configuration.

Missing file → sensible defaults (review works with zero config).
Invalid file → ConfigError with a message that says exactly what's wrong.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

DEFAULT_MODEL = "google/gemma-4-31b-it:free"
DEFAULT_CONFIG_FILENAME = "reviewbot.yml"


class ConfigError(Exception):
    """Raised when reviewbot.yml exists but cannot be parsed or validated."""


class ReviewSettings(BaseModel):
    categories: list[str] = Field(
        default_factory=lambda: ["bugs", "security", "error_handling", "code_quality"]
    )
    block_merge_on: list[str] = Field(default_factory=lambda: ["bug", "security"])
    ignore: list[str] = Field(
        default_factory=lambda: ["*.md", "tests/**", "migrations/**"]
    )
    max_files_per_pr: int = Field(default=20, gt=0)
    max_lines_per_file: int = Field(default=400, gt=0)


class ReviewBotConfig(BaseModel):
    model: str = DEFAULT_MODEL
    review: ReviewSettings = Field(default_factory=ReviewSettings)

    def is_ignored(self, path: str) -> bool:
        """True if a file path matches any ignore glob.

        Matches both the full path and the basename so that `*.md` ignores
        `docs/guide.md`, and `tests/**` ignores everything under tests/.
        """
        for pattern in self.review.ignore:
            if fnmatch.fnmatch(path, pattern):
                return True
            if fnmatch.fnmatch(Path(path).name, pattern):
                return True
        return False


def load_config(path: str | Path | None = None) -> ReviewBotConfig:
    """Load config from reviewbot.yml, falling back to defaults if absent."""
    config_path = Path(path) if path else Path.cwd() / DEFAULT_CONFIG_FILENAME

    if not config_path.exists():
        return ReviewBotConfig()

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {config_path}: {exc}") from exc

    if raw is None:  # empty file
        return ReviewBotConfig()
    if not isinstance(raw, dict):
        raise ConfigError(
            f"Top level of {config_path} must be a mapping, got {type(raw).__name__}"
        )

    try:
        return ReviewBotConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors()
        )
        raise ConfigError(f"Invalid config in {config_path}: {problems}") from exc
