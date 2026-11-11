import json
from pathlib import Path

import httpx
import pytest
from evals.dataset import DATASET
from evals.run import BackoffLlm, DailyQuotaExhausted, run_eval
from evals.score import EvalCase, Reported, parse_case, score_case, summarize, to_patch

from reviewer.llm import FakeLlm, LlmError
from reviewer.ports import LlmResponse
from reviewer.review.diff import parse_patch

PARSED = [parse_case(c) for c in DATASET]


class TestDatasetIntegrity:
    def test_has_unique_ids_enough_cases_and_both_buggy_and_clean_ones(self) -> None:
        assert len({c.id for c in DATASET}) == len(DATASET)
        assert len(DATASET) >= 20
        assert sum(1 for p in PARSED if p.bugs) >= 12
        assert sum(1 for p in PARSED if not p.bugs) >= 6

    def test_never_leaks_the_answer_key(self) -> None:
        for p in PARSED:
            assert "@@BUG" not in p.code, p.id

    def test_points_every_seeded_bug_at_a_real_commentable_line(self) -> None:
        for p in PARSED:
            valid = parse_patch(to_patch(p.code)).valid_lines
            lines = p.code.split("\n")
            for b in p.bugs:
                assert b.line in valid, f"{p.id} line {b.line}"
                assert lines[b.line - 1].strip(), f"{p.id} line {b.line} is blank"


class TestScoring:
    case = parse_case(
        EvalCase("x", "t", "a.ts", "a\nb @@BUG:bug@@\nc\nd\ne\nf\ng\nh\ni\nj\nk\nl @@BUG:security@@")
    )

    def test_derives_bug_lines_from_the_markers(self) -> None:
        assert [(b.line, b.category) for b in self.case.bugs] == [(2, "bug"), (12, "security")]
        assert self.case.code == "a\nb\nc\nd\ne\nf\ng\nh\ni\nj\nk\nl"

    def test_counts_comments_within_two_lines_as_hits_and_bugs_once(self) -> None:
        s = score_case(
            self.case,
            [
                Reported("a.ts", 3),  # within tolerance of line 2
                Reported("a.ts", 2),  # second comment on the same bug: still a true positive, bug found once
                Reported("a.ts", 6),  # 4 lines from one bug and 6 from the other: matches nothing
                Reported("other.ts", 12),  # right line, wrong file
            ],
        )
        assert (s.bugs, s.found, s.reported, s.true_positives, s.false_positives) == (2, 1, 4, 2, 2)

    def test_summarises_recall_precision_and_clean_file_false_alarms(self) -> None:
        clean = parse_case(EvalCase("c", "t", "a.ts", "a\nb"))
        s = summarize(
            [
                score_case(self.case, [Reported("a.ts", 2)]),
                score_case(clean, [Reported("a.ts", 1)]),
                score_case(clean, []),
            ]
        )
        assert (s.cases, s.buggy_cases, s.clean_cases, s.bugs, s.found) == (3, 1, 2, 2, 1)
        assert (s.recall, s.precision, s.clean_cases_flagged, s.clean_flagged_rate) == (0.5, 0.5, 1, 0.5)
        assert summarize([score_case(clean, [])]).precision is None  # nothing reported: undefined, not 0


class FlakyLlm:
    """Fails with the given errors first, then answers like the fake model."""

    model = "flaky"

    def __init__(self, failures: list[Exception]) -> None:
        self.failures = failures
        self.calls = 0

    async def review(self, *, system: str, user: str) -> LlmResponse:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return await FakeLlm().review(system=system, user=user)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", instant)


class TestBackoff:
    async def test_retries_rate_limits_and_network_errors_then_succeeds(self) -> None:
        inner = FlakyLlm([LlmError(429, "slow down"), httpx.ConnectError("down"), LlmError(503, "busy")])
        res = await BackoffLlm(inner).review(system="", user="### File: a.py\n    1 + x\n")
        assert inner.calls == 4 and res.text

    async def test_gives_up_after_the_attempt_limit(self) -> None:
        inner = FlakyLlm([LlmError(429, "x")] * 10)
        with pytest.raises(LlmError):
            await BackoffLlm(inner, attempts=3).review(system="", user="")
        assert inner.calls == 3

    async def test_does_not_retry_errors_that_waiting_cannot_fix(self) -> None:
        inner = FlakyLlm([LlmError(400, "bad request")])
        with pytest.raises(LlmError):
            await BackoffLlm(inner).review(system="", user="")
        assert inner.calls == 1

    async def test_a_daily_quota_error_stops_immediately(self) -> None:
        inner = FlakyLlm([LlmError(429, "GenerateRequestsPerDayPerProjectPerModel exceeded")])
        with pytest.raises(DailyQuotaExhausted):
            await BackoffLlm(inner).review(system="", user="")
        assert inner.calls == 1


class TestRunEval:
    cases = DATASET[:3]

    async def test_saves_results_after_every_case_and_summarises(self, tmp_path: Path) -> None:
        out = tmp_path / "latest.json"
        summary, stopped = await run_eval(
            self.cases, FakeLlm(), out_file=out, provider="fake", emit=lambda _: None, pause_s=0
        )
        assert stopped is None and summary.cases == 3
        saved = json.loads(out.read_text(encoding="utf-8"))
        assert saved["complete"] is True and len(saved["cases"]) == 3
        assert saved["cases"][0]["seeded_bug_lines"]  # the answer key is recorded next to the result

    async def test_errored_cases_never_count_towards_the_numbers(self, tmp_path: Path) -> None:
        out = tmp_path / "latest.json"
        llm = FlakyLlm([LlmError(500, "boom")] * 99)
        summary, _ = await run_eval(
            self.cases[:1], BackoffLlm(llm, attempts=1), out_file=out, emit=lambda _: None, pause_s=0
        )
        saved = json.loads(out.read_text(encoding="utf-8"))
        assert summary.cases == 0 and saved["errored_cases"] == 1 and saved["complete"] is False

    async def test_resume_skips_finished_cases_and_redoes_errored_ones(self, tmp_path: Path) -> None:
        out = tmp_path / "latest.json"
        first = FlakyLlm([LlmError(500, "boom")])  # the first case errors, the others succeed
        await run_eval(
            self.cases, BackoffLlm(first, attempts=1), out_file=out, emit=lambda _: None, pause_s=0
        )
        assert json.loads(out.read_text(encoding="utf-8"))["errored_cases"] == 1

        again = FlakyLlm([])
        summary, _ = await run_eval(
            self.cases,
            BackoffLlm(again, attempts=1),
            out_file=out,
            resume=True,
            emit=lambda _: None,
            pause_s=0,
        )
        assert again.calls == 1  # only the errored case was run again
        assert summary.cases == 3 and json.loads(out.read_text(encoding="utf-8"))["complete"] is True

    async def test_stops_early_after_repeated_quota_rejections(self, tmp_path: Path) -> None:
        llm = FlakyLlm([LlmError(429, "quota")] * 99)
        _, stopped = await run_eval(
            DATASET[:5],
            BackoffLlm(llm, attempts=1),
            out_file=tmp_path / "r.json",
            emit=lambda _: None,
            pause_s=0,
        )
        assert stopped is not None and "quota" in stopped.lower()
        assert llm.calls == 2  # did not hammer the provider for the remaining cases

    async def test_a_different_model_starts_over(self, tmp_path: Path) -> None:
        out = tmp_path / "latest.json"
        await run_eval(self.cases, FakeLlm(), out_file=out, emit=lambda _: None, pause_s=0)
        llm = FlakyLlm([])
        await run_eval(self.cases, llm, out_file=out, resume=True, emit=lambda _: None, pause_s=0)
        assert llm.calls == 3  # results from "fake-llm" are not reused for "flaky"
