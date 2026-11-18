"""Backend Ollama (LLM local) via son API native /api/chat."""

import json
import logging
import uuid

import requests

from .base import LLMClient, LLMError, LLMResponse, ToolCall, parse_arguments

log = logging.getLogger(__name__)


class OllamaClient(LLMClient):
    name = "ollama"

    def __init__(self, model: str = "qwen2.5:7b", base_url: str = "http://localhost:11434",
                 temperature: float = 0.0, seed: int | None = 42, num_ctx: int = 8192,
                 num_predict: int = 512, timeout: float = 300):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.options = {"temperature": temperature, "num_ctx": num_ctx, "num_predict": num_predict}
        if seed is not None:
            self.options["seed"] = seed
        self.timeout = timeout  # large : le premier appel charge le modèle en mémoire

    @staticmethod
    def _to_ollama(messages: list[dict]) -> list[dict]:
        """Format interne (OpenAI) -> format Ollama."""
        out = []
        for m in messages:
            if m["role"] == "assistant" and m.get("tool_calls"):
                calls = []
                for tc in m["tool_calls"]:
                    args, _, _ = parse_arguments(tc["function"]["arguments"])
                    calls.append({"function": {"name": tc["function"]["name"], "arguments": args}})
                out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
            elif m["role"] == "tool":
                out.append({"role": "tool", "content": m["content"], "tool_name": m.get("name", "")})
            else:
                out.append({"role": m["role"], "content": m.get("content") or ""})
        return out

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        payload = {
            "model": self.model,
            "messages": self._to_ollama(messages),
            "tools": tools,  # même schéma que l'API OpenAI
            "stream": False,
            "options": self.options,
        }
        try:
            r = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout)
        except requests.ConnectionError as e:
            raise LLMError(f"Ollama injoignable sur {self.base_url} (lancer `ollama serve`)") from e
        except requests.Timeout as e:
            raise LLMError(f"Ollama n'a pas répondu en {self.timeout:.0f} s") from e
        if r.status_code != 200:
            raise LLMError(f"Ollama HTTP {r.status_code} : {r.text[:300]}")
        try:
            data = r.json()
            msg = data["message"]
        except (ValueError, KeyError) as e:
            raise LLMError(f"réponse Ollama inattendue : {r.text[:300]}") from e
        if data.get("done_reason") == "length":
            log.warning("Réponse tronquée à num_predict=%s tokens : le modèle s'est emballé",
                        self.options["num_predict"])

        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            args, raw, err = parse_arguments(fn.get("arguments"))
            calls.append(ToolCall(id=tc.get("id") or f"call_{uuid.uuid4().hex[:8]}", name=fn.get("name", ""),
                                  arguments=args, raw_arguments=raw, parse_error=err))
        return LLMResponse(content=msg.get("content") or "", tool_calls=calls)

    def __repr__(self) -> str:
        return f"OllamaClient(model={self.model!r}, options={json.dumps(self.options)})"
