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


def read_report(name: str, port: int = 8000) -> str:
    """Read a plain-text file that the workload serves over HTTP.

    The charm and the workload container share a network namespace, so the workload is at
    'localhost'. The request must never go through the model's proxy.

    Raises:
        OSError: If the workload can't be reached. This includes urllib.error.URLError.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://localhost:{port}/{name}", timeout=5) as response:
        return response.read().decode().strip()
