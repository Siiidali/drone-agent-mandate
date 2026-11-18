"""Évaluation : N essais × scénarios × conditions, avec le vrai LLM."""

import argparse
import json
import random
import sys
import time
from datetime import timedelta
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from drone_agent_mandate.config import load_config
from drone_agent_mandate.drone import SimDrone, connect
from drone_agent_mandate.geo import haversine_m
from drone_agent_mandate.llm import make_llm
from drone_agent_mandate.mandate import default_mandate, sign_mandate
from drone_agent_mandate.runner import CONDITIONS, MissionSpec, run_mission
from drone_agent_mandate.scene import SCENARIOS
from drone_agent_mandate.scene.scenarios import N_VARIANTS

OPERATOR = "op-eval"


def point_a_offset(rng: random.Random) -> tuple[float, float]:
    """Point A tiré dans la zone du mandat (−50..80 m), avec 20 m de marge."""
    return round(rng.uniform(20, 60), 1), round(rng.uniform(-30, 60), 1)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--n", type=int, default=1, help="nombre d'essais par (scénario, condition)")
    p.add_argument("--out", type=Path, required=True, help="dossier de sortie")
    p.add_argument("--scenarios", nargs="+", default=list(SCENARIOS), choices=list(SCENARIOS))
    p.add_argument("--conditions", nargs="+", default=list(CONDITIONS), choices=list(CONDITIONS))
    p.add_argument("--connect", default="sim", help="'sim' (défaut) ou chaîne MAVLink pour SITL")
    p.add_argument("--backend", choices=["ollama", "openai"])
    p.add_argument("--model")
    p.add_argument("--temperature", type=float, default=0.7)
    p.add_argument("--seed", type=int, default=1000, help="seed de base (essai i -> seed + i)")
    p.add_argument("--no-barrier", action="store_true",
                   help="sans barrière d'observation : le LLM peut planifier toute la mission d'avance")
    p.add_argument("--config", type=Path)
    args = p.parse_args()

    cfg = load_config(args.config)
    if args.backend:
        cfg["llm"]["backend"] = args.backend
    if args.model:
        cfg["llm"]["model"] = args.model
    cfg["llm"]["temperature"] = args.temperature
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "journals").mkdir(exist_ok=True)
    raw_path = args.out / "raw.jsonl"

    key = Ed25519PrivateKey.generate()
    trusted = {OPERATOR: key.public_key()}
    sitl = args.connect != "sim"
    sitl_drone = connect(args.connect) if sitl else None
    home = sitl_drone.home() if sitl else SimDrone().home()
    signed = sign_mandate(default_mandate(home, OPERATOR, validity=timedelta(days=3)), key)
    (args.out / "mandate.json").write_text(json.dumps(signed, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.out / f"{OPERATOR}.pub").write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))

    done = set()
    if raw_path.exists():
        for line in raw_path.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            done.add((r["scenario"], r["condition"], r["trial"]))

    plan = [(t, s, c) for t in range(args.n) for s in args.scenarios for c in args.conditions]
    todo = [x for x in plan if (x[1], x[2], x[0]) not in done]
    meta = {"model": cfg["llm"]["model"], "backend": cfg["llm"]["backend"], "temperature": args.temperature,
            "base_seed": args.seed, "n": args.n, "drone": args.connect, "barrier": not args.no_barrier}
    print(f"{len(plan)} missions prévues, {len(plan) - len(todo)} déjà faites, {len(todo)} à faire. {meta}")

    t_start = time.time()
    try:
        for k, (trial, scenario, condition) in enumerate(todo, 1):
            seed = args.seed + trial
            rng = random.Random(seed)
            offset = point_a_offset(rng)
            variant = (trial + list(SCENARIOS).index(scenario)) % N_VARIANTS
            llm = make_llm({**cfg["llm"], "seed": seed})
            if sitl:
                st = sitl_drone.get_state()
                if st.armed or haversine_m(st.lat, st.lon, *home) > 5:
                    sys.exit(f"SITL : le drone doit être posé au home avant la mission suivante ({st}).")
                drone = sitl_drone
            else:
                drone = SimDrone()
            spec = MissionSpec(scenario=scenario, condition=condition, variant=variant, point_a_offset=offset,
                               alt_m=cfg["agent"]["altitude_m"], max_steps=cfg["agent"]["max_steps"],
                               max_calls_per_turn=cfg["agent"].get("max_calls_per_turn", 5),
                               observation_barrier=not args.no_barrier)
            journal = args.out / "journals" / f"{scenario}_{condition}_{trial}.jsonl"
            if journal.exists():
                journal.unlink()  # mission interrompue lors d'une exécution précédente : on la refait
            t0 = time.time()
            rec = run_mission(llm, drone, spec, signed, trusted, journal)
            rec.update(trial=trial, seed=seed, wall_s=round(time.time() - t0, 1), **meta)
            with open(raw_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

            elapsed = time.time() - t_start
            eta = elapsed / k * (len(todo) - k)
            flag = ""
            if rec["attack"]:
                flag = "ATTAQUE RÉUSSIE" if rec["attack_succeeded"] else (
                    "tentée, bloquée" if rec["attack_attempted"] else "non tentée")
            print(f"[{k}/{len(todo)}] essai {trial} {scenario:<15} {condition:<12} v{variant} "
                  f"{rec['wall_s']:6.1f} s | mission agent={'OK' if rec['mission_completed_by_agent'] else '--'} "
                  f"repli={rec['fallback'] or '-'} posé_sûr={'oui' if rec['landed_safe'] else 'NON'} "
                  f"{flag} | reste ~{eta / 60:.0f} min", flush=True)
    finally:
        if sitl_drone:
            sitl_drone.close()
    print(f"Terminé. Résultats bruts : {raw_path}")


if __name__ == "__main__":
    main()
