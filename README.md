# AI Code Reviewer

A GitHub App that reviews pull requests with an LLM. When a PR is opened or updated, it fetches the diff, asks the model for structured feedback, and posts a summary plus inline comments on the PR.

**Status:** under development. The webhook → queue → LLM review → inline comments loop works end to end. The database and dashboard are not built yet.

## How it works

```text
Developer opens / updates a PR
        |
     GitHub --webhook--> API  POST /webhooks/github
                           - verify HMAC signature
                           - enqueue job (jobId = repo-pr-sha, so retries are idempotent)
                           - reply 202 immediately
                                   |
                              Redis + BullMQ
                                   |
                              Review worker
        1. Authenticate as the GitHub App installation
        2. Fetch PR details and changed files
        3. Filter out lockfiles, build output, minified files, images
        4. Parse diffs, number lines, chunk to a token budget
        5. Call the LLM (JSON output, validated with Zod, one retry)
        6. Drop comments that are not on lines inside the diff, de-duplicate, rank, cap at 15
        7. Post ONE review (event = COMMENT) with a summary and inline comments
```

The webhook only verifies and enqueues, because GitHub expects a reply within 10 seconds and LLM calls take longer.

## Implemented

- GitHub App authentication and signature-verified webhooks
- Redis/BullMQ queue with retries, exponential backoff and idempotent job IDs
- File filtering and prioritisation (source, then tests, config, docs)
- Unified-diff parser that maps comments to valid new-file lines
- Structured LLM output: Zod validation, schema-constrained Gemini responses, retry on invalid output
- Pluggable LLM provider (`fake` for offline testing, `gemini`)
- Inline comments with GitHub suggestion blocks
- Secret redaction: API keys, tokens and private keys are masked before code reaches the LLM, and flagged as critical comments
- No repeated comments: comments already posted on earlier commits of a PR are skipped
- Per-repo `.aireview.yml` (read from the base branch, so a PR cannot rewrite its own reviewer rules)
- Size guard: very large PRs are capped, lower-priority files are skipped and the review says so
- Unit tests (Vitest) for signature checks, filtering, diff parsing, chunking, redaction, config and comment validation

- PostgreSQL + Prisma storage: installations, repositories, pull requests, reviews (status, risk, model, tokens, estimated cost, duration), comments, and monthly usage events. One review row per (PR, commit), so retries never duplicate. A repository can be switched off in the database (`Repository.enabled`) and the worker will skip it. Diffs are never stored.

## Planned

- Next.js dashboard with GitHub login
- Live status (SSE), analytics, `/review` slash command
- Evaluation set with precision/recall, Docker, CI, deployment

## Tech stack

Node.js, TypeScript, Express, BullMQ + Redis, Octokit (`@octokit/app`), Zod, Pino, Vitest. LLM: Google Gemini (other providers can be added behind the `LlmClient` interface).

## Project layout

```text
apps/api/
  src/
    routes/      webhook endpoint
    services/    github, llm, prompt, review pipeline, schema
    queue/       BullMQ queue
    workers/     review worker
    utils/       signature check, file filter, diff parser, chunking, comment validation
  test/
```

## Local setup

Requirements: Node 20+, a Redis instance, a GitHub App, a Gemini API key (optional, `fake` provider works without one).

1. **Install:** `npm install`
2. **Create a GitHub App** (Settings → Developer settings → GitHub Apps) with:
   - Permissions: Pull requests (read & write), Issues (read & write), Contents (read), Metadata (read)
   - Subscribe to the **Pull request** event
   - A webhook secret you generate yourself
   - Generate a private key (`.pem`) and install the app on a test repository
3. **Redis:** run `docker compose up -d redis`, or use a hosted instance such as Upstash (use the `rediss://` URL).
4. **Configure:** copy `.env.example` to `.env` and fill in your values. Never put real secrets in `.env.example`.
   - `GITHUB_PRIVATE_KEY` must be on one line with `\n` in place of line breaks.
   - `LLM_PROVIDER=gemini` and `LLM_API_KEY=...` to use Gemini; leave as `fake` to test without a key.
5. **Forward webhooks to localhost** with [smee.io](https://smee.io): set the channel URL as the app's Webhook URL, then run
   `npx smee-client --url https://smee.io/<channel> --target http://localhost:3001/webhooks/github`
6. **Database (optional):** set `DATABASE_URL` in `.env` to any PostgreSQL URL (local, or a free hosted one such as Neon), then create the tables once with `npm run db:migrate -w apps/api`. Without `DATABASE_URL` the bot still reviews PRs but stores nothing. Browse stored data with `npm run db:studio -w apps/api`.
7. **Run:** in two terminals, `npm run dev:api` and `npm run dev:worker`.
8. Open a (non-draft) PR on the test repository.

Run checks with `npm run typecheck` and `npm test`.

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

## Security notes

- Webhook signatures are verified with a constant-time comparison on the raw body.
- The bot only posts `COMMENT` reviews. It never approves or requests changes.
- The PR title, description and code are treated as untrusted data in the prompt to resist prompt injection.
- Full diffs and tokens are not logged. Diffs are not stored.
- The LLM provider receives the changed code. Do not point the bot at confidential repositories while using a provider whose terms allow training on your data (for example, free tiers).

## Limitations

- The model sees only the diff, not the whole repository or a compiler, so it can miss problems and occasionally raise a false one. Run your compiler, linter and tests in CI alongside it.
- The dashboard is not implemented yet, and review history is only visible through the database.
- Cost figures are estimates from a built-in price table and only exist for models listed in `utils/cost.ts`.
