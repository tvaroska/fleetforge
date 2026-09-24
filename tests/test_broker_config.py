"""The broker's *configuration* invariants. **CRITICAL** — no broker, no database.

`CRITICAL.md` → *Mosquitto ACL configuration / dynsec provisioning*: "Two pattern
rules are the entire fleet authz. A wrong pattern lets any device impersonate any
other." Those rules live in a config file, so nothing in the Python suite would
notice a bad edit — the live proof is `just broker-check`
(`python -m fleetforge.broker selftest`), which needs a running stack and therefore
cannot be part of `just test` (`R0-be-6` set the same rule for MinIO).

What is checkable without a broker is exactly this: that the files still say what the
security model claims they say. Each assertion below is a specific way the fleet has
already been able to break:

* a `%u` turned into a `+` (every device may publish as every other);
* a bare `topic` line (applies to every authenticated client, including boards);
* `allow_anonymous true` coming back;
* the dynsec role name drifting from `broker.DEVICE_ROLE` (503 on every enrollment).

`docker-compose.yml` is read as text on purpose: there is no YAML dependency in this
project and `just stack-check` is the real syntax gate. These checks stay coarse.
"""

import os
import re
from pathlib import Path

import pytest

from fleetforge.broker import DEVICE_ROLE

REPO_ROOT = Path(__file__).resolve().parent.parent
MOSQUITTO_DIR = REPO_ROOT / "mosquitto"
ACL_FILE = MOSQUITTO_DIR / "acl"
DYNSEC_CONF = MOSQUITTO_DIR / "conf.d" / "20-dynsec.conf"
BOOTSTRAP = MOSQUITTO_DIR / "bootstrap.sh"
CONFIGURE = MOSQUITTO_DIR / "configure.sh"
COMPOSE = REPO_ROOT / "docker-compose.yml"

# Verbatim from spec/device-protocol.md → "Why the up/dn split". Retyped here on
# purpose: this test is the tripwire, so it must not import the value it guards.
EXPECTED_ACL_RULES = [
    "pattern write ff/v1/d/%u/up/#",
    "pattern read ff/v1/d/%u/dn/#",
]


def _code(path: Path) -> str:
    """A shell script with its comments stripped.

    The two scripts carry long comments explaining F-2026-09-23-002 — including the
    `1884` throwaway broker and the `|| true` that used to swallow every refusal. A
    check for "this script no longer does X" that reads the comments finds the
    warning *about* X and fails. Assert against what runs.
    """
    return "\n".join(
        line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")
    )


def _directives(path: Path) -> list[str]:
    """Every non-comment, non-blank line, with runs of whitespace collapsed."""
    return [
        re.sub(r"\s+", " ", line.strip())
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


class TestAclFile:
    """`mosquitto/acl` is the entire fleet authorisation model."""

    def test_holds_exactly_the_two_pattern_rules(self) -> None:
        assert _directives(ACL_FILE) == EXPECTED_ACL_RULES

    @pytest.mark.parametrize("rule", EXPECTED_ACL_RULES)
    def test_each_rule_binds_to_the_username(self, rule: str) -> None:
        """`%u` is the whole control: a `+` here lets any device be any other."""
        assert "%u" in rule
        assert "ff/v1/d/%u/" in rule

    def test_has_no_bare_topic_line(self) -> None:
        """An unqualified `topic` applies to EVERY authenticated client — boards too."""
        assert not [d for d in _directives(ACL_FILE) if d.startswith("topic ")]

    def test_has_no_user_section(self) -> None:
        """A `user` block would be a per-device ACL row: not the model, and unprovisioned."""
        assert not [d for d in _directives(ACL_FILE) if d.startswith("user ")]


class TestDynsecConf:
    """`conf.d/20-dynsec.conf` — authentication, and where the ACL file is."""

    def test_anonymous_access_is_off(self) -> None:
        assert "allow_anonymous false" in _directives(DYNSEC_CONF)

    def test_loads_the_dynamic_security_plugin(self) -> None:
        text = DYNSEC_CONF.read_text()
        assert "mosquitto_dynamic_security.so" in text
        assert "plugin_opt_config_file /mosquitto/data/dynamic-security.json" in text

    def test_dynamic_security_store_lives_in_the_data_volume(self) -> None:
        """It is rewritten on every enrollment: a read-only mount loses every credential."""
        assert "plugin_opt_config_file /mosquitto/data/" in DYNSEC_CONF.read_text()

    def test_points_acl_file_at_the_mounted_acl(self) -> None:
        assert "acl_file /mosquitto/config/acl" in _directives(DYNSEC_CONF)


class TestNothingGrantsAnonymousAccess:
    """The property `R0-sec-1` exists to establish, checked over the whole directory."""

    def test_no_file_under_mosquitto_allows_anonymous(self) -> None:
        offenders = [
            path.relative_to(REPO_ROOT)
            for path in MOSQUITTO_DIR.rglob("*")
            if path.is_file() and "allow_anonymous true" in path.read_text()
        ]
        assert offenders == []

    def test_the_dev_anonymous_dropin_is_gone(self) -> None:
        assert not (MOSQUITTO_DIR / "conf.d" / "10-dev-anonymous.conf").exists()


class TestBootstrapTouchesOnlyTheFile:
    """`mosquitto/bootstrap.sh` — phase 1, and it must stay as small as it now is.

    These are regression guards for ops-log **F-2026-09-23-002**, in which this script
    applied the whole estate to a *throwaway* broker on 127.0.0.1:1884 and wrote the
    result into the shared volume. The dynsec plugin reads that file once at broker
    startup and rewrites it from memory thereafter, so on any broker that was already
    running the bootstrap was invisible AND its entries were scheduled for deletion.
    Prod's `commander` client never existed; every OTA deploy answered `Not authorized`
    while this script logged a clean run on every deploy.

    The only thing that genuinely has to happen before the broker starts is creating
    the file, because the plugin will not load without one. Everything else belongs to
    `configure.sh`, against the live broker.
    """

    def test_creates_no_role_and_no_client(self) -> None:
        """The whole finding, as one assertion. A role here reaches no running broker."""
        for command in ("createRole", "createClient", "addRoleACL", "addClientRole"):
            assert not re.search(
                rf"^\s*(mosquitto_ctrl|ctrl|grant).*\b{command}\b", _code(BOOTSTRAP), re.M
            )

    def test_starts_no_throwaway_broker(self) -> None:
        """The 1884 broker is the mechanism of the finding, not an implementation detail."""
        code = _code(BOOTSTRAP)
        assert "1884" not in code
        assert not re.search(r"^\s*mosquitto\s+-c", code, re.M)

    def test_is_given_no_service_credential(self) -> None:
        """A service credential passed here is one written to a file the broker overwrites."""
        text = BOOTSTRAP.read_text()
        assert "MOSQUITTO_COMMANDER_PASSWORD" not in text
        assert "MOSQUITTO_INGESTOR_PASSWORD" not in text

    def test_still_creates_the_store(self) -> None:
        """`dynsec init` is the only subcommand that works on a file, and must stay here."""
        assert "dynsec init" in BOOTSTRAP.read_text()

    def test_fixes_ownership_of_the_mutable_store(self) -> None:
        """Root-owned = "not writable", applied in memory, and lost on the next restart."""
        text = BOOTSTRAP.read_text()
        assert "chown mosquitto:mosquitto" in text
        assert "chmod 0600" in text

    def test_is_executable(self) -> None:
        """Bind-mounted and run through `sh`, but a non-executable script is a foot-gun."""
        assert os.access(BOOTSTRAP, os.X_OK)


class TestConfigure:
    """`mosquitto/configure.sh` — phase 2, the estate, applied to the RUNNING broker.

    Every assertion that used to be made about `bootstrap.sh` lives here now, because
    this is where the roles and clients moved. The new ones are about *where* it
    applies them: a live broker over `$CONTROL/dynamic-security/v1`, never the file.
    """

    def test_targets_the_running_broker_and_not_a_throwaway(self) -> None:
        """The point of the split. `MOSQUITTO_HOST` is the real broker; 1884 is gone."""
        code = _code(CONFIGURE)
        assert "1884" not in code
        assert not re.search(r"^\s*mosquitto\s+-c", code, re.M)
        assert "HOST=${MOSQUITTO_HOST:-mosquitto}" in code
        assert '-h "$HOST"' in code

    def test_never_touches_the_dynsec_file(self) -> None:
        """If it cannot be done over $CONTROL it does not belong in this script."""
        assert "dynamic-security.json" not in _code(CONFIGURE)

    def test_creates_a_role_named_exactly_device_role(self) -> None:
        """A name mismatch answers 503 on every enrollment. Import it, never retype it."""
        assert f"createRole {DEVICE_ROLE}\n" in CONFIGURE.read_text()

    def test_the_device_role_is_left_empty(self) -> None:
        """The two `%u` patterns cannot live in a dynsec role — it has no substitution."""
        assert f"addRoleACL {DEVICE_ROLE} " not in CONFIGURE.read_text()

    def test_denies_by_default(self) -> None:
        """`dynsec init` leaves publishClientReceive allowed."""
        text = CONFIGURE.read_text()
        for acltype in ("publishClientSend", "publishClientReceive", "subscribe"):
            assert re.search(rf"setDefaultACLAccess\s+{acltype}\s+deny", text)

    def test_the_ingestor_role_is_read_only_and_scoped_to_up(self) -> None:
        text = CONFIGURE.read_text()
        assert re.search(r"addRoleACL ingestor\s+subscribePattern\s+'ff/v1/d/\+/up/#' allow", text)
        assert re.search(
            r"addRoleACL ingestor\s+publishClientReceive\s+'ff/v1/d/\+/up/#' allow", text
        )
        assert "addRoleACL ingestor publishClientSend" not in text

    def test_the_commander_role_may_only_send_on_dn(self) -> None:
        """R1-be-2: the API's deploy credential. One rule, `publishClientSend`, `dn/` only.

        A `subscribePattern` or a `publishClientReceive` here would make the API a second
        MQTT subscriber — the ingestor being the only one is load bearing — and a `up/`
        rule would let a leaked API credential forge telemetry and fake `up/status`.
        """
        text = CONFIGURE.read_text()
        assert "createRole commander\n" in text
        assert re.search(
            r"addRoleACL commander\s+publishClientSend\s+'ff/v1/d/\+/dn/#' allow", text
        )
        assert len(re.findall(r"addRoleACL commander\s+", text)) == 1
        assert "addRoleACL commander subscribePattern" not in text
        assert "addRoleACL commander publishClientReceive" not in text

    def test_the_commander_role_is_never_granted_to_a_device(self) -> None:
        """Every board holds `device`; one client holds `commander`."""
        text = CONFIGURE.read_text()
        assert re.findall(r"^\s*grant\s+\S+\s+commander", text, re.M) == [
            'grant "$MOSQUITTO_COMMANDER_USERNAME" commander'
        ]

    def test_role_grants_go_through_the_read_first_helper(self) -> None:
        """A bare `addClientRole` is not idempotent: on a duplicate mosquitto 2.0.22
        answers "Error: Internal error" and exits 0, which cannot be told apart from a
        real failure. `grant` reads the client's roles first instead.

        `grant` itself ends in `ctrl addClientRole`, which is the point — what must not
        come back is a *caller* reaching past it."""
        assert not re.search(r"^\s*ctrl\s+addClientRole\s+\"?\$MOSQUITTO", _code(CONFIGURE), re.M)

    def test_the_commander_credential_is_mandatory(self) -> None:
        """An unset variable must fail the run, not create a passwordless client."""
        text = CONFIGURE.read_text()
        assert "${MOSQUITTO_COMMANDER_USERNAME:?" in text
        assert "${MOSQUITTO_COMMANDER_PASSWORD:?" in text

    def test_a_refused_command_is_fatal(self) -> None:
        """The old script ended its `ctrl` helper with `|| true`, so a broker that had
        never been configured still logged a clean run. The only `|| true` left may be
        the one that keeps a no-match `grep` from killing the script under `set -e`."""
        survivors = [
            line
            for line in _code(CONFIGURE).splitlines()
            if "|| true" in line and "grep -v" not in line
        ]
        assert survivors == []

    def test_verifies_the_estate_against_the_broker(self) -> None:
        """Read it back out of memory rather than trusting a silent success — this is
        the assertion F-2026-09-23-002 got past for months."""
        text = CONFIGURE.read_text()
        assert "verify Role   commander" in text
        assert 'verify Client "$MOSQUITTO_COMMANDER_USERNAME"' in text
        assert 'verify Client "$MOSQUITTO_INGESTOR_USERNAME"' in text

    def test_the_device_role_grants_nothing_on_dn(self) -> None:
        """Both ACL backends are OR-combined, so a `+` rule on `device` would be a breach."""
        assert "addRoleACL device" not in _code(CONFIGURE)

    def test_is_executable(self) -> None:
        """Bind-mounted and run through `sh`, but a non-executable script is a foot-gun."""
        assert os.access(CONFIGURE, os.X_OK)


class TestComposeWiring:
    """Coarse text checks — `just stack-check` is the real syntax gate."""

    def test_the_broker_healthcheck_authenticates(self) -> None:
        """Anonymous is gone; an unauthenticated probe is a permanently unhealthy broker."""
        healthcheck = next(
            line
            for line in COMPOSE.read_text().splitlines()
            if "mosquitto_sub -h 127.0.0.1" in line
        )
        assert "-u " in healthcheck
        assert "-P " in healthcheck

    def test_the_healthcheck_single_quotes_the_sys_topic(self) -> None:
        """Inside double quotes the shell expands `$SYS/...` to `/broker/uptime`."""
        healthcheck = next(
            line
            for line in COMPOSE.read_text().splitlines()
            if "mosquitto_sub -h 127.0.0.1" in line
        )
        assert "'$$SYS/broker/uptime'" in healthcheck

    def test_the_api_gets_a_mandatory_dynsec_credential(self) -> None:
        """Unset silently selects `NullProvisioner` — every board enrolls uncredentialed."""
        text = COMPOSE.read_text()
        assert "MQTT_DYNSEC_USERNAME: ${MQTT_DYNSEC_USERNAME:?" in text
        assert "MQTT_DYNSEC_PASSWORD: ${MQTT_DYNSEC_PASSWORD:?" in text

    def test_the_ingestor_gets_its_own_mandatory_credential(self) -> None:
        text = COMPOSE.read_text()
        assert "MQTT_USERNAME: ${MQTT_INGESTOR_USERNAME:?" in text
        assert "MQTT_PASSWORD: ${MQTT_INGESTOR_PASSWORD:?" in text

    def test_the_api_gets_a_mandatory_command_credential(self) -> None:
        """Unset silently selects `NullCommandPublisher` — every deploy answers 503."""
        text = COMPOSE.read_text()
        assert "MQTT_COMMAND_USERNAME: ${MQTT_COMMAND_USERNAME:?" in text
        assert "MQTT_COMMAND_PASSWORD: ${MQTT_COMMAND_PASSWORD:?" in text

    def test_the_configure_phase_is_given_the_commander_credential(self) -> None:
        """The same pair, under the names `configure.sh` reads."""
        text = COMPOSE.read_text()
        assert "MOSQUITTO_COMMANDER_USERNAME: ${MQTT_COMMAND_USERNAME:?" in text
        assert "MOSQUITTO_COMMANDER_PASSWORD: ${MQTT_COMMAND_PASSWORD:?" in text

    def test_the_two_broker_phases_both_exist(self) -> None:
        """One-phase configuration is F-2026-09-23-002. Both containers, or neither works."""
        text = COMPOSE.read_text()
        assert "mosquitto-init:" in text
        assert "mosquitto-config:" in text

    def test_the_configure_phase_waits_for_a_HEALTHY_broker(self) -> None:
        """`service_completed_successfully` on the init phase would put it back before
        the broker, which is the whole defect. It must run against a live one."""
        block = COMPOSE.read_text().split("mosquitto-config:")[1].split("\n  mosquitto:")[0]
        assert re.search(r"depends_on:\s*\n\s*mosquitto:\s*\n\s*condition: service_healthy", block)

    def test_the_configure_phase_cannot_reach_the_dynsec_file(self) -> None:
        """No data volume: a phase-2 script that can write the file will eventually try."""
        block = COMPOSE.read_text().split("mosquitto-config:")[1].split("\n  mosquitto:")[0]
        assert "/mosquitto/data" not in block

    def test_the_ingestor_is_not_given_the_command_credential(self) -> None:
        """One privilege per service: the ingestor subscribes, the API commands."""
        ingestor_block = COMPOSE.read_text().split("ingestor:")[1].split("\n  frontend:")[0]
        assert "MQTT_COMMAND_" not in ingestor_block

    def test_the_broker_waits_for_the_bootstrap(self) -> None:
        """Without this the plugin starts with no store and refuses every client."""
        assert "condition: service_completed_successfully" in COMPOSE.read_text()

    def test_the_acl_is_mounted_read_only(self) -> None:
        assert "./mosquitto/acl:/mosquitto/config/acl:ro" in COMPOSE.read_text()
