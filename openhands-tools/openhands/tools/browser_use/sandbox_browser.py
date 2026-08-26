"""Sandbox-backed browser lifecycle.

Launches Chromium INSIDE a Cloud Run sandbox session (via ``sandbox exec``)
so the browser shares the session's network namespace with the dev servers
(localhost:5173/3000 reachability — visual verification) and runs with no
credentials and no egress by default.

The trusted agent-server connects to the in-session Chromium over CDP
(browser-use's BrowserProfile.cdp_url). Transport note: sandboxes expose
no TCP ports to the parent, so the CDP endpoint must be reachable from the
trusted side — the launch uses ``--remote-debugging-port`` on the session's
loopback plus the sandbox CLI's port-forwarding if available, falling back
to a documented spike item (see CDP_TRANSPORT_SPIKE below).
"""

import subprocess
import time

from openhands.sdk.logger import get_logger


logger = get_logger(__name__)

SANDBOX_BIN = "/usr/local/gcp/bin/sandbox"

# Conventional in-session CDP port (Chromium --remote-debugging-port).
CDP_PORT = 9222

# How long to wait for Chromium's CDP endpoint to answer /json/version.
CDP_READY_TIMEOUT_S = 20.0


def _sandbox_exec(session_id: str, args: list[str], timeout: float = 30.0) -> str:
    """Run one command inside the sandbox session; return stdout."""
    cmd = [SANDBOX_BIN, "exec", session_id, "--", *args]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(
            f"sandbox exec failed (exit {result.returncode}): "
            f"{result.stderr.strip()[:500]}"
        )
    return result.stdout


def launch_sandbox_chromium(
    session_id: str,
    chromium_path: str = "/usr/bin/chromium",
    cdp_port: int = CDP_PORT,
) -> str:
    """Launch headless Chromium inside the sandbox session with CDP enabled.

    Returns the CDP URL for browser-use's BrowserProfile.cdp_url.

    The Chromium process is detached inside the session (nohup) so it
    survives this exec invocation; the session is stateful.
    """
    # Kill any stale Chromium from a previous attempt in this session.
    _sandbox_exec(
        session_id,
        ["sh", "-c", "pkill -f remote-debugging-port 2>/dev/null; true"],
    )
    # Launch detached headless Chromium with CDP on the session's loopback.
    _sandbox_exec(
        session_id,
        [
            "sh",
            "-c",
            f"nohup {chromium_path} --headless=new --no-sandbox "
            f"--disable-gpu --disable-dev-shm-usage "
            f"--remote-debugging-address=127.0.0.1 "
            f"--remote-debugging-port={cdp_port} "
            f"about:blank > /tmp/chromium.log 2>&1 & echo $! > /tmp/chromium.pid",
        ],
    )
    # Wait for the CDP endpoint to answer.
    deadline = time.time() + CDP_READY_TIMEOUT_S
    while time.time() < deadline:
        try:
            out = _sandbox_exec(
                session_id,
                ["sh", "-c", f"curl -sf http://127.0.0.1:{cdp_port}/json/version"],
                timeout=5.0,
            )
            if out.strip():
                logger.info(
                    f"sandbox chromium ready (session={session_id}, port={cdp_port})"
                )
                return f"http://127.0.0.1:{cdp_port}"
        except RuntimeError:
            pass  # not up yet
        time.sleep(0.5)
    # Diagnose: dump the chromium log so the failure is visible.
    try:
        log = _sandbox_exec(session_id, ["sh", "-c", "tail -20 /tmp/chromium.log"])
    except RuntimeError:
        log = "(log unavailable)"
    raise RuntimeError(
        f"sandbox chromium did not expose CDP within {CDP_READY_TIMEOUT_S}s "
        f"(session={session_id}, port={cdp_port}). Chromium log:\n{log}"
    )


def stop_sandbox_chromium(session_id: str) -> None:
    """Best-effort: kill the in-session Chromium."""
    try:
        _sandbox_exec(
            session_id,
            ["sh", "-c", "kill $(cat /tmp/chromium.pid) 2>/dev/null; true"],
        )
    except RuntimeError as e:
        logger.debug(f"chromium stop: {e}")


# ── CDP TRANSPORT SPIKE (Phase 0) ──────────────────────────────────────────
# Sandboxes expose NO TCP ports to the parent instance. The CDP URL above
# (127.0.0.1:9222) is reachable INSIDE the session (dev servers + Chromium
# share the namespace) but the trusted agent-server lives OUTSIDE it.
# Options to validate in the Phase 0 spike:
#   1. Does the sandbox CLI offer port-forwarding to the parent?
#   2. Does CDP-over-unix-socket work (Chromium --remote-debugging-pipe +
#      browser-use cdp_url accepting a pipe transport)?
#   3. Can socat in the session bridge CDP to a channel the CLI exposes?
# Until resolved, visual verification on Cloud Run uses the artifact model:
# screenshots are captured INSIDE the session and exported (migration plan §4).
