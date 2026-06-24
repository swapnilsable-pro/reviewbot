"""Core data models: findings, file hunks, and review results.

Pydantic models are used throughout so that LLM JSON output is validated
with the same machinery as everything else.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator


class Severity(str, Enum):
    """How serious a finding is. Drives emoji, grouping, and merge blocking."""

    BUG = "bug"
    WARNING = "warning"
    SUGGESTION = "suggestion"

    @property
    def emoji(self) -> str:
        return {
            Severity.BUG: "🔴",
            Severity.WARNING: "🟡",
            Severity.SUGGESTION: "🔵",
        }[self]

    @property
    def label(self) -> str:
        return {
            Severity.BUG: "Bug",
            Severity.WARNING: "Warning",
            Severity.SUGGESTION: "Suggestion",
        }[self]


class Finding(BaseModel):
    """A single review finding tied to a line in a changed file."""

    line: int = Field(gt=0, description="Line number in the new version of the file")
    severity: Severity
    category: str = Field(min_length=1)
    message: str = Field(min_length=1)
    suggestion: str | None = None
    # Filled in by the runner; not part of the LLM response schema.
    path: str = ""

    @field_validator("severity", mode="before")
    @classmethod
    def _normalize_severity(cls, v: object) -> object:
        """Tolerate common LLM variations like 'Bug', 'BUG', 'warn'."""
        if isinstance(v, str):
            lowered = v.strip().lower()
            aliases = {
                "bugs": "bug",
                "error": "bug",
                "critical": "bug",
                "warn": "warning",
                "warnings": "warning",
                "suggestions": "suggestion",
                "info": "suggestion",
                "nit": "suggestion",
            }
            return aliases.get(lowered, lowered)
        return v

    @field_validator("category", mode="before")
    @classmethod
    def _normalize_category(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip().lower().replace(" ", "_").replace("-", "_")
        return v

    def comment_body(self) -> str:
        """Render the inline GitHub comment markdown for this finding."""
        body = f"{self.severity.emoji} **{self.severity.label}** — {self.message}"
        if self.suggestion:
            body += f"\n\n**Fix:** {self.suggestion}"
        return body


class FileHunk(BaseModel):
    """A changed file's diff, annotated for the LLM, plus line metadata."""

    path: str
    annotated_diff: str = Field(
        description="Diff where each added/context line is prefixed with its "
        "line number in the new file, e.g. '  42 + user = get_user()'"
    )
    commentable_lines: set[int] = Field(
        default_factory=set,
        description="New-file line numbers that GitHub accepts inline comments on "
        "(added lines in the diff)",
    )
    is_new_file: bool = False
    is_truncated: bool = False
    added_line_count: int = 0


class FileReview(BaseModel):
    """Outcome of reviewing one file."""

    path: str
    findings: list[Finding] = Field(default_factory=list)
    skipped: bool = False
    skip_reason: str | None = None


class ReviewResult(BaseModel):
    """Aggregate outcome of reviewing a whole PR."""

    file_reviews: list[FileReview] = Field(default_factory=list)
    files_reviewed: int = 0
    files_skipped: int = 0
    model: str = ""

    @property
    def findings(self) -> list[Finding]:
        return [f for fr in self.file_reviews for f in fr.findings]

    def count(self, severity: Severity) -> int:
        return sum(1 for f in self.findings if f.severity == severity)

    def blocking_findings(self, block_merge_on: list[str]) -> list[Finding]:
        """Findings whose severity OR category matches the block list."""
        blockers = {b.strip().lower() for b in block_merge_on}
        return [
            f
            for f in self.findings
            if f.severity.value in blockers or f.category in blockers
        ]
