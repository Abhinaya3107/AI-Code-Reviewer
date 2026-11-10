/** Anything that can turn a prompt into raw JSON text. Real providers (Claude, OpenAI) plug in here. */
export interface LlmClient {
  readonly model: string;
  review(args: { system: string; user: string }): Promise<string>;
}

/**
 * Offline stand-in used until a real provider is wired in. It finds the first added line in the
 * prompt and comments on it, so the full pipeline (parse, validate, post) can be tested for free.
 */
export class FakeLlm implements LlmClient {
  readonly model = "fake-llm";

  async review({ user }: { system: string; user: string }): Promise<string> {
    const file = /### File: (.+)/.exec(user)?.[1];
    const added = /^\s*(\d+) \+ /m.exec(user)?.[1];
    const comments =
      file && added
        ? [
            {
              path: file,
              line: Number(added),
              severity: "nitpick",
              category: "maintainability",
              message: "[demo] This comment comes from the fake LLM, to test the review pipeline end to end.",
            },
          ]
        : [];
    return JSON.stringify({
      summary: "[demo] Placeholder review generated without a real model. Pipeline check only.",
      overall_risk: "low",
      comments,
    });
  }
}

/** Google Gemini via the REST API. Uses JSON mode and a low temperature for consistent reviews. */
export class GeminiLlm implements LlmClient {
  constructor(
    private readonly apiKey: string,
    readonly model: string,
  ) {}

  async review({ system, user }: { system: string; user: string }): Promise<string> {
    const url = `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(this.model)}:generateContent`;
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-goog-api-key": this.apiKey },
      body: JSON.stringify({
        systemInstruction: { parts: [{ text: system }] },
        contents: [{ role: "user", parts: [{ text: user }] }],
        generationConfig: { temperature: 0.2, responseMimeType: "application/json" },
      }),
      signal: AbortSignal.timeout(120_000),
    });
    if (!res.ok) {
      // Never include the request (it holds the key and the user's code) in the error.
      const detail = (await res.text()).slice(0, 300);
      throw new Error(`Gemini API error ${res.status}: ${detail}`);
    }
    const data = (await res.json()) as {
      candidates?: { content?: { parts?: { text?: string }[] }; finishReason?: string }[];
    };
    const text = data.candidates?.[0]?.content?.parts?.map((p) => p.text ?? "").join("");
    if (!text) throw new Error(`Gemini returned no text (finishReason: ${data.candidates?.[0]?.finishReason ?? "unknown"})`);
    return text;
  }
}

export interface LlmSettings {
  provider: string;
  apiKey?: string;
  model?: string;
}

export function createLlm({ provider, apiKey, model }: LlmSettings): LlmClient {
  switch (provider) {
    case "fake":
      return new FakeLlm();
    case "gemini":
      if (!apiKey) throw new Error('LLM_PROVIDER=gemini requires LLM_API_KEY in .env');
      return new GeminiLlm(apiKey, model || "gemini-2.5-flash");
    default:
      throw new Error(`Unsupported LLM_PROVIDER "${provider}". Use "fake" or "gemini".`);
  }
}
