import json

import pytest

from drone_agent_mandate.monitor import Journal, verify_journal


def write(path, n=5):
    j = Journal(path, fsync=False)
    for i in range(n):
        j.append({"type": "decision", "action": "goto", "i": i})
    j.close()


def lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def test_valid_chain(tmp_path):
    p = tmp_path / "j.jsonl"
    write(p)
    assert verify_journal(p) == (True, 5, None)
    entries = [json.loads(line) for line in lines(p)]
    assert entries[0]["prev_hash"] == "0" * 64
    assert all(entries[i]["prev_hash"] == entries[i - 1]["hash"] for i in range(1, 5))


def test_modification_detected(tmp_path):
    p = tmp_path / "j.jsonl"
    write(p)
    ls = lines(p)
    e = json.loads(ls[2])
    e["action"] = "land"  # on falsifie une décision
    ls[2] = json.dumps(e)
    p.write_text("\n".join(ls) + "\n")
    ok, n, err = verify_journal(p)
    assert not ok and n == 2 and "modifié" in err


def test_modification_with_recomputed_hash_detected(tmp_path):
    """Recalculer le hash de l'entrée falsifiée ne suffit pas : la suivante ne pointe plus dessus."""
    from drone_agent_mandate.monitor.journal import entry_hash
    p = tmp_path / "j.jsonl"
    write(p)
    ls = lines(p)
    e = json.loads(ls[2])
    e["action"] = "land"
    e["hash"] = entry_hash(e)
    ls[2] = json.dumps(e)
    p.write_text("\n".join(ls) + "\n")
    ok, n, err = verify_journal(p)
    assert not ok and n == 3 and "prev_hash" in err


@pytest.mark.parametrize("mutate", [
    lambda ls: ls[:2] + ls[3:],                  # suppression au milieu
    lambda ls: [ls[0], ls[2], ls[1], *ls[3:]],   # réordonnancement
    lambda ls: [ls[0], ls[0], *ls[1:]],          # duplication / rejeu
])
def test_structural_tampering_detected(tmp_path, mutate):
    p = tmp_path / "j.jsonl"
    write(p)
    p.write_text("\n".join(mutate(lines(p))) + "\n")
    assert not verify_journal(p)[0]


def test_truncation_is_NOT_detected(tmp_path):
    """Limite documentée : supprimer la fin donne une chaîne valide. D'où l'ancrage du dernier hash."""
    p = tmp_path / "j.jsonl"
    write(p)
    p.write_text("\n".join(lines(p)[:3]) + "\n")
    assert verify_journal(p) == (True, 3, None)


def test_reopen_continues_chain(tmp_path):
    p = tmp_path / "j.jsonl"
    write(p, 3)
    write(p, 2)  # deuxième session sur le même fichier
    assert verify_journal(p) == (True, 5, None)


def test_refuses_to_extend_corrupted_journal(tmp_path):
    p = tmp_path / "j.jsonl"
    write(p, 3)
    p.write_text(p.read_text().replace('"goto"', '"land"', 1))
    with pytest.raises(ValueError, match="corrompu"):
        Journal(p)
