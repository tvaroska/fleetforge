"""The artifact URL signing scheme. **CRITICAL.** R1-be-3.

`src/fleetforge/artifact_urls.py` is the whole authorization for the one public endpoint
that hands out firmware, so these tests are about the ways a signature scheme is
normally wrong, not about the happy path:

* **Round trip**, with a frozen clock, so "it verifies" is not "it verifies today".
* **Every field is covered by the MAC.** A signature minted for digest A must not
  authorize digest B, and `exp` must not be movable after the fact.
* **`exp` has one spelling.** `0123`, `+123`, `1_23` and ` 123 ` are not second names for
  `123` — Python's `int()` accepts three of those four, which is why the parse is a
  digits-only regex and why the signed message carries the *re-serialised* integer.
* **The MAC is checked before the expiry**, so a forged far-future link cannot be used to
  tell "not ours" from "too old" and probe the secret.
* **The digest is rejected, never normalised** — the `storage/blobs.py` rule, because an
  uppercased digest that got "repaired" would be a second spelling of one artifact.

`clock.now_utc` is never patched here: every function takes `now`, which is the reason it
does.
"""

import base64
import datetime as dt
import hashlib
import hmac

import pytest

from fleetforge.artifact_urls import (
    ARTIFACT_PATH,
    SIG_VERSION,
    ArtifactUrlExpired,
    ArtifactUrlInvalid,
    artifact_path,
    is_valid_digest,
    mint_artifact_url,
    verify_artifact_url,
)

SHA256 = "a" * 63 + "b"
OTHER_SHA256 = "c" * 63 + "d"
SECRET = "unit-test-secret"  # noqa: S105 - a test fixture, not a credential
BASE_URL = "https://downloads.test"
NOW = dt.datetime(2026, 9, 17, 12, 0, 0, tzinfo=dt.UTC)


def parts(url: str) -> tuple[str, str]:
    """`(exp, sig)` out of a minted URL, without trusting a URL parser to round trip."""
    query = url.split("?", 1)[1]
    fields = dict(pair.split("=", 1) for pair in query.split("&"))
    return fields["exp"], fields["sig"]


def at(offset_s: int) -> dt.datetime:
    return NOW + dt.timedelta(seconds=offset_s)


class TestMinting:
    """What a minted URL looks like, and what it refuses to be."""

    def test_the_url_is_the_shape_the_protocol_fixed(self) -> None:
        url = mint_artifact_url(BASE_URL, SHA256, secret=SECRET, ttl_s=600, now=NOW)

        # `spec/device-protocol.md`'s stage payload: absolute, our origin, our path.
        assert url.startswith(f"{BASE_URL}{ARTIFACT_PATH.format(sha256=SHA256)}?")
        exp, sig = parts(url)
        assert exp == str(int(NOW.timestamp()) + 600)
        assert sig

    def test_the_signature_needs_no_percent_encoding(self) -> None:
        """base64url, unpadded: an escaped signature is one two readers spell apart."""
        _, sig = parts(mint_artifact_url(BASE_URL, SHA256, secret=SECRET, ttl_s=600, now=NOW))

        assert "=" not in sig
        assert "+" not in sig
        assert "/" not in sig

    def test_the_signed_message_is_version_digest_exp(self) -> None:
        """Pinned by hand, not by calling the module's own helper.

        A test that computes the expectation with the code under test proves only that
        the code is self-consistent. If this assertion has to change, the scheme changed
        and every URL in flight died — which is exactly the review conversation the
        `v1` prefix exists to force.
        """
        url = mint_artifact_url(BASE_URL, SHA256, secret=SECRET, ttl_s=600, now=NOW)
        exp, sig = parts(url)

        message = f"{SIG_VERSION}\n{SHA256}\n{exp}".encode()
        expected = hmac.new(SECRET.encode(), message, hashlib.sha256).digest()
        assert sig == base64.urlsafe_b64encode(expected).rstrip(b"=").decode()

    def test_a_trailing_slash_on_the_base_url_does_not_double(self) -> None:
        url = mint_artifact_url(f"{BASE_URL}/", SHA256, secret=SECRET, ttl_s=600, now=NOW)

        assert url.startswith(f"{BASE_URL}/v1/artifact/")
        assert "//v1" not in url

    def test_an_empty_secret_is_refused_rather_than_used(self) -> None:
        """A URL signed with "" is forgeable by anyone who has read the module."""
        with pytest.raises(ValueError, match="secret is empty"):
            mint_artifact_url(BASE_URL, SHA256, secret="", ttl_s=600, now=NOW)

    def test_an_empty_base_url_is_refused(self) -> None:
        with pytest.raises(ValueError, match="base is empty"):
            mint_artifact_url("", SHA256, secret=SECRET, ttl_s=600, now=NOW)

    @pytest.mark.parametrize("ttl_s", [0, -1])
    def test_a_non_positive_ttl_is_refused(self, ttl_s: int) -> None:
        """Born-expired is a bug at the mint site, not a link to hand a board."""
        with pytest.raises(ValueError, match="TTL must be positive"):
            mint_artifact_url(BASE_URL, SHA256, secret=SECRET, ttl_s=ttl_s, now=NOW)

    @pytest.mark.parametrize("digest", ["", "abc", SHA256.upper(), SHA256 + "a", "g" * 64])
    def test_a_digest_that_is_not_64_lowercase_hex_is_refused(self, digest: str) -> None:
        with pytest.raises(ArtifactUrlInvalid):
            mint_artifact_url(BASE_URL, digest, secret=SECRET, ttl_s=600, now=NOW)


class TestVerifying:
    """The refusals, which are the whole point of the module."""

    def mint(self, *, ttl_s: int = 600, digest: str = SHA256) -> tuple[str, str]:
        return parts(mint_artifact_url(BASE_URL, digest, secret=SECRET, ttl_s=ttl_s, now=NOW))

    def test_a_fresh_link_verifies_and_returns_its_expiry(self) -> None:
        exp, sig = self.mint()

        accepted = verify_artifact_url(SHA256, exp=exp, sig=sig, secret=SECRET, now=at(1))

        assert accepted == int(NOW.timestamp()) + 600

    def test_it_is_still_good_one_second_before_it_dies(self) -> None:
        exp, sig = self.mint(ttl_s=600)

        assert verify_artifact_url(SHA256, exp=exp, sig=sig, secret=SECRET, now=at(599))

    def test_it_is_dead_at_its_expiry_second_not_after_it(self) -> None:
        """`exp` is the instant it stops working: `<=`, so the boundary is not a gap."""
        exp, sig = self.mint(ttl_s=600)

        with pytest.raises(ArtifactUrlExpired):
            verify_artifact_url(SHA256, exp=exp, sig=sig, secret=SECRET, now=at(600))

    def test_a_flipped_signature_character_is_invalid_not_expired(self) -> None:
        exp, sig = self.mint()
        tampered = sig[:-1] + ("X" if sig[-1] != "X" else "Y")

        with pytest.raises(ArtifactUrlInvalid):
            verify_artifact_url(SHA256, exp=exp, sig=tampered, secret=SECRET, now=at(1))

    def test_a_signature_for_one_digest_does_not_authorize_another(self) -> None:
        """The digest is in the signed message, so a link is a link to *one* artifact."""
        exp, sig = self.mint(digest=SHA256)

        with pytest.raises(ArtifactUrlInvalid):
            verify_artifact_url(OTHER_SHA256, exp=exp, sig=sig, secret=SECRET, now=at(1))

    def test_extending_the_expiry_invalidates_the_signature(self) -> None:
        exp, sig = self.mint(ttl_s=1)
        extended = str(int(exp) + 86_400)

        with pytest.raises(ArtifactUrlInvalid):
            verify_artifact_url(SHA256, exp=extended, sig=sig, secret=SECRET, now=at(1))

    def test_another_secret_does_not_verify(self) -> None:
        exp, sig = self.mint()

        with pytest.raises(ArtifactUrlInvalid):
            verify_artifact_url(SHA256, exp=exp, sig=sig, secret="another-secret", now=at(1))

    def test_a_forged_far_future_link_reads_as_invalid_not_expired(self) -> None:
        """Property 3: the MAC is checked first, so the pair is not an oracle.

        If expiry were checked first, an attacker with a far-future `exp` would learn
        "signature bad" only for links that had not expired — a filter on the search
        space. Both answers are the same 403 at the endpoint; this pins the *ordering*
        that makes them the same answer.
        """
        with pytest.raises(ArtifactUrlInvalid):
            verify_artifact_url(
                SHA256, exp="99999999999", sig="not-a-signature", secret=SECRET, now=at(1)
            )

    @pytest.mark.parametrize("exp", [None, "", "later", "12.5", "-5", "1e9", "0x10"])
    def test_an_unparseable_expiry_is_invalid(self, exp: str | None) -> None:
        _, sig = self.mint()

        with pytest.raises(ArtifactUrlInvalid):
            verify_artifact_url(SHA256, exp=exp, sig=sig, secret=SECRET, now=at(1))

    @pytest.mark.parametrize("spelling", ["0{exp}", "+{exp}", " {exp}", "{exp} ", "1_000"])
    def test_an_expiry_has_exactly_one_spelling(self, spelling: str) -> None:
        """`int()` accepts leading zeros, a sign, whitespace and underscores. We do not.

        Each of these would parse to a number under a naive `int(exp)`; each would then
        be re-signed as the canonical integer and verify, giving one link two names —
        and, with a `+`, a shape that survives a careless URL rewrite.
        """
        exp, sig = self.mint()

        with pytest.raises(ArtifactUrlInvalid):
            verify_artifact_url(
                SHA256, exp=spelling.format(exp=exp), sig=sig, secret=SECRET, now=at(1)
            )

    def test_an_absurdly_long_expiry_is_refused_before_it_is_parsed(self) -> None:
        """No `int()` call on caller-controlled digits of unbounded length."""
        _, sig = self.mint()

        with pytest.raises(ArtifactUrlInvalid):
            verify_artifact_url(SHA256, exp="9" * 4000, sig=sig, secret=SECRET, now=at(1))

    @pytest.mark.parametrize("sig", [None, ""])
    def test_a_missing_signature_is_invalid(self, sig: str | None) -> None:
        exp, _ = self.mint()

        with pytest.raises(ArtifactUrlInvalid):
            verify_artifact_url(SHA256, exp=exp, sig=sig, secret=SECRET, now=at(1))

    def test_an_uppercased_digest_is_refused_not_repaired(self) -> None:
        exp, sig = self.mint()

        with pytest.raises(ArtifactUrlInvalid):
            verify_artifact_url(SHA256.upper(), exp=exp, sig=sig, secret=SECRET, now=at(1))

    def test_verification_without_a_secret_is_an_error_not_a_refusal(self) -> None:
        """An unconfigured server is a 503, not a 403: `ValueError`, not `ArtifactUrl*`."""
        exp, sig = self.mint()

        with pytest.raises(ValueError, match="secret is empty") as caught:
            verify_artifact_url(SHA256, exp=exp, sig=sig, secret="", now=at(1))
        assert not isinstance(caught.value, ArtifactUrlInvalid)


class TestHelpers:
    def test_is_valid_digest_matches_the_blob_key_rule(self) -> None:
        assert is_valid_digest(SHA256)
        assert not is_valid_digest(SHA256.upper())
        assert not is_valid_digest(f"sha256:{SHA256}")
        assert not is_valid_digest(f"{SHA256}\n")

    def test_artifact_path_is_store_agnostic(self) -> None:
        """The path is ours; the blob key is the store's. They share only the digest."""
        assert artifact_path(SHA256) == f"/v1/artifact/{SHA256}/bin"
