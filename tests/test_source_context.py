"""Tests for reviewbot.source_context — reading and widening on-disk source."""

from reviewbot.source_context import enclosing_context, extract_imports, read_source

SMALL_FILE = """import os
from app.db import get_user

def login(session, uid):
    user = get_user(session, uid)
    return user.email
""".rstrip("\n")

BIG_FILE = "\n".join(
    ["import os", ""]
    + [f"def fn_{i}():\n    x = {i}\n    return x\n" for i in range(60)]
)


class TestReadSource:
    def test_reads_existing_file(self, tmp_path):
        (tmp_path / "a.py").write_text("print(1)\n")
        assert read_source(str(tmp_path), "a.py") == "print(1)\n"

    def test_missing_file_returns_none(self, tmp_path):
        assert read_source(str(tmp_path), "nope.py") is None

    def test_missing_root_returns_none(self):
        assert read_source(None, "a.py") is None

    def test_path_traversal_refused(self, tmp_path):
        assert read_source(str(tmp_path), "../../etc/passwd") is None

    def test_absolute_path_refused(self, tmp_path):
        assert read_source(str(tmp_path), "/etc/passwd") is None


class TestEnclosingContext:
    def test_small_file_returns_whole_file_numbered(self):
        ctx = enclosing_context(SMALL_FILE, {5}, max_lines=400)
        assert "  4 |   def login(session, uid):" in ctx
        assert "  5 | >     user = get_user(session, uid)" in ctx  # changed line marked
        assert "  6 |       return user.email" in ctx

    def test_large_file_widens_to_enclosing_def_only(self):
        # change is inside fn_30's body; we should see its def, not fn_0 or fn_59
        lines = BIG_FILE.split("\n")
        target = next(i for i, l in enumerate(lines, 1) if l == "def fn_30():")
        ctx = enclosing_context(BIG_FILE, {target + 1}, max_lines=20)
        assert "def fn_30():" in ctx
        assert "def fn_0():" not in ctx
        assert "def fn_59():" not in ctx

    def test_no_header_falls_back_to_window(self):
        src = "\n".join(f"x{i} = {i}" for i in range(100))
        ctx = enclosing_context(src, {50}, max_lines=10)
        assert "x50 = 50" in ctx
        assert len(ctx.splitlines()) == 10


class TestExtractImports:
    def test_python_imports(self):
        out = extract_imports(SMALL_FILE)
        assert "import os" in out
        assert "from app.db import get_user" in out
        assert "def login" not in out

    def test_no_imports_returns_empty(self):
        assert extract_imports("def f():\n    return 1\n") == ""
