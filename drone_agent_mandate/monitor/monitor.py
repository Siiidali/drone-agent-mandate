"""Moniteur de mandat : un DroneAPI qui vérifie chaque ordre avant de le transmettre."""

import math
import time
from typing import Callable

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ..drone.interface import DroneAPI, DroneError, DroneState
from ..mandate.mandate import Mandate, MandateError, verify_signed_mandate
from .journal import Journal


class MandateViolation(DroneError):
    """Ordre refusé par le moniteur. Hérite de DroneError : l'agent le reçoit comme un refus."""


def _loggable(v):
    """Valeur enregistrable dans le journal (JSON strict : NaN/infini deviennent du texte)."""
    if isinstance(v, dict):
        return {str(k): _loggable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_loggable(x) for x in v]
    if v is None or isinstance(v, (str, bool)) or (isinstance(v, (int, float)) and math.isfinite(v)):
        return v
    return repr(v)


class MandateMonitor(DroneAPI):
    def __init__(self, drone: DroneAPI, signed_mandate: dict, trusted_keys: dict[str, Ed25519PublicKey],
                 journal: Journal, wall_clock: Callable[[], float] = time.time):
        self._drone = drone
        self._journal = journal
        self._wall_clock = wall_clock  # injectable pour tester l'expiration
        try:
            mandate = verify_signed_mandate(signed_mandate, trusted_keys)
            if wall_clock() >= mandate.expiration.timestamp():
                raise MandateError(f"mandat {mandate.id_mandat} expiré depuis {mandate.expiration:%Y-%m-%d %H:%M} UTC")
        except MandateError as e:
            journal.append({"type": "mandat_refuse", "raison": str(e)})
            raise
        self._chain: list[Mandate] = [mandate]
        self._start = drone.now()
        self.latencies_s: list[float] = []          # durée de chaque vérification (pour l'évaluation)
        self.journal_latencies_s: list[float] = []  # durée de chaque écriture de décision dans le journal
        self.rejects = 0
        journal.append({"type": "mandat_charge", "id_mandat": mandate.id_mandat,
                        "id_operateur": mandate.id_operateur, "empreinte": mandate.fingerprint()})

    @property
    def mandate(self) -> Mandate:
        """Mandat actif (le plus restreint de la chaîne)."""
        return self._chain[-1]

    # ------------------------------------------------------------------ vérification

    def _violation(self, m: Mandate, action: str, params: dict, st: DroneState) -> str | None:
        """Raison du refus selon le mandat `m`, ou None si l'ordre est autorisé."""
        returning = action in ("return_to_launch", "land")
        if not returning:
            if self._wall_clock() >= m.expiration.timestamp():
                return f"mandat {m.id_mandat} expiré"
            elapsed = self._drone.now() - self._start
            planned = params.get("duration_s", 0.0) if action == "hover" else 0.0
            if planned < 0:  # sinon une durée négative « rendrait » du temps de mission
                return "durée de hover négative"
            if elapsed + planned > m.duree_max_s:
                return (f"durée maximale de mission dépassée ({elapsed:.0f} s écoulées"
                        f"{f' + {planned:.0f} s demandées' if planned else ''} > {m.duree_max_s:g} s)")
        if action not in m.actions_permises:
            return f"action « {action} » non permise par le mandat"

        if action in ("takeoff", "goto"):
            alt = params["altitude_m"]
            if not 0 < alt <= m.altitude_max_m:
                return f"altitude {alt:g} m hors de ]0, {m.altitude_max_m:g}] m"
        if action == "takeoff" and not m.contains(st.lat, st.lon):
            return "décollage depuis une position hors de la zone autorisée"
        if action == "goto":
            target = (params["lat"], params["lon"])
            if not m.contains(*target):
                return f"destination ({target[0]:.7f}, {target[1]:.7f}) hors de la zone autorisée"
            if not m.path_inside((st.lat, st.lon), target):
                return "le trajet en ligne droite sort de la zone autorisée"
        if action == "land" and not m.landing_allowed(st.lat, st.lon):
            return "atterrissage non autorisé à la position actuelle"
        if action == "return_to_launch":
            home = self._drone.home()
            if not m.landing_allowed(*home):
                return f"le home de l'autopilote ({home[0]:.7f}, {home[1]:.7f}) n'est pas une zone d'atterrissage autorisée"
            if not m.path_inside((st.lat, st.lon), home):
                return "le trajet de retour sort de la zone autorisée"
        return None

    def _guard(self, action: str, params: dict) -> None:
        """Vérifie l'ordre, journalise la décision, lève MandateViolation si refusé."""
        t0 = time.perf_counter()
        st = self._drone.get_state()
        numbers = [v for k, v in params.items() if k != "name"]
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in numbers):
            reason = "paramètre numérique invalide (NaN, infini ou non numérique)"
        else:
            reason = next((r for m in self._chain if (r := self._violation(m, action, params, st))), None)
        latency = time.perf_counter() - t0
        self.latencies_s.append(latency)
        t1 = time.perf_counter()
        self._journal.append({
            "type": "decision", "action": action, "params": _loggable(params),
            "decision": "REJECT" if reason else "ACCEPT", "raison": reason or "conforme au mandat",
            "id_mandat": self.mandate.id_mandat,
            "etat": {"lat": st.lat, "lon": st.lon, "alt_m": round(st.alt_m, 2), "mode": st.mode},
            "latence_us": round(latency * 1e6, 1),
        })
        self.journal_latencies_s.append(time.perf_counter() - t1)
        if reason:
            self.rejects += 1
            raise MandateViolation(f"refusé par le moniteur de mandat : {reason}")

    def _run(self, action: str, fn: Callable[[], None]) -> None:
        """Transmet un ordre accepté à l'autopilote et journalise le résultat de l'exécution."""
        try:
            fn()
        except DroneError as e:
            self._journal.append({"type": "execution", "action": action, "resultat": f"erreur : {e}"})
            raise
        self._journal.append({"type": "execution", "action": action, "resultat": "ok"})

    # ------------------------------------------------------------------ DroneAPI

    def takeoff(self, alt_m: float) -> None:
        self._guard("takeoff", {"altitude_m": alt_m})
        self._run("takeoff", lambda: self._drone.takeoff(alt_m))

    def goto(self, lat: float, lon: float, alt_m: float) -> None:
        self._guard("goto", {"lat": lat, "lon": lon, "altitude_m": alt_m})
        self._run("goto", lambda: self._drone.goto(lat, lon, alt_m))

    def hover(self, duration_s: float) -> None:
        self._guard("hover", {"duration_s": duration_s})
        self._run("hover", lambda: self._drone.hover(duration_s))

    def land(self) -> None:
        self._guard("land", {})
        self._run("land", self._drone.land)

    def rtl(self) -> None:
        self._guard("return_to_launch", {})
        self._run("return_to_launch", self._drone.rtl)

    def set_param(self, name: str, value: float) -> None:
        self._guard("set_parameter", {"name": name, "value": value})
        self._run("set_parameter", lambda: self._drone.set_param(name, value))

    def get_state(self) -> DroneState:
        return self._drone.get_state()

    def home(self) -> tuple[float, float]:
        return self._drone.home()

    def now(self) -> float:
        return self._drone.now()

    def close(self) -> None:
        self._drone.close()

    # ------------------------------------------------------------------ sous-mandat

    def restrict(self, changes: dict) -> Mandate:
        """Applique un sous-mandat plus strict demandé par l'agent. Refuse tout élargissement."""
        t0 = time.perf_counter()
        try:
            child = self.mandate.derive(changes)
        except MandateError as e:
            self._journal.append({"type": "restriction", "decision": "REJECT", "demande": _loggable(changes),
                                  "raison": str(e), "latence_us": round((time.perf_counter() - t0) * 1e6, 1)})
            raise MandateViolation(f"restriction refusée : {e}") from None
        self._chain.append(child)
        self._journal.append({"type": "restriction", "decision": "ACCEPT", "demande": _loggable(changes),
                              "id_mandat": child.id_mandat, "empreinte": child.fingerprint(),
                              "latence_us": round((time.perf_counter() - t0) * 1e6, 1)})
        return child
