"""Tests for reviewbot.parser — the diff → line-number mapping is the core of
inline commenting, so these assertions are deliberately exact."""

import re
from pathlib import Path

import pytest
from unidiff import PatchSet

from reviewbot.parser import DiffParseError, build_file_hunk

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def sample_files():
    """Parse sample.diff into {path: per-file patch text} like GitHub gives us."""
    patch_set = PatchSet(
        (FIXTURES / "sample.diff").read_text(encoding="utf-8")
    )
    return {pf.path: str(pf) for pf in patch_set}


class TestLineMapping:
    def test_modified_file_added_lines_get_correct_numbers(self, sample_files):
        hunk = build_file_hunk("app/auth.py", sample_files["app/auth.py"])
        assert hunk is not None
        # First hunk: lines 13-14 are the added lines in the new file
        assert re.search(r"^\s*13 \+ \s*email = user\.email", hunk.annotated_diff, re.M)
        assert re.search(
            r"^\s*14 \+ \s*if user\.check_password", hunk.annotated_diff, re.M
        )
        # Second hunk: added lines are 46-49
        assert re.search(r"^\s*48 \+ \s*raise", hunk.annotated_diff, re.M)
        assert re.search(r"^\s*49 \+ \s*return True", hunk.annotated_diff, re.M)

    def test_context_lines_numbered_without_plus(self, sample_files):
        hunk = build_file_hunk("app/auth.py", sample_files["app/auth.py"])
        assert re.search(r"^\s*10   def login", hunk.annotated_diff, re.M)
        assert re.search(r"^\s*15   \s*return create_token", hunk.annotated_diff, re.M)

    def test_removed_lines_have_no_line_number(self, sample_files):
        hunk = build_file_hunk("app/auth.py", sample_files["app/auth.py"])
        removed = [
            l for l in hunk.annotated_diff.splitlines() if l.startswith("       - ")
        ]
        assert any("pass" in l for l in removed)
        assert not any(re.match(r"^\s*\d", l) for l in removed)

    def test_commentable_lines_cover_added_and_context(self, sample_files):
        hunk = build_file_hunk("app/auth.py", sample_files["app/auth.py"])
        # Added lines from both hunks
        assert {13, 14, 46, 47, 48, 49} <= hunk.commentable_lines
        # Context lines that appear in the diff
        assert {10, 11, 12, 15, 16, 43, 44, 45} <= hunk.commentable_lines
        # Lines outside the diff are not commentable
        assert 30 not in hunk.commentable_lines

    def test_added_line_count(self, sample_files):
        hunk = build_file_hunk("app/auth.py", sample_files["app/auth.py"])
        assert hunk.added_line_count == 6  # 2 in hunk one + 4 in hunk two


class TestFileKinds:
    def test_new_file_lines_start_at_one(self, sample_files):
        hunk = build_file_hunk(
            "app/utils.py", sample_files["app/utils.py"], is_new_file=True
        )
        assert hunk is not None
        assert hunk.is_new_file
        assert hunk.commentable_lines == {1, 2, 3, 4}
        assert hunk.added_line_count == 4
        assert re.search(r"^\s*4 \+ \s*return hashlib\.md5", hunk.annotated_diff, re.M)

    def test_deleted_file_returns_none(self, sample_files):
        assert build_file_hunk("old/legacy.py", sample_files["old/legacy.py"]) is None

    def test_binary_file_none_patch_returns_none(self):
        assert build_file_hunk("logo.png", None) is None
        assert build_file_hunk("logo.png", "") is None


class TestGitHubStylePatch:
    """GitHub's API gives per-file patches starting at @@ with no ---/+++ headers."""

    def test_headerless_patch_parses(self):
        patch = (
            "@@ -1,3 +1,4 @@\n"
            " import os\n"
            "+import sys\n"
            " \n"
            " print(os.getcwd())\n"
        )
        hunk = build_file_hunk("script.py", patch)
        assert hunk is not None
        assert 2 in hunk.commentable_lines
        assert re.search(r"^\s*2 \+ import sys", hunk.annotated_diff, re.M)

    def test_garbage_patch_raises(self):
        with pytest.raises(DiffParseError):
            build_file_hunk("x.py", "this is not a diff at all\njust text\n")


class TestTruncation:
    def test_large_diff_is_truncated(self):
        added = "\n".join(f"+line_{i} = {i}" for i in range(1, 51))
        patch = f"@@ -0,0 +1,50 @@\n{added}\n"
        hunk = build_file_hunk("big.py", patch, max_lines=10)
        assert hunk is not None
        assert hunk.is_truncated
        assert hunk.annotated_diff.splitlines()[-1] == "... (diff truncated)"
        assert len(hunk.annotated_diff.splitlines()) <= 12

    def test_small_diff_not_truncated(self, sample_files):
        hunk = build_file_hunk("app/auth.py", sample_files["app/auth.py"])
        assert not hunk.is_truncated
