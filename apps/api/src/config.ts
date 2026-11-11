import { z } from "zod";

const schema = z.object({
  PORT: z.coerce.number().default(3001),
  GITHUB_APP_ID: z.string().min(1),
  GITHUB_PRIVATE_KEY: z.string().min(1),
  GITHUB_WEBHOOK_SECRET: z.string().min(1),
  REDIS_URL: z.string().default("redis://localhost:6379"),
  // "fake" = offline demo model, "gemini" = Google Gemini (needs LLM_API_KEY).
  LLM_PROVIDER: z.string().default("fake"),
  LLM_API_KEY: z.string().optional(),
  LLM_MODEL: z.string().optional(),
  // Without it the worker still reviews PRs but stores nothing.
  DATABASE_URL: z.string().optional(),
});

export const config = schema.parse(process.env);
