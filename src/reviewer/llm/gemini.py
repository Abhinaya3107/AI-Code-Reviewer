import httpx

from reviewer.ports import LlmResponse
from reviewer.review.schema import CATEGORIES, RISKS, SEVERITIES

API_URL = "https://generativelanguage.googleapis.com/v1beta"

# Constrains Gemini's output so the enum fields (severity, category) cannot be mixed up or invented.
RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary": {"type": "STRING"},
        "overall_risk": {"type": "STRING", "enum": list(RISKS)},
        "comments": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "path": {"type": "STRING"},
                    "line": {"type": "INTEGER"},
                    "severity": {"type": "STRING", "enum": list(SEVERITIES)},
                    "category": {"type": "STRING", "enum": list(CATEGORIES)},
                    "message": {"type": "STRING"},
                    "suggested_fix": {"type": "STRING"},
                },
                "required": ["path", "line", "severity", "category", "message"],
            },
        },
    },
    "required": ["summary", "overall_risk", "comments"],
}


class LlmError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"Gemini API error {status}: {detail}")
        self.status = status


class GeminiLlm:
    """Google Gemini over REST, in JSON mode at a low temperature for consistent reviews."""

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.5-flash",
        *,
        http: httpx.AsyncClient | None = None,
        api_url: str = API_URL,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._api_url = api_url
        self._http = http or httpx.AsyncClient(timeout=120)

    async def review(self, *, system: str, user: str) -> LlmResponse:
        res = await self._http.post(
            f"{self._api_url}/models/{self.model}:generateContent",
            # The key goes in a header, not the URL, so it cannot end up in access logs.
            headers={"x-goog-api-key": self._api_key},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {
                    "temperature": 0.2,
                    "responseMimeType": "application/json",
                    "responseSchema": RESPONSE_SCHEMA,
                },
            },
        )
        if res.is_error:
            # Never include the request here: it holds the key and the user's code.
            raise LlmError(res.status_code, res.text[:300])
        data = res.json()
        candidate = (data.get("candidates") or [{}])[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        if not text:
            raise LlmError(
                200, f"no text returned (finishReason: {candidate.get('finishReason', 'unknown')})"
            )
        usage = data.get("usageMetadata") or {}
        return LlmResponse(
            text=text,
            input_tokens=usage.get("promptTokenCount"),
            output_tokens=usage.get("candidatesTokenCount"),
        )
