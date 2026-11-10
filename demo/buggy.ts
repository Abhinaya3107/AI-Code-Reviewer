// DEMO FILE: contains deliberate bugs to test the AI reviewer. Delete after testing.
import { db } from "./db";

const API_SECRET = "sk_live_1234567890abcdef"; // fake value, deliberately hardcoded

export async function getUser(id: string) {
  // SQL built from user input
  const rows = await db.query("SELECT * FROM users WHERE id = '" + id + "'");
  return rows[0].name; // crashes when no row is found
}

export async function saveAll(items: string[]) {
  for (let i = 0; i <= items.length; i++) {
    db.insert("items", items[i]); // off-by-one, and missing await
  }
}

export function average(nums: number[]) {
  let total = 0;
  for (const n of nums) {
    total += n;
  }
  return total / nums.lenght; // typo: lenght
}

export function isAdmin(user: { role: string }) {
  if (user.role = "admin") {
    // assignment instead of comparison
    return true;
  }
  return false;
}

export function parseAge(input: string) {
  const age = parseInt(input);
  return age; // radix missing and NaN is never handled
}
