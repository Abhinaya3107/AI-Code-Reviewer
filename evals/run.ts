/**
 * Runs the real review pipeline over the evaluation dataset and reports precision and recall.
 *
 *   npm run eval -w apps/api                         # all cases
 *   npm run eval -w apps/api -- --only=ts-sql-injection,clean-retry
 *   npm run eval -w apps/api -- --resume             # continue a partial run, skipping finished cases
 *
 * Uses LLM_PROVIDER / LLM_API_KEY / LLM_MODEL from .env. With the "fake" provider the numbers are
 * meaningless: it exists only to check the harness itself.
 *
 * Results are saved after EVERY case, so a quota error or a crash never loses finished work.
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import pino from "pino";
import { config } from "../apps/api/src/config.js";
import { createLlm, type LlmClient } from "../apps/api/src/services/llm.js";
import { reviewPullRequest } from "../apps/api/src/services/reviewPipeline.js";
import { DATASET } from "./dataset.js";
import { fakeGithubForCase } from "./fakeGithub.js";
import { parseCase, scoreCase, summarize, toPatch, type CaseScore } from "./score.js";

const here = dirname(fileURLToPath(import.meta.url));
const outFile = join(here, "results", "latest.json");
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const pct = (x: number | null) => (x === null ? "n/a" : `${(x * 100).toFixed(1)}%`);

/** The provider's DAILY quota is used up: waiting a few minutes will not help. */
class DailyQuotaExhausted extends Error {}

/** Free-tier limits are tight: retry per-minute rate limits and overload errors with a growing pause. */
function withBackoff(llm: LlmClient): LlmClient {
  return {
    model: llm.model,
    review: async (args) => {
      for (let attempt = 0; ; attempt++) {
        try {
          return await llm.review(args);
        } catch (err) {
          const msg = err instanceof Error ? err.message : String(err);
          if (/PerDay/i.test(msg)) throw new DailyQuotaExhausted(msg);
          // Rate limits, provider overload, and flaky-network errors are all worth waiting out.
          if (attempt < 4 && (/\b(429|500|502|503|504)\b/.test(msg) || /fetch failed|ECONN|ENOTFOUND|ETIMEDOUT|terminated/i.test(msg))) {
            console.log(`    (provider busy, waiting ${20 * (attempt + 1)}s)`);
            await sleep(20_000 * (attempt + 1));
            continue;
          }
          throw err;
        }
      }
    },
  };
}

interface StoredCase extends CaseScore {
  seededBugLines: number[];
  error?: string;
  comments: { path: string; line: number; severity: string; category: string; message: string }[];
}

function arg(name: string): string | undefined {
  const eq = process.argv.find((a) => a.startsWith(`--${name}=`));
  if (eq) return eq.slice(name.length + 3);
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 ? process.argv[i + 1] : undefined;
}

async function main() {
  const only = arg("only") ? new Set(arg("only")!.split(",")) : null;
  const all = DATASET.map(parseCase).filter((c) => !only || only.has(c.id));
  if (all.length === 0) throw new Error("No cases selected");

  const llm = withBackoff(createLlm({ provider: config.LLM_PROVIDER, apiKey: config.LLM_API_KEY, model: config.LLM_MODEL }));
  if (llm.model === "fake-llm") console.log("WARNING: provider is 'fake'. Set LLM_PROVIDER=gemini for meaningful numbers.\n");

  // --resume: keep finished cases from the previous run (same model only), redo the rest.
  const done = new Map<string, StoredCase>();
  if (process.argv.includes("--resume") && existsSync(outFile)) {
    const prev = JSON.parse(readFileSync(outFile, "utf8"));
    if (prev.model === llm.model) {
      for (const c of prev.cases as StoredCase[]) {
        if (!c.error) done.set(c.id, c);
      }
    } else {
      console.log(`Previous results used ${prev.model}, not ${llm.model}: starting over.`);
    }
  }
  const todo = all.filter((c) => !done.has(c.id));
  console.log(`Evaluating ${todo.length} case(s) with ${llm.model}${done.size ? ` (${done.size} already done, resuming)` : ""}\n`);

  const save = (note?: string) => {
    const cases = all.map((c) => done.get(c.id)).filter((c): c is StoredCase => Boolean(c));
    // Errored cases (rate limit, network) say nothing about review quality: never count them.
    const valid = cases.filter((c) => !c.error);
    mkdirSync(dirname(outFile), { recursive: true });
    writeFileSync(
      outFile,
      JSON.stringify({ date: new Date().toISOString(), provider: config.LLM_PROVIDER, model: llm.model, tolerance: 2, complete: valid.length === all.length, validCases: valid.length, erroredCases: cases.length - valid.length, summary: summarize(valid), note, cases }, null, 2) + "\n",
    );
    return cases;
  };

  const log = pino({ level: "silent" });
  let stoppedEarly: string | undefined;
  let quotaStreak = 0; // consecutive cases rejected for quota even after backoff

  for (const [i, c] of todo.entries()) {
    const gh = fakeGithubForCase({ title: c.title, filename: c.filename, patch: toPatch(c.code) });
    let comments: StoredCase["comments"] = [];
    let error: string | undefined;
    try {
      const out = await reviewPullRequest(gh.octokit, llm, { owner: "eval", repo: "eval", prNumber: 1 }, log);
      comments = out.comments ?? [];
    } catch (err) {
      if (err instanceof DailyQuotaExhausted) {
        stoppedEarly = "Daily quota exhausted";
        console.log(`[${String(i + 1).padStart(2)}/${todo.length}] ${c.id.padEnd(28)} STOPPED: provider daily quota used up`);
        break;
      }
      error = err instanceof Error ? err.message.slice(0, 200) : String(err);
    }
    quotaStreak = error && /\b429\b/.test(error) ? quotaStreak + 1 : 0;

    const score = scoreCase(c, comments);
    const verdict = error
      ? `ERROR ${error.split("\n")[0]}`
      : c.bugs.length === 0
        ? comments.length === 0 ? "clean, no comments" : `clean but ${comments.length} comment(s)`
        : `found ${score.found}/${score.bugs} bug(s), ${score.falsePositives} extra`;
    console.log(`[${String(i + 1).padStart(2)}/${todo.length}] ${c.id.padEnd(28)} ${verdict}`);
    for (const cm of comments) console.log(`        line ${cm.line} [${cm.severity}/${cm.category}] ${cm.message.slice(0, 90)}`);

    done.set(c.id, { ...score, seededBugLines: c.bugs.map((b) => b.line), error, comments });
    save(); // after every case
    if (quotaStreak >= 2) {
      stoppedEarly = "Provider keeps rejecting requests for quota (likely the daily limit)";
      break;
    }
    if (i < todo.length - 1) await sleep(8_000); // stay under ~7 requests/minute
  }

  const finished = save(stoppedEarly);
  const good = finished.filter((c) => !c.error);
  const s = summarize(good);
  console.log(`\n==== Results${good.length < all.length ? ` (PARTIAL: ${good.length} of ${all.length} cases)` : ""} ====`);
  console.log(`Cases:               ${s.cases} (${s.buggyCases} with seeded bugs, ${s.cleanCases} clean)`);
  console.log(`Recall:              ${pct(s.recall)}   (${s.found} of ${s.bugs} seeded bugs found)`);
  console.log(`Precision:           ${pct(s.precision)}   (${s.truePositives} of ${s.reported} comments pointed at a seeded bug)`);
  console.log(`Clean files flagged: ${pct(s.cleanFlaggedRate)}   (${s.cleanCasesFlagged} of ${s.cleanCases} clean cases got a comment)`);
  console.log(`\nSaved ${outFile}`);
  if (stoppedEarly) console.log(`\n${stoppedEarly}. Run again later with:  npm run eval -w apps/api -- --resume`);
}

// Let the event loop drain instead of calling process.exit(): on Windows, exiting while network
// handles are still closing crashes Node with a libuv assertion.
main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
