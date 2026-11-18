"""Interface commune à tous les « drones » (SITL via MAVLink, simulateur Python, moniteur)."""

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass


class DroneError(RuntimeError):
    """L'autopilote a refusé un ordre, ou une confirmation n'est pas arrivée à temps."""


@dataclass(frozen=True)
class DroneState:
    lat: float                 # degrés
    lon: float                 # degrés
    alt_m: float               # altitude relative au point de décollage (home), en mètres
    mode: str                  # mode de vol ArduCopter : GUIDED, LAND, RTL...
    armed: bool
    battery_pct: float | None  # None si l'autopilote ne la fournit pas
    groundspeed_mps: float

    def to_dict(self) -> dict:
        return asdict(self)


class DroneAPI(ABC):
    """Ordres de haut niveau. Chaque méthode bloque jusqu'à confirmation ou lève DroneError."""

    @abstractmethod
    def takeoff(self, alt_m: float) -> None:
        """Passe en GUIDED, arme et monte jusqu'à `alt_m` mètres (relatif au home)."""

    @abstractmethod
    def goto(self, lat: float, lon: float, alt_m: float) -> None:
        """Va au point donné (altitude relative au home) et attend d'y être."""

    @abstractmethod
    def land(self) -> None:
        """Atterrit à la verticale (mode LAND) et attend le désarmement."""

    @abstractmethod
    def rtl(self) -> None:
        """Retourne au point de décollage et atterrit (mode RTL)."""

    @abstractmethod
    def hover(self, duration_s: float) -> None:
        """Reste en vol stationnaire à la position actuelle pendant `duration_s` secondes."""

    @abstractmethod
    def set_param(self, name: str, value: float) -> None:
        """Modifie un paramètre de l'autopilote (ex. FENCE_ENABLE) et attend la confirmation."""

    @abstractmethod
    def get_state(self) -> DroneState:
        """Dernier état connu du drone."""

    @abstractmethod
    def home(self) -> tuple[float, float]:
        """Position (lat, lon) du point de décollage."""

    @abstractmethod
    def now(self) -> float:
        """Horloge de mission en secondes (réelle pour SITL, simulée pour SimDrone)."""

    def close(self) -> None:
        """Libère les ressources (connexion, thread). Par défaut : rien à faire."""
