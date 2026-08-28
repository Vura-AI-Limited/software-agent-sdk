"""Sandbox-backed browser lifecycle.

Launches Chromium INSIDE a Cloud Run sandbox session (via ``sandbox exec``)
so the browser shares the session's network namespace with the dev servers
(localhost:5173/3000 reachability — visual verification) and runs with no
credentials and no egress by default.

The trusted agent-server connects to the in-session Chromium over CDP
(browser-use's BrowserProfile.cdp_url). Sandboxes expose NO TCP ports to the
parent, so the CDP endpoint is bridged over ``sandbox exec`` stdio by
:mod:`sandbox_cdp_relay` — spike option 3, now implemented. ``launch_sandbox
_chromium`` returns the relay's URL on the trusted side, not the in-session
one, so browser-use can dial it like any ordinary CDP endpoint.
"""

import subprocess
import time

from openhands.sdk.logger import get_logger

from openhands.tools.browser_use.sandbox_cdp_relay import SandboxCdpRelay


logger = get_logger(__name__)

SANDBOX_BIN = "/usr/local/gcp/bin/sandbox"

# Conventional in-session CDP port (Chromium --remote-debugging-port).
CDP_PORT = 9222

# How long to wait for Chromium's CDP endpoint to answer /json/version.
CDP_READY_TIMEOUT_S = 20.0

# Live relays keyed by session id, so stop_sandbox_chromium() can close the
# listener. A leaked listener would hold port 9222 and make the NEXT session's
# relay fail to bind (see SandboxCdpRelay.start).
_RELAYS: dict[str, SandboxCdpRelay] = {}


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

    Returns the CDP URL for browser-use's BrowserProfile.cdp_url. That URL
    points at the stdio relay on the TRUSTED side, which forwards into the
    session — the in-session port is unreachable from here (no sandbox ports).

    The Chromium process is detached inside the session (nohup) so it
    survives this exec invocation; the session is stateful.
    """
    # socat carries CDP across the sandbox boundary. Without it the browser
    # silently has no transport, which is exactly the failure this replaces,
    # so check up front and say so plainly.
    try:
        _sandbox_exec(
            session_id,
            ["/bin/sh", "-c",
             "export PATH=/usr/local/bin:/usr/bin:/bin; command -v socat"],
            timeout=10.0,
        )
    except RuntimeError as e:
        raise RuntimeError(
            "socat is not available inside the sandbox session; the CDP relay "
            "cannot bridge the browser to the trusted side. Install socat in "
            f"the ticket-runner image rootfs. ({e})"
        ) from e
    # Kill any stale Chromium from a previous attempt in this session.
    #
    # The bracket in "[r]emote-debugging-port" is load-bearing. `pkill -f`
    # matches full command lines, and this pkill's OWN command line contains
    # the pattern, so an unbracketed pattern makes pkill SIGTERM its own shell:
    # the exec died with 143 every single time. The bracketed character class
    # matches the running Chromium (whose argv has the literal text) but not
    # this command line (which has the brackets).
    _sandbox_exec(
        session_id,
        ["/bin/sh", "-c", "export PATH=/usr/local/bin:/usr/bin:/bin; pkill -f '[r]emote-debugging-port' 2>/dev/null; true"],
    )
    # Launch detached headless Chromium with CDP on the session's loopback.
    _sandbox_exec(
        session_id,
        [
            "/bin/sh",
            "-c",
            f"export PATH=/usr/local/bin:/usr/bin:/bin; "
            # HOME is unset inside the sandbox (the CLI strips the environment),
            # so Chromium tried to build its profile under a path it could not
            # create and died with "Failed to create headless user data
            # directory container". Both the profile and the crash-dump dir must
            # be pointed at somewhere writable, and /tmp is the session's only
            # guaranteed-writable location.
            f"export HOME=/tmp; mkdir -p /tmp/chromium-profile; "
            f"nohup {chromium_path} --headless=new --no-sandbox "
            f"--disable-gpu --disable-dev-shm-usage "
            f"--user-data-dir=/tmp/chromium-profile "
            f"--crash-dumps-dir=/tmp/chromium-profile/crashes "
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
                ["/bin/sh", "-c", f"export PATH=/usr/local/bin:/usr/bin:/bin; curl -sf http://127.0.0.1:{cdp_port}/json/version"],
                timeout=5.0,
            )
            if out.strip():
                logger.info(
                    f"sandbox chromium ready (session={session_id}, port={cdp_port})"
                )
                # Chromium is up INSIDE the session. Stand up the relay so the
                # trusted side has something to dial, and hand back its URL.
                relay = SandboxCdpRelay(session_id, cdp_port)
                url = relay.start()
                _RELAYS[session_id] = relay
                return url
        except RuntimeError:
            pass  # not up yet
        time.sleep(0.5)
    # Diagnose: dump the chromium log so the failure is visible.
    try:
        log = _sandbox_exec(session_id, ["/bin/sh", "-c", "export PATH=/usr/local/bin:/usr/bin:/bin; tail -20 /tmp/chromium.log"])
    except RuntimeError:
        log = "(log unavailable)"
    raise RuntimeError(
        f"sandbox chromium did not expose CDP within {CDP_READY_TIMEOUT_S}s "
        f"(session={session_id}, port={cdp_port}). Chromium log:\n{log}"
    )


def stop_sandbox_chromium(session_id: str) -> None:
    """Best-effort: kill the in-session Chromium and close its relay."""
    relay = _RELAYS.pop(session_id, None)
    if relay is not None:
        relay.stop()
    try:
        _sandbox_exec(
            session_id,
            ["/bin/sh", "-c", "export PATH=/usr/local/bin:/usr/bin:/bin; kill $(cat /tmp/chromium.pid) 2>/dev/null; true"],
        )
    except RuntimeError as e:
        logger.debug(f"chromium stop: {e}")


# ── CDP TRANSPORT (Phase 0 spike — RESOLVED) ───────────────────────────
# Sandboxes expose NO TCP ports to the parent instance, so the in-session CDP
# port is not directly dialable from the trusted agent-server. Of the three
# candidate transports:
#   1. sandbox CLI port-forwarding — no such subcommand exists.
#   2. CDP-over-unix-socket (--remote-debugging-pipe) — browser-use's cdp_url
#      takes an http/ws URL only; it has no pipe transport.
#   3. socat bridging CDP over `sandbox exec` stdio — IMPLEMENTED, see
#      sandbox_cdp_relay.py. stdio is already proven byte-exact across the
#      boundary (the workspace tarball streams over it).
# The artifact fallback described in migration plan §4 is no longer needed:
# the agent drives a real browser and sees real pages with its own vision.
