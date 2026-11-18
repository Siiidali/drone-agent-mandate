"""Interface commune aux LLM, indépendante du fournisseur."""

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class LLMError(RuntimeError):
    """Le LLM est injoignable ou a renvoyé une réponse inexploitable."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    raw_arguments: str | None = None
    parse_error: str | None = None


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)

    def to_message(self) -> dict:
        """Message « assistant » à ajouter à l'historique de la conversation."""
        msg: dict = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.name,
                              "arguments": tc.raw_arguments if tc.parse_error else json.dumps(tc.arguments)}}
                for tc in self.tool_calls
            ]
        return msg


def parse_arguments(raw) -> tuple[dict, str | None, str | None]:
    """Normalise les arguments d'un appel d'outil -> (dict, texte brut, erreur)."""
    if isinstance(raw, dict):
        return raw, None, None
    if raw is None or raw == "":
        return {}, None, None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as e:
        return {}, str(raw), f"arguments JSON invalides : {e}"
    if not isinstance(value, dict):
        return {}, str(raw), "les arguments doivent être un objet JSON"
    return value, None, None


class LLMClient(ABC):
    """Un LLM capable d'appeler des outils (tool calling)."""

    name: str = "abstract"

    @abstractmethod
    def chat(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        """Envoie l'historique et la liste des outils, renvoie la réponse du modèle."""
