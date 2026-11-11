import json
import re

from reviewer.ports import LlmResponse


class FakeLlm:
    """Offline stand-in. It comments on the first added line it sees, so the whole pipeline (parse,
    validate, post) can be exercised without a paid key."""

    model = "fake-llm"

    async def review(self, *, system: str, user: str) -> LlmResponse:
        file = re.search(r"### File: (.+)", user)
        added = re.search(r"^\s*(\d+) \+ ", user, re.MULTILINE)
        comments = []
        if file and added:
            comments.append(
                {
                    "path": file.group(1),
                    "line": int(added.group(1)),
                    "severity": "nitpick",
                    "category": "maintainability",
                    "message": "[demo] This comment comes from the fake LLM, to test the pipeline end to end.",
                }
            )
        return LlmResponse(
            text=json.dumps(
                {
                    "summary": "[demo] Placeholder review generated without a real model. Pipeline check only.",
                    "overall_risk": "low",
                    "comments": comments,
                }
            )
        )
