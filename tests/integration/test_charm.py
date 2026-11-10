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
import time
from collections.abc import Iterator

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


# The tests below check the reconciler pattern for the 'juju-*-proxy' model config. Juju emits no
# event when these settings change, so the charm can only notice them at the next event. The
# tests don't trigger any event on purpose. They only make Juju run 'update-status' often, and
# check that the charm applies the settings from there.

UNIT = "boo/0"
PROXY_MODEL_CONFIG_KEYS = ["juju-http-proxy", "juju-https-proxy", "juju-no-proxy"]
NO_PROXY = "127.0.0.1,localhost,::1,10.0.0.0/8"


def expected_environment(
    http: str | None = None, https: str | None = None, no: str | None = None
) -> dict[str, str]:
    """Return the proxy variables that the workload should have for these model configs."""
    settings = {"HTTP_PROXY": http, "HTTPS_PROXY": https}
    if http or https:
        settings["NO_PROXY"] = no
    environment: dict[str, str] = {}
    for name, value in settings.items():
        if value:
            environment[name] = environment[name.lower()] = value
    return environment


def read_workload(juju: jubilant.Juju) -> tuple[dict[str, str], str]:
    """Ask the workload which proxy variables it started with, and when it started."""
    results = juju.run(UNIT, "show-proxy").results
    proxy, started = results["proxy"], results["started"]
    if proxy == "no proxy configured":
        return {}, started
    return dict(line.split("=", 1) for line in proxy.splitlines()), started


def wait_for_workload(juju: jubilant.Juju, expected: dict[str, str], timeout: float = 180) -> str:
    """Wait until the workload has these proxy variables. Return the time that it started."""
    deadline = time.monotonic() + timeout
    seen: object = None
    while time.monotonic() < deadline:
        try:
            seen, started = read_workload(juju)
            if seen == expected:
                return started
        except jubilant.TaskError as e:
            seen = f"action failed: {e}"  # For example, the workload is restarting.
        time.sleep(3)
    pytest.fail(f"The workload didn't get {expected}. Last seen: {seen}")


@pytest.fixture
def proxy_model_config(juju: jubilant.Juju) -> Iterator[None]:
    """Make Juju run update-status every 10 seconds, and start and end without proxy settings."""
    juju.model_config({"update-status-hook-interval": "10s"})
    juju.model_config(reset=PROXY_MODEL_CONFIG_KEYS)
    wait_for_workload(juju, {})
    yield
    juju.model_config(reset=PROXY_MODEL_CONFIG_KEYS + ["update-status-hook-interval"])


def test_workload_without_proxy(
    charm: pathlib.Path, juju: jubilant.Juju, proxy_model_config: None
):
    """Test that the workload has no proxy variables if the user hasn't set any."""
    assert read_workload(juju)[0] == {}


def test_set_proxy(charm: pathlib.Path, juju: jubilant.Juju, proxy_model_config: None):
    """Test that the workload gets proxy settings that the user sets while it's running."""
    _, started_before = read_workload(juju)

    juju.model_config(
        {
            "juju-http-proxy": "http://squid.internal:3128",
            "juju-https-proxy": "http://squid.internal:3129",
            "juju-no-proxy": NO_PROXY,
        }
    )

    expected = expected_environment(
        "http://squid.internal:3128", "http://squid.internal:3129", NO_PROXY
    )
    assert wait_for_workload(juju, expected) != started_before  # The workload restarted.


def test_unchanged_proxy_does_not_restart_workload(
    charm: pathlib.Path, juju: jubilant.Juju, proxy_model_config: None
):
    """Test that handling events with unchanged settings doesn't restart the workload."""
    juju.model_config({"juju-https-proxy": "http://squid.internal:3129"})
    expected = expected_environment(
        https="http://squid.internal:3129", no="127.0.0.1,localhost,::1"
    )
    started = wait_for_workload(juju, expected)

    # Juju runs update-status every 10 seconds, so the charm handles several events.
    time.sleep(45)

    assert read_workload(juju) == (expected, started)


def test_change_proxy(charm: pathlib.Path, juju: jubilant.Juju, proxy_model_config: None):
    """Test that the workload follows a change to one of the settings."""
    juju.model_config({"juju-http-proxy": "http://squid.internal:3128", "juju-no-proxy": NO_PROXY})
    started_before = wait_for_workload(
        juju, expected_environment("http://squid.internal:3128", no=NO_PROXY)
    )

    juju.model_config({"juju-http-proxy": "http://other.internal:8080"})

    expected = expected_environment("http://other.internal:8080", no=NO_PROXY)
    assert wait_for_workload(juju, expected) != started_before


def test_unset_one_proxy_setting(
    charm: pathlib.Path, juju: jubilant.Juju, proxy_model_config: None
):
    """Test that unsetting one setting removes only that variable."""
    juju.model_config(
        {
            "juju-http-proxy": "http://squid.internal:3128",
            "juju-https-proxy": "http://squid.internal:3129",
            "juju-no-proxy": NO_PROXY,
        }
    )
    wait_for_workload(
        juju,
        expected_environment("http://squid.internal:3128", "http://squid.internal:3129", NO_PROXY),
    )

    juju.model_config(reset=["juju-https-proxy"])

    wait_for_workload(juju, expected_environment("http://squid.internal:3128", no=NO_PROXY))


def test_unset_all_proxy_settings(
    charm: pathlib.Path, juju: jubilant.Juju, proxy_model_config: None
):
    """Test that the workload loses all proxy variables when the user unsets the settings."""
    juju.model_config({"juju-http-proxy": "http://squid.internal:3128", "juju-no-proxy": NO_PROXY})
    started_before = wait_for_workload(
        juju, expected_environment("http://squid.internal:3128", no=NO_PROXY)
    )

    # Without an HTTP or HTTPS proxy, 'juju-no-proxy' (which has a default value) has no effect.
    juju.model_config(reset=PROXY_MODEL_CONFIG_KEYS)

    assert wait_for_workload(juju, {}) != started_before


def test_proxy_settings_apply_without_update_status(
    charm: pathlib.Path, juju: jubilant.Juju, proxy_model_config: None
):
    """Test that any event applies the settings, not only update-status."""
    # Stop Juju from running update-status soon, so that only config-changed can apply them.
    juju.model_config({"update-status-hook-interval": "1h"})
    juju.model_config({"juju-http-proxy": "http://squid.internal:3128"})
    juju.config("boo", {"log-level": "debug"})
    try:
        wait_for_workload(
            juju, expected_environment("http://squid.internal:3128", no="127.0.0.1,localhost,::1")
        )
    finally:
        juju.config("boo", reset=["log-level"])


# If you implement boo.get_version in the charm source,
# remove the @pytest.mark.skip line to enable this test.
# Alternatively, remove this test if you don't need it.
@pytest.mark.skip(reason="boo.get_version is not implemented")
def test_workload_version_is_set(charm: pathlib.Path, juju: jubilant.Juju):
    """Check that the correct version of the workload is running."""
    version = juju.status().apps["boo"].version
    assert version == "3.14"  # Replace 3.14 by the expected version of the workload.
