# Copyright 2026 Test Author
# See LICENSE file for licensing details.
#
# The integration tests use the Jubilant library and the pytest-jubilant plugin.
# See https://canonical.com/juju/docs/ops/latest/howto/write-integration-tests-for-a-charm/
#
# pytest-jubilant provides a module-scoped `juju` fixture that creates a temporary Juju model.
# The `charm` fixture is defined in conftest.py.

import logging
import pathlib

import jubilant
import pytest
import yaml

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(pathlib.Path("charmcraft.yaml").read_text())


@pytest.mark.juju_setup
def test_deploy(charm: pathlib.Path, juju: jubilant.Juju):
    """Deploy the charm under test."""
    resources = {
        "some-container-image": METADATA["resources"]["some-container-image"]["upstream-source"]
    }
    juju.deploy(charm, app="boo", resources=resources)
    juju.wait(jubilant.all_active)


def test_restart(charm: pathlib.Path, juju: jubilant.Juju):
    """Restart the workload with the restart action."""
    juju.run("boo/0", "restart")


def test_app_managed_secret_created(charm: pathlib.Path, juju: jubilant.Juju):
    """Check that the charm created the app-managed workload password on pebble-ready."""
    labels = [secret.label for secret in juju.secrets()]
    assert "workload-password" in labels, (
        "Expected the charm to create a 'workload-password' app secret on start. "
        "Check that the leader unit ran pebble-ready successfully."
    )


def test_user_secret(charm: pathlib.Path, juju: jubilant.Juju):
    """Test that the charm resolves a user-provided api-token secret."""
    # Create a user secret with a 'token' key, grant the charm access, and
    # point the charm at it.
    secret_uri = juju.add_secret("test-api-token", {"token": "shortvalue"})
    juju.grant_secret(secret_uri, "boo")
    juju.config("boo", {"api-token": secret_uri})
    juju.wait(jubilant.all_active)

    # The charm logs the length of the token it resolved.
    assert "api-token resolved (10 chars)" in juju.debug_log()

    # Act — add a new revision of the same secret. Juju notifies the charm with
    # a secret-changed event, and the charm re-reads the content.
    juju.update_secret(secret_uri, {"token": "a-much-longer-token-value"})
    juju.wait(jubilant.all_active)

    # Assert — the charm picked up the new revision, not the original one.
    assert "api-token resolved (25 chars)" in juju.debug_log()

    # Clean up: reset the config option so subsequent tests start clean.
    juju.config("boo", reset=["api-token"])
    juju.wait(jubilant.all_active)


def get_service_environment(juju: jubilant.Juju) -> dict[str, str]:
    """Get the environment of the workload's Pebble service from the Pebble plan."""
    plan = yaml.safe_load(
        juju.ssh("boo/0", "/charm/bin/pebble", "plan", container="some-container")
    )
    return plan["services"]["some-service"].get("environment", {})


def wait_for_settled(juju: jubilant.Juju) -> None:
    """Wait for the charm to be active and to have finished handling events."""
    juju.wait(lambda status: jubilant.all_active(status) and jubilant.all_agents_idle(status))


def test_model_proxy_settings(charm: pathlib.Path, juju: jubilant.Juju):
    """Test that the charm gives the workload the model's proxy settings."""
    # Juju emits no event when 'juju-*-proxy' model config changes. The charm picks up the new
    # settings the next time that it handles an event, so trigger config-changed by changing a
    # charm config option.
    juju.model_config(
        {
            "juju-http-proxy": "http://squid.internal:3128",
            "juju-https-proxy": "http://squid.internal:3129",
            "juju-no-proxy": "127.0.0.1,localhost,::1,10.0.0.0/8",
        }
    )
    try:
        juju.config("boo", {"log-level": "debug"})
        wait_for_settled(juju)

        environment = get_service_environment(juju)
        assert environment["HTTP_PROXY"] == "http://squid.internal:3128"
        assert environment["http_proxy"] == "http://squid.internal:3128"
        assert environment["HTTPS_PROXY"] == "http://squid.internal:3129"
        assert environment["https_proxy"] == "http://squid.internal:3129"
        assert environment["NO_PROXY"] == "127.0.0.1,localhost,::1,10.0.0.0/8"
        assert environment["no_proxy"] == "127.0.0.1,localhost,::1,10.0.0.0/8"
        # The model has no FTP proxy.
        assert "FTP_PROXY" not in environment
    finally:
        # Clean up: restore the defaults so that subsequent tests start clean.
        juju.model_config(reset=["juju-http-proxy", "juju-https-proxy", "juju-no-proxy"])
        juju.config("boo", reset=["log-level"])
        wait_for_settled(juju)

    # Without the model's proxy settings, the charm must remove the variables that it set before.
    # (The model's no-proxy setting has a default value, so NO_PROXY remains.)
    environment = get_service_environment(juju)
    assert "HTTP_PROXY" not in environment
    assert "HTTPS_PROXY" not in environment
    assert "http_proxy" not in environment
    assert "https_proxy" not in environment


# If you implement boo.get_version in the charm source,
# remove the @pytest.mark.skip line to enable this test.
# Alternatively, remove this test if you don't need it.
@pytest.mark.skip(reason="boo.get_version is not implemented")
def test_workload_version_is_set(charm: pathlib.Path, juju: jubilant.Juju):
    """Check that the correct version of the workload is running."""
    version = juju.status().apps["boo"].version
    assert version == "3.14"  # Replace 3.14 by the expected version of the workload.
