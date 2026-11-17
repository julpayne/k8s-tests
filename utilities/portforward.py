"""`kubectl port-forward` as a context manager, for the API checks.

The harness has no Ingress and no OpenShift Route, so the checks that need to
speak HTTP to a Service forward a local port to it instead.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator

import pytest

LOGGER = logging.getLogger(__name__)

PORT_FORWARD_TIMEOUT = 30.0


def kubectl_binary() -> str:
    """The kubectl to shell out to, skipped when it is not installed."""
    kubectl = os.environ.get("KUBECTL") or "kubectl"
    if shutil.which(kubectl) is None:
        pytest.skip(f"{kubectl} is not installed, so the Service cannot be reached")
    return kubectl


def _wait_for_local_port(port: int, process: subprocess.Popen[str], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            pytest.fail(f"port-forward exited before serving port {port}:\n{output}", pytrace=False)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    pytest.fail(f"port-forward did not serve port {port} within {timeout}s", pytrace=False)


@contextlib.contextmanager
def port_forward(
    context: str,
    namespace: str,
    target: str,
    local_port: int,
    remote_port: int,
    scheme: str = "http",
) -> Iterator[str]:
    """Forward `local_port` to `target` and yield the local base URL."""
    command = [
        kubectl_binary(),
        "--context",
        context,
        "--namespace",
        namespace,
        "port-forward",
        target,
        f"{local_port}:{remote_port}",
    ]
    LOGGER.info("forwarding 127.0.0.1:%d to %s/%s:%d over %s", local_port, namespace, target, remote_port, scheme)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        _wait_for_local_port(local_port, process, PORT_FORWARD_TIMEOUT)
        yield f"{scheme}://127.0.0.1:{local_port}"
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)
        if process.poll() is None:
            process.kill()
