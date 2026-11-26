"""Compatibility re-exports of `fleetforge.ingest.otel` (`A-29`)."""

from fleetforge.ingest.otel import *  # noqa: F403
from fleetforge.ingest.otel import (
    _enqueue_batch as _enqueue_batch,
)
from fleetforge.ingest.otel import (
    _project_otel_cache as _project_otel_cache,
)
from fleetforge.ingest.otel import (
    _resolve_endpoint as _resolve_endpoint,
)
from fleetforge.ingest.otel import (
    _resolve_project_otel as _resolve_project_otel,
)
from fleetforge.ingest.otel import (
    _resource_attributes as _resource_attributes,
)
from fleetforge.ingest.otel import (
    _safe_settings as _safe_settings,
)
