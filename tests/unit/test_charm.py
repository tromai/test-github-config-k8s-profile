# Copyright 2026 Test Author
# See LICENSE file for licensing details.
#
# To learn more about testing, see https://canonical.com/juju/docs/ops/latest/explanation/testing/

import ops
import pytest
from ops import testing

from charm import API_TOKEN_LABEL, APP_SECRET_LABEL, SERVICE_NAME, BooCharm, proxy_environment

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
    state_in = testing.State(secrets={secret}, config={"api-token": secret.id})

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


def make_proxy_state(container: testing.Container) -> testing.State:
    """Build a state in which the model has proxy settings (like 'juju model-config')."""
    proxy_config = testing.ProxyConfig(
        http_proxy="http://squid.internal:3128",
        https_proxy="http://squid.internal:3129",
        no_proxy="127.0.0.1,localhost,::1,10.0.0.0/8",
    )
    return testing.State(
        containers={container},
        model=testing.Model(proxy_config=proxy_config),
    )


# What the workload should see for the settings in make_proxy_state:
EXPECTED_PROXY_ENVIRONMENT = {
    "HTTP_PROXY": "http://squid.internal:3128",
    "http_proxy": "http://squid.internal:3128",
    "HTTPS_PROXY": "http://squid.internal:3129",
    "https_proxy": "http://squid.internal:3129",
    "NO_PROXY": "127.0.0.1,localhost,::1,10.0.0.0/8",
    "no_proxy": "127.0.0.1,localhost,::1,10.0.0.0/8",
}


def get_service_environment(state: testing.State) -> dict[str, str]:
    """Get the environment that Pebble will give the workload's service."""
    container = state.get_container("some-container")
    return dict(container.plan.services[SERVICE_NAME].environment)


def test_proxy_environment():
    """Test that all of the model's proxy settings become upper- and lower-case variables."""
    proxy = ops.ProxyConfig(
        http_proxy="http://a:1",
        https_proxy="http://b:2",
        ftp_proxy="http://c:3",
        no_proxy="localhost",
    )
    assert proxy_environment(proxy) == {
        "HTTP_PROXY": "http://a:1",
        "http_proxy": "http://a:1",
        "HTTPS_PROXY": "http://b:2",
        "https_proxy": "http://b:2",
        "FTP_PROXY": "http://c:3",
        "ftp_proxy": "http://c:3",
        "NO_PROXY": "localhost",
        "no_proxy": "localhost",
    }


def test_proxy_environment_omits_missing_settings():
    """Test that settings that the model doesn't provide are left out."""
    assert proxy_environment(ops.ProxyConfig()) == {}
    assert proxy_environment(ops.ProxyConfig(https_proxy="http://b:2")) == {
        "HTTPS_PROXY": "http://b:2",
        "https_proxy": "http://b:2",
    }


def test_pebble_ready_passes_proxy_settings_to_workload(monkeypatch: pytest.MonkeyPatch):
    """Test that the Pebble layer gives the workload the model's proxy settings."""
    # Arrange:
    ctx = testing.Context(BooCharm)
    container_in = make_container(ops.pebble.CheckStatus.UP)
    monkeypatch.setattr("charm.boo.get_version", mock_get_version)

    # Act:
    state_out = ctx.run(ctx.on.pebble_ready(container_in), make_proxy_state(container_in))

    # Assert:
    assert get_service_environment(state_out) == EXPECTED_PROXY_ENVIRONMENT
    assert state_out.unit_status == testing.ActiveStatus()


def test_pebble_ready_without_proxy_settings(monkeypatch: pytest.MonkeyPatch):
    """Test that the workload gets no proxy variables if the model has no proxy settings."""
    # Arrange:
    ctx = testing.Context(BooCharm)
    container_in = make_container(ops.pebble.CheckStatus.UP)
    monkeypatch.setattr("charm.boo.get_version", mock_get_version)

    # Act:
    state_out = ctx.run(
        ctx.on.pebble_ready(container_in), testing.State(containers={container_in})
    )

    # Assert:
    assert get_service_environment(state_out) == {}


def test_config_changed_updates_proxy_settings():
    """Test that config-changed applies proxy settings that changed after the workload started."""
    # Arrange — the workload is running without proxy settings:
    ctx = testing.Context(BooCharm)
    container_in = make_container(ops.pebble.CheckStatus.UP)

    # Act:
    state_out = ctx.run(ctx.on.config_changed(), make_proxy_state(container_in))

    # Assert:
    assert get_service_environment(state_out) == EXPECTED_PROXY_ENVIRONMENT
    container_out = state_out.get_container(container_in.name)
    assert container_out.service_statuses[SERVICE_NAME] == ops.pebble.ServiceStatus.ACTIVE


def test_config_changed_removes_stale_proxy_settings():
    """Test that config-changed removes settings that the model no longer provides."""
    # Arrange — the workload is running with proxy settings that the model no longer has:
    ctx = testing.Context(BooCharm)
    stale_layer = ops.pebble.Layer(MOCK_LAYER.to_dict())
    stale_layer.services[SERVICE_NAME].environment = {"HTTP_PROXY": "http://stale.internal:3128"}
    check_in = testing.CheckInfo(
        CHECK_NAME, level=ops.pebble.CheckLevel.READY, status=ops.pebble.CheckStatus.UP
    )
    container_in = testing.Container(
        "some-container",
        can_connect=True,
        layers={"base": stale_layer},
        service_statuses={SERVICE_NAME: ops.pebble.ServiceStatus.ACTIVE},
        check_infos={check_in},
    )

    # Act:
    state_out = ctx.run(ctx.on.config_changed(), testing.State(containers={container_in}))

    # Assert:
    assert get_service_environment(state_out) == {}


def test_upgrade_charm_updates_proxy_settings():
    """Test that upgrade-charm applies the model's current proxy settings."""
    # Arrange:
    ctx = testing.Context(BooCharm)
    container_in = make_container(ops.pebble.CheckStatus.UP)

    # Act:
    state_out = ctx.run(ctx.on.upgrade_charm(), make_proxy_state(container_in))

    # Assert:
    assert get_service_environment(state_out) == EXPECTED_PROXY_ENVIRONMENT


def test_config_changed_container_not_ready():
    """Test that config-changed leaves the workload alone while Pebble isn't ready."""
    # Arrange — pebble-ready will apply the proxy settings later:
    ctx = testing.Context(BooCharm)
    container_in = testing.Container("some-container", can_connect=False)

    # Act:
    state_out = ctx.run(ctx.on.config_changed(), make_proxy_state(container_in))

    # Assert:
    assert not state_out.get_container(container_in.name).layers
    assert not isinstance(state_out.unit_status, testing.BlockedStatus)


def test_proxy_settings_are_visible_in_the_charm():
    """Test that the charm reads the settings from the model."""
    # Arrange:
    ctx = testing.Context(BooCharm)
    container_in = testing.Container("some-container", can_connect=False)

    # Act and assert:
    with ctx(ctx.on.config_changed(), make_proxy_state(container_in)) as manager:
        assert manager.charm.model.proxy_config.https_proxy == "http://squid.internal:3129"
        assert manager.charm.model.proxy_config.ftp_proxy is None
        manager.run()
