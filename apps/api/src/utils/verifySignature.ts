import crypto from "node:crypto";

/** Verifies GitHub's X-Hub-Signature-256 header against the raw request body. */
export function verifySignature(rawBody: Buffer, signature: string | undefined, secret: string): boolean {
  if (!signature) return false;
  const expected = "sha256=" + crypto.createHmac("sha256", secret).update(rawBody).digest("hex");
  const a = Buffer.from(signature);
  const b = Buffer.from(expected);
  // timingSafeEqual throws on length mismatch, so check first.
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
