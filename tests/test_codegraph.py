# tests/test_codegraph.py
"""Tests for reviewbot.codegraph — pure-Python definition/caller resolution."""

from reviewbot.codegraph import build_codegraph


def _tree(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "db.py").write_text(
        "def get_user(session, uid):\n"
        "    u = session.get(User, uid)\n"
        "    if u is None:\n"
        "        raise NotFound(uid)\n"
        "    return u\n"
    )
    (tmp_path / "app" / "auth.py").write_text(
        "from app.db import get_user\n\n"
        "def login(session, uid):\n"
        "    user = get_user(session, uid)\n"
        "    return user.email\n"
    )
    return str(tmp_path)


class TestRelatedDefinitions:
    def test_resolves_callee_signature(self, tmp_path):
        graph = build_codegraph(_tree(tmp_path))
        out = graph.related_definitions(
            "app/auth.py", "user = get_user(session, uid)", budget_lines=20
        )
        assert "get_user" in out
        assert "app/db.py" in out
        assert "raise NotFound" in out  # body shown proves the guard exists

    def test_unknown_symbol_omitted(self, tmp_path):
        graph = build_codegraph(_tree(tmp_path))
        out = graph.related_definitions("app/auth.py", "frobnicate(x)", budget_lines=20)
        assert "frobnicate" not in out


class TestAffectedCallers:
    def test_finds_caller_of_changed_function(self, tmp_path):
        graph = build_codegraph(_tree(tmp_path))
        out = graph.affected_callers("app/db.py", {"get_user"}, budget_lines=20)
        assert "app/auth.py" in out
        assert "get_user" in out


class TestBuild:
    def test_none_root_returns_none(self):
        assert build_codegraph(None) is None
