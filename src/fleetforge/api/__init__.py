"""The HTTP control plane — one of the package's two entrypoints.

`fleetforge.api.main:app` is what uvicorn serves. The sibling entrypoint is
`fleetforge.ingestor.main`; both ship in the same image (one image, two commands).
"""
