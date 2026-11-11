import pytest
from pydantic import ValidationError

from reviewer.cost import estimate_cost_usd
from reviewer.review.comments import comment_key, format_comment, key_from_posted_body
from reviewer.review.schema import ReviewComment, ReviewResult
from reviewer.review.validate import validate_comments


def c(**over: object) -> ReviewComment:
    base: dict[str, object] = {
        "path": "a.ts",
        "line": 5,
        "severity": "warning",
        "category": "bug",
        "message": "problem",
    }
    return ReviewComment.model_validate({**base, **over})


VALID = {"a.ts": {1, 2, 3, 4, 5, 6, 7}}


class TestValidate:
    def test_drops_comments_on_lines_or_files_not_in_the_diff(self) -> None:
        r = validate_comments([c(line=99), c(path="other.ts"), c()], VALID, max_comments=10)
        assert len(r.kept) == 1
        assert r.dropped_invalid_line == 2

    def test_removes_duplicates(self) -> None:
        r = validate_comments([c(), c()], VALID, max_comments=10)
        assert len(r.kept) == 1 and r.dropped_duplicate == 1

    def test_ranks_by_severity_and_caps_keeping_the_most_severe(self) -> None:
        r = validate_comments(
            [c(line=5, severity="nitpick"), c(line=6, severity="critical"), c(line=7, severity="warning")],
            VALID,
            max_comments=2,
        )
        assert [x.severity for x in r.kept] == ["critical", "warning"]
        assert r.dropped_over_cap == 1

    def test_drops_comments_below_the_severity_threshold(self) -> None:
        mk = lambda sev, line: c(severity=sev, line=line, message=f"m{line}")  # noqa: E731
        r = validate_comments(
            [mk("critical", 1), mk("warning", 2), mk("suggestion", 3), mk("nitpick", 4)],
            VALID,
            max_comments=10,
            min_severity="warning",
        )
        assert [x.severity for x in r.kept] == ["critical", "warning"]
        assert r.dropped_below_threshold == 2


class TestSchema:
    def test_accepts_a_valid_result_and_rejects_a_bad_severity(self) -> None:
        ok = {"summary": "s", "overall_risk": "low", "comments": [c().model_dump()]}
        ReviewResult.model_validate(ok)
        bad = {**ok, "comments": [{**c().model_dump(), "severity": "huge"}]}
        with pytest.raises(ValidationError):
            ReviewResult.model_validate(bad)

    def test_rejects_bug_used_as_a_severity(self) -> None:
        # The exact mistake Gemini made during development.
        with pytest.raises(ValidationError):
            c(severity="bug")


class TestRepeatDetection:
    def test_recognises_a_previously_posted_comment_even_if_its_line_moved(self) -> None:
        comment = c(line=10, message="Missing await.", suggested_fix="await x();")
        posted = key_from_posted_body("a.ts", format_comment(comment))
        assert posted == comment_key("a.ts", "  missing   AWAIT. ")
        assert comment_key(comment.path, comment.message) == posted

    def test_format_has_header_message_and_suggestion_block(self) -> None:
        text = format_comment(c(suggested_fix="x = 1"))
        assert text.startswith("**Warning | Bug**\n\nproblem")
        assert "```suggestion\nx = 1\n```" in text


class TestCost:
    def test_unknown_models_have_no_price(self) -> None:
        assert estimate_cost_usd("fake-llm", 1000, 1000) is None
        assert estimate_cost_usd("gemini-2.5-flash", 1_000_000, 0) == pytest.approx(0.3)
