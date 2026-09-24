#!/bin/sh
# Apply the estate's dynsec roles and clients to the RUNNING broker, over
# $CONTROL/dynamic-security/v1 — the same interface the API already uses for
# per-device enrolment (src/fleetforge/broker/dynsec.py). Idempotent: it is meant to
# be re-run on every `up` and every deploy, and converges memory and file each time.
#
# Runs AFTER the broker is healthy (docker-compose.yml: mosquitto-config). It is a
# separate container from mosquitto-init and must stay one — see bootstrap.sh and
# ops-log F-2026-09-23-002 for why writing the dynsec file underneath a live broker
# is invisible to it and then erased by it.
#
# It deliberately does NOT mount /mosquitto/data. This script has no business
# touching that file; if it can't be done over the control topic it doesn't belong
# here.
set -eu

HOST=${MOSQUITTO_HOST:-mosquitto}
PORT=${MOSQUITTO_PORT:-1883}
: "${MOSQUITTO_ADMIN_USERNAME:?}" ; : "${MOSQUITTO_ADMIN_PASSWORD:?}"
: "${MOSQUITTO_INGESTOR_USERNAME:?}" ; : "${MOSQUITTO_INGESTOR_PASSWORD:?}"
: "${MOSQUITTO_COMMANDER_USERNAME:?}" ; : "${MOSQUITTO_COMMANDER_PASSWORD:?}"

# compose's `service_healthy` should have covered this; the loop is for a hand-run
# `docker compose run --rm mosquitto-config` and for the good error message below.
i=0
until mosquitto_sub -h "$HOST" -p "$PORT" -u "$MOSQUITTO_ADMIN_USERNAME" \
        -P "$MOSQUITTO_ADMIN_PASSWORD" -t '$SYS/broker/uptime' -C 1 -W 2 >/dev/null 2>&1; do
  i=$((i+1))
  [ "$i" -gt 15 ] && {
    echo "configure: cannot authenticate as $MOSQUITTO_ADMIN_USERNAME on $HOST:$PORT." >&2
    echo "configure: MQTT_DYNSEC_PASSWORD was probably changed after the store was created." >&2
    echo "configure: rotate with 'mosquitto_ctrl … dynsec setClientPassword', or 'just nuke'." >&2
    exit 1
  }
  sleep 1
done
echo "configure: connected to $HOST:$PORT as $MOSQUITTO_ADMIN_USERNAME"

# One raw dynsec call, with the TLS nag and blank lines stripped. Sets `rc`.
# NOTE `mosquitto_ctrl` EXITS 0 ON A REFUSED COMMAND — verified against 2.0.22, where
# a duplicate `addClientRole` prints "Error: Internal error" and returns 0. So the
# exit status is checked but is never the thing being relied on; the output is.
mctrl() {
  set +e
  out=$(mosquitto_ctrl -h "$HOST" -p "$PORT" -u "$MOSQUITTO_ADMIN_USERNAME" \
          -P "$MOSQUITTO_ADMIN_PASSWORD" dynsec "$@" 2>&1)
  rc=$?
  set -e
  out=$(printf '%s\n' "$out" |
          grep -v -i 'encryption\|visible on the network' |
          grep -v '^[[:space:]]*$' || true)
}

# A command that must take. A successful dynsec command prints nothing at all, so
# anything left is the broker refusing, and "already exists" is the one tolerated
# refusal — every command here is meant to be re-applied on every deploy. EVERYTHING
# else is fatal. The old bootstrap ended this function with `|| true`, which is a
# large part of why a broker that had never been configured still logged a clean run.
ctrl() {
  mctrl "$@"
  case "$out" in
    *[Aa]lready\ exists*) return 0 ;;
  esac
  if [ "$rc" -ne 0 ] || [ -n "$out" ]; then
    [ -n "$out" ] && printf '%s\n' "$out" >&2
    echo "configure: FAILED (rc=$rc): dynsec $*" >&2
    exit 1
  fi
  echo "configure: dynsec $1 ${2:-}"
}

# `addClientRole` is the one command with no idempotent form: on a client that
# already holds the role it answers "Error: Internal error", which is indistinguishable
# from a real internal error, so it cannot go in ctrl()'s tolerated branch without
# swallowing genuine failures. Read the client's roles first instead.
#
# The role list is matched only within `getClient`'s "Roles:" section: `grep -w`
# against the whole output would match role `ingestor` inside username `ff-ingestor`
# and silently skip the grant.
grant() {
  user=$1 ; role=$2
  mctrl getClient "$user"
  if [ "$rc" -eq 0 ] && printf '%s\n' "$out" | sed -n '/^Roles:/,$p' | grep -qw -- "$role"; then
    echo "configure: $user already holds role $role"
    return 0
  fi
  ctrl addClientRole "$user" "$role"
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
grant "$MOSQUITTO_INGESTOR_USERNAME" ingestor

# The API's deploy publisher (R1-be-2). WRITE-ONLY and dn/-ONLY: no
# subscribePattern and no publishClientReceive, because the ingestor is the sole
# MQTT subscriber and that invariant is load bearing. Nothing else in the estate
# may publish a command — the dynsec ADMIN's rights are over $CONTROL, not over
# ff/v1, and a device is denied its own dn/cmd (`just broker-check` proves both).
#
# The `+` is safe here and a `+` on the `device` role would not be: both ACL
# backends are consulted and ALLOW WINS, so a dn/ rule on `device` would let every
# board receive every other board's commands. This rule grants a CLASS of topics to
# a role that holds exactly one client. Per-device dynsec ACLs would cost a control
# -plane call per enrolment and contain nothing extra (the API already holds admin).
ctrl createRole commander
ctrl addRoleACL commander publishClientSend 'ff/v1/d/+/dn/#' allow
ctrl createClient    "$MOSQUITTO_COMMANDER_USERNAME" -p "$MOSQUITTO_COMMANDER_PASSWORD"
ctrl setClientPassword "$MOSQUITTO_COMMANDER_USERNAME" "$MOSQUITTO_COMMANDER_PASSWORD"
grant "$MOSQUITTO_COMMANDER_USERNAME" commander

# Deny by default. `dynsec init` leaves publishClientReceive=true.
ctrl setDefaultACLAccess publishClientSend    deny
ctrl setDefaultACLAccess publishClientReceive deny
ctrl setDefaultACLAccess subscribe            deny

# Read the estate back out of the broker's MEMORY rather than trusting a silent
# success. This is the assertion F-2026-09-23-002 got past: every command above
# "succeeded" against a throwaway broker for months while the live one had neither
# the commander client nor the commander role.
verify() {
  what=$1 ; name=$2
  mctrl "get$what" "$name"
  case "$out" in
    *"$name"*) echo "configure: verified $what $name" ;;
    *)
      printf '%s\n' "$out" >&2
      echo "configure: FAILED: $what '$name' is not on the running broker after configuring it" >&2
      exit 1
      ;;
  esac
}
verify Role   device
verify Role   ingestor
verify Role   commander
verify Client "$MOSQUITTO_INGESTOR_USERNAME"
verify Client "$MOSQUITTO_COMMANDER_USERNAME"

echo "configure: done"
