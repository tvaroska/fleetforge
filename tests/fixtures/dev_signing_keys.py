"""Test-only ECDSA P-256 private signing keys matching the built-in dev public key ring.

Moved out of `src/fleetforge/signing.py` (`D-1 / A-8`) so production packages never ship
private signing keys.
"""

from __future__ import annotations

DEV_PRIMARY_PRIVATE_KEY_PEM = (  # pragma: allowlist secret
    "-----BEGIN PRIVATE KEY-----\n"
    "MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgYXch2r9SqzagXXyK\n"
    "3samj7Q09s4gZK77KcYQ/vz1qouhRANCAARseDW1ULbxr3LxJtljSOKzs4opbGtf\n"
    "b5LmDQ2js0zJXycj69QcdDww1Cc1INI8RJQBP2baIvI0L+czbpG/FhXV\n"
    "-----END PRIVATE KEY-----\n"
)

DEV_SECONDARY_PRIVATE_KEY_PEM = (  # pragma: allowlist secret
    "-----BEGIN PRIVATE KEY-----\n"
    "MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgDxHmzFRHCcsZT1Jz\n"
    "fAbiiWc2Eck90QgEbcBy5d2iNhyhRANCAATAly301s6I4hucH9OhhO+rTYQGvOxQ\n"
    "ARPGwUABYcVaHF+kvfHQ5wgfrJb2roSFgDerq80YgTALso8lBd3U40Sp\n"
    "-----END PRIVATE KEY-----\n"
)

DEFAULT_PRIMARY_PRIVATE_KEY_PEM = DEV_PRIMARY_PRIVATE_KEY_PEM
DEFAULT_SECONDARY_PRIVATE_KEY_PEM = DEV_SECONDARY_PRIVATE_KEY_PEM
