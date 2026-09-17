"""The signature on a public artifact download URL. **CRITICAL.** R1-be-3.

`CRITICAL.md` → *Artifact signing keys & `signed_url` generation*: "the signature **is**
the authorization". A board downloading firmware presents no token at all — it presents
the URL `spec/device-protocol.md` fixed into the `stage` payload:

    https://fleet.example/v1/artifact/<sha256>/bin?exp=<unix-seconds>&sig=<base64url>

This module is the whole of that authorization: one HMAC over `(version, digest, exp)`,
minted by `api/routers/deploys.py` and verified by `api/routers/artifact_download.py`.
It conforms to the protocol rather than extending it — the URL shape above is already in
`spec/device-protocol.md`, so this task proposes no spec change.

**Transport-agnostic**, like `presence.py`, `progress.py` and `deploys.py`: no FastAPI
import, no `Settings` object. The caller passes the secret and the numbers, which is what
keeps the rules below testable without an app and readable without a request.

Five properties, each of them a way this gets got wrong:

1. **The version prefix is in the signed message.** `v1\\n<digest>\\n<exp>` — so a later
   scheme (a different digest algorithm, a per-device binding) cannot be silently
   accepted by a verifier that was only taught the old one.
2. **`hmac.compare_digest`, never `==`.** A `==` over the signature is a timing oracle on
   the one credential this endpoint has.
3. **The MAC is verified BEFORE `exp` is looked at.** Otherwise an attacker learns
   "expired" vs "invalid" for a forged far-future `exp` and has a probe against the
   secret. The two are still *reported* apart afterwards, because a device's log must be
   able to say "the link expired" (retriable: re-deploy) rather than "this is not our
   link" (never retriable).
4. **`exp` is re-serialised from the parsed integer into the signed message**, so
   `exp=0123`, `exp=+123` and `exp=1_23` are not second spellings of `exp=123`; they
   simply do not verify. The parse is a strict digits-only match for the same reason —
   Python's `int()` accepts underscores and surrounding whitespace.
5. **The digest is rejected, never normalised** (`identity.py`'s and
   `storage/blobs.py`'s rule): `AB…` is not `ab…`, and repairing it would make two
   spellings of one artifact.

The secret is `Settings.artifact_url_secret`, a 32-byte hex string from
`just artifact-secret`. Rotating it invalidates every URL in flight (at most
`signed_url_ttl_s` of staged deploys, which simply re-deploy); there is deliberately no
key rollover to reason about. It is never logged and never returned in a response.

Mint one by hand — for T2, and for "is this link dead or is the board wrong?" in
production — with `python -m fleetforge.artifact_urls mint <sha256>` (`just artifact-url`),
the same argparse/stdout-is-the-result shape as `python -m fleetforge.auth hash-password`
and `python -m fleetforge.storage selftest`.
"""

import base64
import datetime as dt
import hashlib
import hmac
import re

from fleetforge.clock import now_utc
from fleetforge.storage.blobs import SHA256_HEX

# Fixed by `spec/device-protocol.md`'s `stage` payload example. Not a setting: a device
# flashed in R0 resolves this path for as long as the board exists.
ARTIFACT_PATH = "/v1/artifact/{sha256}/bin"

# Signed into the message, so a v2 scheme cannot be verified by a v1 verifier.
SIG_VERSION = "v1"

# `exp` as it may appear in the query string: digits only, nothing to normalise. No
# leading zero, because `exp=01789…` and `exp=1789…` parse to the same integer and would
# otherwise be two spellings of one link. Bounded so an absurd run of digits cannot be
# handed to `int()` at all.
_EXP_RE = re.compile(r"^[1-9][0-9]{0,18}\Z")


class ArtifactUrlRefused(ValueError):
    """Base for the two refusals. A `ValueError`, never an `ObjectStoreError`.

    Both map to **403** at the endpoint, and neither is retriable by trying the same
    URL again — the distinction below is for the *log* and for the device's own
    diagnosis, not for the status code.
    """


class ArtifactUrlInvalid(ArtifactUrlRefused):
    """Missing or unparseable `exp`, missing `sig`, bad digest shape, or a bad MAC.

    "This is not our link." Nothing about it will ever work, so a device must not retry
    it and an operator should look at who minted it.
    """


class ArtifactUrlExpired(ArtifactUrlRefused):
    """The MAC is ours and `exp` has passed.

    "The link expired." Retriable in the only sense that matters: re-deploy, and the
    board is handed a fresh one.
    """


def is_valid_digest(sha256: str) -> bool:
    """Is `sha256` exactly 64 lowercase hex characters?

    `SHA256_HEX` comes from `storage/blobs.py` rather than being retyped here: the
    digest in the URL and the digest in the blob key must be the same string under the
    same rule, or a URL could authorize a key that does not exist.
    """
    return SHA256_HEX.match(sha256) is not None


def artifact_path(sha256: str) -> str:
    """The path component of the download URL for `sha256`. Validates the digest."""
    if not is_valid_digest(sha256):
        raise ArtifactUrlInvalid("digest is not 64 lowercase hex characters")
    return ARTIFACT_PATH.format(sha256=sha256)


def _signature(sha256: str, exp: int, secret: str) -> str:
    """The base64url (unpadded) HMAC-SHA256 over `v1\\n<digest>\\n<exp>`.

    Unpadded base64url so the value needs no percent-encoding in a query string — an
    escaped signature is a signature two readers can spell differently.
    """
    message = f"{SIG_VERSION}\n{sha256}\n{exp}"
    digest = hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def mint_artifact_url(
    base_url: str,
    sha256: str,
    *,
    secret: str,
    ttl_s: int,
    now: dt.datetime | None = None,
) -> str:
    """A signed, absolute download URL for `sha256`, good for `ttl_s` seconds.

    `base_url` is the origin a **device** reaches (`Settings.public_base_url`), not the
    container-internal one; its trailing `/` is stripped the way `ff_cfg.py` and
    `frontend/src/ffcfg.ts` strip theirs.

    The return value is a bearer credential. `deploys.py` puts it in the command payload
    and nowhere else — never a log line, never the 202 body, never `deploy_events`.
    """
    if not secret:
        # A URL signed with an empty key is forgeable by anyone who reads this module.
        raise ValueError("artifact URL secret is empty; nothing may be signed with it")
    if not base_url:
        raise ValueError("artifact URL base is empty; a device cannot resolve a bare path")
    if ttl_s <= 0:
        raise ValueError("artifact URL TTL must be positive")

    path = artifact_path(sha256)
    exp = int((now or now_utc()).timestamp()) + ttl_s
    sig = _signature(sha256, exp, secret)
    return f"{base_url.rstrip('/')}{path}?exp={exp}&sig={sig}"


def verify_artifact_url(
    sha256: str,
    *,
    exp: str | None,
    sig: str | None,
    secret: str,
    now: dt.datetime | None = None,
) -> int:
    """Authorize a download, or raise. Returns the `exp` that was accepted.

    Order is security-relevant and is property 3 of the module docstring: shape, then
    **MAC**, then expiry. Nothing here touches the database or the object store, so a
    caller cannot spend an IAM `signBlob` call — or learn which digests exist — without
    a valid signature first.
    """
    if not secret:
        raise ValueError("artifact URL secret is empty; no signature can be verified")
    if not is_valid_digest(sha256):
        raise ArtifactUrlInvalid("digest is not 64 lowercase hex characters")
    if not sig:
        raise ArtifactUrlInvalid("no signature")
    if not exp or not _EXP_RE.match(exp):
        # Unparseable is invalid, not expired: we cannot say a link expired when we
        # cannot say when it would have.
        raise ArtifactUrlInvalid("no usable expiry")

    expires_at = int(exp)
    if not hmac.compare_digest(_signature(sha256, expires_at, secret), sig):
        raise ArtifactUrlInvalid("signature does not match")

    if expires_at <= int((now or now_utc()).timestamp()):
        raise ArtifactUrlExpired("the link expired")
    return expires_at


def main(argv: list[str] | None = None) -> int:
    """`python -m fleetforge.artifact_urls mint <sha256>` — print one signed URL.

    **stdout carries the URL and nothing else**, so `URL=$(just artifact-url $SHA)` works.
    The secret is read from the environment (`ARTIFACT_URL_SECRET`) and is never printed,
    not even truncated.

    `Settings` is imported here rather than at module scope: the signing functions above
    are deliberately configuration-free, and importing them must not drag pydantic
    settings validation into the ingestor or into a unit test.
    """
    import argparse
    import sys

    from fleetforge.config import Settings

    parser = argparse.ArgumentParser(prog="python -m fleetforge.artifact_urls")
    subparsers = parser.add_subparsers(dest="command", required=True)
    mint = subparsers.add_parser("mint", help="sign a public download URL for one digest")
    mint.add_argument("sha256", help="the artifact's digest: 64 lowercase hex characters")
    mint.add_argument("--ttl", type=int, help="lifetime in seconds (default SIGNED_URL_TTL_S)")
    mint.add_argument("--base-url", help="origin a device reaches (default PUBLIC_BASE_URL)")
    args = parser.parse_args(argv)

    settings = Settings()  # type: ignore[call-arg]  # values come from the environment
    secret = settings.artifact_url_secret
    if not secret:
        print(
            "ARTIFACT_URL_SECRET is not set: nothing can be signed. Mint one with "
            "`just artifact-secret` and put it in .env.",
            file=sys.stderr,
        )
        return 1
    base_url = args.base_url or settings.public_base_url
    if not base_url:
        print(
            "PUBLIC_BASE_URL is not set and --base-url was not given: a device needs an "
            "absolute origin, e.g. http://localhost:8080.",
            file=sys.stderr,
        )
        return 1

    try:
        url = mint_artifact_url(
            base_url,
            args.sha256,
            secret=secret,
            ttl_s=args.ttl if args.ttl is not None else settings.signed_url_ttl_s,
        )
    except ValueError as exc:
        # ArtifactUrlInvalid is a ValueError too, so a malformed digest lands here
        # rather than as a traceback.
        print(f"cannot mint a URL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
