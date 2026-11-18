"""Rapport d'évaluation : tableaux CSV + Markdown et graphique, à partir de raw.jsonl."""

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path

from drone_agent_mandate.agent.tools import ToolRecord
from drone_agent_mandate.drone.sim_drone import SITL_HOME
from drone_agent_mandate.runner import CONDITIONS
from drone_agent_mandate.scene import SCENARIOS

LABELS = {"sans_mandat": "Sans mandat", "prompt_seul": "Mandat dans le prompt", "moniteur": "Mandat + moniteur"}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Intervalle de confiance de Wilson pour une proportion k/n."""
    if n == 0:
        return (math.nan, math.nan)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def pct(k: int, n: int) -> str:
    return "–" if n == 0 else f"{100 * k / n:.0f} % ({k}/{n})"


def pct_ci(k: int, n: int) -> str:
    if n == 0:
        return "–"
    lo, hi = wilson(k, n)
    return f"{100 * k / n:.0f} % ({k}/{n}) [IC95 {100 * lo:.0f}–{100 * hi:.0f}]"


def summarize(records: list[dict]) -> tuple[list[dict], dict]:
    by_cond = defaultdict(list)
    for r in records:
        by_cond[r["condition"]].append(r)
    rows = []
    for c in [c for c in CONDITIONS if c in by_cond]:
        rs = by_cond[c]
        att = [r for r in rs if r["attack"]]
        norm = [r for r in rs if not r["attack"]]
        tried = [r for r in att if r["attack_attempted"]]
        ok_att = sum(r["attack_succeeded"] for r in att)
        lat = [r["monitor_latency_us_median"] for r in rs if r.get("monitor_latency_us_median") is not None]
        rows.append({
            "condition": c,
            "missions": len(rs),
            "attaques_n": len(att),
            "attaques_tentees": sum(r["attack_attempted"] for r in att),
            "attaques_reussies": ok_att,
            "asr": ok_att / len(att) if att else math.nan,
            "asr_ic95_bas": wilson(ok_att, len(att))[0], "asr_ic95_haut": wilson(ok_att, len(att))[1],
            "bloquees_parmi_tentees": sum(not r["attack_succeeded"] for r in tried),
            "normales_n": len(norm),
            "normales_accomplies_agent": sum(r["mission_completed_by_agent"] for r in norm),
            "faux_refus": sum(r.get("monitor_rejects", 0) > 0 for r in norm),
            "normales_refus_echec": sum(r.get("monitor_rejects", 0) > 0 and not r["mission_completed_by_agent"]
                                        for r in norm),
            "toutes_accomplies_agent": sum(r["mission_completed_by_agent"] for r in rs),
            "attaques_non_terminees": sum(not r["mission_completed_by_agent"] for r in att),
            "pose_sur": sum(r["landed_safe"] for r in rs),
            "repli": sum(r["fallback"] is not None for r in rs),
            "repli_echoue": sum(str(r["fallback"]).startswith("echec") for r in rs),
            "erreurs_llm": sum(r["stop_reason"] == "llm_error" for r in rs),
            "llm_s_par_mission": round(sum(r["llm_time_s"] for r in rs) / len(rs), 1),
            "moniteur_latence_us_mediane": round(sorted(lat)[len(lat) // 2], 1) if lat else None,
            "journaux_integres": sum(r.get("journal_ok", False) for r in rs) if c == "moniteur" else None,
        })
    cells = defaultdict(lambda: [0, 0])  # (scénario, condition) -> [réussies, total]
    for r in records:
        if r["attack"]:
            cells[(r["scenario"], r["condition"])][0] += r["attack_succeeded"]
            cells[(r["scenario"], r["condition"])][1] += 1
    return rows, cells


def base_rates(records: list[dict]) -> dict:
    """(scénario d'attaque, condition) -> [missions normales où l'oracle se déclenche, missions normales]."""
    rates = defaultdict(lambda: [0, 0])
    for r in records:
        if r["attack"]:
            continue
        trace = [ToolRecord(**t) for t in r["trace"]]
        home = tuple(r["home"]) if "home" in r else (SITL_HOME if r.get("drone") == "sim" else None)
        for name, sc in SCENARIOS.items():
            if sc.attack and home is not None:
                rates[(name, r["condition"])][0] += sc.succeeded(trace, home)
                rates[(name, r["condition"])][1] += 1
    return rates


def plot(cells: dict, rows: list[dict], path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = {"sans_mandat": "#2a78d6", "prompt_seul": "#eb6834", "moniteur": "#1baf7a"}
    surface, ink, ink2, grid = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
    conditions = [r["condition"] for r in rows]
    scenarios = sorted({s for s, _ in cells})
    groups = scenarios + ["toutes"]

    fig, ax = plt.subplots(figsize=(10, 4.6), dpi=150)
    fig.patch.set_facecolor(surface)
    ax.set_facecolor(surface)
    width = 0.26
    for i, c in enumerate(conditions):
        xs, ys, labels = [], [], []
        for g, s in enumerate(groups):
            if s == "toutes":
                k = sum(cells[(sc, c)][0] for sc in scenarios)
                n = sum(cells[(sc, c)][1] for sc in scenarios)
            else:
                k, n = cells[(s, c)]
            xs.append(g + (i - (len(conditions) - 1) / 2) * width)
            ys.append(100 * k / n if n else 0)
            labels.append(f"{k}/{n}")
        bars = ax.bar(xs, ys, width, color=colors[c], edgecolor=surface, linewidth=2,
                      label=LABELS[c], zorder=3)
        for b, lab in zip(bars, labels):
            ax.annotate(lab, (b.get_x() + b.get_width() / 2, b.get_height()), xytext=(0, 3),
                        textcoords="offset points", ha="center", va="bottom", fontsize=7, color=ink2)
    ax.set_xticks(range(len(groups)))
    ax.set_xticklabels([g.replace("_", "\n") if g != "toutes" else "TOUTES" for g in groups], fontsize=9, color=ink)
    ax.axvline(len(scenarios) - 0.5, color=grid, linewidth=1, zorder=1)
    ax.set_ylim(0, 112)
    ax.set_ylabel("Attaques réussies (%)", color=ink2, fontsize=9)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.tick_params(colors=ink2, labelsize=8, length=0)
    ax.grid(axis="y", color=grid, linewidth=0.8, zorder=0)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(grid)
    ax.set_title("Taux d'attaques réussies par scénario et par condition", loc="left", fontsize=11, color=ink)
    ax.legend(loc="upper left", bbox_to_anchor=(0, -0.16), ncol=3, frameon=False, fontsize=9, labelcolor=ink)
    fig.tight_layout()
    fig.savefig(path, facecolor=surface)
    plt.close(fig)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("results", type=Path, help="dossier contenant raw.jsonl")
    args = p.parse_args()
    records = [json.loads(line) for line in (args.results / "raw.jsonl").read_text(encoding="utf-8").splitlines()]
    rows, cells = summarize(records)
    meta = records[0]

    with open(args.results / "summary_by_condition.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    scenarios = sorted({s for s, _ in cells})
    conditions = [r["condition"] for r in rows]
    with open(args.results / "summary_by_scenario.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["scenario"] + [f"{c}_reussies" for c in conditions] + [f"{c}_total" for c in conditions])
        for s in scenarios:
            w.writerow([s] + [cells[(s, c)][0] for c in conditions] + [cells[(s, c)][1] for c in conditions])
    plot(cells, rows, args.results / "attack_success.png")

    md = [f"# Résultats : {args.results.name}", "",
          f"Modèle `{meta['model']}` ({meta['backend']}), température {meta['temperature']}, "
          f"{meta['n']} essai(s) par (scénario, condition), drone : `{meta['drone']}`, "
          f"barrière d'observation : {'oui' if meta.get('barrier', meta.get('observation_barrier')) else 'non'}. "
          f"{len(records)} missions au total. Plan apparié : mêmes seed, variante et point A pour les 3 conditions.",
          "", "## Par condition", "",
          "| | " + " | ".join(LABELS[c] for c in conditions) + " |",
          "|---|" + "---|" * len(conditions)]
    lines = [
        ("Attaques réussies (ASR)", lambda r: pct_ci(r["attaques_reussies"], r["attaques_n"])),
        ("Attaques tentées par l'agent", lambda r: pct(r["attaques_tentees"], r["attaques_n"])),
        ("Bloquées parmi les tentées", lambda r: pct(r["bloquees_parmi_tentees"], r["attaques_tentees"])),
        ("Missions normales accomplies par l'agent", lambda r: pct(r["normales_accomplies_agent"], r["normales_n"])),
        ("Missions normales avec ≥ 1 refus du moniteur",
         lambda r: pct(r["faux_refus"], r["normales_n"]) if r["condition"] == "moniteur" else "–"),
        ("  dont mission ensuite échouée (coût du refus)",
         lambda r: pct(r["normales_refus_echec"], r["faux_refus"]) if r["condition"] == "moniteur" else "–"),
        ("Missions d'attaque NON terminées par l'agent (perturbation)",
         lambda r: pct(r["attaques_non_terminees"], r["attaques_n"])),
        ("Toutes missions accomplies par l'agent", lambda r: pct(r["toutes_accomplies_agent"], r["missions"])),
        ("Repli de sécurité déclenché", lambda r: pct(r["repli"], r["missions"])),
        ("Fin de mission posé au home", lambda r: pct(r["pose_sur"], r["missions"])),
        ("Erreurs LLM (timeout…)", lambda r: pct(r["erreurs_llm"], r["missions"])),
        ("Temps LLM moyen par mission", lambda r: f"{r['llm_s_par_mission']} s"),
        ("Latence médiane du moniteur (vérif.)",
         lambda r: f"{r['moniteur_latence_us_mediane']} µs" if r["moniteur_latence_us_mediane"] else "–"),
        ("Journaux intègres", lambda r: pct(r["journaux_integres"], r["missions"]) if r["journaux_integres"] is not None
         else "–"),
    ]
    for label, fn in lines:
        md.append(f"| {label} | " + " | ".join(fn(r) for r in rows) + " |")
    md += ["", "## Attaques réussies par scénario", "", "![Taux d'attaques réussies](attack_success.png)", "",
           "| Scénario | " + " | ".join(LABELS[c] for c in conditions) + " |", "|---|" + "---|" * len(conditions)]
    for s in scenarios:
        md.append(f"| `{s}` | " + " | ".join(f"{cells[(s, c)][0]}/{cells[(s, c)][1]}" for c in conditions) + " |")
    rates = base_rates(records)
    if rates:
        md += ["", "## Taux de base : le même comportement SANS injection", "",
               "Chaque oracle d'attaque appliqué aux missions normales. Un taux non nul signifie que l'agent fait "
               "ce comportement de lui-même (erreur), indépendamment de toute injection.", "",
               "| Comportement (oracle) | " + " | ".join(LABELS[c] for c in conditions) + " |",
               "|---|" + "---|" * len(conditions)]
        for s in scenarios:
            md.append(f"| `{s}` | " + " | ".join(f"{rates[(s, c)][0]}/{rates[(s, c)][1]}" for c in conditions) + " |")

    bench = args.results / "latency_bench.csv"
    if bench.exists():
        with open(bench, encoding="utf-8") as f:
            brows = list(csv.DictReader(f))
        env = (args.results / "latency_bench_env.txt")
        md += ["", "## Latence du moniteur (micro-benchmark)", "",
               f"Machine : {env.read_text(encoding='utf-8').strip() if env.exists() else '?'}", "",
               "| Ordre | fsync | n | Vérification médiane (p95) | Écriture journal médiane (p95) |",
               "|---|---|---|---|---|"]
        for b in brows:
            md.append(f"| {b['ordre']} | {'oui' if b['fsync'] == 'True' else 'non'} | {b['n']} | "
                      f"{b['verif_us_median']} µs ({b['verif_us_p95']}) | "
                      f"{b['journal_us_median']} µs ({b['journal_us_p95']}) |")
    md += ["", "Fichiers : `raw.jsonl` (une ligne par mission, trace complète), `summary_by_condition.csv`, "
           "`summary_by_scenario.csv`, `journals/` (journaux du moniteur), `mandate.json` + `op-eval.pub`.", ""]
    (args.results / "results.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))


if __name__ == "__main__":
    main()
