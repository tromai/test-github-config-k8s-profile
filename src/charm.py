#!/usr/bin/env python3
# Copyright 2026 Test Author
# See LICENSE file for licensing details.

"""Charm the application."""

import datetime
import logging
import secrets
import time

import ops

# A standalone module for workload-specific logic (no charming concerns):
import boo

logger = logging.getLogger(__name__)

SERVICE_NAME = "some-service"  # Name of Pebble service that runs in the workload container.

# The workload is a tiny web server. When it starts, it writes the proxy variables from its own
# environment to 'proxy.txt', and the time to 'started.txt', then serves both files over HTTP. The
# files are plain text, so that the integration tests can see which proxy settings the running
# workload actually received, and whether it was restarted.
WORKLOAD_PORT = 8000
WORKLOAD_COMMAND = (
    "/bin/sh -c '"
    "mkdir -p /srv/www && cd /srv/www && date +%s%N > started.txt"
    " && { env | grep -i _proxy= | sort > proxy.txt || true; }"
    " && { [ -s proxy.txt ] || echo no proxy configured > proxy.txt; }"
    f" && exec python3 -m http.server {WORKLOAD_PORT}'"
)

# This charm demonstrates the two independent roles a charm plays with Juju secrets.
# The two secrets below are unrelated: one is observed, the other is owned.
#  - api-token: granted by the user and referenced in a config option.
#  - workload-password: created and rotated by this charm for its own workload.
API_TOKEN_LABEL = "api-token"  # Any label works; matching the config option name is clearest.
APP_SECRET_LABEL = "workload-password"


def proxy_environment(proxy: ops.ProxyConfig) -> dict[str, str]:
    """Convert the model's proxy settings to environment variables for the workload.

    Tools disagree on the case of the variable names (curl only honours the lower-case
    'http_proxy', for example), so each setting is provided in both cases. Settings that the
    model doesn't provide are left out.

    The 'juju-no-proxy' model config has a default value, so it's always present. It only has an
    effect when there's a proxy, so it's left out unless the model has an HTTP or HTTPS proxy.
    """
    settings = {"HTTP_PROXY": proxy.http, "HTTPS_PROXY": proxy.https}
    if proxy.http or proxy.https:
        settings["NO_PROXY"] = ",".join(proxy.no)
    environment: dict[str, str] = {}
    for name, value in settings.items():
        if value:
            environment[name] = value
            environment[name.lower()] = value
    return environment


class BooCharm(ops.CharmBase):
    """Charm the application."""

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        framework.observe(self.on["some_container"].pebble_ready, self._on_pebble_ready)
        framework.observe(self.on.upgrade_charm, self._on_upgrade_charm)
        framework.observe(self.on.update_status, self._on_update_status)
        framework.observe(self.on["restart"].action, self._on_restart_action)
        framework.observe(self.on["show-proxy"].action, self._on_show_proxy_action)
        framework.observe(self.on.config_changed, self._on_config_changed)
        framework.observe(self.on.secret_changed, self._on_secret_changed)
        framework.observe(self.on.secret_rotate, self._on_secret_rotate)
        framework.observe(self.on.secret_expired, self._on_secret_expired)
        framework.observe(self.on.secret_remove, self._on_secret_remove)
        self.container = self.unit.get_container("some-container")

    def _build_layer(self) -> ops.pebble.LayerDict:
        """Build the Pebble layer that runs the workload."""
        # The layer specifies which service to run.
        # The charm expects the service to have a long-running command. Don't use a command that
        # starts the workload as a daemon then exits, otherwise Pebble either reports the service
        # as stopped (if the command exits quickly) or keeps restarting the service.
        return {
            "services": {
                SERVICE_NAME: {
                    "override": "replace",
                    "summary": "A web server that shows its proxy settings",
                    "command": WORKLOAD_COMMAND,
                    "startup": "enabled",
                    # Pass the model's proxy settings (the 'juju-*-proxy' model config) to the
                    # workload. Because the layer replaces the service, a setting that the model
                    # no longer provides disappears from the service's environment.
                    "environment": proxy_environment(self.model.proxy_config),
                }
            }
        }

    def _update_workload(self) -> None:
        """Add the Pebble layer to the workload container and apply any changes."""
        self.container.add_layer("base", self._build_layer(), combine=True)
        # If the container image is a rock, the container already has a Pebble layer.
        # In this case, you could remove 'add_layer' or use 'add_layer' to extend the rock's layer.
        # To learn about rocks, see https://ubuntu.com/containers/rockcraft/docs/
        # 'replan' starts the service (because 'startup' is enabled in the layer). If the service
        # is already running, 'replan' restarts it only if the layer changed.
        self.container.replan()

    def _on_pebble_ready(self, event: ops.PebbleReadyEvent):
        """Handle pebble-ready event."""
        self.unit.status = ops.MaintenanceStatus("starting workload")
        self._update_workload()
        self.wait_for_ready()
        version = boo.get_version()
        if version is not None:
            self.unit.set_workload_version(version)
        # Create the app-managed workload secret on first start (leader only).
        if self.unit.is_leader():
            secret = self._ensure_app_secret()
            # A secret's owner reads the latest revision with 'peek_content', not 'get_content'.
            password = secret.peek_content()["password"]
            self._configure_workload_password(password)
        # A non-leader unit should get the secret ID from a peer relation, then use
        # 'get_content' to read the latest revision. This charm doesn't demonstrate that; see
        # https://canonical.com/juju/docs/ops/latest/howto/manage-secrets/
        self.unit.status = ops.ActiveStatus()

    def _on_upgrade_charm(self, event: ops.UpgradeCharmEvent):
        """Handle upgrade-charm event — the model's proxy settings might have changed."""
        self._reconcile()

    def _on_update_status(self, event: ops.UpdateStatusEvent):
        """Handle update-status event — pick up changes to the model's proxy settings."""
        # Juju doesn't emit an event when the 'juju-*-proxy' model config changes, so the next
        # event of any kind is the earliest that the charm can notice. Juju emits update-status
        # periodically, which bounds how long the workload can use outdated settings.
        self._reconcile()

    def _reconcile(self) -> None:
        """Make the workload match the model's current proxy settings, if the container is up."""
        # Call this from every handler that might run after the settings changed. It's safe to
        # call often: if the layer is unchanged, 'replan' leaves the running service alone. If
        # Pebble isn't ready yet, the pebble-ready handler applies the settings.
        if not self.container.can_connect():
            return
        self._update_workload()
        self.wait_for_ready()

    def _on_restart_action(self, event: ops.ActionEvent):
        """Handle the restart action."""
        self.unit.status = ops.MaintenanceStatus("restarting workload")
        try:
            self.container.restart(SERVICE_NAME)
            self.wait_for_ready()
        except ops.pebble.ConnectionError:
            # Pebble isn't running yet, or the container is going away.
            # Checking with 'can_connect' before restarting wouldn't help: the
            # container can stop being reachable between the check and the restart.
            self.unit.status = ops.WaitingStatus("waiting for workload container")
            event.fail("cannot connect to workload container")
            return
        self.unit.status = ops.ActiveStatus()

    def _on_show_proxy_action(self, event: ops.ActionEvent):
        """Handle the show-proxy action: report the proxy settings that the workload received."""
        try:
            event.set_results(
                {
                    "proxy": boo.read_report("proxy.txt"),
                    "started": boo.read_report("started.txt"),
                }
            )
        except OSError as e:
            event.fail(f"cannot read from the workload: {e}")

    def _on_config_changed(self, event: ops.ConfigChangedEvent):
        """Handle config-changed event — api-token might now hold a different secret's ID."""
        self._reconcile()
        self._configure_api_token()

    def _on_secret_changed(self, event: ops.SecretChangedEvent):
        """Handle secret-changed event — the api-token secret has a new revision."""
        if event.secret.label == API_TOKEN_LABEL:
            self._configure_api_token()

    def _configure_api_token(self) -> None:
        """Read the user-provided api-token secret and configure the workload with it."""
        api_token_id = self.config.get("api-token")
        # A 'secret' config option is always a string, but self.config is typed as the union
        # of every config type, so narrow it here to satisfy static type checks.
        if not isinstance(api_token_id, str):
            return
        try:
            # Attaching a label lets the secret-changed handler recognise this secret.
            secret = self.model.get_secret(id=api_token_id, label=API_TOKEN_LABEL)
            # 'refresh' tells Juju to start tracking the latest revision.
            content = secret.get_content(refresh=True)
        except ops.SecretNotFoundError:
            self.unit.status = ops.BlockedStatus("api-token secret not found or not granted")
            return
        if "token" not in content:
            self.unit.status = ops.BlockedStatus("api-token secret has no 'token' key")
            return
        token = content["token"]
        # Reconfiguring the workload can be slow, so tell the user what's happening.
        self.unit.status = ops.MaintenanceStatus("reconfiguring workload")
        logger.info("api-token resolved (%d chars); reconfiguring workload", len(token))
        # Use 'token' to configure your workload, for example:
        # boo.configure(api_token=token)
        self.unit.status = ops.ActiveStatus()

    def _ensure_app_secret(self) -> ops.Secret:
        """Get or create the app-managed workload password (idempotent, leader only)."""
        try:
            return self.model.get_secret(label=APP_SECRET_LABEL)
        except ops.SecretNotFoundError:
            return self.app.add_secret(
                {"password": secrets.token_hex(32)},
                label=APP_SECRET_LABEL,
                rotate=ops.SecretRotate.MONTHLY,
                expire=datetime.timedelta(days=90),
            )

    def _configure_workload_password(self, password: str) -> None:
        """Tell the workload which password it should accept."""
        # Use 'password' to configure your workload, for example:
        # boo.configure(workload_password=password)
        # If this 'configure' call is slow, set maintenance status before the call.
        logger.info("workload password configured (%d chars)", len(password))

    def _on_secret_rotate(self, event: ops.SecretRotateEvent):
        """Handle secret-rotate event — the rotation policy says it's time for a new password."""
        if event.secret.label == APP_SECRET_LABEL:
            self._set_new_password(event.secret)

    def _on_secret_expired(self, event: ops.SecretExpiredEvent):
        """Handle secret-expired event — the current password must not be used any more."""
        if event.secret.label == APP_SECRET_LABEL:
            self._set_new_password(event.secret)

    def _set_new_password(self, secret: ops.Secret) -> None:
        """Add a new revision of the app-managed workload password."""
        password = secrets.token_hex(32)
        # Don't remove the old revision here: other units might still be using it.
        # Juju emits secret-remove once no unit is tracking it any more.
        secret.set_content({"password": password})
        self._configure_workload_password(password)

    def _on_secret_remove(self, event: ops.SecretRemoveEvent):
        """Handle secret-remove event — no unit is using this revision any more."""
        # Without this, every rotation leaves another revision behind.
        if event.secret.label == APP_SECRET_LABEL:
            event.remove_revision()

    def is_ready(self) -> bool:
        """Check whether the workload is ready to use."""
        # We'll first check whether all Pebble services are running.
        for name, service_info in self.container.get_services().items():
            if not service_info.is_running():
                logger.info("the workload is not ready (service '%s' is not running)", name)
                return False
        # The Pebble services are running, but the workload might not be ready to use.
        # So we'll check whether all Pebble 'ready' checks are passing.
        checks = self.container.get_checks(level=ops.pebble.CheckLevel.READY)
        for check_info in checks.values():
            if check_info.status != ops.pebble.CheckStatus.UP:
                return False
        return True

    def wait_for_ready(self) -> None:
        """Wait for the workload to be ready to use."""
        for _ in range(3):
            if self.is_ready():
                return
            time.sleep(1)
        logger.error("the workload was not ready within the expected time")
        raise RuntimeError("workload is not ready")
        # The runtime error is for you (the charm author) to see, not for the user of the charm.
        # Make sure that this function waits long enough for the workload to be ready.


if __name__ == "__main__":  # pragma: nocover
    ops.main(BooCharm)
