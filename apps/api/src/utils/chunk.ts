/**
 * Groups items into chunks whose combined text length stays under maxChars (roughly 4 chars per token).
 * An item that alone exceeds the budget is truncated so it still fits in its own chunk.
 */
export function chunkByBudget<T extends { annotated: string }>(items: T[], maxChars: number): T[][] {
  const chunks: T[][] = [];
  let current: T[] = [];
  let size = 0;

  for (const item of items) {
    const fitted =
      item.annotated.length > maxChars
        ? { ...item, annotated: item.annotated.slice(0, maxChars) + "\n[diff truncated]" }
        : item;
    if (size + fitted.annotated.length > maxChars && current.length > 0) {
      chunks.push(current);
      current = [];
      size = 0;
    }
    current.push(fitted);
    size += fitted.annotated.length;
  }
  if (current.length > 0) chunks.push(current);
  return chunks;
}
