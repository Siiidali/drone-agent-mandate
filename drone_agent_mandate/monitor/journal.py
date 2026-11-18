"""Journal append-only chaîné par hash (format JSON Lines, une entrée par ligne)."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from ..canonical import sha256_hex

GENESIS_HASH = "0" * 64  # « hash précédent » de la première entrée


def entry_hash(entry: dict) -> str:
    return sha256_hex({k: v for k, v in entry.items() if k != "hash"})


class Journal:
    def __init__(self, path: str | Path, fsync: bool = True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fsync = fsync  # écriture forcée sur disque à chaque entrée (plus lent, plus sûr)
        self._seq, self._last_hash = 0, GENESIS_HASH
        if self.path.exists() and self.path.stat().st_size > 0:
            ok, n, error = verify_journal(self.path)
            if not ok:
                raise ValueError(f"journal existant corrompu, refus de le prolonger : {error}")
            last = json.loads(self.path.read_text(encoding="utf-8").splitlines()[-1])
            self._seq, self._last_hash = last["seq"] + 1, last["hash"]
        self._file = open(self.path, "a", encoding="utf-8")

    def append(self, event: dict) -> dict:
        entry = {"seq": self._seq, "ts": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                 **event, "prev_hash": self._last_hash}
        entry["hash"] = entry_hash(entry)
        self._file.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        self._file.flush()
        if self.fsync:
            os.fsync(self._file.fileno())
        self._seq, self._last_hash = self._seq + 1, entry["hash"]
        return entry

    @property
    def last_hash(self) -> str:
        return self._last_hash

    def close(self) -> None:
        self._file.close()


def verify_journal(path: str | Path) -> tuple[bool, int, str | None]:
    """Vérifie toute la chaîne. Renvoie (intègre ?, nombre d'entrées valides, message d'erreur)."""
    prev, n = GENESIS_HASH, 0
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                return False, n, f"ligne {lineno} : JSON illisible"
            if entry.get("seq") != n:
                return False, n, f"ligne {lineno} : numéro de séquence {entry.get('seq')} au lieu de {n}"
            if entry.get("prev_hash") != prev:
                return False, n, f"ligne {lineno} : prev_hash ne correspond pas à l'entrée précédente"
            if entry.get("hash") != entry_hash(entry):
                return False, n, f"ligne {lineno} : contenu modifié (hash recalculé différent)"
            prev, n = entry["hash"], n + 1
    return True, n, None
