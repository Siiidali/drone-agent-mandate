"""Backend pour toute API compatible OpenAI Chat Completions."""

import os

import requests

from .base import LLMClient, LLMError, LLMResponse, ToolCall, parse_arguments


class OpenAICompatibleClient(LLMClient):
    name = "openai"

    def __init__(self, model: str, base_url: str = "https://api.openai.com/v1",
                 api_key_env: str = "OPENAI_API_KEY", temperature: float = 0.0,
                 seed: int | None = 42, timeout: float = 120):
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise LLMError(f"variable d'environnement {api_key_env} absente ou vide")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.seed = seed
        self.timeout = timeout
        self._headers = {"Authorization": f"Bearer {api_key}"}

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        payload = {"model": self.model, "messages": messages, "tools": tools, "temperature": self.temperature}
        if self.seed is not None:
            payload["seed"] = self.seed
        try:
            r = requests.post(f"{self.base_url}/chat/completions", json=payload,
                              headers=self._headers, timeout=self.timeout)
        except requests.RequestException as e:
            raise LLMError(f"API injoignable ({self.base_url}) : {e}") from e
        if r.status_code != 200:
            raise LLMError(f"API HTTP {r.status_code} : {r.text[:300]}")
        try:
            msg = r.json()["choices"][0]["message"]
        except (ValueError, KeyError, IndexError) as e:
            raise LLMError(f"réponse inattendue : {r.text[:300]}") from e

        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            args, raw, err = parse_arguments(fn.get("arguments"))
            calls.append(ToolCall(id=tc["id"], name=fn.get("name", ""), arguments=args,
                                  raw_arguments=raw, parse_error=err))
        return LLMResponse(content=msg.get("content") or "", tool_calls=calls)

    def __repr__(self) -> str:
        return f"OpenAICompatibleClient(model={self.model!r}, base_url={self.base_url!r})"
