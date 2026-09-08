"""The MQTT ingestor — the *sole* MQTT subscriber in the system.

Exactly one instance ever runs (`design/production.md` → *The single-subscriber
rule*). The API publishes commands but never subscribes.
"""
