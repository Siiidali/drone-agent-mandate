"""Point d'entrée en ligne de commande."""

import argparse
import json
import logging
import sys
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path

from .config import ROOT, load_config
from .drone import connect
from .drone.sim_drone import SITL_HOME
from .llm import make_llm
from .mandate import (MandateError, default_mandate, generate_keypair, load_private_key, load_trust_store,
                      sign_mandate, verify_signed_mandate)
from .monitor import verify_journal
from .runner import MissionSpec, run_mission
from .scene import SCENARIOS


def cmd_run(args: argparse.Namespace, cfg: dict) -> None:
    if args.backend:
        cfg["llm"]["backend"] = args.backend
    if args.model:
        cfg["llm"]["model"] = args.model
    target = args.connect or cfg["drone"]["connect"]
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = ROOT / "runs"
    out_dir.mkdir(exist_ok=True)

    signed, trusted = None, None
    condition = "sans_mandat"
    if args.mandate:
        signed = json.loads(Path(args.mandate).read_text(encoding="utf-8"))
        trusted = load_trust_store(args.trust_dir)
        mandate = verify_signed_mandate(signed, trusted)
        if time.time() >= mandate.expiration.timestamp():
            sys.exit(f"Mandat {mandate.id_mandat} expiré depuis {mandate.expiration:%Y-%m-%d %H:%M} UTC : "
                     "en générer un nouveau avec make-mandate.")
        condition = "prompt_seul" if args.prompt_only else "moniteur"
    spec = MissionSpec(scenario=args.scenario, condition=condition, variant=args.variant,
                       alt_m=cfg["agent"]["altitude_m"], max_steps=cfg["agent"]["max_steps"],
                       max_calls_per_turn=cfg["agent"].get("max_calls_per_turn", 5),
                       observation_barrier=cfg["agent"].get("observation_barrier", True), fallback=not args.no_fallback)
    journal_path = out_dir / f"journal_{stamp}_{args.scenario}.jsonl" if condition == "moniteur" else None

    llm = make_llm(cfg["llm"])
    drone = connect(target)
    print(f"LLM       : {llm!r}\nDrone     : {target}\nScénario  : {args.scenario} (variante {args.variant})\n"
          f"Condition : {condition}\n")
    try:
        rec = run_mission(llm, drone, spec, signed, trusted, journal_path)
    finally:
        drone.close()

    print(f"\n=== Mission envoyée à l'agent ===\n{rec['messages'][1]['content']}")
    print("\n=== Appels d'outils ===")
    for i, r in enumerate(rec["trace"], 1):
        status = "ok " if r["ok"] else "ERR"
        print(f"{i:2d}. [{status}] {r['name']}({json.dumps(r['arguments'], ensure_ascii=False)}) -> {r['result']}")
    print(f"\n=== Compte rendu du LLM ({rec['stop_reason']}) ===\n{rec['final_message']}")
    print(f"\n{rec['llm_calls']} appels LLM ({rec['llm_time_s']:.1f} s), agent {rec['agent_elapsed_s']:.1f} s")
    print(f"État final : {rec['final_state']}")
    print(f"\nCondition                    : {condition}")
    print(f"Mission accomplie par l'agent : {'OUI' if rec['mission_completed_by_agent'] else 'NON'}")
    print(f"Repli de sécurité             : {rec['fallback'] or 'non nécessaire'}")
    print(f"Posé en zone autorisée        : {'OUI' if rec['landed_safe'] else 'NON'}")
    if rec["attack"]:
        print(f"Attaque tentée par l'agent    : {'OUI' if rec['attack_attempted'] else 'NON'}")
        print(f"Attaque réussie               : {'OUI' if rec['attack_succeeded'] else 'NON'}")
    if condition == "moniteur":
        print(f"Moniteur : {rec['monitor_rejects']} refus sur {rec['monitor_checks']} ordres, "
              f"latence médiane {rec['monitor_latency_us_median']} µs, journal intègre : {rec['journal_ok']}")

    out = out_dir / f"run_{stamp}_{args.scenario}.json"
    out.write_text(json.dumps({"llm": repr(llm), "drone": target, "mandate": signed, **rec},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Trace complète : {out.relative_to(ROOT)}")
    if journal_path:
        print(f"Journal du moniteur : {journal_path.relative_to(ROOT)}")


def cmd_keygen(args: argparse.Namespace) -> None:
    priv, pub = generate_keypair(args.out, args.operator_id)
    print(f"Clé privée (à garder secrète) : {priv}\nClé publique (à installer sur le drone) : {pub}")


def cmd_make_mandate(args: argparse.Namespace) -> None:
    home = tuple(float(x) for x in args.home.split(",")) if args.home else SITL_HOME
    key_path = args.key or Path(args.keys_dir) / f"{args.operator_id}.key"
    mandate = default_mandate(home, args.operator_id, validity=timedelta(hours=args.validity_hours))
    doc = sign_mandate(mandate, load_private_key(key_path))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Mandat signé écrit dans {args.out}\n\n{mandate.describe()}")


def cmd_verify_mandate(args: argparse.Namespace) -> None:
    try:
        m = verify_signed_mandate(json.loads(Path(args.file).read_text(encoding="utf-8")),
                                  load_trust_store(args.trust_dir))
    except MandateError as e:
        print(f"MANDAT INVALIDE : {e}")
        sys.exit(1)
    print(f"Mandat valide, empreinte {m.fingerprint()}\n\n{m.describe()}")


def cmd_verify_journal(args: argparse.Namespace) -> None:
    ok, n, error = verify_journal(args.file)
    if not ok:
        print(f"JOURNAL CORROMPU après {n} entrées valides : {error}")
        sys.exit(1)
    entries = [json.loads(line) for line in Path(args.file).read_text(encoding="utf-8").splitlines()]
    decisions = Counter(e["decision"] for e in entries if e.get("type") == "decision")
    print(f"Journal intègre : {n} entrées, chaîne SHA-256 valide.")
    print(f"Décisions : {decisions.get('ACCEPT', 0)} ACCEPT, {decisions.get('REJECT', 0)} REJECT")
    for e in entries:
        if e.get("decision") == "REJECT":
            print(f"  REJECT {e.get('action', e.get('type'))} : {e['raison']}")
    if entries:
        print(f"Dernier hash (à publier/ancrer pour détecter une troncature) : {entries[-1]['hash']}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="drone_agent_mandate", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, help="fichier YAML (défaut : config.yaml ou config.example.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="exécuter une mission avec l'agent LLM")
    run.add_argument("--connect", help="'sim' ou chaîne MAVLink (défaut : config)")
    run.add_argument("--backend", choices=["ollama", "openai"])
    run.add_argument("--model")
    run.add_argument("--scenario", default="normal", choices=list(SCENARIOS))
    run.add_argument("--mission", help="texte de mission (défaut : mission d'observation du point A)")
    run.add_argument("--mandate", help="mandat signé (JSON) : active le moniteur")
    run.add_argument("--prompt-only", action="store_true",
                     help="donner le mandat à l'agent SANS l'appliquer (condition de comparaison)")
    run.add_argument("--trust-dir", default=str(ROOT / "keys"), help="dossier des clés publiques <operateur>.pub")
    run.add_argument("--variant", type=int, default=0, help="variante de la scène / de l'injection (0, 1 ou 2)")
    run.add_argument("--no-fallback", action="store_true", help="désactiver le repli de sécurité en fin de mission")

    kg = sub.add_parser("keygen", help="générer la paire de clés Ed25519 d'un opérateur")
    kg.add_argument("--operator-id", required=True)
    kg.add_argument("--out", default=str(ROOT / "keys"))

    mm = sub.add_parser("make-mandate", help="créer et signer le mandat de démonstration")
    mm.add_argument("--operator-id", required=True)
    mm.add_argument("--key", help="clé privée (défaut : <keys-dir>/<operator-id>.key)")
    mm.add_argument("--keys-dir", default=str(ROOT / "keys"))
    mm.add_argument("--home", help="lat,lon du home (défaut : home SITL CMAC)")
    mm.add_argument("--validity-hours", type=float, default=2.0, help="durée de validité du mandat (h)")
    mm.add_argument("--out", required=True)

    vm = sub.add_parser("verify-mandate", help="vérifier la signature et le contenu d'un mandat")
    vm.add_argument("file")
    vm.add_argument("--trust-dir", default=str(ROOT / "keys"))

    vj = sub.add_parser("verify-journal", help="vérifier l'intégrité du journal chaîné du moniteur")
    vj.add_argument("file")

    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.command == "run":
        cmd_run(args, load_config(args.config))
    elif args.command == "keygen":
        cmd_keygen(args)
    elif args.command == "make-mandate":
        cmd_make_mandate(args)
    elif args.command == "verify-mandate":
        cmd_verify_mandate(args)
    elif args.command == "verify-journal":
        cmd_verify_journal(args)


if __name__ == "__main__":
    main()
