"""Standalone offline unit tests for SSRF URL safety, HTTP link checks, and Git ref guards (`T-18`, `T-19`)."""

from __future__ import annotations

import pytest

from fleetforge.url_safety import (
    sanitize_git_repo_url,
    validate_git_ref,
    validate_http_link_url,
    validate_public_https_url,
)


def test_public_https_only() -> None:
    """T-18 (`M5b`) & T-19: `validate_public_https_url` accepts public HTTPS hosts and blocks
    non-HTTPS, internal suffixes, loopback/private/CGNAT/link-local/hex/decimal/octal IPs."""
    for good in (
        "https://github.com/example/fleet-fw.git",
        "https://hooks.example.org:8443/v1/deploy",
        "https://93.184.216.34/otel/v1/logs",
        "https://[2606:2800:220:1:248:1893:25c8:1946]/hook",
    ):
        assert validate_public_https_url(good) == good

    rejected = [
        "",
        "   ",
        "http://github.com/example/fw",
        "ftp://github.com/example/fw",
        "javascript:alert(1)",
        "https://user:pass@github.com/example/fw",
        "https://github.com/example/fw\nnewline",
        "https://github.com/example/fw with spaces",
        "https://github.com:99999/example/fw",
        "https:///missing-host",
        "https://localhost/hook",
        "https://sub.localhost/hook",
        "https://printer.local/hook",
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://postgres.default.svc.cluster.local:5432/",
        "https://minio.svc/",
        "https://router.home.arpa/",
        "https://postgres:5432/",
        "https://127.0.0.1/hook",
        "https://10.0.0.1/hook",
        "https://172.16.0.5/hook",
        "https://192.168.1.1/hook",
        "https://169.254.169.254/latest/meta-data/",
        "https://100.64.0.1/hook",
        "https://0.0.0.0/hook",
        "https://[::1]/hook",
        "https://[::ffff:127.0.0.1]/hook",
        "https://[::ffff:10.0.0.1]/hook",
        "https://[fe80::1]/hook",
        "https://0x7f.0.0.1/hook",
        "https://0x7f000001/hook",
        "https://2130706433/hook",
        "https://0177.0.0.1/hook",
    ]
    for bad in rejected:
        with pytest.raises(ValueError):
            validate_public_https_url(bad)


def test_sanitize_git_repo_url_strips_query_and_fragment() -> None:
    cleaned = sanitize_git_repo_url("https://github.com/example/fw.git?token=secret#frag")
    assert cleaned == "https://github.com/example/fw.git"
    with pytest.raises(ValueError):
        sanitize_git_repo_url("http://github.com/example/fw.git")


def test_validate_git_ref_rejects_option_injection_and_bad_syntax() -> None:
    for good in ("main", "release/v1.2.3", "refs/tags/v2.0.0", "a" * 40):
        assert validate_git_ref(good) == good

    for bad in (
        "",
        "a" * 129,
        "-u",
        "--upload-pack=sh",
        "main..HEAD",
        "main@{0}",
        "a//b",
        "/leading",
        "trailing/",
        "trailing.",
        "branch.lock",
        "bad ref",
        "bad\nref",
        "bad~1",
        "bad^2",
        "bad:ref",
        "bad?ref",
        "bad*ref",
        "bad[0]",
        "bad\\ref",
    ):
        with pytest.raises(ValueError):
            validate_git_ref(bad)


def test_validate_http_link_url_blocks_non_http_schemes() -> None:
    assert (
        validate_http_link_url("https://ci.example.com/run/42") == "https://ci.example.com/run/42"
    )
    assert validate_http_link_url("http://ci.local/run/42") == "http://ci.local/run/42"
    for bad in (
        "",
        "javascript:alert(1)",
        "JAVASCRIPT:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
        "https:///no-host",
        "https://ci.example.com/run 1",
    ):
        with pytest.raises(ValueError):
            validate_http_link_url(bad)
