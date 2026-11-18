from .interface import DroneAPI, DroneError, DroneState
from .sim_drone import SimDrone


def connect(target: str) -> DroneAPI:
    """« sim » -> SimDrone ; sinon une chaîne de connexion MAVLink (ex. udpin:0.0.0.0:14550)."""
    if target == "sim":
        return SimDrone()
    from .mavlink_drone import MAVLinkDrone
    return MAVLinkDrone(target)


__all__ = ["DroneAPI", "DroneError", "DroneState", "SimDrone", "connect"]
