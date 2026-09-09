"""The HTTPS half of a simulated board: enroll, and (for `fleet`) issue tokens.

`urllib.request`, **never `httpx`** — inherited verbatim from `storage/__main__.py`:
httpx is a dev dependency and this module ships in the production image. The blocking
calls go through `asyncio.to_thread` so the MQTT side keeps its event loop.

`spec/device-protocol.md` → *Enrolment happens over HTTPS, not MQTT*, steps 2 → 4.
Two things this module is careful about, both because **enrollment tokens are
single-use**:

* Every status the device can actually receive gets its own message, because each one
  is a different thing to go and fix — and the 503 in particular has to say out loud
  that the enrollment *is* committed and the token *is* burned, or the operator reads
  `routers/enroll.py` to find out.
* Nothing here validates the identity. `DeviceIdentity.validate()` already ran, before
  the token was presented; re-checking after the burn would be theatre.

**No secret is ever returned in an error message or printed.** The enroll response's
`mqtt_password` goes straight into `state.Credential`; the admin password, the `ffa_`
session token and the `ffe_` plaintexts are handled but never rendered — only token
*ids*, which `routers/enroll.py` says are not secrets.
"""

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any

from fleetforge.simulator.errors import SimulatorError

# Generous: a "slow link" run deliberately adds seconds, and the API's argon2
# verification is ~40 ms under a capacity limiter.
DEFAULT_TIMEOUT_S = 30.0

# R0-be-1: the login cookie carries the SAME `ffa_` token a CLI sends as a bearer.
SESSION_COOKIE = "ff_session"

ENROLL_PATH = "/v1/enroll"
LOGIN_PATH = "/v1/auth/login"
ENROLLMENT_TOKENS_PATH = "/v1/enrollment-tokens"

# 429 is the only status worth one automatic retry: `routers/enroll.py` rate-limits
# on failures only, so a 429 means someone else is hammering the endpoint, not that
# this board is wrong.
RETRY_AFTER_CAP_S = 30.0


@dataclass(frozen=True, slots=True)
class HttpResult:
    """One HTTP response, reduced to what the caller branches on."""

    status: int
    body: bytes
    headers: list[tuple[str, str]]

    def json(self) -> dict[str, Any]:
        """Decode a JSON object body, or raise `SimulatorError` saying what came back."""
        try:
            data = json.loads(self.body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SimulatorError(
                f"the server answered {self.status} with a body that is not JSON "
                f"({type(exc).__name__}, {len(self.body)} bytes)"
            ) from exc
        if not isinstance(data, dict):
            raise SimulatorError(f"the server answered {self.status} with a non-object JSON body")
        return data

    def cookie(self, name: str) -> str | None:
        """The value of `name` from any `Set-Cookie` header, or `None`."""
        for key, value in self.headers:
            if key.lower() != "set-cookie":
                continue
            jar: SimpleCookie = SimpleCookie()
            jar.load(value)
            if name in jar:
                return jar[name].value
        return None


@dataclass(frozen=True, slots=True)
class EnrollResult:
    """`api/schemas.py::EnrollResponse`, the three fields the spec's step 4 promises."""

    device_id: str
    mqtt_username: str
    mqtt_password: str


def _request(
    url: str, payload: dict[str, Any] | None, headers: dict[str, str], timeout_s: float
) -> HttpResult:
    """POST (or GET, when `payload` is None) and return the response, 4xx/5xx included.

    A non-2xx status is data here, not an exception: the whole point of this module is
    that 401, 409, 422 and 503 mean four different things to the operator.
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(  # noqa: S310 - http(s) URL from --api-base
        url,
        data=data,
        headers={"content-type": "application/json", **headers},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310 - as above
            return HttpResult(
                status=int(response.status),
                body=bytes(response.read()),
                headers=list(response.getheaders()),
            )
    except urllib.error.HTTPError as exc:
        return HttpResult(
            status=int(exc.code), body=bytes(exc.read()), headers=list(exc.headers.items())
        )


async def _post(
    api_base: str,
    path: str,
    payload: dict[str, Any] | None,
    *,
    headers: dict[str, str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> HttpResult:
    """`_request` off the event loop."""
    url = f"{api_base.rstrip('/')}{path}"
    return await asyncio.to_thread(_request, url, payload, headers or {}, timeout_s)


def _detail(result: HttpResult) -> str:
    """The server's `detail`, or a short rendering of whatever it actually sent."""
    try:
        body = json.loads(result.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return result.body[:300].decode("utf-8", "replace")
    if isinstance(body, dict) and "detail" in body:
        return json.dumps(body["detail"])[:300]
    return json.dumps(body)[:300]


def _enroll_failure(result: HttpResult) -> SimulatorError:
    """Turn a refused enrollment into the sentence that says what to go and do.

    Each of these costs a real token or a real board, so none of them is "enrollment
    failed":

    * **401** the token is not one this server issued (or the prefix is wrong).
    * **409** it is used, revoked or expired. Nothing was burned by *this* call.
    * **422/400** the server refused the identity — a firmware typo, and again
      **no token was burned**: `routers/enroll.py` validates before it burns.
    * **503** the enrollment IS committed and the token IS burned; only the broker
      credential is missing, and the grace window is the sole way back.
    """
    detail = _detail(result)
    if result.status == 401:
        return SimulatorError(
            "enroll 401: the enrollment token was rejected (bad/unknown ffe_ token). "
            "Issue a fresh one with POST /v1/enrollment-tokens."
        )
    if result.status == 409:
        return SimulatorError(
            "enroll 409: this token is already used, revoked or expired — issue a new one. "
            "The one exception is the grace window: the SAME device_id re-presenting the "
            "SAME token within enroll_retry_window_s (600 s) is accepted, so if this board "
            "just lost its state file, re-run it under the same --name."
        )
    if result.status in (400, 422):
        return SimulatorError(
            f"enroll {result.status}: the server refused this identity: {detail}. "
            "No token was burned — validation runs before the burn."
        )
    if result.status == 503:
        return SimulatorError(
            "enroll 503: broker provisioning is unavailable. The enrollment IS committed "
            "and the token IS burned, so re-run within the 600 s grace window (same --name, "
            "same --token) once the broker is back; the device row is already there with "
            "broker_provisioned_at NULL."
        )
    return SimulatorError(f"enroll {result.status}: {detail}")


async def enroll(
    api_base: str,
    body: dict[str, Any],
    *,
    delay_s: float = 0.0,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> tuple[EnrollResult, int]:
    """`POST /v1/enroll` — token in, broker credential out. Returns the result and status.

    `delay_s` is the slow-link profile's first-contact penalty, applied before the
    request rather than inside it, so the transcript can show it.

    One automatic retry on 429, honouring `Retry-After` (capped): 25 boards behind one
    NAT rebooting together is a real scenario and the limiter counts only failures.
    """
    if delay_s:
        await asyncio.sleep(delay_s)

    try:
        result = await _post(api_base, ENROLL_PATH, body, timeout_s=timeout_s)
        if result.status == 429:
            await asyncio.sleep(_retry_after(result))
            result = await _post(api_base, ENROLL_PATH, body, timeout_s=timeout_s)
    except urllib.error.URLError as exc:
        raise SimulatorError(
            f"cannot reach {api_base}{ENROLL_PATH}: {exc.reason}. Is `just up` healthy, and is "
            "--api-base the origin nginx serves /v1 on?"
        ) from exc

    if result.status != 200:
        raise _enroll_failure(result)

    data = result.json()
    missing = [key for key in ("device_id", "mqtt_username", "mqtt_password") if key not in data]
    if missing:
        raise SimulatorError(f"the enroll response is missing {', '.join(missing)}")
    return (
        EnrollResult(
            device_id=str(data["device_id"]),
            mqtt_username=str(data["mqtt_username"]),
            mqtt_password=str(data["mqtt_password"]),
        ),
        result.status,
    )


def _retry_after(result: HttpResult) -> float:
    """`Retry-After` in seconds, capped, defaulting to 1 s."""
    for key, value in result.headers:
        if key.lower() == "retry-after":
            try:
                return min(float(value), RETRY_AFTER_CAP_S)
            except ValueError:
                return 1.0
    return 1.0


async def login(api_base: str, password: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> str:
    """`POST /v1/auth/login` → the raw `ffa_` token out of the `Set-Cookie`.

    The same credential the dashboard's cookie carries and the same one a CLI sends as
    a bearer — R0-be-1's "one credential, two transports". The response *body*
    deliberately does not contain it (browser XSS must not be able to read it), which
    is why this reads a header.
    """
    try:
        result = await _post(api_base, LOGIN_PATH, {"password": password}, timeout_s=timeout_s)
    except urllib.error.URLError as exc:
        raise SimulatorError(f"cannot reach {api_base}{LOGIN_PATH}: {exc.reason}") from exc

    if result.status != 200:
        raise SimulatorError(
            f"admin login failed ({result.status}). `fleet` needs the admin PASSWORD "
            "(not ADMIN_PASSWORD_HASH) — pass --admin-password or set $FF_ADMIN_PASSWORD."
        )
    token = result.cookie(SESSION_COOKIE)
    if not token:
        raise SimulatorError(f"login succeeded but no {SESSION_COOKIE} cookie came back")
    return token


async def issue_enrollment_token(
    api_base: str, admin_token: str, *, timeout_s: float = DEFAULT_TIMEOUT_S
) -> tuple[str, str]:
    """`POST /v1/enrollment-tokens` → `(id, plaintext)`.

    The **id** is safe to print (`routers/enroll.py` logs it, and the dashboard lists
    it); the plaintext is the credential and is never printed, never written anywhere
    but into the enroll body.
    """
    try:
        result = await _post(
            api_base,
            ENROLLMENT_TOKENS_PATH,
            {},
            headers={"authorization": f"Bearer {admin_token}"},
            timeout_s=timeout_s,
        )
    except urllib.error.URLError as exc:
        raise SimulatorError(
            f"cannot reach {api_base}{ENROLLMENT_TOKENS_PATH}: {exc.reason}"
        ) from exc

    if result.status not in (200, 201):
        raise SimulatorError(f"could not issue an enrollment token ({result.status})")
    data = result.json()
    if "token" not in data or "id" not in data:
        raise SimulatorError("the issuance response carried no token")
    return str(data["id"]), str(data["token"])
