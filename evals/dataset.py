"""The evaluation dataset: small new files, some with seeded bugs.

A line ending in "@@BUG:<category>@@" is a seeded bug (the marker is removed before the reviewer sees the
code). Cases with no marker are CLEAN: any comment on them is a false alarm. Keep cases small and the
intended bug unambiguous.
"""

from evals.score import EvalCase


def c(id: str, title: str, filename: str, code: str) -> EvalCase:
    return EvalCase(id, title, filename, code.strip("\n"))


DATASET: list[EvalCase] = [
    # ---------- seeded bugs: TypeScript ----------
    c(
        "ts-sql-injection",
        "Add user lookup by email",
        "src/users.ts",
        """
import { db } from "./db";

export async function getUserByEmail(email: string) {
  const sql = "SELECT id, name FROM users WHERE email = '" + email + "'"; @@BUG:security@@
  const rows = await db.query(sql);
  return rows[0];
}
""",
    ),
    c(
        "ts-missing-await",
        "Save items in bulk",
        "src/save.ts",
        """
import { db } from "./db";

export async function saveAll(items: string[]) {
  for (const item of items) {
    db.insert("items", { value: item }); @@BUG:bug@@
  }
  return items.length;
}
""",
    ),
    c(
        "ts-off-by-one",
        "Add lastN helper",
        "src/lastN.ts",
        """
export function lastN(items: number[], n: number): number[] {
  const out: number[] = [];
  for (let i = items.length - n; i <= items.length; i++) { @@BUG:bug@@
    out.push(items[i]);
  }
  return out;
}
""",
    ),
    c(
        "ts-assignment-in-condition",
        "Add delete permission check",
        "src/perm.ts",
        """
type User = { role: string; name: string };

export function canDelete(user: User): boolean {
  if (user.role = "admin") { @@BUG:bug@@
    return true;
  }
  return false;
}
""",
    ),
    c(
        "ts-property-typo",
        "Add average helper",
        "src/average.ts",
        """
export function average(nums: number[]): number {
  let total = 0;
  for (const n of nums) {
    total += n;
  }
  return total / nums.lenght; @@BUG:bug@@
}
""",
    ),
    c(
        "ts-hardcoded-password",
        "Add database connection",
        "src/db.ts",
        """
import { createConnection } from "mysql2";

export function connect() {
  return createConnection({
    host: "db.internal",
    user: "admin",
    password: "hunter2-hunter2", @@BUG:security@@
  });
}
""",
    ),
    c(
        "ts-command-injection",
        "List a directory",
        "src/ls.ts",
        """
import { exec } from "node:child_process";

export function listDir(dir: string, cb: (out: string) => void) {
  exec(`ls -la ${dir}`, (err, stdout) => { @@BUG:security@@
    if (err) throw err;
    cb(stdout);
  });
}
""",
    ),
    c(
        "ts-path-traversal",
        "Serve uploaded files",
        "src/uploads.ts",
        """
import { readFile } from "node:fs/promises";
import path from "node:path";

const BASE = "/srv/uploads";

export async function readUpload(name: string) {
  const full = path.join(BASE, name); @@BUG:security@@
  return readFile(full, "utf8");
}
""",
    ),
    c(
        "ts-xss-innerhtml",
        "Render comments",
        "src/render.ts",
        """
export function renderComment(el: HTMLElement, comment: string) {
  el.innerHTML = "<p>" + comment + "</p>"; @@BUG:security@@
}
""",
    ),
    c(
        "ts-null-deref",
        "Add name lookup",
        "src/names.ts",
        """
type User = { id: number; name: string };

export function nameOf(users: User[], id: number): string {
  return users.find((u) => u.id === id).name; @@BUG:bug@@
}
""",
    ),
    c(
        "ts-default-sort",
        "Add top scores",
        "src/scores.ts",
        """
export function topThree(scores: number[]): number[] {
  return scores.sort().slice(-3); @@BUG:bug@@
}
""",
    ),
    c(
        "ts-divide-by-zero",
        "Add percent formatter",
        "src/percent.ts",
        """
export function percent(done: number, total: number): string {
  return ((done / total) * 100).toFixed(1) + "%"; @@BUG:bug@@
}
""",
    ),
    c(
        "ts-two-bugs-transfer",
        "Add money transfer",
        "src/transfer.ts",
        """
import { db } from "./db";

export async function transfer(from: string, to: string, amount: number) {
  const balance = await db.getBalance(from);
  if (balance > amount) {
    db.debit(from, amount); @@BUG:bug@@
    await db.credit(to, amount);
  }
  console.log("Transferred " + amount + " using token " + process.env.API_TOKEN); @@BUG:security@@
}
""",
    ),
    # ---------- seeded bugs: Python ----------
    c(
        "py-mutable-default",
        "Add tag helper",
        "app/tags.py",
        """
def add_tag(tag, tags=[]):  @@BUG:bug@@
    tags.append(tag)
    return tags
""",
    ),
    c(
        "py-bare-except",
        "Load config file",
        "app/config.py",
        """
def load_config(path):
    try:
        with open(path) as f:
            return f.read()
    except:  @@BUG:bug@@
        pass
""",
    ),
    c(
        "py-sql-fstring",
        "Find user by name",
        "app/users.py",
        """
def find_user(cursor, name):
    cursor.execute(f"SELECT * FROM users WHERE name = '{name}'")  @@BUG:security@@
    return cursor.fetchone()
""",
    ),
    c(
        "py-is-literal",
        "Add admin check",
        "app/roles.py",
        """
def is_admin(role):
    if role is "admin":  @@BUG:bug@@
        return True
    return False
""",
    ),
    c(
        "py-unsafe-yaml",
        "Parse settings",
        "app/settings.py",
        """
import yaml

def parse(text):
    return yaml.load(text)  @@BUG:security@@
""",
    ),
    c(
        "py-pickle-loads",
        "Restore saved state",
        "app/state.py",
        """
import pickle

def restore(blob: bytes):
    return pickle.loads(blob)  @@BUG:security@@
""",
    ),
    # ---------- clean: any comment here is a false alarm ----------
    c(
        "clean-parameterized-query",
        "Add user lookup by email",
        "src/users.ts",
        """
import { db } from "./db";

export async function getUserByEmail(email: string) {
  const rows = await db.query("SELECT id, name FROM users WHERE email = $1", [email]);
  return rows[0] ?? null;
}
""",
    ),
    c(
        "clean-safe-path",
        "Serve uploaded files",
        "src/uploads.ts",
        """
import { readFile } from "node:fs/promises";
import path from "node:path";

const BASE = path.resolve("/srv/uploads");

export async function readUpload(name: string) {
  const full = path.resolve(BASE, name);
  if (!full.startsWith(BASE + path.sep)) {
    throw new Error("Invalid path");
  }
  return readFile(full, "utf8");
}
""",
    ),
    c(
        "clean-math-helpers",
        "Add math helpers",
        "src/math.ts",
        """
export function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

export function average(nums: number[]): number {
  if (nums.length === 0) return 0;
  return nums.reduce((sum, n) => sum + n, 0) / nums.length;
}
""",
    ),
    c(
        "clean-retry",
        "Add retry helper",
        "src/retry.ts",
        """
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export async function withRetry<T>(fn: () => Promise<T>, attempts = 3): Promise<T> {
  if (attempts < 1) throw new RangeError("attempts must be at least 1");
  let lastError: unknown;
  for (let i = 0; i < attempts; i++) {
    try {
      return await fn();
    } catch (err) {
      lastError = err;
      await sleep(100 * 2 ** i);
    }
  }
  throw lastError;
}
""",
    ),
    c(
        "clean-debounce",
        "Add debounce",
        "src/debounce.ts",
        """
export function debounce<A extends unknown[]>(fn: (...args: A) => void, waitMs: number) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  return (...args: A) => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => fn(...args), waitMs);
  };
}
""",
    ),
    c(
        "clean-type-guard",
        "Add status type guard",
        "src/status.ts",
        """
export type Status = "queued" | "running" | "done";

const STATUSES: readonly Status[] = ["queued", "running", "done"];

export function isStatus(value: unknown): value is Status {
  return typeof value === "string" && (STATUSES as readonly string[]).includes(value);
}
""",
    ),
    c(
        "clean-py-dataclass",
        "Add shopping cart",
        "app/cart.py",
        """
from dataclasses import dataclass, field


@dataclass
class Cart:
    items: list = field(default_factory=list)

    def add(self, item):
        self.items.append(item)

    def total(self):
        return sum(i["price"] * i["qty"] for i in self.items)
""",
    ),
    c(
        "clean-py-safe-sql",
        "Find user by name",
        "app/users.py",
        """
def find_user(cursor, name):
    cursor.execute("SELECT * FROM users WHERE name = %s", (name,))
    return cursor.fetchone()
""",
    ),
]
