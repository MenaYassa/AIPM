"""Small strict Ed25519 wrapper for MC-6.12B."""
from __future__ import annotations

from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

SIGNATURE_BYTES = 64


def load_private_key(path: str | Path) -> Ed25519PrivateKey:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError("invalid private-key path")
    mode = candidate.stat().st_mode & 0o777
    if mode != 0o600:
        raise ValueError("private-key mode must be 0600")
    key = serialization.load_pem_private_key(candidate.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("private key is not Ed25519")
    return key


def sign(private_key: Ed25519PrivateKey, message: bytes) -> bytes:
    signature = private_key.sign(message)
    if len(signature) != SIGNATURE_BYTES:
        raise ValueError("unexpected signature length")
    return signature


def verify(public_key: Ed25519PublicKey, signature: bytes, message: bytes) -> None:
    if len(signature) != SIGNATURE_BYTES:
        raise ValueError("unexpected signature length")
    public_key.verify(signature, message)
