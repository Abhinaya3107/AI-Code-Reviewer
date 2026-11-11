import json
import logging

import httpx
import pytest
import respx

from reviewer.config import Settings
from reviewer.llm import FakeLlm, GeminiLlm, LlmError, create_llm
from reviewer.logging_setup import JsonFormatter

URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"


class TestGemini:
    async def test_sends_the_key_in_a_header_never_the_url_and_constrains_the_output(self) -> None:
        reply = {
            "candidates": [{"content": {"parts": [{"text": '{"summary":"s"}'}]}}],
            "usageMetadata": {"promptTokenCount": 120, "candidatesTokenCount": 30},
        }
        with respx.mock() as router:
            route = router.post(URL).mock(return_value=httpx.Response(200, json=reply))
            res = await GeminiLlm("SECRET-KEY", http=httpx.AsyncClient()).review(system="SYS", user="USER")

        request = route.calls[0].request
        assert request.headers["x-goog-api-key"] == "SECRET-KEY"
        assert "SECRET-KEY" not in str(request.url)
        body = json.loads(request.content)
        assert body["systemInstruction"]["parts"][0]["text"] == "SYS"
        assert body["contents"][0]["parts"][0]["text"] == "USER"
        assert body["generationConfig"]["temperature"] == 0.2
        schema = body["generationConfig"]["responseSchema"]["properties"]["comments"]["items"]["properties"]
        assert schema["severity"]["enum"] == ["critical", "warning", "suggestion", "nitpick"]
        assert "bug" in schema["category"]["enum"] and "bug" not in schema["severity"]["enum"]
        assert (res.text, res.input_tokens, res.output_tokens) == ('{"summary":"s"}', 120, 30)

    async def test_api_errors_do_not_leak_the_key_or_the_code(self) -> None:
        with respx.mock() as router:
            router.post(URL).mock(return_value=httpx.Response(429, text="quota exceeded"))
            with pytest.raises(LlmError) as err:
                await GeminiLlm("SECRET-KEY", http=httpx.AsyncClient()).review(
                    system="SYS", user="MY PRIVATE CODE"
                )
        assert err.value.status == 429
        assert "SECRET-KEY" not in str(err.value) and "MY PRIVATE CODE" not in str(err.value)

    async def test_an_empty_reply_is_an_error_with_the_finish_reason(self) -> None:
        with respx.mock() as router:
            router.post(URL).mock(
                return_value=httpx.Response(200, json={"candidates": [{"finishReason": "SAFETY"}]})
            )
            with pytest.raises(LlmError, match="SAFETY"):
                await GeminiLlm("k", http=httpx.AsyncClient()).review(system="s", user="u")


class TestFakeAndFactory:
    async def test_fake_comments_on_the_first_added_line(self) -> None:
        res = await FakeLlm().review(system="", user="### File: a.py\n    7 + x = 1\n")
        comment = json.loads(res.text)["comments"][0]
        assert (comment["path"], comment["line"]) == ("a.py", 7)

    def test_factory_builds_known_providers_and_rejects_the_rest(self) -> None:
        assert create_llm("fake").model == "fake-llm"
        assert create_llm("gemini", "key", "gemini-2.5-pro").model == "gemini-2.5-pro"
        assert create_llm("gemini", "key").model == "gemini-2.5-flash"
        with pytest.raises(ValueError, match="LLM_API_KEY"):
            create_llm("gemini")
        with pytest.raises(ValueError, match="Unsupported"):
            create_llm("openai", "key")


def settings(**over: object) -> Settings:
    base: dict[str, object] = {"github_app_id": "1", "github_private_key": "k", "github_webhook_secret": "s"}
    return Settings(_env_file=None, **{**base, **over})  # type: ignore[arg-type]


class TestSettings:
    def test_private_key_literal_newlines_become_real_ones(self) -> None:
        s = settings(github_private_key="-----BEGIN-----\\nABC\\n-----END-----")
        assert s.github_private_key == "-----BEGIN-----\nABC\n-----END-----"

    def test_blank_optional_values_are_treated_as_unset(self) -> None:
        s = settings(llm_api_key="", llm_model="  ", database_url="")
        assert s.llm_api_key is None and s.llm_model is None and s.database_url is None
        assert s.sqlalchemy_url is None

    def test_database_url_is_pointed_at_the_psycopg_driver(self) -> None:
        for scheme in ("postgresql", "postgres"):
            s = settings(database_url=f"{scheme}://u:p@host/db?sslmode=require")
            assert s.sqlalchemy_url == "postgresql+psycopg://u:p@host/db?sslmode=require"

    def test_required_settings_are_enforced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("GITHUB_APP_ID", "GITHUB_PRIVATE_KEY", "GITHUB_WEBHOOK_SECRET"):
            monkeypatch.delenv(name, raising=False)
        with pytest.raises(ValueError):
            Settings(_env_file=None)  # type: ignore[call-arg]


class TestJsonLogging:
    def test_emits_one_json_object_with_extra_fields(self) -> None:
        record = logging.LogRecord("reviewer.test", logging.INFO, __file__, 1, "review prepared", None, None)
        record.kept = 3
        data = json.loads(JsonFormatter().format(record))
        assert data["msg"] == "review prepared" and data["level"] == "info" and data["kept"] == 3
