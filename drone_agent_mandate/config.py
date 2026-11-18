"""Chargement de la configuration : config.yaml si présent, sinon config.example.yaml."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | Path | None = None) -> dict:
    if path is None:
        path = ROOT / "config.yaml"
        if not path.exists():
            path = ROOT / "config.example.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)
