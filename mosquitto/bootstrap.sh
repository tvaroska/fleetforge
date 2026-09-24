#!/bin/sh
# Create the Mosquitto dynamic-security store, and do NOTHING else. Runs before the
# broker starts (docker-compose.yml: mosquitto depends_on mosquitto-init).
#
# THIS SCRIPT MUST NEVER CREATE A ROLE OR A CLIENT, and must never be given a
# credential other than the dynsec admin's. Roles and clients are configure.sh's
# job, applied to the RUNNING broker after it is healthy.
#
# Why (ops-log F-2026-09-23-002): the dynamic-security plugin reads this file exactly
# once, at broker startup, and is the sole authority on its contents thereafter —
# rewriting the file from its own memory on every change a device enrolment makes. So
# anything written here underneath a live broker is not merely inert, it is scheduled
# to be erased. Until 2026-09-23 this script stood up a throwaway broker on
# 127.0.0.1:1884 and applied the whole estate to *that*, which works on a stack whose
# broker is about to start for the first time (dev, after `just nuke`) and has never
# once reached prod, whose broker is `restart: always` and was never recreated. The
# `commander` client did not exist on prod's broker at all, and every deploy answered
# `Not authorized`.
#
# `mosquitto_ctrl dynsec init` is the only subcommand that works on a file, and the
# only thing that genuinely has to happen before startup: the plugin refuses to load
# when its config file is absent. That is the whole remit of this script.
set -eu

CONFIG=/mosquitto/data/dynamic-security.json
: "${MOSQUITTO_ADMIN_USERNAME:?}" ; : "${MOSQUITTO_ADMIN_PASSWORD:?}"

if [ ! -f "$CONFIG" ]; then
  echo "bootstrap: creating $CONFIG"
  mosquitto_ctrl dynsec init "$CONFIG" "$MOSQUITTO_ADMIN_USERNAME" "$MOSQUITTO_ADMIN_PASSWORD"
else
  echo "bootstrap: $CONFIG exists — leaving it alone, the broker owns it now"
fi

# The plugin rewrites this file on every enrolment. Root-owned = every device
# credential is lost on the next restart, and nothing fails loudly.
chown mosquitto:mosquitto "$CONFIG"
chmod 0600 "$CONFIG"

echo "bootstrap: done — roles and clients are configure.sh's job, once the broker is up"
