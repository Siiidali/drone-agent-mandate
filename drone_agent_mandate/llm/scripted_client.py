"""Faux LLM qui rejoue des réponses écrites à l'avance."""

import copy
import itertools
from typing import Callable

from .base import LLMClient, LLMResponse, ToolCall

Step = LLMResponse | Callable[[list[dict]], LLMResponse]


_ids = itertools.count(1)


def call(tool: str, /, **arguments) -> LLMResponse:
    """Raccourci : une réponse qui appelle un seul outil."""
    return LLMResponse(content="", tool_calls=[ToolCall(id=f"call_{next(_ids)}", name=tool, arguments=arguments)])


def say(text: str) -> LLMResponse:
    """Raccourci : une réponse texte sans appel d'outil (fin de mission)."""
    return LLMResponse(content=text)


class ScriptedLLM(LLMClient):
    name = "scripted"

    def __init__(self, script: list[Step]):
        self.script = list(script)
        self.seen: list[list[dict]] = []  # historique reçu à chaque appel, pour les assertions

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        self.seen.append(copy.deepcopy(messages))
        if not self.script:
            return say("Fin du script.")
        step = self.script.pop(0)
        return step(messages) if callable(step) else step
