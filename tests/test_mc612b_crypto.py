from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aipm.provenance.crypto import load_private_key, sign, verify


def _write_key(path: Path, private: Ed25519PrivateKey, mode: int = 0o600) -> None:
    path.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    path.chmod(mode)


def test_ed25519_sign_verify_and_modified_message(tmp_path: Path) -> None:
    private = Ed25519PrivateKey.generate()
    path = tmp_path / "key.pem"
    _write_key(path, private)
    loaded = load_private_key(path)
    message = b"mc612b"
    signature = sign(loaded, message)
    verify(loaded.public_key(), signature, message)
    with pytest.raises(Exception):
        verify(loaded.public_key(), signature, b"modified")
    with pytest.raises(Exception):
        verify(Ed25519PrivateKey.generate().public_key(), signature, message)


def test_key_mode_and_type_are_strict(tmp_path: Path) -> None:
    private = Ed25519PrivateKey.generate()
    path = tmp_path / "key.pem"
    _write_key(path, private, 0o644)
    with pytest.raises(ValueError):
        load_private_key(path)
    path.chmod(0o600)
    link = tmp_path / "link.pem"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        load_private_key(link)
