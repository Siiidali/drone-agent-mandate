from .base import LLMClient, LLMError, LLMResponse, ToolCall


def make_llm(cfg: dict) -> LLMClient:
    """Construit le backend décrit par la section `llm` de la configuration."""
    backend = cfg["backend"]
    common = {"model": cfg["model"], "temperature": cfg.get("temperature", 0.0), "seed": cfg.get("seed")}
    if backend == "ollama":
        from .ollama_client import OllamaClient
        return OllamaClient(**common, **cfg.get("ollama", {}))
    if backend == "openai":
        from .openai_client import OpenAICompatibleClient
        return OpenAICompatibleClient(**common, **cfg.get("openai", {}))
    raise ValueError(f"backend LLM inconnu : {backend!r} (attendu : ollama ou openai)")


__all__ = ["LLMClient", "LLMError", "LLMResponse", "ToolCall", "make_llm"]
