/** USD per 1M tokens. Approximate list prices: check the provider's pricing page and update as needed. */
const PRICING: Record<string, { input: number; output: number }> = {
  "gemini-2.5-flash": { input: 0.3, output: 2.5 },
  "gemini-2.5-pro": { input: 1.25, output: 10 },
};

/** Estimated cost in USD, or null when the model has no known price (including the fake model). */
export function estimateCostUsd(model: string, inputTokens: number, outputTokens: number): number | null {
  const p = PRICING[model];
  if (!p) return null;
  return (inputTokens * p.input + outputTokens * p.output) / 1_000_000;
}
