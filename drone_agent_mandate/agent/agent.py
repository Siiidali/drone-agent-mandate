"""Boucle de l'agent : le LLM choisit un outil, on l'exécute, on lui renvoie le résultat."""

import logging
import time
from dataclasses import dataclass, field

from ..llm.base import LLMClient, LLMError
from .tools import TOOLS, ToolExecutor, ToolRecord

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Tu es le système de pilotage automatique d'un drone quadrirotor.
Tu exécutes la mission confiée par l'opérateur en appelant les outils disponibles,
un ordre après l'autre, en attendant le résultat de chaque outil avant de continuer.

Règles :
- Les altitudes sont en mètres au-dessus du point de décollage.
- Utilise les coordonnées exactes données dans la mission.
- Si un outil renvoie une erreur, adapte-toi ; ne répète pas indéfiniment le même appel.
- Quand la mission est terminée, réponds par un court compte rendu, sans appeler d'outil."""


@dataclass
class AgentResult:
    mission: str
    final_message: str
    stop_reason: str                      # "done" | "max_steps" | "llm_error"
    trace: list[ToolRecord]
    messages: list[dict] = field(repr=False)
    llm_calls: int = 0
    llm_time_s: float = 0.0

    def to_dict(self) -> dict:
        return {"mission": self.mission, "final_message": self.final_message, "stop_reason": self.stop_reason,
                "llm_calls": self.llm_calls, "llm_time_s": round(self.llm_time_s, 2),
                "trace": [r.to_dict() for r in self.trace], "messages": self.messages}


class DroneAgent:
    def __init__(self, llm: LLMClient, executor: ToolExecutor, max_steps: int = 25,
                 system_prompt: str = SYSTEM_PROMPT, max_calls_per_turn: int = 5,
                 observation_barrier: bool = True):
        self.llm = llm
        self.executor = executor
        self.max_steps = max_steps
        self.max_calls_per_turn = max_calls_per_turn
        self.observation_barrier = observation_barrier
        self.system_prompt = system_prompt

    def run(self, mission: str) -> AgentResult:
        messages: list[dict] = [{"role": "system", "content": self.system_prompt},
                                {"role": "user", "content": mission}]
        llm_calls, llm_time = 0, 0.0

        def result(final: str, reason: str) -> AgentResult:
            return AgentResult(mission, final, reason, self.executor.trace, messages, llm_calls, llm_time)

        for _ in range(self.max_steps):
            t0 = time.perf_counter()
            try:
                response = self.llm.chat(messages, TOOLS)
            except LLMError as e:
                log.error("Erreur LLM : %s", e)
                return result(str(e), "llm_error")
            llm_calls += 1
            llm_time += time.perf_counter() - t0
            messages.append(response.to_message())
            if response.content:
                log.info("LLM : %s", response.content.strip())

            if not response.tool_calls:  # pas d'outil demandé = compte rendu final
                return result(response.content, "done")

            failed = observed = False
            for i, call in enumerate(response.tool_calls):
                if i >= self.max_calls_per_turn:
                    output = self.executor.skip(call, f"plus de {self.max_calls_per_turn} appels dans un même tour")
                elif failed:
                    output = self.executor.skip(call)
                elif observed:
                    output = self.executor.skip(call, "une observation vient d'être faite : analyse-la avant de "
                                                      "décider de la suite")
                else:
                    output = self.executor.execute(call)
                    failed = not self.executor.trace[-1].ok
                    observed = self.observation_barrier and call.name == "observe_scene" and not failed
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": output})

        log.warning("Nombre maximal de tours atteint (%d)", self.max_steps)
        return result("", "max_steps")
