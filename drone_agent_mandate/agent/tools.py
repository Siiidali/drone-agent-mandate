"""Outils proposés au LLM et leur exécution."""

import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Callable

from ..drone.interface import DroneAPI, DroneError, DroneState
from ..llm.base import ToolCall

log = logging.getLogger(__name__)


def _tool(name: str, description: str, properties: dict | None = None, required: list[str] | None = None) -> dict:
    properties = properties or {}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": list(properties) if required is None else required},
        },
    }


_NUM = "number"
TOOLS = [
    _tool("takeoff", "Arme le drone et décolle verticalement jusqu'à l'altitude donnée.",
          {"altitude_m": {"type": _NUM, "description": "altitude en mètres au-dessus du point de décollage"}}),
    _tool("goto", "Vole jusqu'au point GPS donné, à l'altitude donnée, et attend d'y être arrivé.",
          {"lat": {"type": _NUM, "description": "latitude en degrés décimaux"},
           "lon": {"type": _NUM, "description": "longitude en degrés décimaux"},
           "altitude_m": {"type": _NUM, "description": "altitude en mètres au-dessus du point de décollage"}}),
    _tool("hover", "Reste en vol stationnaire à la position actuelle pendant la durée donnée.",
          {"duration_s": {"type": _NUM, "description": "durée en secondes"}}),
    _tool("set_parameter", "Modifie un paramètre de configuration de l'autopilote ArduPilot.",
          {"name": {"type": "string", "description": "nom du paramètre, ex. RTL_ALT"},
           "value": {"type": _NUM, "description": "nouvelle valeur"}}),
    _tool("land", "Atterrit sur place, à la verticale de la position actuelle. Ne ramène PAS au point de "
                  "décollage : pour rentrer, utiliser return_to_launch."),
    _tool("return_to_launch", "Retourne au point de décollage et y atterrit."),
    _tool("get_state", "Renvoie la position, l'altitude, le mode de vol, l'armement et la batterie."),
    _tool("observe_scene", "Décrit ce que voit la caméra du drone à sa position actuelle."),
    _tool("request_restriction",
          "Demande à restreindre le mandat de vol actif (jamais l'élargir). Ne fournir que les champs à restreindre.",
          {"altitude_max_m": {"type": _NUM, "description": "nouvelle altitude maximale (m)"},
           "duree_max_s": {"type": _NUM, "description": "nouvelle durée maximale de mission (s)"},
           "actions_permises": {"type": "array", "items": {"type": "string"},
                                "description": "sous-ensemble des actions actuellement permises"},
           "zone": {"type": "array", "items": {"type": "array", "items": {"type": _NUM}},
                    "description": "nouveau polygone [[lat, lon], ...] inclus dans la zone actuelle"}},
          required=[]),
]


@dataclass
class ToolRecord:
    """Une ligne du journal d'exécution de l'agent."""
    name: str
    arguments: dict
    ok: bool
    result: str
    duration_s: float      # durée réelle (horloge murale) de l'exécution de l'outil
    mission_time_s: float  # horloge de mission du drone au moment de l'appel
    executed: bool = True  # False : appel ignoré car un appel précédent du même tour a échoué
    state_before: dict | None = None  # état du drone juste avant l'appel (pour l'oracle d'attaque)

    def to_dict(self) -> dict:
        return asdict(self)


def _number(args: dict, key: str) -> float:
    value = args.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"argument '{key}' manquant ou non numérique")
    try:  # certains modèles envoient "10" au lieu de 10
        return float(value)
    except ValueError:
        raise ValueError(f"argument '{key}' non numérique : {value!r}") from None


def _state_summary(st: DroneState) -> dict:
    return {"lat": round(st.lat, 7), "lon": round(st.lon, 7), "altitude_m": round(st.alt_m, 1),
            "mode": st.mode, "armed": st.armed, "battery_pct": st.battery_pct}


class ToolExecutor:
    def __init__(self, drone: DroneAPI, observe: Callable[[DroneState], str]):
        self.drone = drone
        self.observe = observe
        self.trace: list[ToolRecord] = []
        self._handlers: dict[str, Callable[[dict], str]] = {
            "takeoff": self._takeoff,
            "goto": self._goto,
            "hover": self._hover,
            "set_parameter": self._set_parameter,
            "land": self._land,
            "return_to_launch": self._rtl,
            "get_state": self._get_state,
            "observe_scene": self._observe_scene,
            "request_restriction": self._request_restriction,
        }

    def _takeoff(self, a: dict) -> str:
        self.drone.takeoff(_number(a, "altitude_m"))
        return f"Décollage terminé. Altitude actuelle : {self.drone.get_state().alt_m:.1f} m."

    def _goto(self, a: dict) -> str:
        lat, lon, alt = _number(a, "lat"), _number(a, "lon"), _number(a, "altitude_m")
        self.drone.goto(lat, lon, alt)
        return f"Arrivé au point ({lat:.7f}, {lon:.7f}) à {alt:.1f} m."

    def _hover(self, a: dict) -> str:
        duration = _number(a, "duration_s")
        self.drone.hover(duration)
        return f"Vol stationnaire de {duration:.0f} s terminé."

    def _set_parameter(self, a: dict) -> str:
        name = a.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("argument 'name' manquant")
        value = _number(a, "value")
        self.drone.set_param(name, value)
        return f"Paramètre {name.upper()} = {value:g} appliqué."

    def _land(self, a: dict) -> str:
        self.drone.land()
        return "Atterrissage terminé, moteurs désarmés."

    def _rtl(self, a: dict) -> str:
        self.drone.rtl()
        return "Retour au point de décollage terminé, drone posé et désarmé."

    def _get_state(self, a: dict) -> str:
        return json.dumps(_state_summary(self.drone.get_state()), ensure_ascii=False)

    def _observe_scene(self, a: dict) -> str:
        return f"Description de l'image caméra : {self.observe(self.drone.get_state())}"

    def _request_restriction(self, a: dict) -> str:
        restrict = getattr(self.drone, "restrict", None)
        if restrict is None:  # pas de moniteur : aucun mandat n'est appliqué
            raise ValueError("aucun moniteur de mandat actif : restriction impossible")
        if not a:
            raise ValueError("aucun champ à restreindre")
        child = restrict(a)
        return "Restriction acceptée. Nouveau mandat actif :\n" + child.describe()

    def skip(self, call: ToolCall, reason: str = "un appel précédent de ce tour a échoué") -> str:
        """Enregistre un appel NON exécuté (voir DroneAgent.run) et renvoie le texte pour le LLM."""
        result = f"NON EXÉCUTÉ : {reason}. Réévalue la situation."
        self.trace.append(ToolRecord(call.name, call.arguments, False, result, 0.0, self.drone.now(), executed=False))
        log.info("outil %s ignoré (%s)", call.name, reason)
        return result

    def execute(self, call: ToolCall) -> str:
        t0 = time.perf_counter()
        mission_time = self.drone.now()
        state_before = _state_summary(self.drone.get_state())
        ok = False
        try:
            if call.parse_error:
                raise ValueError(call.parse_error)
            handler = self._handlers.get(call.name)
            if handler is None:
                raise ValueError(f"outil inconnu : {call.name!r}")
            result = handler(call.arguments)
            ok = True
        except (DroneError, ValueError) as e:
            result = f"ERREUR : {e}"
        record = ToolRecord(call.name, call.arguments, ok, result, time.perf_counter() - t0, mission_time,
                            state_before=state_before)
        self.trace.append(record)
        log.info("outil %s(%s) -> %s", call.name, json.dumps(call.arguments, ensure_ascii=False), result)
        return result
