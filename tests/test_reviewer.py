"""Tests for reviewbot.reviewer — all OpenRouter calls are mocked with
httpx.MockTransport. Backoff sleeps are patched out."""

import json

import httpx
import pytest

from reviewbot.models import FileHunk, Severity
from reviewbot.reviewer import (
    LLMError,
    LLMReviewer,
    extract_json_array,
)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("reviewbot.reviewer.time.sleep", lambda *_: None)


def make_hunk(**overrides) -> FileHunk:
    defaults = dict(
        path="app/auth.py",
        annotated_diff="    13 +     email = user.email",
        commentable_lines={13, 14},
        added_line_count=1,
    )
    defaults.update(overrides)
    return FileHunk(**defaults)


def llm_response(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def make_reviewer(handler, **kwargs) -> LLMReviewer:
    reviewer = LLMReviewer(api_key="test-key", model="test/model", **kwargs)
    reviewer._client = httpx.Client(transport=httpx.MockTransport(handler))
    return reviewer


VALID_FINDINGS = json.dumps(
    [
        {
            "line": 13,
            "severity": "bug",
            "category": "bugs",
            "message": "user may be None",
            "suggestion": "guard with `if user is None`",
        }
    ]
)


class TestExtractJsonArray:
    def test_plain_array(self):
        assert extract_json_array('[{"line": 1}]') == [{"line": 1}]

    def test_empty_array(self):
        assert extract_json_array("[]") == []

    def test_fenced_json(self):
        assert extract_json_array('```json\n[{"line": 2}]\n```') == [{"line": 2}]

    def test_array_with_prose_around_it(self):
        text = 'Here are the findings:\n[{"line": 3}]\nHope that helps!'
        assert extract_json_array(text) == [{"line": 3}]

    def test_wrapped_in_findings_object(self):
        assert extract_json_array('{"findings": [{"line": 4}]}') == [{"line": 4}]

    def test_no_json_raises(self):
        with pytest.raises(LLMError):
            extract_json_array("I could not find any issues, great code!")

    def test_malformed_array_raises(self):
        with pytest.raises(LLMError):
            extract_json_array('[{"line": 1,]')


class TestReviewFile:
    def test_valid_response_produces_findings(self):
        def handler(request):
            return httpx.Response(200, json=llm_response(VALID_FINDINGS))

        review = make_reviewer(handler).review_file(make_hunk())
        assert not review.skipped
        assert len(review.findings) == 1
        finding = review.findings[0]
        assert finding.line == 13
        assert finding.severity == Severity.BUG
        assert finding.path == "app/auth.py"
        assert finding.suggestion.startswith("guard")

    def test_fenced_response_parses(self):
        def handler(request):
            return httpx.Response(
                200, json=llm_response(f"```json\n{VALID_FINDINGS}\n```")
            )

        review = make_reviewer(handler).review_file(make_hunk())
        assert not review.skipped
        assert len(review.findings) == 1

    def test_malformed_json_retries_then_succeeds(self):
        calls = []

        def handler(request):
            calls.append(json.loads(request.content))
            if len(calls) < 3:
                return httpx.Response(200, json=llm_response("sorry, no JSON here"))
            return httpx.Response(200, json=llm_response(VALID_FINDINGS))

        review = make_reviewer(handler).review_file(make_hunk())
        assert not review.skipped
        assert len(review.findings) == 1
        assert len(calls) == 3
        # Retry prompt should include the nudge message
        assert "ONLY the JSON array" in calls[1]["messages"][-1]["content"]

    def test_malformed_json_exhausts_retries_and_skips(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(200, json=llm_response("still not json"))

        review = make_reviewer(handler).review_file(make_hunk())
        assert review.skipped
        assert "malformed JSON" in review.skip_reason
        assert len(calls) == 3  # 1 + max_json_retries(2)

    def test_rate_limit_backs_off_then_succeeds(self):
        calls = []

        def handler(request):
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(429, text="rate limited")
            return httpx.Response(200, json=llm_response(VALID_FINDINGS))

        review = make_reviewer(handler).review_file(make_hunk())
        assert not review.skipped
        assert len(calls) == 2

    def test_persistent_server_error_skips_file(self):
        def handler(request):
            return httpx.Response(500, text="boom")

        review = make_reviewer(handler).review_file(make_hunk())
        assert review.skipped
        assert "unavailable" in review.skip_reason

    def test_bad_api_key_skips_with_clear_reason(self):
        def handler(request):
            return httpx.Response(401, text="unauthorized")

        review = make_reviewer(handler).review_file(make_hunk())
        assert review.skipped
        assert "OPENROUTER_API_KEY" in review.skip_reason

    def test_network_error_retries_then_skips(self):
        def handler(request):
            raise httpx.ConnectError("connection refused")

        review = make_reviewer(handler).review_file(make_hunk())
        assert review.skipped

    def test_empty_findings_array_is_clean_review(self):
        def handler(request):
            return httpx.Response(200, json=llm_response("[]"))

        review = make_reviewer(handler).review_file(make_hunk())
        assert not review.skipped
        assert review.findings == []


class TestFindingValidation:
    def test_invalid_items_dropped_valid_kept(self):
        content = json.dumps(
            [
                {"line": 13, "severity": "bug", "category": "bugs", "message": "real"},
                {"severity": "bug", "category": "bugs", "message": "no line"},
                {"line": 0, "severity": "bug", "category": "bugs", "message": "bad line"},
                "not even a dict",
                {"line": 14, "severity": "nonsense", "category": "bugs", "message": "x"},
            ]
        )

        def handler(request):
            return httpx.Response(200, json=llm_response(content))

        review = make_reviewer(handler).review_file(make_hunk())
        assert not review.skipped
        assert [f.line for f in review.findings] == [13]

    def test_severity_aliases_normalized(self):
        content = json.dumps(
            [
                {"line": 13, "severity": "Bug", "category": "bugs", "message": "a"},
                {"line": 14, "severity": "warn", "category": "style", "message": "b"},
            ]
        )

        def handler(request):
            return httpx.Response(200, json=llm_response(content))

        review = make_reviewer(handler).review_file(make_hunk())
        severities = {f.line: f.severity for f in review.findings}
        assert severities[13] == Severity.BUG
        assert severities[14] == Severity.WARNING

    def test_findings_capped_and_bugs_first(self):
        items = [
            {"line": i, "severity": "suggestion", "category": "style", "message": f"s{i}"}
            for i in range(1, 15)
        ]
        items.append(
            {"line": 99, "severity": "bug", "category": "bugs", "message": "the bug"}
        )

        def handler(request):
            return httpx.Response(200, json=llm_response(json.dumps(items)))

        review = make_reviewer(handler).review_file(make_hunk())
        assert len(review.findings) == 10
        assert review.findings[0].severity == Severity.BUG


class TestPing:
    def test_ping_returns_reply(self):
        def handler(request):
            body = json.loads(request.content)
            assert body["max_tokens"] == 10
            return httpx.Response(200, json=llm_response("OK"))

        assert make_reviewer(handler).ping() == "OK"
