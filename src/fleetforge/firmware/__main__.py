"""`python -m fleetforge.firmware` — publish, list and roll back agent bundles.

```
just agent-publish esp32                 # verify agent/dist/esp32, upload it, point the index
just agent-publish-all                   # every target in `agent_targets`
just agent-list                          # what is current, and what can be rolled back to
just agent-rollback esp32 <digest>       # make a previously published bundle current
```

Publishing is **not** deploying (S0-infra-6): nothing here restarts a container or builds
an image. The api notices within `AGENT_CATALOG_TTL_S`.

The credential comes from the environment, exactly as `python -m fleetforge.storage` takes
it — `.env` on this box points at the dev MinIO. **Publishing to prod's GCS from the dev
box needs `CLOUDSDK_CONFIG=/tmp/empty`**: the ADC *file* here is a user principal with no
`roles/iam.serviceAccountTokenCreator`, and emptying the config dir makes the metadata
server answer with the VM's attached identity, which is granted (DECISIONS.md
2026-09-15).

**Nothing here prints a credential.** The backend, bucket and prefix are printed by
`describe`; keys, secrets and key-file contents never are. Precedent for the
argparse/stdout-is-the-result shape: `storage/__main__.py`, `auth/__main__.py`.
"""

import argparse
import asyncio
import sys

from fleetforge.config import Settings
from fleetforge.firmware.bundledir import load_bundle_dir
from fleetforge.firmware.index import AgentIndex
from fleetforge.firmware.publish import publish_bundle, read_index, rollback
from fleetforge.storage.factory import create_object_store, select_backend
from fleetforge.storage.objectstore import ObjectStore


def _step(message: str) -> None:
    """One line per step, to stdout, so the whole run reads as a transcript."""
    print(message, flush=True)


def _store(settings: Settings) -> ObjectStore:
    store = create_object_store(settings)
    _step(f"backend  {getattr(store, 'describe', select_backend(settings))}")
    _step(f"index    {settings.agent_index_key}")
    return store


def _print_index(index: AgentIndex) -> None:
    """The rollback menu: what is current, and every digest it can be pointed back at."""
    if not index.bundles:
        _step("index    (empty — nothing published)")
        return
    _step(f"{'target':<10} {'layout':<12} {'version':<10} {'manifest':<64} published")
    for entry in index.bundles:
        _step(
            f"{entry.target:<10} {entry.partition_layout:<12} {entry.agent_version:<10} "
            f"{entry.manifest_sha256:<64} {entry.published_at}"
        )
        for item in entry.superseded:
            _step(
                f"{'':<10} {'  ↳ was':<12} {item.agent_version:<10} "
                f"{item.manifest_sha256:<64} {item.published_at}"
            )


async def _publish(args: argparse.Namespace, settings: Settings) -> int:
    store = _store(settings)
    index: AgentIndex | None = None
    for directory in args.bundle_dir:
        # Verified BEFORE a byte is uploaded: a corrupt bundle is refused with the target
        # named and nothing is written (`firmware/bundledir.py`).
        bundle = load_bundle_dir(directory)
        _step(f"verify   {bundle.target}/{bundle.partition_layout}@{bundle.agent_version} OK")
        result = await publish_bundle(store, bundle, index_key=settings.agent_index_key)
        for key in result.part_keys:
            _step(f"blob     {key}")
        _step(f"manifest {result.manifest_sha256}")
        index = result.index
    if index is not None:
        # Printed after the write so a lost read-modify-write update is visible here
        # rather than in the flasher (`firmware/index.py` → the RMW race).
        _print_index(index)
    return 0


async def _list(args: argparse.Namespace, settings: Settings) -> int:
    store = _store(settings)
    _print_index(await read_index(store, settings.agent_index_key))
    return 0


async def _rollback(args: argparse.Namespace, settings: Settings) -> int:
    store = _store(settings)
    index = await rollback(
        store,
        args.target,
        args.manifest,
        layout=args.layout,
        index_key=settings.agent_index_key,
    )
    _print_index(index)
    return 0


async def _run(args: argparse.Namespace) -> int:
    settings = Settings()  # type: ignore[call-arg]  # values come from the environment
    handlers = {"publish": _publish, "list": _list, "rollback": _rollback}
    return await handlers[args.command](args, settings)


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand. Returns a process exit code; `PUBLISH OK` means success."""
    parser = argparse.ArgumentParser(prog="python -m fleetforge.firmware")
    subparsers = parser.add_subparsers(dest="command", required=True)

    publish = subparsers.add_parser("publish", help="upload one or more bundle directories")
    publish.add_argument("bundle_dir", nargs="+", help="e.g. agent/dist/esp32")

    subparsers.add_parser("list", help="print the index: what is current and what was")

    roll = subparsers.add_parser("rollback", help="make a superseded bundle current again")
    roll.add_argument("target", help="e.g. esp32")
    roll.add_argument("--manifest", required=True, help="the manifest sha256 to restore")
    roll.add_argument("--layout", default=None, help="needed only when a target has two layouts")

    args = parser.parse_args(argv)

    try:
        code = asyncio.run(_run(args))
    except (ValueError, RuntimeError, OSError) as exc:
        # AgentBundleError and ObjectKeyError are ValueErrors; every ObjectStore*Error is
        # a RuntimeError. Printed with its type because "which failure was it" is the
        # whole question, and never with a traceback.
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"{args.command.upper()} OK")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
