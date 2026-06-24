"""Parse unified diffs into LLM-ready, line-annotated hunks.

The critical job here is the line-number mapping: every added/context line in
the annotated diff is prefixed with its line number in the *new* version of
the file, so the LLM can cite exact lines and GitHub inline comments
(`line` + `side="RIGHT"`) land in the right place.

Annotated diff format sent to the LLM:

      40   def get_user(session, user_id):       <- context line (unchanged)
      41 +     user = session.query(...)          <- added line
         -     return session.get(user_id)        <- removed line (no new number)
"""

from __future__ import annotations

from unidiff import PatchSet
from unidiff.errors import UnidiffParseError

from reviewbot.models import FileHunk


class DiffParseError(Exception):
    """Raised when a patch can't be parsed as a unified diff."""


def build_file_hunk(
    path: str,
    patch: str | None,
    max_lines: int = 400,
    is_new_file: bool = False,
) -> FileHunk | None:
    """Turn a GitHub per-file patch into an annotated FileHunk.

    Returns None when there is nothing reviewable (binary file, or a diff
    with no added lines such as a pure deletion).
    """
    if not patch or not patch.strip():
        return None  # binary file or empty patch

    patched_file = _parse_single_file_patch(path, patch)

    annotated_lines: list[str] = []
    commentable: set[int] = set()
    added_count = 0
    truncated = False

    for hunk in patched_file:
        header = (
            f"@@ -{hunk.source_start},{hunk.source_length} "
            f"+{hunk.target_start},{hunk.target_length} @@"
        )
        annotated_lines.append(header)
        for line in hunk:
            content = line.value.rstrip("\n")
            if line.is_added:
                annotated_lines.append(f"{line.target_line_no:>6} + {content}")
                commentable.add(line.target_line_no)
                added_count += 1
            elif line.is_context:
                annotated_lines.append(f"{line.target_line_no:>6}   {content}")
                commentable.add(line.target_line_no)
            elif line.is_removed:
                annotated_lines.append(f"       - {content}")

        if len(annotated_lines) >= max_lines:
            truncated = True
            break

    if added_count == 0:
        return None  # pure deletion / rename without edits — nothing to review

    if len(annotated_lines) > max_lines:
        annotated_lines = annotated_lines[:max_lines]
        truncated = True
    if truncated:
        annotated_lines.append("... (diff truncated)")

    return FileHunk(
        path=path,
        annotated_diff="\n".join(annotated_lines),
        commentable_lines=commentable,
        is_new_file=is_new_file,
        is_truncated=truncated,
        added_line_count=added_count,
    )


def _parse_single_file_patch(path: str, patch: str):
    """Parse one file's patch text (GitHub API style, headers optional)."""
    has_headers = patch.lstrip().startswith(("---", "diff --git"))
    text = patch if has_headers else f"--- a/{path}\n+++ b/{path}\n{patch}"
    if not text.endswith("\n"):
        text += "\n"
    try:
        patch_set = PatchSet(text)
    except UnidiffParseError as exc:
        raise DiffParseError(f"Could not parse diff for {path}: {exc}") from exc
    if not patch_set:
        raise DiffParseError(f"Diff for {path} contained no files")
    patched_file = patch_set[0]
    if len(patched_file) == 0:
        raise DiffParseError(f"Diff for {path} contained no hunks")
    return patched_file
