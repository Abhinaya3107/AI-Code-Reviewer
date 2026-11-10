import YAML from "yaml";
import { z } from "zod";
import { CATEGORIES, SEVERITIES } from "./reviewSchema.js";

export const CONFIG_PATH = ".aireview.yml";

const repoConfigSchema = z.object({
  enabled: z.boolean().default(true),
  /** Lowest severity to post: "warning" hides suggestions and nitpicks. */
  severity_threshold: z.enum(SEVERITIES).default("nitpick"),
  max_comments: z.number().int().min(1).max(30).default(15),
  ignore_paths: z.array(z.string().min(1).max(200)).max(50).default([]),
  focus: z.array(z.enum(CATEGORIES)).default([]),
  custom_instructions: z.string().max(2000).default(""),
});

export type RepoConfig = z.infer<typeof repoConfigSchema>;

export const DEFAULT_CONFIG: RepoConfig = repoConfigSchema.parse({});

/** Parses and validates .aireview.yml. Invalid files fall back to defaults and report why. */
export function parseRepoConfig(text: string): { config: RepoConfig; error?: string } {
  try {
    const raw = YAML.parse(text) ?? {};
    const parsed = repoConfigSchema.safeParse(raw);
    if (!parsed.success) {
      const issue = parsed.error.issues[0];
      return { config: DEFAULT_CONFIG, error: `${issue.path.join(".") || "file"}: ${issue.message}` };
    }
    return { config: parsed.data };
  } catch (err) {
    return { config: DEFAULT_CONFIG, error: `invalid YAML (${err instanceof Error ? err.message.split("\n")[0] : "parse error"})` };
  }
}
