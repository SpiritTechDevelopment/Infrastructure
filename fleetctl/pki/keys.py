"""Local key and CSR generation for identities that have no machine of their own.

Node identities never come through here: roles/pki_agent generates the agent key
on the node and only the CSR leaves it. This module exists for the control-plane
identities — the backend's two keys and the two client identities — which belong
to a process, not to a host, and so have to be created wherever the operator is
standing. The private key still never reaches the CA: only the CSR does.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

from .model import PkiError


def generate_key_and_csr(
    key_path: Path,
    csr_path: Path,
    *,
    common_name: str,
    subject_alt_names: Sequence[str],
) -> bytes:
    """Create an EC P-256 key and a CSR declaring exactly these SANs.

    Refuses to overwrite an existing key. Silently replacing one would leave a
    certificate in Vault that no longer matches the key beside it, and
    control_runtime compares the two public keys only after both have been
    written to the host.
    """
    if not subject_alt_names:
        raise PkiError("a CSR without SANs cannot be authorised by either side")
    if key_path.exists():
        raise PkiError(f"refusing to overwrite an existing private key at {key_path}")

    key_path.parent.mkdir(parents=True, exist_ok=True)
    previous = os.umask(0o077)
    try:
        _run(
            "genpkey",
            "-algorithm",
            "EC",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-out",
            str(key_path),
        )
        os.chmod(key_path, 0o600)
        _run(
            "req",
            "-new",
            "-key",
            str(key_path),
            "-subj",
            f"/CN={common_name}",
            "-addext",
            f"subjectAltName={','.join(subject_alt_names)}",
            "-out",
            str(csr_path),
        )
    finally:
        os.umask(previous)
    return csr_path.read_bytes()


def _run(*arguments: str) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            ["openssl", *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise PkiError(f"cannot execute openssl: {exc}") from exc
    if result.returncode != 0:
        operation = arguments[0] if arguments else "command"
        raise PkiError(f"openssl {operation} failed")
    return result
