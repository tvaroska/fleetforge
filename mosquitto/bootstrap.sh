#!/bin/sh
# Bootstrap the Mosquitto dynamic-security store. Idempotent; runs before the
# broker starts (docker-compose.yml: mosquitto depends_on mosquitto-init).
#
# Why a throwaway broker: `mosquitto_ctrl dynsec init` is the only subcommand
# that works on a file; every other command needs a live broker to talk to.
set -eu

CONFIG=/mosquitto/data/dynamic-security.json
PORT=1884
: "${MOSQUITTO_ADMIN_USERNAME:?}" ; : "${MOSQUITTO_ADMIN_PASSWORD:?}"
: "${MOSQUITTO_INGESTOR_USERNAME:?}" ; : "${MOSQUITTO_INGESTOR_PASSWORD:?}"

if [ ! -f "$CONFIG" ]; then
  echo "bootstrap: creating $CONFIG"
  mosquitto_ctrl dynsec init "$CONFIG" "$MOSQUITTO_ADMIN_USERNAME" "$MOSQUITTO_ADMIN_PASSWORD"
fi
# The plugin rewrites this file on every enrolment. Root-owned = every device
# credential is lost on the next restart, and nothing fails loudly.
chown mosquitto:mosquitto "$CONFIG"
chmod 0600 "$CONFIG"

cat > /tmp/bootstrap.conf <<EOF
listener $PORT 127.0.0.1
allow_anonymous false
plugin /usr/lib/mosquitto_dynamic_security.so
plugin_opt_config_file $CONFIG
EOF
mosquitto -c /tmp/bootstrap.conf &
trap 'kill %1 2>/dev/null || true' EXIT

i=0
until mosquitto_sub -h 127.0.0.1 -p $PORT -u "$MOSQUITTO_ADMIN_USERNAME" \
        -P "$MOSQUITTO_ADMIN_PASSWORD" -t '$SYS/broker/uptime' -C 1 -W 2 >/dev/null 2>&1; do
  i=$((i+1))
  [ "$i" -gt 10 ] && {
    echo "bootstrap: cannot authenticate as $MOSQUITTO_ADMIN_USERNAME." >&2
    echo "bootstrap: MQTT_DYNSEC_PASSWORD was probably changed after the store was created." >&2
    echo "bootstrap: rotate with 'mosquitto_ctrl … dynsec setClientPassword', or 'just nuke'." >&2
    exit 1
  }
  sleep 1
done

# "already exists" is the second-run path, not a failure.
ctrl() {
  mosquitto_ctrl -h 127.0.0.1 -p $PORT -u "$MOSQUITTO_ADMIN_USERNAME" \
    -P "$MOSQUITTO_ADMIN_PASSWORD" dynsec "$@" 2>&1 |
    grep -v -i 'encryption\|visible on the network' || true
}

# The device role MUST exist and MUST be named exactly broker.DEVICE_ROLE —
# createClient names it, so a mismatch answers 503 on every enrolment. It is
# EMPTY on purpose: the fleet ACL is mosquitto/acl (dynsec has no %u).
ctrl createRole device

# The ingestor is the sole MQTT subscriber. Read-only, up/ only, no $SYS.
ctrl createRole ingestor
ctrl addRoleACL ingestor subscribePattern     'ff/v1/d/+/up/#' allow
ctrl addRoleACL ingestor publishClientReceive 'ff/v1/d/+/up/#' allow
ctrl createClient    "$MOSQUITTO_INGESTOR_USERNAME" -p "$MOSQUITTO_INGESTOR_PASSWORD"
ctrl setClientPassword "$MOSQUITTO_INGESTOR_USERNAME" "$MOSQUITTO_INGESTOR_PASSWORD"
ctrl addClientRole   "$MOSQUITTO_INGESTOR_USERNAME" ingestor

# Deny by default. `dynsec init` leaves publishClientReceive=true.
ctrl setDefaultACLAccess publishClientSend    deny
ctrl setDefaultACLAccess publishClientReceive deny
ctrl setDefaultACLAccess subscribe            deny

echo "bootstrap: done"
