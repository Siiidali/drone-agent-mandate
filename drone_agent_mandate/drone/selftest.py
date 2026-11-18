"""Vol de vérification de la couche drone (sans LLM)."""

import argparse
import logging
import time

from ..geo import haversine_m, offset_latlon
from . import connect


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--connect", default="udpin:0.0.0.0:14550", help="'sim' ou chaîne de connexion MAVLink")
    parser.add_argument("--alt", type=float, default=10.0, help="altitude de vol (m)")
    parser.add_argument("--distance", type=float, default=30.0, help="distance des points A et B au home (m)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    drone = connect(args.connect)
    home = drone.home()  # home de départ ; attention, l'autopilote le redéfinit à chaque armement
    point_a = offset_latlon(*home, north_m=args.distance, east_m=0)
    point_b = offset_latlon(*home, north_m=0, east_m=args.distance)

    def step(label, fn, *a):
        t0 = time.monotonic()
        fn(*a)
        st = drone.get_state()
        print(f"[OK] {label:<22} {time.monotonic() - t0:6.1f} s | lat={st.lat:.7f} lon={st.lon:.7f} "
              f"alt={st.alt_m:5.1f} m mode={st.mode:<8} armé={st.armed} batterie={st.battery_pct}% "
              f"| à {haversine_m(st.lat, st.lon, *home):5.1f} m du départ, "
              f"{haversine_m(st.lat, st.lon, *drone.home()):5.1f} m du home autopilote")

    print(f"État initial : {drone.get_state()}")
    try:
        step(f"takeoff {args.alt} m", drone.takeoff, args.alt)
        step("goto A (nord)", drone.goto, *point_a, args.alt)
        step("land en A", drone.land)
        step(f"takeoff {args.alt} m", drone.takeoff, args.alt)
        step("goto B (est)", drone.goto, *point_b, args.alt)
        step("rtl", drone.rtl)
        print("Séquence terminée sans erreur.")
    finally:
        drone.close()


if __name__ == "__main__":
    main()
