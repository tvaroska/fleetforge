"""The one exception family the simulator raises.

It lives in its own module rather than in `device.py` or `state.py` because all
three of those raise it and none of them may import the others: `state.py` is the
credential store, `device.py` is the protocol, `client.py` is the HTTP half, and a
cycle between them would be the first thing to break the import-purity tripwire in
`tests/test_simulator.py`.

`SimulatorError` is a `RuntimeError` for the same reason `BrokerProvisioningError`
is: `__main__.py` funnels a small, named set of exception families into one
`SIMULATOR FAILED: <Type>: <message>` line, and a bare `Exception` catch there would
swallow programming errors along with the expected ones.
"""


class SimulatorError(RuntimeError):
    """A simulated board cannot proceed, and the reason is worth printing."""
