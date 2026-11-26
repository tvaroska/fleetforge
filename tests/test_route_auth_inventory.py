"""Route auth-inventory guard, project-pinned token matrix, and review-hardening invariants (`T-15`, `A-10`, `A-12`, `A-13`)."""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from fleetforge.api.deps import (
    get_command_publisher,
    get_object_store,
    get_sessionmaker,
    get_settings,
    require_admin,
    require_unpinned_admin,
)
from fleetforge.api.main import create_app
from fleetforge.auth.tokens import ADMIN_TOKEN_PREFIX, ENROLLMENT_TOKEN_PREFIX, issue_token
from fleetforge.db.models import (
    AdminToken,
    DeployEvent,
    Device,
    EnrollmentToken,
    Project,
    ProjectMember,
    ProjectRole,
    User,
)
from fleetforge.secrets_crypto import SecretKeyNotConfiguredError, encrypt_secret
from tests.conftest import (
    FakeCommandPublisher,
    MemoryObjectStore,
    client_for,
    settings_for_tests,
)

# Explicit public endpoints (each has its own dedicated security control: signature, token-in-body,
# login rate limit, or read-only health probe). Every other route MUST depend on `require_admin`.
PUBLIC_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/v1/healthz"),
        ("GET", "/v1/readyz"),
        ("POST", "/v1/enroll"),
        ("POST", "/v1/progress"),
        ("GET", "/v1/artifact/{sha256}/bin"),
        ("POST", "/v1/auth/login"),
        ("POST", "/v1/auth/firebase"),
        ("POST", "/v1/auth/reset-password"),
    }
)

# Device-authenticated routes using per-device Basic auth (`device_id:mqtt_password`).
DEVICE_BASIC_AUTH_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/v1/devices/{device_id}/secrets"),
        ("GET", "/v1/devices/{device_id}/relay-sync"),
    }
)

_FASTAPI_BUILTINS: frozenset[str] = frozenset(
    {"/v1/openapi.json", "/v1/docs", "/v1/docs/oauth2-redirect", "/v1/redoc"}
)


def _iter_dependant_calls(dep: Dependant) -> Iterable[object]:
    if dep.call is not None:
        yield dep.call
    for child in dep.dependencies:
        yield from _iter_dependant_calls(child)


def find_unprotected_routes(app: FastAPI) -> list[tuple[str, str]]:
    """Return `(method, path)` pairs lacking admin auth outside the explicit public allowlists."""
    offenders: list[tuple[str, str]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path in _FASTAPI_BUILTINS:
            continue
        calls = set(_iter_dependant_calls(route.dependant))
        has_admin = require_admin in calls or require_unpinned_admin in calls
        for method in sorted(route.methods or ()):
            if method in {"HEAD", "OPTIONS"}:
                continue
            key = (method, route.path)
            if has_admin or key in PUBLIC_ALLOWLIST or key in DEVICE_BASIC_AUTH_ALLOWLIST:
                continue
            offenders.append(key)
    return offenders


@pytest.mark.smoke
def test_every_route_outside_public_allowlist_requires_admin_auth() -> None:
    """T-15: no FastAPI route can silently ship without `require_admin` or an explicit allowlist."""
    app = create_app()
    offenders = find_unprotected_routes(app)
    assert offenders == [], f"unprotected API routes outside allowlist: {offenders}"


@pytest.mark.unit
def test_route_auth_inventory_positive_control() -> None:
    """T-15 non-vacuity: a synthetic unprotected route MUST be flagged."""
    app = FastAPI()

    @app.post("/v1/secret-backdoor")
    async def _backdoor() -> dict[str, bool]:
        return {"ok": True}

    assert find_unprotected_routes(app) == [("POST", "/v1/secret-backdoor")]


@pytest.mark.unit
def test_server_review_hardening_invariants() -> None:
    """A-10, A-12, A-14 static/unit invariants."""
    repo_root = Path(__file__).resolve().parents[1]
    src_root = repo_root / "src" / "fleetforge"

    # A-10: no global `_safe_settings` or hidden `get_settings()` in `secrets_crypto.py` / `otel.py`.
    for rel in ("secrets_crypto.py", "ingest/otel.py", "ingestor/otel.py"):
        text = (src_root / rel).read_text(encoding="utf-8")
        assert "_safe_settings" not in text, f"{rel} still references _safe_settings"
        assert "get_settings" not in text, f"{rel} still reads global get_settings()"
    with pytest.raises(SecretKeyNotConfiguredError):
        encrypt_secret("secret", None)

    # A-12: no `anyio.to_thread.run_sync` bypassing `_ARGON2_EXECUTOR` in auth/users, no deploys aliases.
    for rel in ("auth/tokens.py", "api/routers/auth.py", "api/routers/users.py"):
        text = (src_root / rel).read_text(encoding="utf-8")
        assert "anyio.to_thread" not in text, f"{rel} still bypasses _ARGON2_EXECUTOR"
    deploys_src = (src_root / "deploys.py").read_text(encoding="utf-8")
    assert "_sanitise_detail" not in deploys_src
    assert "_snapshot_detail" not in deploys_src

    # A-13: cohort cancel uses batched `publish_many`.
    projects_src = (src_root / "api/routers/projects.py").read_text(encoding="utf-8")
    assert "publisher.publish_many(" in projects_src

    # A-14: `proto=http` documented in markdown.
    assert "proto=http" in (repo_root / "design/production.md").read_text(encoding="utf-8")


@pytest.mark.unit
def test_ci_workflow_template_python_snippets_execute_cleanly() -> None:
    """D-16: execute the exact `python3 -c` query/JSON snippets embedded in `fleetforge.yml`."""
    import json
    import re
    import subprocess
    import sys
    from urllib.parse import parse_qs

    wf_path = (
        Path(__file__).resolve().parents[1]
        / "templates"
        / "esp-idf"
        / ".github"
        / "workflows"
        / "fleetforge.yml"
    )
    wf_text = wf_path.read_text(encoding="utf-8")
    # Never interpolate untrusted `${{ github.event.inputs.* }}` directly inside `run:` shell code.
    run_section = wf_text.split("run: |", maxsplit=2)[-1]
    assert "${{ github.event.inputs." not in run_section

    rendered = (
        wf_text.replace("{{PROJECT_SLUG}}", "demo-proj")
        .replace("{{TARGET}}", "esp32s3")
        .replace("{{APP_NAME}}", "demo")
    )
    snippets = re.findall(r"python3 -c '([^']+)'", rendered)
    assert len(snippets) == 2, f"expected 2 python3 -c snippets, got {len(snippets)}"

    env = {
        "VERSION": '1.0.0-abc"def',
        "BRANCH": "feat/a&b+c",
        "TAG": "",
        "BUILD_URL": "https://github.com/org/repo/actions/runs/42",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "org/repo",
        "GITHUB_SHA": "0123456789abcdef",
        "DEPLOY_TARGET": "dev",
    }
    q_proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", snippets[0]], env=env, capture_output=True, text=True, check=False
    )
    assert q_proc.returncode == 0, q_proc.stderr
    parsed = parse_qs(q_proc.stdout.strip(), keep_blank_values=True)
    assert parsed["branch"] == ["feat/a&b+c"]
    assert parsed["repo_url"] == ["https://github.com/org/repo"]
    assert parsed["version"] == ['1.0.0-abc"def']

    j_proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", snippets[1]], env=env, capture_output=True, text=True, check=False
    )
    assert j_proc.returncode == 0, j_proc.stderr
    body = json.loads(j_proc.stdout)
    assert body == {"version": '1.0.0-abc"def', "target_filter": "dev", "apply": "auto"}


@pytest.mark.integration
async def test_project_pinned_token_cannot_mutate_other_project_routes(
    engine: AsyncEngine,
    clean_tables: None,
) -> None:
    """T-15: a project-pinned token for `alpha` cannot mutate `beta` across any mutating route."""
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    now = dt.datetime.now(dt.UTC)
    issued = issue_token(ADMIN_TOKEN_PREFIX)
    beta_dev_id = "b4cf12b3de99"

    async with sessionmaker() as session:
        alpha = Project(slug="alpha", name="Alpha")
        beta = Project(slug="beta", name="Beta")
        session.add_all([alpha, beta])
        await session.flush()
        session.add(
            AdminToken(
                id=issued.token_id,
                name="alpha-pinned",
                secret_hash=issued.secret_hash,
                subject="ci-alpha",
                scopes=["admin", "read", "write:artifacts", "deploy", "enroll"],
                project_id=alpha.id,
                expires_at=now + dt.timedelta(hours=1),
            )
        )
        session.add(
            Device(
                device_id=beta_dev_id,
                project_id=beta.id,
                platform_type="esp32s3",
                fw_version="1.0.0",
                link_type="wifi",
                power_class="always_on",
                partition_layout="ab-4m-v1",
                ota_slot_size=1966080,
                enrolled_at=now,
                mqtt_password_hash=hashlib.sha256(b"pw").hexdigest(),
            )
        )
        await session.commit()

    app = create_app()
    settings = settings_for_tests(
        secrets_encryption_key="test-secrets-key-0123456789abcdef",
    )
    app.dependency_overrides[get_sessionmaker] = lambda: sessionmaker
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_object_store] = lambda: MemoryObjectStore()
    app.dependency_overrides[get_command_publisher] = lambda: FakeCommandPublisher()
    headers = {"Authorization": f"Bearer {issued.token}"}

    mutating_calls: list[tuple[str, str, dict[str, Any]]] = [
        ("PATCH", "/v1/projects/beta", {"json": {"name": "Hijacked"}}),
        ("POST", "/v1/projects/beta/deploy", {"json": {"version": "1.0.0"}}),
        ("POST", "/v1/projects/beta/deploy/cancel", {"json": {}}),
        ("PUT", "/v1/projects/beta/secrets/api_key", {"json": {"value": "evil"}}),
        (
            "POST",
            "/v1/projects/beta/builds",
            {
                "json": {
                    "repo_url": "https://github.com/tvaroska/eink-frame",
                    "target": "esp32s3",
                    "git_ref": "main",
                    "version": "1.0.0",
                }
            },
        ),
        ("POST", "/v1/enrollment-tokens?project=beta", {"json": {}}),
        (
            "POST",
            "/v1/artifact?project=beta&target=esp32s3&version=9.9.9&partition_layout=ab-4m-v1",
            {
                "content": b"\xe9\x00" + b"x" * 64,
                "headers": {"content-type": "application/octet-stream"},
            },
        ),
        ("PATCH", f"/v1/devices/{beta_dev_id}", {"json": {"name": "hijacked"}}),
        ("POST", f"/v1/devices/{beta_dev_id}/decommission", {}),
        ("DELETE", f"/v1/devices/{beta_dev_id}", {}),
        ("POST", f"/v1/devices/{beta_dev_id}/reboot", {}),
        ("POST", "/v1/groups?project=beta", {"json": {"name": "rogue"}}),
    ]

    async with client_for(app) as client:
        for method, path, kwargs in mutating_calls:
            req_headers = {**headers, **kwargs.pop("headers", {})}
            resp = await client.request(method, path, headers=req_headers, **kwargs)
            assert resp.status_code in (403, 404), (
                f"{method} {path} unexpectedly returned {resp.status_code}: {resp.text}"
            )


@pytest.mark.integration
async def test_patch_device_project_transfer_hides_inaccessible_project_as_404(
    engine: AsyncEngine,
    clean_tables: None,
) -> None:
    """A-13: project transfer never leaks existence of inaccessible destination projects."""
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    now = dt.datetime.now(dt.UTC)
    owner_issued = issue_token(ADMIN_TOKEN_PREFIX)
    ops_issued = issue_token(ADMIN_TOKEN_PREFIX)
    dev_id = "a4cf12b3de88"

    async with sessionmaker() as session:
        alpha = Project(slug="alpha", name="Alpha")
        beta = Project(slug="beta", name="Beta")
        owner = User(username="owner-alpha", password_hash="unused", is_superadmin=False)
        ops = User(username="ops-alpha", password_hash="unused", is_superadmin=False)
        session.add_all([alpha, beta, owner, ops])
        await session.flush()
        session.add_all(
            [
                ProjectMember(project_id=alpha.id, user_id=owner.id, role=ProjectRole.OWNER),
                ProjectMember(project_id=alpha.id, user_id=ops.id, role=ProjectRole.OPERATOR),
                AdminToken(
                    id=owner_issued.token_id,
                    name="user-owner-alpha",
                    secret_hash=owner_issued.secret_hash,
                    subject="owner-alpha",
                    user_id=owner.id,
                    scopes=["admin", "read", "deploy"],
                    expires_at=now + dt.timedelta(hours=1),
                ),
                AdminToken(
                    id=ops_issued.token_id,
                    name="user-ops-alpha",
                    secret_hash=ops_issued.secret_hash,
                    subject="ops-alpha",
                    user_id=ops.id,
                    scopes=["admin", "read", "deploy"],
                    expires_at=now + dt.timedelta(hours=1),
                ),
                Device(
                    device_id=dev_id,
                    project_id=alpha.id,
                    platform_type="esp32s3",
                    fw_version="1.0.0",
                    link_type="wifi",
                    power_class="always_on",
                    partition_layout="ab-4m-v1",
                    ota_slot_size=1966080,
                    enrolled_at=now,
                    mqtt_password_hash=hashlib.sha256(b"pw").hexdigest(),
                ),
            ]
        )
        await session.commit()

    app = create_app()
    app.dependency_overrides[get_sessionmaker] = lambda: sessionmaker
    app.dependency_overrides[get_settings] = lambda: settings_for_tests()
    app.dependency_overrides[get_command_publisher] = lambda: FakeCommandPublisher()
    owner_headers = {"Authorization": f"Bearer {owner_issued.token}"}
    ops_headers = {"Authorization": f"Bearer {ops_issued.token}"}

    async with client_for(app) as client:
        # Owner of alpha cannot distinguish existing inaccessible `beta` from nonexistent slug.
        r_hidden = await client.patch(
            f"/v1/devices/{dev_id}", headers=owner_headers, json={"project": "beta"}
        )
        r_missing = await client.patch(
            f"/v1/devices/{dev_id}", headers=owner_headers, json={"project": "no-such-proj"}
        )
        assert r_hidden.status_code == 404, r_hidden.text
        assert r_missing.status_code == 404, r_missing.text
        assert "no such project" in r_hidden.json()["detail"]

        # Operator of alpha gets 403 for both existing and nonexistent destination slugs.
        r_ops_hidden = await client.patch(
            f"/v1/devices/{dev_id}", headers=ops_headers, json={"project": "beta"}
        )
        r_ops_missing = await client.patch(
            f"/v1/devices/{dev_id}", headers=ops_headers, json={"project": "no-such-proj"}
        )
        assert r_ops_hidden.status_code == 403, r_ops_hidden.text
        assert r_ops_missing.status_code == 403, r_ops_missing.text


@pytest.mark.integration
async def test_cohort_cancel_batches_via_publish_many(
    engine: AsyncEngine,
    clean_tables: None,
) -> None:
    """A-13: project cohort cancel publishes via one `publish_many` batch, not per-device `publish`."""
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    now = dt.datetime.now(dt.UTC)
    issued = issue_token(ADMIN_TOKEN_PREFIX)

    class _CountingPublisher(FakeCommandPublisher):
        def __init__(self) -> None:
            super().__init__()
            self.publish_many_calls = 0

        async def publish_many(self, items: Sequence[tuple[str, dict[str, Any]]]) -> None:
            self.publish_many_calls += 1
            await super().publish_many(items)

    publisher = _CountingPublisher()
    async with sessionmaker() as session:
        alpha = Project(slug="alpha", name="Alpha")
        session.add(alpha)
        await session.flush()
        session.add(
            AdminToken(
                id=issued.token_id,
                name="alpha-ops",
                secret_hash=issued.secret_hash,
                subject="ops",
                scopes=["admin", "read", "deploy"],
                project_id=alpha.id,
                expires_at=now + dt.timedelta(hours=1),
            )
        )
        for idx, dev_id in enumerate(("a4cf12b3de01", "a4cf12b3de02")):
            session.add(
                Device(
                    device_id=dev_id,
                    project_id=alpha.id,
                    platform_type="esp32s3",
                    fw_version="1.0.0",
                    link_type="wifi",
                    power_class="always_on",
                    partition_layout="ab-4m-v1",
                    ota_slot_size=1966080,
                    enrolled_at=now,
                    last_seen=now,
                    capabilities=["ota", "cancel"],
                    mqtt_password_hash=hashlib.sha256(b"pw").hexdigest(),
                )
            )
            await session.flush()
            session.add(
                DeployEvent(
                    device_id=dev_id,
                    cmd_id=f"cmd-batch-{idx}",
                    artifact_version="2.0.0",
                    state="requested",
                    is_terminal=False,
                    at=now - dt.timedelta(seconds=2),
                    detail={"apply": "on_command", "sha256": "a" * 64},
                )
            )
            session.add(
                DeployEvent(
                    device_id=dev_id,
                    cmd_id=f"cmd-batch-{idx}",
                    artifact_version="2.0.0",
                    state="staged",
                    is_terminal=False,
                    at=now,
                )
            )
        await session.commit()

    app = create_app()
    app.dependency_overrides[get_sessionmaker] = lambda: sessionmaker
    app.dependency_overrides[get_settings] = lambda: settings_for_tests()
    app.dependency_overrides[get_command_publisher] = lambda: publisher
    headers = {"Authorization": f"Bearer {issued.token}"}

    async with client_for(app) as client:
        resp = await client.post("/v1/projects/alpha/deploy/cancel", headers=headers, json={})
        assert resp.status_code == 202, resp.text
        data = resp.json()
        assert len(data["cancelled"]) == 2
        assert publisher.publish_many_calls == 1
        assert publisher._publish_calls == 0
        assert len(publisher.published) == 2


@pytest.mark.integration
async def test_enroll_non_parent_integrity_error_returns_409_not_unknown_parent(
    engine: AsyncEngine,
    clean_tables: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A-13: only `parent_device_id` FK violations return 400; other IntegrityErrors return 409."""
    from fleetforge.api.routers import enroll as enroll_router

    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    now = dt.datetime.now(dt.UTC)
    tok = issue_token(ENROLLMENT_TOKEN_PREFIX)

    async with sessionmaker() as session:
        alpha = Project(slug="alpha", name="Alpha")
        session.add(alpha)
        await session.flush()
        session.add(
            EnrollmentToken(
                id=tok.token_id,
                secret_hash=tok.secret_hash,
                project_id=alpha.id,
                expires_at=now + dt.timedelta(hours=1),
            )
        )
        await session.commit()

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise IntegrityError(
            "INSERT INTO devices ...",
            {},
            Exception('duplicate key value violates unique constraint "devices_pkey"'),
        )

    monkeypatch.setattr(enroll_router, "enroll_device", _boom)

    app = create_app()
    app.dependency_overrides[get_sessionmaker] = lambda: sessionmaker
    app.dependency_overrides[get_settings] = lambda: settings_for_tests()

    async with client_for(app) as client:
        resp = await client.post(
            "/v1/enroll",
            json={
                "token": tok.token,
                "device_id": "a4cf12b3de77",
                "platform_type": "esp32s3",
                "fw_version": "1.0.0",
                "link_type": "wifi",
                "power_class": "always_on",
                "partition_layout": "ab-4m-v1",
                "ota_slot_size": 1966080,
            },
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == "device enrollment conflict"
