from reviewer.llm.fake import FakeLlm
from reviewer.llm.gemini import GeminiLlm, LlmError
from reviewer.ports import LlmClient


def create_llm(provider: str, api_key: str | None = None, model: str | None = None) -> LlmClient:
    if provider == "fake":
        return FakeLlm()
    if provider == "gemini":
        if not api_key:
            raise ValueError("LLM_PROVIDER=gemini requires LLM_API_KEY")
        return GeminiLlm(api_key, model or "gemini-2.5-flash")
    raise ValueError(f'Unsupported LLM_PROVIDER "{provider}". Use "fake" or "gemini".')


__all__ = ["FakeLlm", "GeminiLlm", "LlmError", "create_llm"]
