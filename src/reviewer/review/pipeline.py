"""The review itself: fetch a PR, mask secrets, ask the LLM, validate, de-duplicate, and post ONE review."""

import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import ValidationError

from reviewer.ports import GitHubPort, LlmClient, PrRef
from reviewer.review.chunk import PromptFile, chunk_by_budget
from reviewer.review.comments import comment_key
from reviewer.review.diff import parse_patch
from reviewer.review.filters import select_files
from reviewer.review.glob_match import glob_to_regex
from reviewer.review.prompt import build_system_prompt, build_user_prompt
from reviewer.review.redact import redact_patch
from reviewer.review.repo_config import CONFIG_PATH, DEFAULT_CONFIG, parse_repo_config
from reviewer.review.schema import RISKS, ReviewComment, ReviewResult
from reviewer.review.validate import validate_comments

log = logging.getLogger(__name__)

MAX_CHUNK_CHARS = 60_000  # ~15k tokens of diff per LLM call
MAX_TOTAL_CHARS = 240_000  # ~60k tokens per PR: beyond this, lower-priority files are skipped


class InvalidLlmOutput(Exception):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class ReviewOutcome:
    status: Literal["completed", "skipped"]
    reason: str | None = None
    # Everything below is set only when status is "completed".
    comments_posted: int = 0
    dropped_comments: int = 0
    summary: str | None = None
    risk: str | None = None
    model: str | None = None
    files_reviewed: int = 0
    usage: Usage = field(default_factory=Usage)
    comments: list[ReviewComment] = field(default_factory=list)


async def _review_chunk(llm: LlmClient, system: str, user: str, usage: Usage) -> ReviewResult:
    """Calls the LLM and validates its JSON. On a parse/schema failure, retries once with the error."""
    prompt = user
    last_error = ""
    for _ in range(2):
        res = await llm.review(system=system, user=prompt)
        usage.input_tokens += res.input_tokens or 0  # a retry costs tokens too
        usage.output_tokens += res.output_tokens or 0
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", res.text.strip())
        try:
            return ReviewResult.model_validate_json(text)
        except ValidationError as err:
            last_error = str(err)
            prompt = (
                f"{user}\n\nYour previous reply was invalid: {last_error[:500]}\n"
                "Return ONLY valid JSON matching the schema."
            )
    raise InvalidLlmOutput(f"LLM returned invalid output twice: {last_error[:300]}")


async def review_pull_request(gh: GitHubPort, llm: LlmClient, ref: PrRef) -> ReviewOutcome:
    pr = await gh.get_pull_request(ref)
    if pr.draft:
        return ReviewOutcome(status="skipped", reason="draft")

    # Config is read from the BASE branch, not the PR head: otherwise a PR author could rewrite the
    # reviewer's instructions inside the very PR being reviewed.
    config_text = await gh.get_file(ref, CONFIG_PATH, pr.base_ref)
    cfg, config_error = parse_repo_config(config_text) if config_text else (DEFAULT_CONFIG, None)
    if not cfg.enabled:
        return ReviewOutcome(status="skipped", reason=f"disabled in {CONFIG_PATH}")

    all_files = await gh.get_pull_files(ref)
    files = select_files(all_files, [glob_to_regex(g) for g in cfg.ignore_paths])
    if not files:
        return ReviewOutcome(status="skipped", reason="no reviewable files")

    valid_lines_by_path: dict[str, frozenset[int]] = {}
    secret_comments: list[ReviewComment] = []
    parsed: list[PromptFile] = []
    skipped_files: list[str] = []
    total_chars = 0

    for f in files:
        # Secrets are masked BEFORE anything is sent to the LLM or logged.
        masked_patch, findings = redact_patch(f.patch or "")
        p = parse_patch(masked_patch)
        size = min(len(p.annotated), MAX_CHUNK_CHARS)
        if total_chars + size > MAX_TOTAL_CHARS:
            skipped_files.append(f.filename)
            continue
        total_chars += size
        valid_lines_by_path[f.filename] = p.valid_lines
        parsed.append(PromptFile(f.filename, p.annotated))
        for s in findings:
            secret_comments.append(
                ReviewComment(
                    path=f.filename,
                    line=s.line,
                    severity="critical",
                    category="security",
                    message=(
                        f"Possible secret ({s.type}) added here. It was masked before this review, but it is now "
                        "in the git history. Revoke and rotate it, and load it from an environment variable or "
                        "secret manager instead."
                    ),
                )
            )

    system = build_system_prompt(cfg.focus, cfg.custom_instructions)
    usage = Usage()
    results: list[ReviewResult] = []
    for chunk in chunk_by_budget(parsed, MAX_CHUNK_CHARS):
        results.append(await _review_chunk(llm, system, build_user_prompt(pr.title, pr.body, chunk), usage))

    risk = max((r.overall_risk for r in results), key=RISKS.index, default="low")
    summary = " ".join(r.summary for r in results)
    proposed = [*secret_comments, *(c for r in results for c in r.comments)]

    # Skip what this bot already said on an earlier commit of the same PR.
    posted: set[str] = set()
    try:
        posted = await gh.get_posted_comment_keys(ref)
    except Exception as err:  # de-duplication is a nicety: never fail the review over it
        log.warning("could not load earlier comments; not de-duplicating", extra={"error": str(err)})
    fresh = [c for c in proposed if comment_key(c.path, c.message) not in posted]
    already_posted = len(proposed) - len(fresh)

    outcome = validate_comments(
        fresh, valid_lines_by_path, max_comments=cfg.max_comments, min_severity=cfg.severity_threshold
    )
    dropped = (
        already_posted
        + outcome.dropped_below_threshold
        + outcome.dropped_invalid_line
        + outcome.dropped_duplicate
        + outcome.dropped_over_cap
    )
    log.info(
        "review prepared",
        extra={
            "files": len(parsed),
            "skipped_files": len(skipped_files),
            "approx_tokens": round(total_chars / 4),
            "secrets": len(secret_comments),
            "proposed": len(proposed),
            "kept": len(outcome.kept),
            "already_posted": already_posted,
            "dropped_below_threshold": outcome.dropped_below_threshold,
            "dropped_invalid_line": outcome.dropped_invalid_line,
            "dropped_duplicate": outcome.dropped_duplicate,
            "dropped_over_cap": outcome.dropped_over_cap,
        },
    )

    notes: list[str] = []
    if secret_comments:
        notes.append(f"{len(secret_comments)} possible secret(s) were detected and masked before analysis.")
    if already_posted:
        notes.append(f"{already_posted} comment(s) already posted on an earlier commit were not repeated.")
    if outcome.dropped_invalid_line:
        notes.append(
            f"{outcome.dropped_invalid_line} comment(s) were skipped because they pointed at lines outside the diff."
        )
    if skipped_files:
        notes.append(f"PR is large: {len(skipped_files)} lower-priority file(s) were not reviewed.")
    if config_error:
        notes.append(f"Ignored invalid {CONFIG_PATH} ({config_error}); defaults used.")

    body = (
        f"## AI Code Review\n\n{summary}\n\n**Overall risk:** {risk}\n\n"
        f"_Reviewed {len(parsed)} file(s) with `{llm.model}`, {len(outcome.kept)} new inline comment(s)._"
    )
    if notes:
        body += "\n\n" + "\n".join(f"- {n}" for n in notes)
    await gh.post_review(ref, head_sha=pr.head_sha, body=body, comments=outcome.kept)

    return ReviewOutcome(
        status="completed",
        comments_posted=len(outcome.kept),
        dropped_comments=dropped,
        summary=summary,
        risk=risk,
        model=llm.model,
        files_reviewed=len(parsed),
        usage=usage,
        comments=outcome.kept,
    )
