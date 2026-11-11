# AI Code Reviewer

A GitHub App that reviews pull requests with an LLM. When a PR is opened or updated, it fetches the diff, asks the model for structured feedback, and posts one review with a summary and inline comments (including one-click suggestion blocks).

**Status:** the review loop works end to end (webhook → queue → LLM review → inline comments → stored results). It is built in Python with FastAPI. See [Limitations](#limitations) for what is not done.

## How it works

```text
Developer opens / updates a PR  (or comments "/review")
        |
     GitHub --webhook--> API  POST /webhooks/github          (FastAPI)
                           - verify HMAC signature (constant-time, on the raw body)
                           - enqueue job; job id = repo-pr-commit, so redeliveries are no-ops
                           - answer 202 immediately
                                   |
                              Redis job queue
                                   |
                              Review worker
        1. Authenticate as the GitHub App installation (JWT -> installation token, cached)
        2. Fetch the PR, its changed files, and .aireview.yml from the BASE branch
        3. Filter lockfiles / build output / binaries, then rank source before tests, config, docs
        4. Mask secrets in the diff, number the lines, chunk to a token budget
        5. Call the LLM (JSON output, validated with Pydantic, one retry on invalid output)
        6. Drop comments outside the diff, below the severity threshold, duplicated, or already posted
        7. Post ONE review (event = COMMENT: it never approves or blocks a PR)
        8. Save the result (status, risk, tokens, estimated cost, comments) to PostgreSQL
```

The webhook only verifies and enqueues, because GitHub expects a reply within about 10 seconds and a review takes longer.

## Features

- GitHub App authentication written from scratch (RS256 JWT, installation tokens cached until near expiry, retries for secondary rate limits and 5xx responses)
- Signature-verified webhooks and an idempotent job queue
- Structured LLM output: schema-constrained Gemini responses, Pydantic validation, retry with the validation error
- **Secret redaction:** API keys, tokens and private keys are masked before code reaches the LLM, and flagged as critical comments
- **No repeated comments:** comments already posted on earlier commits of a PR are skipped
- **`.aireview.yml`** per repository, read from the base branch so a PR cannot rewrite its own reviewer rules
- **`/review` command:** comment `/review` on a PR to re-run the review. Only owners, members and collaborators can use it, bots are ignored, and the bot reacts with 👀 when it picks the command up
- **Size guard:** very large PRs are capped, lower-priority files are skipped, and the review says so
- Review history in PostgreSQL: one row per (PR, commit), token counts, estimated cost, monthly usage events. Diffs are never stored
- Evaluation harness with seeded bugs, reporting precision and recall

## Design decisions

- **A small purpose-built Redis queue, not Celery or RQ.** Celery polls Redis constantly, which burns the command allowance of pay-per-command hosted Redis (such as Upstash), and RQ cannot run on Windows. The queue needs only blocking pops: idempotent enqueue, retries with exponential backoff, a dead-letter list, and recovery of jobs held by a crashed worker.
- **Config from the base branch.** A PR author could otherwise edit `.aireview.yml` inside the PR being reviewed and steer the reviewer.
- **Secrets are masked before the LLM call**, not after, and the line count never changes so comment line numbers stay valid.
- **Results are saved with few round trips** (upserts, one transaction), because a hosted database can be hundreds of milliseconds away.
- **Ports and fakes.** The pipeline depends on two small interfaces (GitHub and the LLM), so the whole review runs in tests with no network.

## Tech stack

Python 3.12, FastAPI, Pydantic, httpx, PyJWT, Redis, SQLAlchemy 2 + Alembic (psycopg 3), pytest, ruff, mypy (strict), uv. LLM: Google Gemini; other providers plug in behind the `LlmClient` interface.

## Project layout

```text
src/reviewer/
  api/        webhook endpoint, signature check, event rules
  github/     GitHub App client
  llm/        Gemini and fake clients
  queue/      Redis job queue
  review/     the review pipeline and its pure logic (diff, filters, redaction, validation, config, prompt)
  db/         models and the store that saves results
  worker/     job handler and the worker loop
  serve.py    runs API and worker in one process
alembic/      database migrations
evals/        evaluation dataset and runner
tests/
```

## Local setup

Requirements: [uv](https://docs.astral.sh/uv/), a Redis instance, a GitHub App. A Gemini API key is optional: the `fake` provider works offline.

1. **Install:** `uv sync` (uv installs Python 3.12 for you).
2. **Create a GitHub App** (Settings → Developer settings → GitHub Apps):
   - Permissions: Pull requests (read & write), Issues (read & write), Contents (read), Metadata (read)
   - Subscribe to the **Pull request** and **Issue comment** events
   - A webhook secret you generate yourself, and a private key (`.pem`)
   - Install it on a test repository
3. **Redis:** `docker compose up -d redis`, or any Redis URL (use `rediss://` for hosted ones).
4. **Configure:** copy `.env.example` to `.env` and fill it in. Never put real secrets in `.env.example`.
   - `GITHUB_PRIVATE_KEY` must be on one line with `\n` in place of line breaks.
   - `LLM_PROVIDER=gemini` and `LLM_API_KEY=...` to use Gemini.
5. **Database (optional):** set `DATABASE_URL` to any PostgreSQL URL (for example from Neon) and run `uv run alembic upgrade head` once. Without it the bot still reviews PRs but stores nothing.
6. **Forward webhooks to localhost** with [smee.io](https://smee.io): set the channel URL as the app's Webhook URL, then run
   `npx smee-client --url https://smee.io/<channel> --target http://localhost:3001/webhooks/github`
7. **Run:** `uv run python -m reviewer.serve` (API and worker together), then open a non-draft PR on the test repository.

Run the checks with `uv run ruff check . && uv run mypy && uv run pytest`.

## Per-repo configuration

Add `.aireview.yml` to the default branch of the repository being reviewed. All fields are optional.

```yaml
enabled: true
severity_threshold: warning   # critical | warning | suggestion | nitpick. "warning" hides suggestions and nitpicks
max_comments: 10              # 1-30
ignore_paths:
  - "docs/**"
  - "**/*.generated.ts"
focus:                        # bug | security | performance | maintainability | testing
  - security
  - performance
custom_instructions: |
  We use Result types instead of exceptions. Flag any thrown errors.
```

An invalid file is ignored (defaults are used) and the review says why.

## Environment variables

| Variable | Purpose |
|---|---|
| `GITHUB_APP_ID` | GitHub App ID |
| `GITHUB_PRIVATE_KEY` | App private key (single line, `\n` for newlines) |
| `GITHUB_WEBHOOK_SECRET` | Secret used to verify webhook signatures |
| `REDIS_URL` | Redis connection URL |
| `LLM_PROVIDER` | `fake` or `gemini` |
| `LLM_API_KEY` | Provider API key |
| `LLM_MODEL` | Optional model override (default `gemini-2.5-flash`) |
| `DATABASE_URL` | Optional PostgreSQL URL for storing results |
| `PORT` | HTTP port (default 3001) |

## Evaluation

`evals/` holds 27 small code samples: 19 contain a seeded bug (SQL injection, missing `await`, off-by-one, `=` instead of `==`, and so on) and 8 are correct code. The answer key is marked in the source and stripped before the reviewer sees it. A comment within 2 lines of a seeded bug counts as a hit; any other comment is a false alarm.

```bash
uv run python -m evals.run            # needs LLM_PROVIDER=gemini and LLM_API_KEY in .env
uv run python -m evals.run --resume   # continue after a rate-limit stop
```

Results from the first run with `gemini-2.5-flash` (26 of 27 cases completed; one hit the free tier's rate limit). That run used the original TypeScript implementation of the same pipeline and prompt:

| Metric | Result |
|---|---|
| Recall | 100% (20 of 20 seeded bugs found) |
| Precision | 85% (29 of 34 comments pointed at a seeded bug) |
| Clean files flagged | 2 of 7 |

Read these with care. I wrote the samples myself, so they are easier than real pull requests, and several of the "false alarms" are fair observations that the scoring simply does not credit. It is a regression check for prompt changes, not proof of review quality. The raw results are in `evals/results/`.

## Deployment

The `Dockerfile` runs API and worker together (`python -m reviewer.serve`) as a non-root user; `railway.json` configures a Railway deploy (build from the Dockerfile, run `alembic upgrade head` before each release, health check on `/health`). To scale them separately, run the API with `uvicorn --factory reviewer.api.app:create_app` and the worker with `python -m reviewer.worker.main`. Set the GitHub App's Webhook URL to `https://<your-host>/webhooks/github`.

CI (`.github/workflows/ci.yml`) runs lint, strict type-checking and tests, and builds the Docker image.

## Security notes

- Webhook signatures are verified with a constant-time comparison on the raw body.
- The bot only posts `COMMENT` reviews. It never approves or requests changes.
- The PR title, description and code are treated as untrusted data in the prompt to resist prompt injection.
- Full diffs, tokens and API keys are not logged, and diffs are not stored. LLM errors never include the request.
- The LLM provider receives the changed code, after secrets are masked. Do not point the bot at confidential repositories while using a provider whose terms allow training on your data (for example, free tiers).

## Limitations

- The model sees only the diff, not the whole repository or a compiler, so it can miss problems and occasionally raise a false one. Run your compiler, linter and tests in CI alongside it.
- Only Gemini is implemented, and the free tier rate-limits aggressively.
- There is no dashboard. Review history is stored but only visible through the database.
- Cost figures are estimates from a built-in price table and exist only for the models listed in `reviewer/cost.py`.
- Usage caps, live status and a second provider with fallback are not built.
