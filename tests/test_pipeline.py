import json

import pytest

from reviewer.review.comments import format_comment
from reviewer.review.pipeline import InvalidLlmOutput, review_pull_request
from reviewer.review.schema import ReviewComment
from tests.fakes import REF, FakeGitHub, ScriptedLlm, file


def llm_comment(
    line: int, severity: str = "warning", message: str | None = None, path: str = "src/a.ts"
) -> dict[str, object]:
    return {
        "path": path,
        "line": line,
        "severity": severity,
        "category": "bug",
        "message": message or f"issue {line}",
    }


class TestSecretRedaction:
    async def test_never_sends_secrets_to_the_llm_and_posts_a_critical_comment_on_the_line(self) -> None:
        gh = FakeGitHub(
            [
                file(
                    "src/a.ts",
                    [
                        'const DB_PASSWORD = "hunter2-not-a-real-one";',
                        'const awsKey = "AKIAIOSFODNN7EXAMPLE";',
                        "export const ok = 1;",
                    ],
                )
            ]
        )
        llm = ScriptedLlm()
        out = await review_pull_request(gh, llm, REF)

        sent = llm.all_user_text
        assert "hunter2-not-a-real-one" not in sent
        assert "AKIAIOSFODNN7EXAMPLE" not in sent
        assert "[REDACTED:" in sent
        assert "export const ok = 1;" in sent

        assert gh.posted is not None
        assert out.status == "completed"
        assert [c.line for c in gh.posted.comments] == [1, 2]
        assert gh.posted.comments[0].severity == "critical"
        assert "Revoke and rotate" in format_comment(gh.posted.comments[0])
        assert "2 possible secret(s) were detected and masked" in gh.posted.body
        assert "hunter2-not-a-real-one" not in json.dumps(gh.posted.body)  # nor in the review text


class TestRepeatSkipping:
    lines = ["let a = 1;", "let b = 2;", "let c = 3;"]

    async def test_does_not_repost_a_comment_already_made_even_if_its_line_moved(self) -> None:
        earlier = ReviewComment(
            path="src/a.ts", line=9, severity="warning", category="bug", message="Variable b is never used."
        )
        from reviewer.review.comments import key_from_posted_body

        gh = FakeGitHub(
            [file("src/a.ts", self.lines)],
            existing_keys={key_from_posted_body("src/a.ts", format_comment(earlier))},
        )
        llm = ScriptedLlm([llm_comment(2, message="Variable b is never used.")])
        out = await review_pull_request(gh, llm, REF)

        assert gh.posted is not None and gh.posted.comments == []
        assert "1 comment(s) already posted on an earlier commit were not repeated" in gh.posted.body
        assert out.comments_posted == 0

    async def test_still_posts_new_comments(self) -> None:
        gh = FakeGitHub([file("src/a.ts", self.lines)])
        await review_pull_request(gh, ScriptedLlm([llm_comment(2)]), REF)
        assert gh.posted is not None and len(gh.posted.comments) == 1


class TestRepoConfig:
    lines = ["a", "b", "c", "d"]

    async def test_reads_config_from_the_base_branch_then_applies_every_setting(self) -> None:
        gh = FakeGitHub(
            [file("src/a.ts", self.lines), file("docs/guide.ts", ["IGNORED_FILE_CONTENT"])],
            config=(
                "severity_threshold: warning\nmax_comments: 1\nignore_paths:\n  - 'docs/**'\n"
                "focus: [security]\ncustom_instructions: Prefer Result types.\n"
            ),
        )
        llm = ScriptedLlm(
            [
                llm_comment(1, "nitpick"),
                llm_comment(2, "warning"),
                llm_comment(3, "critical"),
                llm_comment(4, "suggestion"),
            ]
        )
        await review_pull_request(gh, llm, REF)

        assert ("get_file", (".aireview.yml", "main")) in gh.calls
        assert "IGNORED_FILE_CONTENT" not in llm.all_user_text
        assert "Prioritise these categories: security" in llm.seen[0]["system"]
        assert "Prefer Result types." in llm.seen[0]["system"]
        # nitpick and suggestion are hidden; of critical + warning only the most severe is kept (cap = 1)
        assert gh.posted is not None
        assert [c.line for c in gh.posted.comments] == [3]

    async def test_falls_back_to_defaults_and_says_so_when_invalid(self) -> None:
        gh = FakeGitHub([file("src/a.ts", self.lines)], config="severity_threshold: loud")
        await review_pull_request(gh, ScriptedLlm([llm_comment(1, "nitpick")]), REF)
        assert gh.posted is not None
        assert len(gh.posted.comments) == 1  # defaults keep nitpicks
        assert "Ignored invalid .aireview.yml" in gh.posted.body

    async def test_skips_the_review_when_disabled(self) -> None:
        gh = FakeGitHub([file("src/a.ts", self.lines)], config="enabled: false")
        llm = ScriptedLlm()
        out = await review_pull_request(gh, llm, REF)
        assert out.status == "skipped" and "disabled" in (out.reason or "")
        assert llm.seen == [] and gh.posted is None


class TestSkipsAndSizeGuard:
    async def test_skips_drafts_and_prs_with_nothing_reviewable_without_calling_the_llm(self) -> None:
        llm = ScriptedLlm()
        assert (await review_pull_request(FakeGitHub([], draft=True), llm, REF)).reason == "draft"
        lock = FakeGitHub([file("package-lock.json", ["x"], "modified")])
        assert (await review_pull_request(lock, llm, REF)).reason == "no reviewable files"
        assert llm.seen == []

    async def test_caps_a_huge_pr_reviews_higher_priority_files_first_and_says_what_was_skipped(self) -> None:
        big = ["x" * 50 for _ in range(1000)]
        files = [file("README.md", big), *(file(f"src/f{i}.ts", big) for i in range(6))]
        gh = FakeGitHub(files)
        llm = ScriptedLlm()
        await review_pull_request(gh, llm, REF)

        sent = llm.all_user_text
        assert "### File: src/f0.ts" in sent
        assert "### File: README.md" not in sent  # docs are dropped first
        assert gh.posted is not None
        assert (
            "PR is large:" in gh.posted.body and "lower-priority file(s) were not reviewed" in gh.posted.body
        )


class TestLlmHandling:
    async def test_reports_summed_token_usage_model_and_kept_comments_for_storage(self) -> None:
        gh = FakeGitHub([file("src/a.ts", ["a", "b"])])
        out = await review_pull_request(gh, ScriptedLlm([llm_comment(1)]), REF)
        assert out.model == "scripted" and out.files_reviewed == 1 and out.risk == "low"
        assert (out.usage.input_tokens, out.usage.output_tokens) == (100, 20)
        assert len(out.comments) == 1

    async def test_retries_once_with_the_validation_error_and_counts_both_calls(self) -> None:
        bad = json.dumps(
            {"summary": "s", "overall_risk": "low", "comments": [llm_comment(1, severity="bug")]}
        )
        good = json.dumps({"summary": "s", "overall_risk": "low", "comments": [llm_comment(1)]})
        llm = ScriptedLlm(replies=[bad, good])
        gh = FakeGitHub([file("src/a.ts", ["a", "b"])])
        out = await review_pull_request(gh, llm, REF)

        assert len(llm.seen) == 2
        assert "Your previous reply was invalid" in llm.seen[1]["user"]
        assert out.usage.input_tokens == 200  # the failed attempt cost tokens too
        assert out.comments_posted == 1

    async def test_strips_markdown_fences_around_the_json(self) -> None:
        fenced = "```json\n" + json.dumps({"summary": "s", "overall_risk": "low", "comments": []}) + "\n```"
        gh = FakeGitHub([file("src/a.ts", ["a"])])
        out = await review_pull_request(gh, ScriptedLlm(replies=[fenced]), REF)
        assert out.status == "completed"

    async def test_fails_clearly_when_the_llm_is_invalid_twice(self) -> None:
        gh = FakeGitHub([file("src/a.ts", ["a"])])
        with pytest.raises(InvalidLlmOutput):
            await review_pull_request(gh, ScriptedLlm(replies=["not json at all"]), REF)
        assert gh.posted is None  # nothing is posted when the review failed

    async def test_drops_comments_on_lines_outside_the_diff_and_says_so(self) -> None:
        gh = FakeGitHub([file("src/a.ts", ["a", "b"])])
        await review_pull_request(gh, ScriptedLlm([llm_comment(1), llm_comment(99)]), REF)
        assert gh.posted is not None
        assert [c.line for c in gh.posted.comments] == [1]
        assert "1 comment(s) were skipped because they pointed at lines outside the diff" in gh.posted.body
