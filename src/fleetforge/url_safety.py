"""Outbound URL validation to prevent server-side request forgery (`A-6`).

Project-level `ci_webhook_url` and `otel_endpoint` values are supplied over the API and
dispatched from inside the cluster network (`api` and `ingestor`). Every configured URL
must use `https://` and name a public, routable hostname or global IP address.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import urllib.parse
from typing import Any

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


_FORBIDDEN_GIT_REF_CHARS = frozenset("~^:?*[\\'\"`")


def validate_git_ref(git_ref: str) -> str:
    """Validate a Git branch, tag, or commit SHA against option injection and bad ref syntax."""
    if not git_ref:
        raise ValueError("git_ref must not be empty")
    if len(git_ref) > 128:
        raise ValueError("git_ref must be at most 128 characters")
    if any(ch.isspace() or ord(ch) < 0x20 or ch == "\x7f" for ch in git_ref):
        raise ValueError("git_ref must not contain whitespace or control characters")
    if git_ref.startswith("-"):
        raise ValueError("git_ref must not start with '-'")
    if ".." in git_ref or "@{" in git_ref or "//" in git_ref:
        raise ValueError("git_ref contains a forbidden sequence")
    if git_ref.startswith("/") or git_ref.endswith("/") or git_ref.endswith("."):
        raise ValueError("git_ref must not start or end with '/' or end with '.'")
    if git_ref.endswith(".lock") or any(ch in _FORBIDDEN_GIT_REF_CHARS for ch in git_ref):
        raise ValueError("git_ref contains forbidden Git reference characters")
    return git_ref


def sanitize_git_repo_url(url: str, *, field_name: str = "repo_url") -> str:
    """Validate a public HTTPS Git URL and strip any query string or fragment."""
    validated = validate_public_https_url(url, field_name=field_name)
    parsed = urllib.parse.urlsplit(validated)
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path, "", ""))


async def resolve_public_host_ips(host: str, port: int, *, field_name: str = "url") -> list[str]:
    """Resolve `host` asynchronously and verify every returned IP is globally routable."""
    normalized = host.strip().rstrip(".").lower()
    try:
        ip = ipaddress.ip_address(normalized)
    except ValueError:
        pass
    else:
        _check_ip(ip, field_name)
        return [str(ip)]

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(normalized, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"{field_name} host {normalized!r} could not be resolved") from exc

    if not infos:
        raise ValueError(f"{field_name} host {normalized!r} resolved to no addresses")

    validated_ips: list[str] = []
    for info in infos:
        addr = info[4][0]
        if not isinstance(addr, str):
            raise ValueError(f"{field_name} resolved to an unsupported address")
        ip_obj = ipaddress.ip_address(addr)
        _check_ip(ip_obj, field_name)
        ip_str = str(ip_obj)
        if ip_str not in validated_ips:
            validated_ips.append(ip_str)
    return validated_ips


async def resolve_and_validate_public_https_url(url: str, *, field_name: str = "url") -> str:
    """Validate `url` lexically and verify every DNS-resolved IP address is globally routable."""
    validated = validate_public_https_url(url, field_name=field_name)
    parsed = urllib.parse.urlsplit(validated)
    host = (parsed.hostname or "").strip().rstrip(".").lower()
    port = parsed.port or 443
    await resolve_public_host_ips(host, port, field_name=field_name)
    return validated


def public_https_transport(*, limits: Any = None, field_name: str = "url") -> Any:
    """Return an `httpx.AsyncHTTPTransport` pinning TCP connections to DNS-validated public IPs."""
    import httpcore
    import httpx

    class _PublicOnlyNetworkBackend(httpcore.AnyIOBackend):
        async def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,
            local_address: str | None = None,
            socket_options: Any = None,
        ) -> httpcore.AsyncNetworkStream:
            try:
                ips = await resolve_public_host_ips(host, port, field_name=field_name)
            except ValueError as exc:
                raise httpcore.ConnectError(str(exc)) from exc
            return await super().connect_tcp(
                ips[0],
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )

    transport = (
        httpx.AsyncHTTPTransport(limits=limits)
        if limits is not None
        else httpx.AsyncHTTPTransport()
    )
    transport._pool._network_backend = _PublicOnlyNetworkBackend()  # noqa: SLF001
    return transport
