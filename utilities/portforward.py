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

# Local ports this session has reserved for forwards. A run can hold several
# forwarded Services at once - a test may create its own workload in its own
# namespace - and two forwards cannot share a local port.
_ALLOCATED_PORTS: set[int] = set()
_PORT_SEARCH_LIMIT = 200


def kubectl_binary() -> str:
    """The kubectl to shell out to, skipped when it is not installed."""
    kubectl = os.environ.get("KUBECTL") or "kubectl"
    if shutil.which(kubectl) is None:
        pytest.skip(f"{kubectl} is not installed, so the Service cannot be reached")
    return kubectl


def _port_is_free(port: int) -> bool:
    """Whether nothing is bound to the port on the loopback interface."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def allocate_local_ports(*bases: int) -> tuple[int, ...]:
    """Reserve free local ports for the forwards, starting at the requested bases.

    The first caller gets the ports the environment names, so a run with one
    forwarded Service keeps the documented pair; a second Service - a check that
    created its own workload - gets the next free pair. A port already reserved for
    this session, or bound by something else on the host, is skipped.
    """
    for offset in range(_PORT_SEARCH_LIMIT):
        candidate = tuple(base + offset for base in bases)
        if any(port in _ALLOCATED_PORTS for port in candidate):
            continue
        if not all(_port_is_free(port) for port in candidate):
            continue
        _ALLOCATED_PORTS.update(candidate)
        LOGGER.info("reserved local ports %s for port-forwards", candidate)
        return candidate
    pytest.fail(
        f"no free local ports near {bases} after {_PORT_SEARCH_LIMIT} attempts; "
        "set TRUSTYAI_SERVICE_API_PORT and TRUSTYAI_SERVICE_HEALTH_PORT to other bases",
        pytrace=False,
    )


def release_local_ports(*ports: int) -> None:
    """Give a reservation back, so a later Service can reuse the ports."""
    _ALLOCATED_PORTS.difference_update(ports)


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
