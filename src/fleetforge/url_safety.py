"""Outbound URL validation to prevent server-side request forgery (`A-6`).

Project-level `ci_webhook_url` and `otel_endpoint` values are supplied over the API and
dispatched from inside the cluster network (`api` and `ingestor`). Every configured URL
must use `https://` and name a public, routable hostname or global IP address.
"""

from __future__ import annotations

import ipaddress
import urllib.parse

_FORBIDDEN_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google.internal",
        "instance-data",
    }
)
_FORBIDDEN_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".cluster.local",
    ".svc",
    ".home.arpa",
)
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")


def _check_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, field_name: str) -> None:
    mapped = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) else None
    target = mapped if mapped is not None else ip
    if (
        target.is_private
        or target.is_loopback
        or target.is_link_local
        or target.is_multicast
        or target.is_reserved
        or target.is_unspecified
        or (isinstance(target, ipaddress.IPv4Address) and target in _CGNAT_NET)
        or not target.is_global
    ):
        raise ValueError(
            f"{field_name} must not target a loopback, private, link-local, or reserved IP address"
        )


def validate_public_https_url(url: str, *, field_name: str = "url") -> str:
    """Validate that `url` is an `https://` URL targeting a public host or global IP."""
    trimmed = url.strip()
    if not trimmed:
        raise ValueError(f"{field_name} must not be empty")
    if any(ch <= " " or ch == "\x7f" for ch in trimmed):
        raise ValueError(f"{field_name} must not contain whitespace or control characters")

    parsed = urllib.parse.urlsplit(trimmed)
    if parsed.scheme.lower() != "https":
        raise ValueError(f"{field_name} must use https://")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field_name} must not embed credentials")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"{field_name} has an invalid port") from exc

    host = (parsed.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise ValueError(f"{field_name} must include a hostname")

    if host in _FORBIDDEN_HOSTNAMES or any(host.endswith(suf) for suf in _FORBIDDEN_SUFFIXES):
        raise ValueError(f"{field_name} must target a public host, not an internal or local name")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Reject dotless single-label service names (e.g. `https://postgres:5432`, `https://minio`)
        # and numeric-only octal/decimal IPv4 encodings (`https://2130706433`).
        if "." not in host:
            raise ValueError(f"{field_name} must be a fully-qualified public domain name") from None
        if all(part.isdigit() or part.lower().startswith("0x") for part in host.split(".")):
            raise ValueError(f"{field_name} must use canonical IPv4 or domain notation") from None
    else:
        _check_ip(ip, field_name)

    return trimmed
