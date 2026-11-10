# Copyright 2026 Test Author
# See LICENSE file for licensing details.
#
# To learn more about testing, see https://canonical.com/juju/docs/ops/latest/explanation/testing/

import dataclasses
import urllib.error

import ops
import pytest
from ops import testing

from charm import (
    API_TOKEN_LABEL,
    APP_SECRET_LABEL,
    SERVICE_NAME,
    WORKLOAD_COMMAND,
    BooCharm,
    proxy_environment,
)

CHECK_NAME = "service-ready"  # Name of Pebble check in the mock workload container.

# A minimal Pebble layer for our testing.Container objects.
# Our charm doesn't retrieve the service command or the check URL
# from Pebble, so this layer doesn't need a real command or URL.
MOCK_LAYER = ops.pebble.Layer(
    {
        "services": {
            SERVICE_NAME: {
                "override": "replace",
                "command": "mock-command",
                "startup": "enabled",
            }
        },
        "checks": {
            CHECK_NAME: {
                "override": "replace",
                "level": "ready",
                "threshold": 3,
                "startup": "enabled",
                "http": {
                    "url": "http://localhost:1234/mock-endpoint",
                },
            }
        },
    }
)


def mock_get_version():
    """Get a mock version string without executing the workload code."""
    return "1.0.0"


def make_container(check_status: ops.pebble.CheckStatus) -> testing.Container:
    """Build a test container with the given check status."""
    check_in = testing.CheckInfo(
        CHECK_NAME,
        level=ops.pebble.CheckLevel.READY,
        status=check_status,
    )
    return testing.Container(
        "some-container",
        can_connect=True,
        layers={"base": MOCK_LAYER},
        service_statuses={SERVICE_NAME: ops.pebble.ServiceStatus.INACTIVE},
        check_infos={check_in},
    )


def test_pebble_ready(monkeypatch: pytest.MonkeyPatch):
    """Test that the charm has the correct state after handling the pebble-ready event."""
    # Arrange:
    ctx = testing.Context(BooCharm)
    container_in = make_container(ops.pebble.CheckStatus.UP)
    state_in = testing.State(containers={container_in})
    monkeypatch.setattr("charm.boo.get_version", mock_get_version)

    # Act:
    state_out = ctx.run(ctx.on.pebble_ready(container_in), state_in)

    # Assert:
    container_out = state_out.get_container(container_in.name)
    assert container_out.service_statuses[SERVICE_NAME] == ops.pebble.ServiceStatus.ACTIVE
    assert state_out.workload_version is not None
    assert state_out.unit_status == testing.ActiveStatus()


def test_pebble_ready_service_not_ready():
    """Test that the charm raises an error if the workload isn't ready after Pebble starts it."""
    # Arrange:
    ctx = testing.Context(BooCharm)
    container_in = make_container(ops.pebble.CheckStatus.DOWN)
    state_in = testing.State(containers={container_in})

    # Act & assert:
    with pytest.raises(testing.errors.UncaughtCharmError):
        ctx.run(ctx.on.pebble_ready(container_in), state_in)


def test_restart_action():
    """Test that the restart action starts the service and leaves the unit active."""
    # Arrange:
    ctx = testing.Context(BooCharm)
    check_in = testing.CheckInfo(
        CHECK_NAME,
        level=ops.pebble.CheckLevel.READY,
        status=ops.pebble.CheckStatus.UP,  # Simulate the Pebble check passing.
    )
    container_in = testing.Container(
        "some-container",
        can_connect=True,
        layers={"base": MOCK_LAYER},
        service_statuses={SERVICE_NAME: ops.pebble.ServiceStatus.INACTIVE},
        check_infos={check_in},
    )
    state_in = testing.State(containers={container_in})

    # Act:
    state_out = ctx.run(ctx.on.action("restart"), state_in)

    # Assert:
    container_out = state_out.get_container(container_in.name)
    assert container_out.service_statuses[SERVICE_NAME] == ops.pebble.ServiceStatus.ACTIVE
    assert state_out.unit_status == testing.ActiveStatus()


def test_restart_action_service_not_ready():
    """Test that the charm raises an error if the workload isn't ready after the restart."""
    # Arrange:
    ctx = testing.Context(BooCharm)
    check_in = testing.CheckInfo(
        CHECK_NAME,
        level=ops.pebble.CheckLevel.READY,
        status=ops.pebble.CheckStatus.DOWN,  # Simulate the Pebble check failing.
    )
    container_in = testing.Container(
        "some-container",
        can_connect=True,
        layers={"base": MOCK_LAYER},
        service_statuses={SERVICE_NAME: ops.pebble.ServiceStatus.INACTIVE},
        check_infos={check_in},
    )
    state_in = testing.State(containers={container_in})

    # Act & assert:
    with pytest.raises(testing.errors.UncaughtCharmError):
        ctx.run(ctx.on.action("restart"), state_in)


def test_restart_action_container_not_ready():
    """Test that the restart action fails when the container can't be reached."""
    # Arrange:
    ctx = testing.Context(BooCharm)
    container_in = testing.Container("some-container", can_connect=False)
    state_in = testing.State(containers={container_in})

    # Act & assert:
    with pytest.raises(testing.ActionFailed) as exc_info:
        ctx.run(ctx.on.action("restart"), state_in)
    assert exc_info.value.message == "cannot connect to workload container"
    # 'isinstance' rather than 'is not None': ops annotates 'state' with a fallback stub
    # that static type checkers can't see through, so narrowing it here keeps them happy.
    assert isinstance(exc_info.value.state, testing.State)
    assert exc_info.value.state.unit_status == testing.WaitingStatus(
        "waiting for workload container"
    )


def test_pebble_ready_creates_app_secret(monkeypatch: pytest.MonkeyPatch):
    """Test that the charm creates an app-managed workload secret on pebble-ready (leader)."""
    # Arrange:
    ctx = testing.Context(BooCharm)
    container_in = make_container(ops.pebble.CheckStatus.UP)
    state_in = testing.State(containers={container_in}, leader=True)
    monkeypatch.setattr("charm.boo.get_version", mock_get_version)

    # Act:
    state_out = ctx.run(ctx.on.pebble_ready(container_in), state_in)

    # Assert — the app-owned secret should appear in the output state with a 'password' key:
    app_secrets = [s for s in state_out.secrets if s.label == APP_SECRET_LABEL]
    assert len(app_secrets) == 1
    assert app_secrets[0].latest_content is not None
    assert "password" in app_secrets[0].latest_content


def test_pebble_ready_app_secret_already_exists(monkeypatch: pytest.MonkeyPatch):
    """Test that pebble-ready is idempotent when the app secret already exists."""
    # Arrange:
    ctx = testing.Context(BooCharm)
    container_in = make_container(ops.pebble.CheckStatus.UP)
    existing_secret = testing.Secret(
        tracked_content={"password": "existing-password"},
        label=APP_SECRET_LABEL,
        owner="app",
    )
    state_in = testing.State(containers={container_in}, leader=True, secrets={existing_secret})
    monkeypatch.setattr("charm.boo.get_version", mock_get_version)

    # Act:
    state_out = ctx.run(ctx.on.pebble_ready(container_in), state_in)

    # Assert — the existing secret is still present; no extra secret was created:
    app_secrets = [s for s in state_out.secrets if s.label == APP_SECRET_LABEL]
    assert len(app_secrets) == 1


def test_config_changed_with_api_token():
    """Test that config-changed resolves a user-provided secret and goes active."""
    # Arrange:
    ctx = testing.Context(BooCharm)
    secret = testing.Secret(
        tracked_content={"token": "my-secret-api-token"},
        owner=None,  # user-owned secret (not app- or unit-owned)
    )
    state_in = testing.State(
        containers={testing.Container("some-container")},
        secrets={secret},
        config={"api-token": secret.id},
    )

    # Act:
    state_out = ctx.run(ctx.on.config_changed(), state_in)

    # Assert:
    assert state_out.unit_status == testing.ActiveStatus()


def test_config_changed_api_token_not_found():
    """Test that config-changed blocks when the secret URI is not accessible."""
    # Arrange:
    ctx = testing.Context(BooCharm)
    state_in = testing.State(
        containers={testing.Container("some-container")},
        config={"api-token": "secret:doesnotexist00000000"},
        # No matching secret in state — simulates the secret not being granted.
    )

    # Act:
    state_out = ctx.run(ctx.on.config_changed(), state_in)

    # Assert:
    assert isinstance(state_out.unit_status, testing.BlockedStatus)


def test_config_changed_api_token_missing_key():
    """Test that config-changed blocks when the secret has no 'token' key."""
    # Arrange:
    ctx = testing.Context(BooCharm)
    secret = testing.Secret(
        tracked_content={"not-token": "my-secret-api-token"},
        owner=None,  # user-owned secret (not app- or unit-owned)
    )
    state_in = testing.State(
        containers={testing.Container("some-container")},
        secrets={secret},
        config={"api-token": secret.id},
    )

    # Act:
    state_out = ctx.run(ctx.on.config_changed(), state_in)

    # Assert:
    assert isinstance(state_out.unit_status, testing.BlockedStatus)


def test_config_changed_no_token():
    """Test that config-changed without api-token set does nothing."""
    # Arrange:
    ctx = testing.Context(BooCharm)
    state_in = testing.State(containers={testing.Container("some-container")})

    # Act:
    state_out = ctx.run(ctx.on.config_changed(), state_in)

    # Assert — status unchanged (charm hasn't done anything yet):
    assert not isinstance(state_out.unit_status, testing.BlockedStatus)


def test_secret_rotate():
    """Test that secret-rotate generates a new revision of the workload password."""
    # Arrange:
    ctx = testing.Context(BooCharm)
    secret = testing.Secret(
        tracked_content={"password": "old-password"},
        label=APP_SECRET_LABEL,
        owner="app",
    )
    state_in = testing.State(secrets={secret}, leader=True)

    # Act:
    state_out = ctx.run(ctx.on.secret_rotate(secret), state_in)

    # Assert — a new revision with a different password should have been added:
    out_secret = state_out.get_secret(label=APP_SECRET_LABEL)
    assert out_secret.latest_content is not None
    assert "password" in out_secret.latest_content
    assert out_secret.latest_content["password"] != "old-password"


def test_secret_expired():
    """Test that secret-expired replaces the expired password with a new revision."""
    # Arrange:
    ctx = testing.Context(BooCharm)
    secret = testing.Secret(
        tracked_content={"password": "expired-password"},
        label=APP_SECRET_LABEL,
        owner="app",
    )
    state_in = testing.State(secrets={secret}, leader=True)

    # Act — simulate the tracked revision expiring:
    state_out = ctx.run(ctx.on.secret_expired(secret, revision=1), state_in)

    # Assert — a new revision should have been added, not the old one removed:
    out_secret = state_out.get_secret(label=APP_SECRET_LABEL)
    assert out_secret.latest_content is not None
    assert out_secret.latest_content["password"] != "expired-password"


def test_secret_changed_new_api_token_revision():
    """Test that the charm picks up a new revision of the user-provided api-token."""
    # Arrange:
    ctx = testing.Context(BooCharm)
    secret = testing.Secret(
        tracked_content={"token": "old-api-token"},
        latest_content={"token": "new-api-token"},
        label=API_TOKEN_LABEL,
        owner=None,  # user-owned secret (not app- or unit-owned)
    )
    state_in = testing.State(
        containers={testing.Container("some-container")},
        secrets={secret},
        config={"api-token": secret.id},
    )

    # Act:
    state_out = ctx.run(ctx.on.secret_changed(secret), state_in)

    # Assert — the charm refreshed, so it now tracks the latest revision:
    out_secret = state_out.get_secret(label=API_TOKEN_LABEL)
    assert out_secret.tracked_content == {"token": "new-api-token"}
    assert state_out.unit_status == testing.ActiveStatus()


def test_secret_remove():
    """Test that secret-remove cleans up a revision that no unit is using."""
    # Arrange — rotate twice, so that revision 2 is neither the oldest revision
    # nor the newest one:
    ctx = testing.Context(BooCharm)
    secret = testing.Secret(
        tracked_content={"password": "first-password"},
        label=APP_SECRET_LABEL,
        owner="app",
    )
    state = testing.State(secrets={secret}, leader=True)
    for _ in range(2):
        state = ctx.run(ctx.on.secret_rotate(state.get_secret(label=APP_SECRET_LABEL)), state)

    # Act — Juju reports that revision 2 is no longer tracked by any unit:
    ctx.run(ctx.on.secret_remove(state.get_secret(label=APP_SECRET_LABEL), revision=2), state)

    # Assert — the charm asked Juju to remove that revision:
    assert ctx.removed_secret_revisions == [2]


PROXY_CONFIG = testing.ProxyConfig(
    http="http://squid.internal:3128",
    https="http://squid.internal:3129",
    no=("127.0.0.1", "localhost", "::1", "10.0.0.0/8"),
)

# What the workload should see for PROXY_CONFIG:
EXPECTED_PROXY_ENVIRONMENT = {
    "HTTP_PROXY": "http://squid.internal:3128",
    "http_proxy": "http://squid.internal:3128",
    "HTTPS_PROXY": "http://squid.internal:3129",
    "https_proxy": "http://squid.internal:3129",
    "NO_PROXY": "127.0.0.1,localhost,::1,10.0.0.0/8",
    "no_proxy": "127.0.0.1,localhost,::1,10.0.0.0/8",
}


def make_state(proxy_config: testing.ProxyConfig | None) -> testing.State:
    """Build a state in which the workload container is up and the model has these settings.

    The model's settings are what 'juju model-config juju-*-proxy' would set.
    """
    return testing.State(
        containers={make_container(ops.pebble.CheckStatus.UP)},
        model=testing.Model(proxy_config=proxy_config),
    )


def with_proxy_config(
    state: testing.State, proxy_config: testing.ProxyConfig | None
) -> testing.State:
    """Change the model's proxy settings, like 'juju model-config' does between two events."""
    return dataclasses.replace(state, model=testing.Model(proxy_config=proxy_config))


def get_service_environment(state: testing.State) -> dict[str, str]:
    """Get the environment that Pebble will give the workload's service."""
    container = state.get_container("some-container")
    return dict(container.plan.services[SERVICE_NAME].environment)


def test_proxy_environment():
    """Test that all of the model's proxy settings become upper- and lower-case variables."""
    proxy = ops.ProxyConfig(
        http="http://a:1",
        https="http://b:2",
        no=("localhost",),
    )
    assert proxy_environment(proxy) == {
        "HTTP_PROXY": "http://a:1",
        "http_proxy": "http://a:1",
        "HTTPS_PROXY": "http://b:2",
        "https_proxy": "http://b:2",
        "NO_PROXY": "localhost",
        "no_proxy": "localhost",
    }


def test_proxy_environment_omits_missing_settings():
    """Test that settings that the model doesn't provide are left out."""
    assert proxy_environment(ops.ProxyConfig()) == {}
    # NO_PROXY alone has no effect, so the workload doesn't get it. This matters because Juju's
    # default for 'juju-no-proxy' is not empty.
    assert proxy_environment(ops.ProxyConfig(no=("localhost",))) == {}
    assert proxy_environment(ops.ProxyConfig(https="http://b:2")) == {
        "HTTPS_PROXY": "http://b:2",
        "https_proxy": "http://b:2",
    }


def test_workload_is_a_web_server():
    """Test that the Pebble service runs the web server that shows the proxy settings."""
    ctx = testing.Context(BooCharm)
    state_out = ctx.run(ctx.on.config_changed(), make_state(None))
    service = state_out.get_container("some-container").plan.services[SERVICE_NAME]
    assert service.command == WORKLOAD_COMMAND
    assert "python3 -m http.server" in service.command


@pytest.mark.parametrize(
    "event",
    ["config_changed", "update_status", "upgrade_charm"],
)
def test_event_applies_proxy_settings(event: str):
    """Test that each reconciling event gives the workload the model's proxy settings."""
    # Arrange — the workload runs without proxy settings, and then the user sets them:
    ctx = testing.Context(BooCharm)

    # Act:
    state_out = ctx.run(getattr(ctx.on, event)(), make_state(PROXY_CONFIG))

    # Assert:
    assert get_service_environment(state_out) == EXPECTED_PROXY_ENVIRONMENT
    container_out = state_out.get_container("some-container")
    assert container_out.service_statuses[SERVICE_NAME] == ops.pebble.ServiceStatus.ACTIVE


def test_pebble_ready_applies_proxy_settings(monkeypatch: pytest.MonkeyPatch):
    """Test that the Pebble layer starts with the model's proxy settings."""
    # Arrange:
    ctx = testing.Context(BooCharm)
    state_in = make_state(PROXY_CONFIG)
    container_in = state_in.get_container("some-container")
    monkeypatch.setattr("charm.boo.get_version", mock_get_version)

    # Act:
    state_out = ctx.run(ctx.on.pebble_ready(container_in), state_in)

    # Assert:
    assert get_service_environment(state_out) == EXPECTED_PROXY_ENVIRONMENT


@pytest.mark.parametrize("proxy_config", [None, testing.ProxyConfig()])
def test_no_proxy_settings(proxy_config: testing.ProxyConfig | None):
    """Test that the workload gets no proxy variables if the model has no proxy settings."""
    # Both None and the default ProxyConfig mean that the user hasn't set a proxy.
    ctx = testing.Context(BooCharm)
    state_out = ctx.run(ctx.on.update_status(), make_state(proxy_config))
    assert get_service_environment(state_out) == {}


def test_update_status_picks_up_set_change_and_unset():
    """Test that the workload follows the model's settings as they are set, changed and unset."""
    ctx = testing.Context(BooCharm)
    state = ctx.run(ctx.on.update_status(), make_state(None))
    assert get_service_environment(state) == {}

    # The user sets the proxy settings. The charm doesn't get an event for this.
    state = ctx.run(ctx.on.update_status(), with_proxy_config(state, PROXY_CONFIG))
    assert get_service_environment(state) == EXPECTED_PROXY_ENVIRONMENT

    # The user changes one of the settings.
    changed = testing.ProxyConfig(
        http=PROXY_CONFIG.http, https="http://other.internal:3129", no=()
    )
    state = ctx.run(ctx.on.update_status(), with_proxy_config(state, changed))
    assert get_service_environment(state)["HTTPS_PROXY"] == "http://other.internal:3129"
    assert get_service_environment(state)["HTTP_PROXY"] == PROXY_CONFIG.http
    assert "NO_PROXY" not in get_service_environment(state)

    # The user resets the settings: nothing is left behind.
    state = ctx.run(ctx.on.update_status(), with_proxy_config(state, None))
    assert get_service_environment(state) == {}


def test_update_status_unsets_one_setting_at_a_time():
    """Test that removing one setting leaves the others in place."""
    ctx = testing.Context(BooCharm)
    state = ctx.run(ctx.on.update_status(), make_state(PROXY_CONFIG))

    without_https = testing.ProxyConfig(http=PROXY_CONFIG.http, no=PROXY_CONFIG.no)
    state = ctx.run(ctx.on.update_status(), with_proxy_config(state, without_https))

    environment = get_service_environment(state)
    assert "HTTPS_PROXY" not in environment
    assert "https_proxy" not in environment
    assert environment["HTTP_PROXY"] == "http://squid.internal:3128"
    assert environment["NO_PROXY"] == "127.0.0.1,localhost,::1,10.0.0.0/8"


def test_update_status_is_idempotent():
    """Test that handling events with unchanged settings leaves the Pebble plan unchanged."""
    ctx = testing.Context(BooCharm)
    state = ctx.run(ctx.on.update_status(), make_state(PROXY_CONFIG))
    plan = state.get_container("some-container").plan.to_dict()

    for _ in range(3):
        state = ctx.run(ctx.on.update_status(), state)

    assert state.get_container("some-container").plan.to_dict() == plan


def test_update_status_container_not_ready():
    """Test that update-status leaves the workload alone while Pebble isn't ready."""
    # Arrange — pebble-ready will apply the proxy settings later:
    ctx = testing.Context(BooCharm)
    container_in = testing.Container("some-container", can_connect=False)
    state_in = testing.State(
        containers={container_in}, model=testing.Model(proxy_config=PROXY_CONFIG)
    )

    # Act:
    state_out = ctx.run(ctx.on.update_status(), state_in)

    # Assert:
    assert not state_out.get_container("some-container").layers
    assert not isinstance(state_out.unit_status, testing.BlockedStatus)


def test_show_proxy_action(monkeypatch: pytest.MonkeyPatch):
    """Test that the show-proxy action reports what the workload serves."""
    # Arrange:
    ctx = testing.Context(BooCharm)
    served = {"proxy.txt": "http_proxy=http://squid.internal:3128", "started.txt": "123"}
    monkeypatch.setattr("charm.boo.read_report", served.__getitem__)

    # Act:
    ctx.run(ctx.on.action("show-proxy"), make_state(None))

    # Assert:
    assert ctx.action_results == {"proxy": served["proxy.txt"], "started": "123"}


def test_show_proxy_action_workload_down(monkeypatch: pytest.MonkeyPatch):
    """Test that the show-proxy action fails if the workload can't be reached."""

    def unreachable(name: str) -> str:
        raise urllib.error.URLError("connection refused")

    # Arrange:
    ctx = testing.Context(BooCharm)
    monkeypatch.setattr("charm.boo.read_report", unreachable)

    # Act & assert:
    with pytest.raises(testing.ActionFailed) as exc_info:
        ctx.run(ctx.on.action("show-proxy"), make_state(None))
    assert "cannot read from the workload" in exc_info.value.message


def test_proxy_settings_are_visible_in_the_charm():
    """Test that the charm reads the settings from the model."""
    # Arrange:
    ctx = testing.Context(BooCharm)

    # Act and assert:
    with ctx(ctx.on.config_changed(), make_state(PROXY_CONFIG)) as manager:
        assert manager.charm.model.proxy_config.https == "http://squid.internal:3129"
        manager.run()
