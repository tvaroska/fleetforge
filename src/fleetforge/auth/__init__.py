"""Credential primitives, transport-agnostic; the HTTP wiring lives in `api/`.

Nothing in this package imports FastAPI, Starlette or the ORM. It is the layer the
API's `require_admin` dependency (`fleetforge.api.deps`) and R0-be-2's enrollment
tokens both build on, so it stays free of request/response types on purpose.
"""
