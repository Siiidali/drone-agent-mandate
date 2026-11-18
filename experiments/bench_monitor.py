"""Micro-benchmark de la latence ajoutée par le moniteur (sans LLM, sans SITL)."""

import argparse
import csv
import dataclasses
import platform
import statistics
import tempfile
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from drone_agent_mandate.drone import SimDrone
from drone_agent_mandate.geo import offset_latlon
from drone_agent_mandate.mandate import default_mandate, sign_mandate
from drone_agent_mandate.monitor import Journal, MandateMonitor, MandateViolation


def percentile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--iterations", type=int, default=10_000)
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    drone = SimDrone()
    h = drone.home()
    key = Ed25519PrivateKey.generate()
    mandate = dataclasses.replace(default_mandate(h, "op-bench"), duree_max_s=1e9)
    orders = {
        "takeoff": ("takeoff", {"altitude_m": 10.0}),
        "goto 10 m": ("goto", dict(zip(("lat", "lon"), offset_latlon(*h, 10, 0)), altitude_m=10.0)),
        "goto 45 m": ("goto", dict(zip(("lat", "lon"), offset_latlon(*h, 40, 20)), altitude_m=10.0)),
        "goto 110 m": ("goto", dict(zip(("lat", "lon"), offset_latlon(*h, 78, 78)), altitude_m=10.0)),
        "goto hors zone (REJECT)": ("goto", dict(zip(("lat", "lon"), offset_latlon(*h, 0, 400)), altitude_m=30.0)),
        "set_parameter (REJECT)": ("set_parameter", {"name": "FENCE_ENABLE", "value": 0.0}),
        "land": ("land", {}),
        "return_to_launch": ("return_to_launch", {}),
    }

    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for fsync in (False, True):
            journal = Journal(Path(tmp) / f"bench_{fsync}.jsonl", fsync=fsync)
            mon = MandateMonitor(drone, sign_mandate(mandate, key), {"op-bench": key.public_key()}, journal)
            for label, (action, params) in orders.items():
                mon.latencies_s.clear()
                mon.journal_latencies_s.clear()
                n = args.iterations if not fsync else min(args.iterations, 2000)  # fsync est lent
                for _ in range(n):
                    try:
                        mon._guard(action, params)  # vérification + journal, sans exécuter l'ordre
                    except MandateViolation:
                        pass
                check = [x * 1e6 for x in mon.latencies_s]
                jw = [x * 1e6 for x in mon.journal_latencies_s]
                rows.append({"ordre": label, "fsync": fsync, "n": n,
                             "verif_us_median": round(statistics.median(check), 1),
                             "verif_us_p95": round(percentile(check, 0.95), 1),
                             "verif_us_max": round(max(check), 1),
                             "journal_us_median": round(statistics.median(jw), 1),
                             "journal_us_p95": round(percentile(jw, 0.95), 1)})
            journal.close()

    out = args.out / "latency_bench.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    (args.out / "latency_bench_env.txt").write_text(
        f"{platform.platform()} | {platform.processor() or platform.machine()} | Python {platform.python_version()} "
        f"| {time.strftime('%Y-%m-%d %H:%M')}\n", encoding="utf-8")
    for r in rows:
        print(f"{r['ordre']:<26} fsync={str(r['fsync']):<5} vérif médiane {r['verif_us_median']:>7} µs "
              f"(p95 {r['verif_us_p95']:>7})  journal médiane {r['journal_us_median']:>8} µs "
              f"(p95 {r['journal_us_p95']:>8})")
    print(f"-> {out}")


if __name__ == "__main__":
    main()
