import copy
import json
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from drone_agent_mandate.drone.sim_drone import SITL_HOME
from drone_agent_mandate.geo import offset_latlon
from drone_agent_mandate.mandate import (Mandate, MandateError, default_mandate, generate_keypair,
                                         load_private_key, load_trust_store, sign_mandate, verify_signed_mandate)

OP = "op-test"


@pytest.fixture
def key():
    return Ed25519PrivateKey.generate()


@pytest.fixture
def mandate():
    return default_mandate(SITL_HOME, OP)


def trust(key):
    return {OP: key.public_key()}


def test_sign_and_verify_roundtrip(key, mandate):
    doc = sign_mandate(mandate, key)
    # Passage par du JSON texte, comme un fichier sur disque.
    restored = verify_signed_mandate(json.loads(json.dumps(doc)), trust(key))
    assert restored == mandate and restored.fingerprint() == mandate.fingerprint()


def test_key_order_does_not_matter(key, mandate):
    """Grâce au JSON canonique, réordonner les champs ne casse pas la signature."""
    doc = sign_mandate(mandate, key)
    doc["mandat"] = dict(reversed(list(doc["mandat"].items())))
    verify_signed_mandate(doc, trust(key))


@pytest.mark.parametrize("field,value", [
    ("altitude_max_m", 500.0),
    ("actions_permises", ["goto", "land", "return_to_launch", "set_parameter", "takeoff", "hover"]),
    ("duree_max_s", 9000.0),
    ("zones_atterrissage", None),
])
def test_tampering_is_detected(key, mandate, field, value):
    doc = sign_mandate(mandate, key)
    doc["mandat"][field] = value
    with pytest.raises(MandateError, match="signature invalide"):
        verify_signed_mandate(doc, trust(key))


def test_wrong_key_and_unknown_operator(key, mandate):
    doc = sign_mandate(mandate, Ed25519PrivateKey.generate())  # signé par quelqu'un d'autre
    with pytest.raises(MandateError, match="signature invalide"):
        verify_signed_mandate(doc, trust(key))
    with pytest.raises(MandateError, match="opérateur inconnu"):
        verify_signed_mandate(sign_mandate(mandate, key), {"autre": key.public_key()})


def test_attacker_cannot_change_operator_id(key, mandate):
    """Changer id_operateur vers une clé que l'attaquant contrôle : la signature ne colle plus."""
    attacker = Ed25519PrivateKey.generate()
    doc = sign_mandate(mandate, key)
    doc["mandat"]["id_operateur"] = "attaquant"
    with pytest.raises(MandateError):
        verify_signed_mandate(doc, {OP: key.public_key(), "attaquant": attacker.public_key()})


def test_malformed_documents(key, mandate):
    for doc in [{}, {"mandat": {}}, {"mandat": mandate.to_dict(), "signature": {"alg": "RSA", "valeur": ""}},
                {"mandat": mandate.to_dict(), "signature": {"alg": "Ed25519", "valeur": "pas du base64!"}}]:
        with pytest.raises(MandateError):
            verify_signed_mandate(doc, trust(key))


def test_invalid_content_rejected(mandate):
    d = mandate.to_dict()
    for bad in [{"altitude_max_m": float("nan")}, {"altitude_max_m": -1}, {"actions_permises": ["fly_anywhere"]},
                {"zone": [[0, 0], [1, 1]]}, {"champ_inconnu": 1}, {"expiration": d["emis_le"]}]:
        with pytest.raises(MandateError):
            Mandate.from_dict({**d, **bad})


def test_zone_and_landing(mandate):
    a = offset_latlon(*SITL_HOME, 40, 20)
    far = offset_latlon(*SITL_HOME, 0, 400)
    assert mandate.contains(*a) and not mandate.contains(*far)
    assert mandate.landing_allowed(*SITL_HOME) and not mandate.landing_allowed(*a)


# ------------------------------------------------------------------ sous-mandats

def test_restriction_accepted(mandate):
    child = mandate.derive({"altitude_max_m": 20, "actions_permises": ["takeoff", "goto", "land", "return_to_launch"]})
    assert child.altitude_max_m == 20 and child.parent == mandate.fingerprint()
    assert child.widening_reasons(mandate) == []
    small_zone = [list(offset_latlon(*SITL_HOME, n, e)) for n, e in [(-10, -10), (-10, 50), (50, 50), (50, -10)]]
    assert mandate.derive({"zone": small_zone}).contains(*offset_latlon(*SITL_HOME, 40, 20))


@pytest.mark.parametrize("changes", [
    {"altitude_max_m": 120},
    {"duree_max_s": 5000},
    {"actions_permises": ["takeoff", "goto", "set_parameter"]},
    {"zones_atterrissage": None},
    {"zone": [list(offset_latlon(*SITL_HOME, n, e)) for n, e in [(-50, -50), (-50, 500), (80, 500), (80, -50)]]},
])
def test_widening_refused(mandate, changes):
    with pytest.raises(MandateError, match="élargissement refusé"):
        mandate.derive(changes)


def test_expiration_cannot_be_extended(mandate):
    later = (mandate.expiration + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with pytest.raises(MandateError):
        mandate.derive({"expiration": later})


def test_identity_fields_cannot_be_changed(mandate):
    with pytest.raises(MandateError, match="non modifiables"):
        mandate.derive({"id_operateur": "autre"})


def test_keys_on_disk(tmp_path, mandate):
    priv, pub = generate_keypair(tmp_path, OP)
    assert (priv.stat().st_mode & 0o777) == 0o600
    doc = sign_mandate(mandate, load_private_key(priv))
    assert verify_signed_mandate(copy.deepcopy(doc), load_trust_store(tmp_path)) == mandate


def test_noop_restriction_refused(mandate):
    """Observé sur SITL : l'agent redemande le mandat à l'identique en croyant gagner un droit."""
    d = mandate.to_dict()
    with pytest.raises(MandateError, match="sans effet"):
        mandate.derive({k: d[k] for k in ("zone", "altitude_max_m", "duree_max_s", "actions_permises")})


def test_restriction_cannot_remove_return_actions(mandate):
    """Observé pendant l'évaluation : restreindre à goto/hover bloquait le drone en l'air."""
    with pytest.raises(MandateError, match="actions de retour"):
        mandate.derive({"actions_permises": ["goto", "hover"]})
    child = mandate.derive({"actions_permises": ["goto", "land", "return_to_launch"]})  # retirer hover/takeoff : OK
    assert child.actions_permises == {"goto", "land", "return_to_launch"}


def test_restriction_cannot_exclude_landing_zone(mandate):
    """Une zone restreinte qui ne contient plus la zone d'atterrissage est refusée."""
    far_corner = [list(offset_latlon(*SITL_HOME, n, e)) for n, e in [(20, 20), (20, 70), (70, 70), (70, 20)]]
    with pytest.raises(MandateError, match="atterrissage"):
        mandate.derive({"zone": far_corner})
