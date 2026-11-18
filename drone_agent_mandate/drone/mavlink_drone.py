"""Implémentation de DroneAPI pour ArduCopter (SITL ou réel) via pymavlink."""

import logging
import threading
import time
from typing import Callable

from pymavlink import mavutil

from ..geo import haversine_m
from .interface import DroneAPI, DroneError, DroneState

log = logging.getLogger(__name__)
mavlink = mavutil.mavlink

# Numéro de mode <-> nom pour ArduCopter (table fournie par pymavlink).
COPTER_MODES: dict[int, str] = mavutil.mode_mapping_acm
COPTER_MODE_IDS: dict[str, int] = {name: num for num, name in COPTER_MODES.items()}

POSITION_ONLY_MASK = (
    mavlink.POSITION_TARGET_TYPEMASK_VX_IGNORE
    | mavlink.POSITION_TARGET_TYPEMASK_VY_IGNORE
    | mavlink.POSITION_TARGET_TYPEMASK_VZ_IGNORE
    | mavlink.POSITION_TARGET_TYPEMASK_AX_IGNORE
    | mavlink.POSITION_TARGET_TYPEMASK_AY_IGNORE
    | mavlink.POSITION_TARGET_TYPEMASK_AZ_IGNORE
    | mavlink.POSITION_TARGET_TYPEMASK_YAW_IGNORE
    | mavlink.POSITION_TARGET_TYPEMASK_YAW_RATE_IGNORE
)


def _enum_name(enum: str, value: int) -> str:
    """Nom lisible d'une valeur d'énumération MAVLink (ex. MAV_RESULT_DENIED)."""
    entry = mavlink.enums.get(enum, {}).get(value)
    return entry.name if entry else str(value)


class MAVLinkDrone(DroneAPI):
    def __init__(self, connection: str = "udpin:0.0.0.0:14550", heartbeat_timeout: float = 30.0):
        log.info("Connexion MAVLink sur %s", connection)
        self._master = mavutil.mavlink_connection(
            connection, source_system=250, source_component=mavlink.MAV_COMP_ID_ONBOARD_COMPUTER
        )
        self._lock = threading.Lock()
        self._ack_cond = threading.Condition(self._lock)
        self._last: dict[str, object] = {}                 # type de message -> dernier reçu
        self._rx_time: dict[str, float] = {}               # type de message -> instant de réception
        self._acks: dict[int, tuple[int, float]] = {}      # commande -> (résultat, instant de réception)
        self._params: dict[str, tuple[float, float]] = {}  # paramètre -> (valeur, instant de réception)
        self._stop = threading.Event()

        self._wait_autopilot_heartbeat(heartbeat_timeout)
        self._thread = threading.Thread(target=self._reader_loop, name="mavlink-reader", daemon=True)
        self._thread.start()
        self._request_streams()
        self._wait_for(lambda: self._msg("GLOBAL_POSITION_INT") is not None, 10, "aucune position reçue")

        self._home: tuple[float, float] | None = None
        if not self._refresh_home():
            st = self.get_state()
            self._home = (st.lat, st.lon)
            log.warning("HOME_POSITION indisponible, home provisoire = position actuelle")
        st = self.get_state()
        log.info("Connecté (sysid %d). Home = %.7f, %.7f, mode %s",
                 self._master.target_system, *self._home, st.mode)

    # ------------------------------------------------------------------ réception

    @staticmethod
    def _is_autopilot(hb) -> bool:
        """Écarte les heartbeats des stations sol (MAVProxy, QGroundControl...)."""
        return hb.type != mavlink.MAV_TYPE_GCS and hb.autopilot != mavlink.MAV_AUTOPILOT_INVALID

    def _wait_autopilot_heartbeat(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            hb = self._master.recv_match(type="HEARTBEAT", blocking=True, timeout=1)
            if hb is not None and self._is_autopilot(hb):
                self._master.target_system = hb.get_srcSystem()
                self._master.target_component = hb.get_srcComponent()
                self._last["HEARTBEAT"] = hb
                return
        raise DroneError(f"aucun heartbeat d'autopilote reçu en {timeout:.0f} s (SITL lancé ?)")

    def _reader_loop(self) -> None:
        while not self._stop.is_set():
            try:
                msg = self._master.recv_match(blocking=True, timeout=0.5)
            except OSError:  # connexion fermée pendant close()
                break
            if msg is None or msg.get_type() == "BAD_DATA":
                continue
            if msg.get_srcSystem() != self._master.target_system:
                continue
            mtype = msg.get_type()
            if mtype == "HEARTBEAT" and (
                not self._is_autopilot(msg) or msg.get_srcComponent() != self._master.target_component
            ):
                continue
            with self._lock:
                self._last[mtype] = msg
                self._rx_time[mtype] = time.monotonic()
                if mtype == "COMMAND_ACK":
                    self._acks[msg.command] = (msg.result, time.monotonic())
                    self._ack_cond.notify_all()
                elif mtype == "PARAM_VALUE":
                    self._params[msg.param_id] = (msg.param_value, time.monotonic())
            if mtype == "STATUSTEXT":
                # Messages texte de l'autopilote (ex. raisons d'un refus d'armement).
                log.info("[autopilote] %s", msg.text)

    def _msg(self, mtype: str):
        with self._lock:
            return self._last.get(mtype)

    def _refresh_home(self, timeout: float = 5.0) -> bool:
        """Relit le home de l'autopilote (message HOME_POSITION)."""
        sent_at = time.monotonic()
        result = self._send_command(mavlink.MAV_CMD_REQUEST_MESSAGE, mavlink.MAVLINK_MSG_ID_HOME_POSITION)
        if result != mavlink.MAV_RESULT_ACCEPTED:
            return False
        try:
            self._wait_for(lambda: self._rx_time.get("HOME_POSITION", 0.0) >= sent_at, timeout, "HOME_POSITION")
        except DroneError:
            return False
        hp = self._msg("HOME_POSITION")
        self._home = (hp.latitude / 1e7, hp.longitude / 1e7)
        return True

    def _request_streams(self, rate_hz: int = 4) -> None:
        """Demande à ArduPilot d'envoyer position, état et batterie en continu."""
        self._master.mav.request_data_stream_send(
            self._master.target_system, self._master.target_component,
            mavlink.MAV_DATA_STREAM_ALL, rate_hz, 1,
        )

    def _wait_for(self, predicate: Callable[[], bool], timeout: float, error: str, poll: float = 0.2) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                raise DroneError(f"timeout ({timeout:.0f} s) : {error}")
            time.sleep(poll)

    # ------------------------------------------------------------------ commandes

    def _send_command(self, command: int, *params: float, timeout: float = 5.0) -> int:
        """Envoie un COMMAND_LONG et renvoie le code MAV_RESULT de l'ACK."""
        p = list(params) + [0.0] * (7 - len(params))
        sent_at = time.monotonic()
        self._master.mav.command_long_send(
            self._master.target_system, self._master.target_component, command, 0, *p
        )
        with self._ack_cond:
            got = self._ack_cond.wait_for(
                lambda: command in self._acks and self._acks[command][1] >= sent_at, timeout
            )
            if not got:
                raise DroneError(f"pas de COMMAND_ACK pour {_enum_name('MAV_CMD', command)} en {timeout:.0f} s")
            return self._acks[command][0]

    def _command(self, command: int, *params: float, timeout: float = 5.0) -> None:
        """Comme _send_command, mais lève DroneError si l'autopilote refuse."""
        result = self._send_command(command, *params, timeout=timeout)
        if result != mavlink.MAV_RESULT_ACCEPTED:
            raise DroneError(f"{_enum_name('MAV_CMD', command)} refusée : {_enum_name('MAV_RESULT', result)}")

    def set_mode(self, mode: str) -> None:
        if mode not in COPTER_MODE_IDS:
            raise DroneError(f"mode inconnu : {mode}")
        self._command(mavlink.MAV_CMD_DO_SET_MODE, mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, COPTER_MODE_IDS[mode])
        self._wait_for(lambda: self.get_state().mode == mode, 5, f"le mode n'est pas passé à {mode}")

    def arm(self, timeout: float = 90.0) -> None:
        """Arme les moteurs."""
        deadline = time.monotonic() + timeout
        while True:
            result = self._send_command(mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 1)
            if result == mavlink.MAV_RESULT_ACCEPTED:
                break
            if time.monotonic() > deadline:
                raise DroneError(f"armement refusé ({_enum_name('MAV_RESULT', result)}) après {timeout:.0f} s")
            log.info("Armement refusé (%s), nouvel essai dans 2 s", _enum_name("MAV_RESULT", result))
            time.sleep(2)
        self._wait_for(lambda: self.get_state().armed, 5, "le drone n'apparaît pas armé")

    # ------------------------------------------------------------------ DroneAPI

    def takeoff(self, alt_m: float) -> None:
        st = self.get_state()
        if st.armed and st.alt_m > 1.0:
            raise DroneError("décollage impossible : le drone est déjà en vol")
        self.set_mode("GUIDED")
        self.arm()
        self._command(mavlink.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, alt_m)
        self._wait_for(lambda: self.get_state().alt_m >= 0.95 * alt_m, 30 + alt_m,
                       f"altitude {alt_m} m non atteinte")
        log.info("Décollage terminé à %.1f m", self.get_state().alt_m)
        if self._refresh_home():
            log.info("Home de l'autopilote : %.7f, %.7f", *self._home)
        else:
            log.warning("Impossible de relire le home après armement")

    def goto(self, lat: float, lon: float, alt_m: float, tolerance_m: float = 1.0) -> None:
        st = self.get_state()
        if not st.armed or st.mode != "GUIDED":
            raise DroneError(f"goto impossible : drone {'armé' if st.armed else 'désarmé'} en mode {st.mode}"
                             " (il faut être en vol en GUIDED)")
        self._master.mav.set_position_target_global_int_send(
            0,  # time_boot_ms : ignoré par ArduPilot
            self._master.target_system, self._master.target_component,
            mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,  # altitude relative au home
            POSITION_ONLY_MASK,
            int(lat * 1e7), int(lon * 1e7), alt_m,  # lat/lon en degrés × 10^7
            0, 0, 0,  # vitesses (ignorées)
            0, 0, 0,  # accélérations (ignorées)
            0, 0,     # cap, vitesse de lacet (ignorés)
        )
        dist = haversine_m(st.lat, st.lon, lat, lon)
        timeout = 30 + dist / 2.0 + abs(alt_m - st.alt_m) / 1.0
        deadline = time.monotonic() + timeout
        while True:
            cur = self.get_state()
            if cur.mode != "GUIDED":  # ex. failsafe batterie qui bascule en RTL/LAND
                raise DroneError(f"goto interrompu : l'autopilote est passé en {cur.mode}")
            if (haversine_m(cur.lat, cur.lon, lat, lon) < tolerance_m
                    and abs(cur.alt_m - alt_m) < tolerance_m):
                break
            if time.monotonic() > deadline:
                raise DroneError(f"timeout ({timeout:.0f} s) : point non atteint")
            time.sleep(0.2)
        log.info("Point atteint : %.7f, %.7f, %.1f m", lat, lon, alt_m)

    def land(self) -> None:
        alt = self.get_state().alt_m
        self.set_mode("LAND")
        self._wait_for(lambda: not self.get_state().armed, 60 + alt / 0.5, "atterrissage non terminé")
        log.info("Atterrissage terminé")

    def rtl(self) -> None:
        self.set_mode("RTL")
        self._wait_for(lambda: not self.get_state().armed, 300, "retour au home non terminé")
        log.info("RTL terminé, drone posé au home")

    def hover(self, duration_s: float) -> None:
        if duration_s < 0:
            raise DroneError("durée négative")
        st = self.get_state()
        if not st.armed or st.mode != "GUIDED":
            raise DroneError(f"hover impossible : drone {'armé' if st.armed else 'désarmé'} en mode {st.mode}")
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            if self.get_state().mode != "GUIDED":
                raise DroneError(f"hover interrompu : l'autopilote est passé en {self.get_state().mode}")
            time.sleep(0.5)

    def set_param(self, name: str, value: float, timeout: float = 5.0) -> None:
        """PARAM_SET n'a pas d'ACK : l'autopilote confirme en renvoyant PARAM_VALUE."""
        name = name.upper()
        if len(name) > 16:
            raise DroneError(f"nom de paramètre trop long : {name}")
        sent_at = time.monotonic()
        self._master.mav.param_set_send(self._master.target_system, self._master.target_component,
                                        name.encode(), float(value), mavlink.MAV_PARAM_TYPE_REAL32)
        try:
            self._wait_for(lambda: self._params.get(name, (None, 0.0))[1] >= sent_at, timeout,
                           f"paramètre {name} non confirmé (inconnu ?)")
        except DroneError:
            raise DroneError(f"paramètre {name} inconnu ou non confirmé par l'autopilote") from None
        confirmed = self._params[name][0]
        if abs(confirmed - float(value)) > 1e-3:
            raise DroneError(f"paramètre {name} refusé : valeur actuelle {confirmed}")
        log.info("Paramètre %s = %s", name, confirmed)

    def get_state(self) -> DroneState:
        pos = self._msg("GLOBAL_POSITION_INT")
        hb = self._msg("HEARTBEAT")
        sys_status = self._msg("SYS_STATUS")
        vfr = self._msg("VFR_HUD")
        if pos is None or hb is None:
            raise DroneError("état inconnu : pas encore de position ou de heartbeat")
        battery = None
        if sys_status is not None and sys_status.battery_remaining >= 0:  # -1 = inconnu
            battery = float(sys_status.battery_remaining)
        return DroneState(
            lat=pos.lat / 1e7,
            lon=pos.lon / 1e7,
            alt_m=pos.relative_alt / 1000.0,  # millimètres -> mètres
            mode=COPTER_MODES.get(hb.custom_mode, f"MODE_{hb.custom_mode}"),
            armed=bool(hb.base_mode & mavlink.MAV_MODE_FLAG_SAFETY_ARMED),
            battery_pct=battery,
            groundspeed_mps=float(vfr.groundspeed) if vfr is not None else 0.0,
        )

    def home(self) -> tuple[float, float]:
        """Home courant de l'autopilote (celui du dernier armement)."""
        return self._home

    def now(self) -> float:
        return time.monotonic()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._master.close()
