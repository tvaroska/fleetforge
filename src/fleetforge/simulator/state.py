"""The NVS analogue: where a simulated board keeps its broker credential. **CRITICAL.**

`POST /v1/enroll` returns `mqtt_password` exactly once. It is not in Postgres, not in
a log line, and not in the dynsec store (which keeps a hash) — `api/schemas.py`
::`EnrollResponse` says so. A real board writes it to NVS; a simulated one writes it
here, because **enrollment tokens are single-use** and a board that forgets its
password needs a whole new token (and, in the field, a re-flash).

Four rules, each of which is the reason this is a separate module with its own tests:

1. **Files are created 0600 through `os.open`, never `Path.write_text`**, whose 0644
   default would leave a live fleet credential world-readable on a shared box. The
   directory is 0700 for the same reason, and the mode is re-applied on every write
   so a pre-existing 0644 file is repaired rather than inherited (`O_CREAT`'s mode
   argument only applies when the file is created).
2. **`.sim/` is gitignored**, added in the same commit as this module. A committed
   `.sim/` is a committed broker credential.
3. **State present means no enrollment happens at all.** Not "re-enroll if it looks
   stale" — a silent re-enroll burns a single-use token, which is the one resource
   this simulator must never waste. `__main__.py` is what enforces it; `load()` just
   answers honestly.
4. **A file that does not parse, or whose `device_id` disagrees with its filename, is
   a loud `SimulatorError`.** Falling back to a fresh enrollment there is rule 3
   violated by accident.

Nothing here logs, prints or returns the password in a message. `describe()` exists
so the transcript can say where the credential is without saying what it is.
"""

import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

from fleetforge.simulator.errors import SimulatorError

# 0600 / 0700. Spelled as constants because the whole point of this module is that
# these two numbers are never accidentally something else.
FILE_MODE = 0o600
DIR_MODE = 0o700

STATE_SUFFIX = ".json"


@dataclass(frozen=True, slots=True)
class Credential:
    """Everything a simulated board needs to reconnect without enrolling again.

    `api_base` rides along so a state file is self-describing: a `.sim/` copied
    between a host run and a `docker compose exec` run points at a different origin,
    and silently reusing the wrong one is a confusing failure rather than a loud one.
    """

    device_id: str
    mqtt_username: str
    mqtt_password: str
    api_base: str
    enrolled_at: str


def state_path(state_dir: Path, device_id: str) -> Path:
    """Where `device_id`'s credential lives. The filename IS the device id."""
    return state_dir / f"{device_id}{STATE_SUFFIX}"


def describe(path: Path) -> str:
    """`<path> (0600)` — the transcript line. Never the password."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:  # pragma: no cover - the file was just written
        return str(path)
    return f"{path} ({mode:04o})"


def save(state_dir: Path, credential: Credential) -> Path:
    """Persist `credential` at 0600 and return the path it was written to.

    Called **immediately after the enroll response is read and before connecting**,
    which is the real agent's order and the reason `config.enroll_retry_window_s`
    exists at all ("the device writes NVS only after it reads the response body").
    """
    state_dir.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    path = state_path(state_dir, credential.device_id)
    body = json.dumps(asdict(credential), indent=2, sort_keys=True) + "\n"
    # O_CREAT's mode applies only on creation, so chmod afterwards as well: an
    # existing 0644 file from a bad edit must not silently keep its mode.
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, FILE_MODE)
    try:
        os.write(fd, body.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, FILE_MODE)
    return path


def load(state_dir: Path, device_id: str) -> Credential | None:
    """Read `device_id`'s credential, `None` if this board has never enrolled.

    Raises `SimulatorError` for a file that exists but is not usable. That is
    deliberately not the same answer as `None`: `None` means "enroll", and enrolling
    burns a token.
    """
    path = state_path(state_dir, device_id)
    if not path.exists():
        return None

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SimulatorError(f"cannot read the state file {path}: {exc}") from exc

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SimulatorError(
            f"the state file {path} is not valid JSON ({type(exc).__name__}). Refusing to "
            "re-enroll over it: that would burn a single-use token. Delete it deliberately "
            "(--forget) if the credential is really gone."
        ) from exc

    if not isinstance(data, dict):
        raise SimulatorError(f"the state file {path} is not a JSON object")

    missing = [field for field in Credential.__slots__ if field not in data]
    if missing:
        raise SimulatorError(f"the state file {path} is missing {', '.join(sorted(missing))}")

    stored_id = data["device_id"]
    if stored_id != device_id:
        raise SimulatorError(
            f"the state file {path} holds a credential for {stored_id!r}, not {device_id!r} — "
            "the filename and the credential disagree, so one of them is a copy-paste error"
        )

    return Credential(**{field: data[field] for field in Credential.__slots__})


def forget(state_dir: Path, device_id: str) -> bool:
    """Delete `device_id`'s credential; `True` if there was one.

    The *next* run then enrolls again, which — within `enroll_retry_window_s` of the
    original burn, with the same token and the same device id — is how the grace
    window is demonstrated.
    """
    path = state_path(state_dir, device_id)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise SimulatorError(f"cannot remove the state file {path}: {exc}") from exc
    return True
