"""Artifact signing and key-ring rotation (R6-SEC-1, R6-SEC-2).

Why ECDSA P-256 (`ecdsa-p256-sha256`) over the SHA-256 image digest
-------------------------------------------------------------------
1. ESP-IDF already links `mbedtls` (`mbedtls/pk.h`, `mbedtls/ecdsa.h`) with SECP256R1 enabled
   for TLS (`esp-tls`), so verifying an ECDSA P-256 signature on an ESP32/C3/C6/S3 costs zero
   extra third-party dependencies.
2. `ff_ota.c` already streams the written OTA flash slot once through `mbedtls_sha256` to
   produce the 32-byte digest before `esp_https_ota_finish()` switches the boot pointer
   (`R2-fw-1`). Signing `Prehashed(SHA256())` over that 32-byte digest is mathematically
   identical to `ECDSA(SHA256())` over the raw firmware image bytes, so the board verifies the
   flash contents in one pass without reading the flash slot a second time.
3. The wire format is `ecdsa-p256-sha256:<base64_der_signature>`, carried in the `stage`
   command's `artifact.sig` field when present.

Key rotation ring (`R6-SEC-2`)
------------------------------
A board in the field cannot update its trusted public key without first accepting a signed
OTA update, and an in-flight rollout during a key rotation may mix artifacts signed by the
outgoing key and the incoming key. Every verifier (server, simulator, and C agent) therefore
carries a **rotation ring of >= 2 public keys** (primary + secondary/next) and accepts a
signature verified by *any* public key in the ring:

1. **Steady state**: ring holds `[K1_pub, K2_pub]`; server signs with `K1_priv`.
2. **Switch signer**: operator switches `ARTIFACT_SIGNING_KEY` from `K1_priv` to `K2_priv`;
   every already-flashed board already trusts `K2_pub`, so zero boards brick.
3. **Roll out next ring**: next firmware release replaces `K1_pub` with `K3_pub` while
   continuing to sign with `K2_priv`.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

SIG_SCHEME = "ecdsa-p256-sha256"
SIG_PREFIX = f"{SIG_SCHEME}:"
# ASN.1 DER-encoded ECDSA P-256 signature is 68..72 bytes (92..96 base64 chars); bound tightly
# enough to fit a stack buffer in `ff_ota_cmd_t` (`FF_OTA_MAX_SIG = 160`).
MAX_SIG_LEN = 159
_SIG_RE = re.compile(r"\Aecdsa-p256-sha256:[A-Za-z0-9+/]{80,120}={0,2}\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")

# Built-in development/test 2-key rotation ring. Shared with `ff_ota.c` and
# `simulator/device.py` so development and E2E runs verify out of the box, while production
# deployments supply their own `ARTIFACT_SIGNING_KEY` and `ARTIFACT_SIGNING_PUBLIC_KEYS`
# (and compile-time `FF_OTA_SIGNING_PUBKEY_PRIMARY_PEM` / `FF_OTA_SIGNING_PUBKEY_SECONDARY_PEM`).
DEFAULT_PRIMARY_PRIVATE_KEY_PEM = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgYXch2r9SqzagXXyK\n"
    "3samj7Q09s4gZK77KcYQ/vz1qouhRANCAARseDW1ULbxr3LxJtljSOKzs4opbGtf\n"
    "b5LmDQ2js0zJXycj69QcdDww1Cc1INI8RJQBP2baIvI0L+czbpG/FhXV\n"
    "-----END PRIVATE KEY-----\n"
)

DEFAULT_PRIMARY_PUBLIC_KEY_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEbHg1tVC28a9y8SbZY0jis7OKKWxr\n"
    "X2+S5g0No7NMyV8nI+vUHHQ8MNQnNSDSPESUAT9m2iLyNC/nM26RvxYV1Q==\n"
    "-----END PUBLIC KEY-----\n"
)

DEFAULT_SECONDARY_PRIVATE_KEY_PEM = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgDxHmzFRHCcsZT1Jz\n"
    "fAbiiWc2Eck90QgEbcBy5d2iNhyhRANCAATAly301s6I4hucH9OhhO+rTYQGvOxQ\n"
    "ARPGwUABYcVaHF+kvfHQ5wgfrJb2roSFgDerq80YgTALso8lBd3U40Sp\n"
    "-----END PRIVATE KEY-----\n"
)

DEFAULT_SECONDARY_PUBLIC_KEY_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEwJct9NbOiOIbnB/ToYTvq02EBrzs\n"
    "UAETxsFAAWHFWhxfpL3x0OcIH6yW9q6EhYA3q6vNGIEwC7KPJQXd1ONEqQ==\n"
    "-----END PUBLIC KEY-----\n"
)

DEFAULT_PUBLIC_KEY_RING: tuple[str, str] = (
    DEFAULT_PRIMARY_PUBLIC_KEY_PEM,
    DEFAULT_SECONDARY_PUBLIC_KEY_PEM,
)


def _normalize_pem(raw: str) -> str:
    """Accept literal PEM, `\\n`-escaped env-var PEM, or base64-encoded PEM."""
    text = raw.strip()
    if "\\n" in text and "\n" not in text:
        text = text.replace("\\n", "\n")
    if not text.startswith("-----BEGIN "):
        try:
            decoded = base64.b64decode(text, validate=True).decode("utf-8")
            if decoded.strip().startswith("-----BEGIN "):
                text = decoded.strip()
        except (binascii.Error, UnicodeDecodeError):
            pass
    return text + "\n"


def load_private_key(pem_or_b64: str) -> ec.EllipticCurvePrivateKey:
    """Load and validate an ECDSA P-256 (`SECP256R1`) private key."""
    pem = _normalize_pem(pem_or_b64).encode("utf-8")
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("signing key must be an ECDSA P-256 (SECP256R1) private key")
    return key


def load_public_key(pem_or_b64: str) -> ec.EllipticCurvePublicKey:
    """Load and validate an ECDSA P-256 (`SECP256R1`) public key."""
    pem = _normalize_pem(pem_or_b64).encode("utf-8")
    key = serialization.load_pem_public_key(pem)
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("public key must be an ECDSA P-256 (SECP256R1) public key")
    return key


def public_key_pem_from_private(private_key_pem: str) -> str:
    """Derive the canonical SubjectPublicKeyInfo PEM for a P-256 private key."""
    priv = load_private_key(private_key_pem)
    return (
        priv.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )


def generate_keypair() -> tuple[str, str]:
    """Generate a fresh ECDSA P-256 `(private_key_pem, public_key_pem)` pair."""
    priv = ec.generate_private_key(ec.SECP256R1())
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")
    pub_pem = (
        priv.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    return priv_pem, pub_pem


def parse_public_key_ring(
    raw_ring: str | Sequence[str] | None,
    *,
    active_private_key: str | None = None,
) -> tuple[str, ...]:
    """Parse a `;`- or PEM-block-separated public key ring, ensuring >= 1 valid P-256 key.

    When `raw_ring` is None or empty, falls back to `DEFAULT_PUBLIC_KEY_RING` (and prepends
    the public key derived from `active_private_key` if it is not already in the ring).
    """
    candidates: list[str] = []
    if raw_ring is None:
        candidates.extend(DEFAULT_PUBLIC_KEY_RING)
    elif isinstance(raw_ring, str):
        stripped = raw_ring.strip()
        if not stripped:
            candidates.extend(DEFAULT_PUBLIC_KEY_RING)
        elif "-----BEGIN PUBLIC KEY-----" in stripped:
            blocks = re.findall(
                r"-----BEGIN PUBLIC KEY-----.*?-----END PUBLIC KEY-----",
                stripped.replace("\\n", "\n"),
                flags=re.DOTALL,
            )
            candidates.extend(blocks if blocks else [stripped])
        else:
            candidates.extend(part.strip() for part in stripped.split(";") if part.strip())
    else:
        candidates.extend(raw_ring)

    normalized: list[str] = []
    if active_private_key:
        active_pub = public_key_pem_from_private(active_private_key)
        normalized.append(active_pub)

    for item in candidates:
        pub = load_public_key(item)
        canonical = pub.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")
        if canonical not in normalized:
            normalized.append(canonical)

    if not normalized:
        raise ValueError("public key ring must contain at least one P-256 public key")
    return tuple(normalized)


def is_valid_sig_format(sig: str) -> bool:
    """True when `sig` is `ecdsa-p256-sha256:<base64_der>` with a valid DER SEQUENCE header."""
    if not isinstance(sig, str) or len(sig) > MAX_SIG_LEN or not _SIG_RE.match(sig):
        return False
    b64_part = sig.removeprefix(SIG_PREFIX)
    try:
        der = base64.b64decode(b64_part, validate=True)
    except binascii.Error:
        return False
    # ASN.1 DER SEQUENCE tag is 0x30; P-256 ECDSA DER signatures are 8..80 bytes.
    return 8 <= len(der) <= 80 and der[0] == 0x30


def sign_digest(sha256_hex: str, private_key_pem: str = DEFAULT_PRIMARY_PRIVATE_KEY_PEM) -> str:
    """Sign a 64-char lowercase hex SHA-256 digest and return `ecdsa-p256-sha256:<base64>`."""
    if not _SHA256_RE.match(sha256_hex):
        raise ValueError("sha256_hex must be 64 lowercase hex characters")
    priv = load_private_key(private_key_pem)
    digest_bytes = bytes.fromhex(sha256_hex)
    der_sig = priv.sign(digest_bytes, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
    return f"{SIG_PREFIX}{base64.b64encode(der_sig).decode('ascii')}"


def sign_bytes(data: bytes, private_key_pem: str = DEFAULT_PRIMARY_PRIVATE_KEY_PEM) -> str:
    """Hash `data` with SHA-256 and sign the digest."""
    return sign_digest(hashlib.sha256(data).hexdigest(), private_key_pem)


def verify_digest(
    sha256_hex: str,
    sig: str,
    public_keys_pem: Sequence[str] | None = None,
) -> bool:
    """Verify `sig` against `sha256_hex` across all public keys in the rotation ring."""
    if not _SHA256_RE.match(sha256_hex) or not is_valid_sig_format(sig):
        return False
    der_sig = base64.b64decode(sig.removeprefix(SIG_PREFIX))
    digest_bytes = bytes.fromhex(sha256_hex)
    ring = parse_public_key_ring(public_keys_pem)
    for pub_pem in ring:
        pub = load_public_key(pub_pem)
        try:
            pub.verify(der_sig, digest_bytes, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
            return True
        except InvalidSignature:
            continue
    return False


def verify_bytes(
    data: bytes,
    sig: str,
    public_keys_pem: Sequence[str] | None = None,
) -> bool:
    """Verify `sig` against the SHA-256 digest of `data` across the public key ring."""
    return verify_digest(hashlib.sha256(data).hexdigest(), sig, public_keys_pem)


@dataclass(frozen=True)
class SigningKeyRing:
    """Server-side key ring combining an optional active signer with a >=2-key verify ring."""

    public_keys_pem: tuple[str, ...]
    private_key_pem: str | None = None

    @classmethod
    def from_config(
        cls,
        *,
        private_key: str | None = None,
        public_keys: str | Sequence[str] | None = None,
    ) -> SigningKeyRing:
        ring = parse_public_key_ring(public_keys, active_private_key=private_key)
        norm_priv = _normalize_pem(private_key) if private_key else None
        if norm_priv is not None:
            load_private_key(norm_priv)
        return cls(public_keys_pem=ring, private_key_pem=norm_priv)

    @property
    def can_sign(self) -> bool:
        return self.private_key_pem is not None

    def sign_digest(self, sha256_hex: str) -> str:
        if self.private_key_pem is None:
            raise RuntimeError("no active private signing key configured")
        return sign_digest(sha256_hex, self.private_key_pem)

    def verify_digest(self, sha256_hex: str, sig: str) -> bool:
        return verify_digest(sha256_hex, sig, self.public_keys_pem)


def _cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fleetforge.signing",
        description="Generate ECDSA P-256 signing keys, sign firmware images, or verify signatures.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_gen = sub.add_parser("keygen", help="Generate a new ECDSA P-256 keypair")
    p_gen.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Optional directory to write signing-key.pem (0600) and signing-pub.pem",
    )

    p_sign = sub.add_parser("sign", help="Sign a firmware file or 64-char sha256 digest")
    p_sign.add_argument("target", help="Path to firmware .bin file or 64-char hex sha256")
    p_sign.add_argument(
        "--key",
        type=Path,
        default=None,
        help="Path to PEM private key (defaults to built-in primary dev key)",
    )

    p_verify = sub.add_parser("verify", help="Verify an ecdsa-p256-sha256 signature")
    p_verify.add_argument("target", help="Path to firmware .bin file or 64-char hex sha256")
    p_verify.add_argument("sig", help="Detached signature string (ecdsa-p256-sha256:<base64>)")
    p_verify.add_argument(
        "--pubkey",
        type=Path,
        action="append",
        default=None,
        help="Path to trusted public key PEM (repeatable for a rotation ring)",
    )

    args = parser.parse_args(argv)
    if args.cmd == "keygen":
        priv_pem, pub_pem = generate_keypair()
        if args.out_dir is not None:
            args.out_dir.mkdir(parents=True, exist_ok=True)
            priv_path = args.out_dir / "signing-key.pem"
            pub_path = args.out_dir / "signing-pub.pem"
            priv_path.write_text(priv_pem, encoding="utf-8")
            priv_path.chmod(0o600)
            pub_path.write_text(pub_pem, encoding="utf-8")
            sys.stdout.write(f"wrote {priv_path} and {pub_path}\n")
        else:
            sys.stdout.write(priv_pem)
            sys.stdout.write(pub_pem)
        return 0

    target_str: str = args.target
    if _SHA256_RE.match(target_str):
        digest_hex = target_str
    else:
        digest_hex = hashlib.sha256(Path(target_str).read_bytes()).hexdigest()

    if args.cmd == "sign":
        priv_pem = (
            args.key.read_text(encoding="utf-8")
            if args.key is not None
            else DEFAULT_PRIMARY_PRIVATE_KEY_PEM
        )
        sys.stdout.write(sign_digest(digest_hex, priv_pem) + "\n")
        return 0

    if args.cmd == "verify":
        pub_pems = [p.read_text(encoding="utf-8") for p in args.pubkey] if args.pubkey else None
        ok = verify_digest(digest_hex, args.sig, pub_pems)
        if ok:
            sys.stdout.write("OK\n")
            return 0
        sys.stderr.write("INVALID SIGNATURE\n")
        return 1

    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
