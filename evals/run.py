"""Runs the real review pipeline over the evaluation dataset and reports precision and recall.

    uv run python -m evals.run                         # all cases
    uv run python -m evals.run --only ts-sql-injection,clean-retry
    uv run python -m evals.run --resume                # continue a partial run, skipping finished cases

Uses LLM_PROVIDER / LLM_API_KEY / LLM_MODEL from .env. With the "fake" provider the numbers are meaningless:
it exists only to check the harness itself. Results are saved after EVERY case, so a quota error or a crash
never loses finished work.
"""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from evals.dataset import DATASET
from evals.fake_github import EvalGitHub
from evals.score import CaseScore, EvalCase, Reported, Summary, parse_case, score_case, summarize, to_patch
from reviewer.llm import LlmError, create_llm
from reviewer.ports import LlmClient, LlmResponse, PrRef
from reviewer.review.pipeline import review_pull_request

RESULTS_FILE = Path(__file__).parent / "results" / "latest.json"
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


class DailyQuotaExhausted(Exception):
    """The provider's DAILY quota is used up: waiting a few minutes will not help."""


class BackoffLlm:
    """Wraps an LLM client: retries rate limits, overload and flaky-network errors with a growing pause."""

    def __init__(self, inner: LlmClient, *, base_wait_s: float = 20.0, attempts: int = 5) -> None:
        self._inner = inner
        self.model = inner.model
        self._base_wait = base_wait_s
        self._attempts = attempts

    async def review(self, *, system: str, user: str) -> LlmResponse:
        for attempt in range(self._attempts):
            last = attempt == self._attempts - 1
            try:
                return await self._inner.review(system=system, user=user)
            except LlmError as err:
                if "PerDay" in str(err):
                    raise DailyQuotaExhausted(str(err)) from err
                if last or err.status not in RETRYABLE_STATUSES:
                    raise
            except httpx.TransportError:
                if last:
                    raise
            wait = self._base_wait * (attempt + 1)
            print(f"    (provider busy, waiting {wait:.0f}s)", flush=True)
            await asyncio.sleep(wait)
        raise AssertionError("unreachable")


@dataclass
class CaseResult:
    id: str
    bugs: int
    found: int
    reported: int
    true_positives: int
    false_positives: int
    seeded_bug_lines: list[int]
    comments: list[dict[str, Any]]
    error: str | None = None

    @property
    def score(self) -> CaseScore:
        return CaseScore(
            self.id, self.bugs, self.found, self.reported, self.true_positives, self.false_positives
        )


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _save(
    out_file: Path, model: str, provider: str, cases: list[CaseResult], total: int, note: str | None
) -> Summary:
    valid = [c for c in cases if not c.error]  # errored cases say nothing about review quality
    summary = summarize([c.score for c in valid])
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(
        json.dumps(
            {
                "date": datetime.now(UTC).isoformat(),
                "provider": provider,
                "model": model,
                "tolerance": 2,
                "complete": len(valid) == total,
                "valid_cases": len(valid),
                "errored_cases": len(cases) - len(valid),
                "summary": asdict(summary),
                "note": note,
                "cases": [asdict(c) for c in cases],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return summary


def _load_finished(out_file: Path, model: str) -> dict[str, CaseResult]:
    if not out_file.exists():
        return {}
    prev = json.loads(out_file.read_text(encoding="utf-8"))
    if prev.get("model") != model:
        print(f"Previous results used {prev.get('model')}, not {model}: starting over.")
        return {}
    keys = CaseResult.__dataclass_fields__.keys()
    return {
        c["id"]: CaseResult(**{k: c[k] for k in keys if k in c}) for c in prev["cases"] if not c.get("error")
    }


async def run_eval(
    cases: Sequence[EvalCase],
    llm: LlmClient,
    *,
    out_file: Path = RESULTS_FILE,
    provider: str = "",
    resume: bool = False,
    pause_s: float = 8.0,
    emit: Callable[[str], None] = lambda line: print(line, flush=True),
) -> tuple[Summary, str | None]:
    """Reviews every case, saving after each one. Returns the summary and why it stopped early (if it did)."""
    parsed = [parse_case(c) for c in cases]
    done = _load_finished(out_file, llm.model) if resume else {}
    todo = [c for c in parsed if c.id not in done]
    emit(
        f"Evaluating {len(todo)} case(s) with {llm.model}"
        + (f" ({len(done)} already done, resuming)" if done else "")
    )

    stopped: str | None = None
    quota_streak = 0  # consecutive cases rejected for quota even after backoff

    def ordered() -> list[CaseResult]:
        return [done[c.id] for c in parsed if c.id in done]

    for i, case in enumerate(todo, start=1):
        gh = EvalGitHub(case.title, case.filename, to_patch(case.code))
        comments: list[dict[str, Any]] = []
        error: str | None = None
        try:
            outcome = await review_pull_request(gh, llm, PrRef("eval", "eval", 1))
            comments = [c.model_dump() for c in outcome.comments]
        except DailyQuotaExhausted:
            stopped = "Daily quota exhausted"
            emit(f"[{i:2}/{len(todo)}] {case.id:28} STOPPED: provider daily quota used up")
            break
        except Exception as err:
            error = f"{type(err).__name__}: {str(err)[:200]}"

        score = score_case(case, [Reported(c["path"], c["line"]) for c in comments])
        if error:
            verdict = f"ERROR {error.splitlines()[0]}"
        elif not case.bugs:
            verdict = "clean, no comments" if not comments else f"clean but {len(comments)} comment(s)"
        else:
            verdict = f"found {score.found}/{score.bugs} bug(s), {score.false_positives} extra"
        emit(f"[{i:2}/{len(todo)}] {case.id:28} {verdict}")
        for c in comments:
            emit(f"        line {c['line']} [{c['severity']}/{c['category']}] {c['message'][:90]}")

        done[case.id] = CaseResult(
            **{
                k: getattr(score, k)
                for k in ("id", "bugs", "found", "reported", "true_positives", "false_positives")
            },
            seeded_bug_lines=[b.line for b in case.bugs],
            comments=comments,
            error=error,
        )
        _save(out_file, llm.model, provider, ordered(), len(parsed), None)

        quota_streak = quota_streak + 1 if error and "429" in error else 0
        if quota_streak >= 2:
            stopped = "Provider keeps rejecting requests for quota (likely the daily limit)"
            break
        if i < len(todo):
            await asyncio.sleep(pause_s)  # stay under ~7 requests/minute

    summary = _save(out_file, llm.model, provider, ordered(), len(parsed), stopped)
    return summary, stopped


def print_summary(summary: Summary, total_cases: int) -> None:
    partial = f" (PARTIAL: {summary.cases} of {total_cases} cases)" if summary.cases < total_cases else ""
    print(f"\n==== Results{partial} ====")
    print(
        f"Cases:               {summary.cases} ({summary.buggy_cases} with seeded bugs, {summary.clean_cases} clean)"
    )
    print(
        f"Recall:              {_pct(summary.recall)}   ({summary.found} of {summary.bugs} seeded bugs found)"
    )
    print(
        f"Precision:           {_pct(summary.precision)}   ({summary.true_positives} of {summary.reported} comments pointed at a seeded bug)"
    )
    print(
        f"Clean files flagged: {_pct(summary.clean_flagged_rate)}   ({summary.clean_cases_flagged} of {summary.clean_cases} clean cases got a comment)"
    )


async def amain(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--only", help="comma-separated case ids")
    parser.add_argument("--resume", action="store_true", help="skip cases already finished in the last run")
    args = parser.parse_args(argv)

    load_dotenv()
    provider = os.environ.get("LLM_PROVIDER", "fake")
    llm = BackoffLlm(
        create_llm(provider, os.environ.get("LLM_API_KEY") or None, os.environ.get("LLM_MODEL") or None)
    )
    if llm.model == "fake-llm":
        print("WARNING: provider is 'fake'. Set LLM_PROVIDER=gemini for meaningful numbers.\n")

    only = set(args.only.split(",")) if args.only else None
    cases = [c for c in DATASET if only is None or c.id in only]
    if not cases:
        print("No cases selected", file=sys.stderr)
        return 1

    summary, stopped = await run_eval(cases, llm, provider=provider, resume=args.resume)
    print_summary(summary, len(cases))
    print(f"\nSaved {RESULTS_FILE}")
    if stopped:
        print(f"\n{stopped}. Run again later with:  uv run python -m evals.run --resume")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain(sys.argv[1:])))
