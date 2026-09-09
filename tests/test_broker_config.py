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
COMPOSE = REPO_ROOT / "docker-compose.yml"

# Verbatim from spec/device-protocol.md → "Why the up/dn split". Retyped here on
# purpose: this test is the tripwire, so it must not import the value it guards.
EXPECTED_ACL_RULES = [
    "pattern write ff/v1/d/%u/up/#",
    "pattern read ff/v1/d/%u/dn/#",
]


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


class TestBootstrap:
    """`mosquitto/bootstrap.sh` — what exists in the store before any device enrolls."""

    def test_creates_a_role_named_exactly_device_role(self) -> None:
        """A name mismatch answers 503 on every enrollment. Import it, never retype it."""
        assert f"createRole {DEVICE_ROLE}\n" in BOOTSTRAP.read_text()

    def test_the_device_role_is_left_empty(self) -> None:
        """The two `%u` patterns cannot live in a dynsec role — it has no substitution."""
        assert f"addRoleACL {DEVICE_ROLE} " not in BOOTSTRAP.read_text()

    def test_denies_by_default(self) -> None:
        """`dynsec init` leaves publishClientReceive allowed."""
        text = BOOTSTRAP.read_text()
        for acltype in ("publishClientSend", "publishClientReceive", "subscribe"):
            assert re.search(rf"setDefaultACLAccess\s+{acltype}\s+deny", text)

    def test_fixes_ownership_of_the_mutable_store(self) -> None:
        """Root-owned = "not writable", applied in memory, and lost on the next restart."""
        text = BOOTSTRAP.read_text()
        assert "chown mosquitto:mosquitto" in text
        assert "chmod 0600" in text

    def test_the_ingestor_role_is_read_only_and_scoped_to_up(self) -> None:
        text = BOOTSTRAP.read_text()
        assert re.search(r"addRoleACL ingestor\s+subscribePattern\s+'ff/v1/d/\+/up/#' allow", text)
        assert re.search(
            r"addRoleACL ingestor\s+publishClientReceive\s+'ff/v1/d/\+/up/#' allow", text
        )
        assert "addRoleACL ingestor publishClientSend" not in text

    def test_is_executable(self) -> None:
        """Bind-mounted and run through `sh`, but a non-executable script is a foot-gun."""
        assert os.access(BOOTSTRAP, os.X_OK)


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

    def test_the_broker_waits_for_the_bootstrap(self) -> None:
        """Without this the plugin starts with no store and refuses every client."""
        assert "condition: service_completed_successfully" in COMPOSE.read_text()

    def test_the_acl_is_mounted_read_only(self) -> None:
        assert "./mosquitto/acl:/mosquitto/config/acl:ro" in COMPOSE.read_text()
