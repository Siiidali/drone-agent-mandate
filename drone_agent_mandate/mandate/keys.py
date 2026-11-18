"""Gestion des clés Ed25519 de l'opérateur."""

import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def generate_keypair(out_dir: str | Path, operator_id: str) -> tuple[Path, Path]:
    """Crée <operator_id>.key (privée, droits 600) et <operator_id>.pub (publique)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    private_key = Ed25519PrivateKey.generate()
    priv_path, pub_path = out_dir / f"{operator_id}.key", out_dir / f"{operator_id}.pub"
    priv_bytes = private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption())
    fd = os.open(priv_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(priv_bytes)
    pub_path.write_bytes(private_key.public_key().public_bytes(serialization.Encoding.PEM,
                                                               serialization.PublicFormat.SubjectPublicKeyInfo))
    return priv_path, pub_path


def load_private_key(path: str | Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"{path} n'est pas une clé privée Ed25519")
    return key


def load_public_key(path: str | Path) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(Path(path).read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError(f"{path} n'est pas une clé publique Ed25519")
    return key


def load_trust_store(directory: str | Path) -> dict[str, Ed25519PublicKey]:
    """Clés publiques des opérateurs de confiance : {id_operateur: clé}, d'après <id>.pub."""
    return {p.stem: load_public_key(p) for p in sorted(Path(directory).glob("*.pub"))}
