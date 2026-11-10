import { z } from "zod";

export const SEVERITIES = ["critical", "warning", "suggestion", "nitpick"] as const;
export const CATEGORIES = ["bug", "security", "performance", "maintainability", "testing"] as const;

export const commentSchema = z.object({
  path: z.string().min(1),
  line: z.number().int().positive(),
  severity: z.enum(SEVERITIES),
  category: z.enum(CATEGORIES),
  message: z.string().min(1),
  suggested_fix: z.string().optional(),
});

export const reviewResultSchema = z.object({
  summary: z.string().min(1),
  overall_risk: z.enum(["low", "medium", "high"]),
  comments: z.array(commentSchema),
});

export type ReviewComment = z.infer<typeof commentSchema>;
export type ReviewResult = z.infer<typeof reviewResultSchema>;
