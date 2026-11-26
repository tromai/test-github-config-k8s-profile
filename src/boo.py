# Copyright 2026 Test Author
# See LICENSE file for licensing details.

"""Functions for interacting with the workload.

The intention is that this module could be used outside the context of a charm.
"""

import logging
import urllib.request

logger = logging.getLogger(__name__)


# Functions for interacting with the workload, for example over HTTP:


def get_version() -> str | None:
    """Get the running version of the workload."""
    # You'll need to implement this function (or remove it if not needed).
    return None


def get_proxy(port: int) -> str:
    """Get the proxy configs that the proxy-display service reports."""
    response = urllib.request.urlopen(f"http://localhost:{port}")
    return response.read().decode()
