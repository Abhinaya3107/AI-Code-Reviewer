import { z } from "zod";

const schema = z.object({
  PORT: z.coerce.number().default(3001),
  GITHUB_APP_ID: z.string().min(1),
  GITHUB_PRIVATE_KEY: z.string().min(1),
  GITHUB_WEBHOOK_SECRET: z.string().min(1),
  REDIS_URL: z.string().default("redis://localhost:6379"),
});

export const config = schema.parse(process.env);
