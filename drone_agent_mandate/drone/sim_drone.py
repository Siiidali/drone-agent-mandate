"""Simulateur cinématique minimal, en Python pur."""

from ..geo import haversine_m
from .interface import DroneAPI, DroneError, DroneState

# Home par défaut de SITL : aérodrome CMAC, Canberra.
SITL_HOME = (-35.363261, 149.165230)


class SimDrone(DroneAPI):
    def __init__(self, home: tuple[float, float] = SITL_HOME, speed_mps: float = 5.0,
                 climb_mps: float = 2.5, descent_mps: float = 1.0, rtl_alt_m: float = 15.0,
                 battery_drain_pct_per_min: float = 3.0):
        self._home = home
        self.speed_mps = speed_mps
        self.climb_mps = climb_mps
        self.descent_mps = descent_mps
        self.rtl_alt_m = rtl_alt_m          # comme le paramètre RTL_ALT d'ArduCopter
        self.drain = battery_drain_pct_per_min
        self._t = 0.0                       # horloge simulée (s)
        self._lat, self._lon = home
        self._alt = 0.0
        self._mode = "STABILIZE"
        self._armed = False
        self._battery = 100.0
        self.params: dict[str, float] = {}  # paramètres modifiés via set_param

    def _advance(self, dt: float) -> None:
        """Fait avancer l'horloge simulée et décharge la batterie."""
        self._t += dt
        self._battery = max(0.0, self._battery - self.drain * dt / 60.0)

    def _require_flying(self, action: str) -> None:
        if not self._armed or self._alt < 0.5:
            raise DroneError(f"{action} impossible : le drone n'est pas en vol")

    def takeoff(self, alt_m: float) -> None:
        if self._armed and self._alt > 1.0:
            raise DroneError("décollage impossible : le drone est déjà en vol")
        if alt_m <= 0:
            raise DroneError("altitude de décollage invalide")
        self._mode, self._armed = "GUIDED", True
        self._home = (self._lat, self._lon)
        self._advance(alt_m / self.climb_mps)
        self._alt = alt_m

    def goto(self, lat: float, lon: float, alt_m: float) -> None:
        self._require_flying("goto")
        if self._mode != "GUIDED":
            raise DroneError(f"goto impossible en mode {self._mode}")
        horizontal = haversine_m(self._lat, self._lon, lat, lon)
        vertical = abs(alt_m - self._alt)
        self._advance(max(horizontal / self.speed_mps, vertical / self.climb_mps))
        self._lat, self._lon, self._alt = lat, lon, alt_m

    def land(self) -> None:
        self._mode = "LAND"
        self._advance(self._alt / self.descent_mps)
        self._alt, self._armed = 0.0, False

    def rtl(self) -> None:
        self._require_flying("RTL")
        self._mode = "RTL"
        climb = max(0.0, self.rtl_alt_m - self._alt)
        cruise_alt = self._alt + climb
        dist = haversine_m(self._lat, self._lon, *self._home)
        self._advance(climb / self.climb_mps + dist / self.speed_mps + cruise_alt / self.descent_mps)
        (self._lat, self._lon), self._alt, self._armed = self._home, 0.0, False

    def hover(self, duration_s: float) -> None:
        self._require_flying("hover")
        if duration_s < 0:
            raise DroneError("durée négative")
        self._advance(duration_s)

    def set_param(self, name: str, value: float) -> None:
        self.params[name.upper()] = float(value)

    def get_state(self) -> DroneState:
        return DroneState(lat=self._lat, lon=self._lon, alt_m=self._alt, mode=self._mode,
                          armed=self._armed, battery_pct=round(self._battery, 1), groundspeed_mps=0.0)

    def home(self) -> tuple[float, float]:
        return self._home

    def now(self) -> float:
        return self._t
