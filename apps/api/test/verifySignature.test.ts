import crypto from "node:crypto";
import { describe, expect, it } from "vitest";
import { verifySignature } from "../src/utils/verifySignature.js";

const sign = (body: Buffer, secret: string) =>
  "sha256=" + crypto.createHmac("sha256", secret).update(body).digest("hex");

describe("verifySignature", () => {
  const body = Buffer.from('{"a":1}');
  it("accepts a valid signature", () => expect(verifySignature(body, sign(body, "s"), "s")).toBe(true));
  it("rejects a wrong secret", () => expect(verifySignature(body, sign(body, "x"), "s")).toBe(false));
  it("rejects missing or wrong-length signature without throwing", () => {
    expect(verifySignature(body, undefined, "s")).toBe(false);
    expect(verifySignature(body, "sha256=abc", "s")).toBe(false);
  });
});
