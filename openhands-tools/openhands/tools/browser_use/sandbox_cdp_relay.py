"""CDP stdio relay: trusted side ⇄ in-sandbox Chromium.

Cloud Run sandboxes expose NO TCP ports to the parent instance, so the
trusted agent-server cannot dial the in-session Chromium's
``--remote-debugging-port`` directly. Every browser action therefore failed
to start and ``BrowserToolSet.create()`` returned no tools at all — the agent
was told to verify the UI with a browser it did not have.

This is spike option 3 from ``sandbox_browser.py``: the only channel the
sandbox CLI gives us across the boundary is ``sandbox exec`` stdio, which is
already proven bidirectional and byte-exact (the workspace tarball is streamed
in and out over it). So we bridge TCP over that channel:

    browser-use ──TCP──▶ relay listener ──stdio──▶ sandbox exec socat ──▶ :9222

PORT NUMBER IS NOT ARBITRARY. CDP's ``/json/version`` advertises a
``webSocketDebuggerUrl`` containing the port Chromium itself is bound to
(9222). browser-use dials that URL verbatim, so the relay MUST listen on the
same number on the trusted side or the websocket upgrade lands nowhere. The
two 9222s never collide: they are in different network namespaces.

Each accepted connection gets its own ``sandbox exec`` — CDP opens a fresh
websocket per target/page, and multiplexing them over one pipe would interleave
frames.
"""

import socket
import subprocess
import threading

from openhands.sdk.logger import get_logger


logger = get_logger(__name__)

SANDBOX_BIN = "/usr/local/gcp/bin/sandbox"

# Size of each pump read. CDP screenshot frames are megabytes; a small buffer
# just means more syscalls, never truncation.
_CHUNK = 65536


def _pump(read_fn, write_fn, flush_fn, label: str) -> None:
    """Copy one direction until EOF, then signal the peer by closing.

    A relay that dies silently would leave browser-use hanging on a socket
    that will never answer, so the reason is always logged.
    """
    try:
        while True:
            chunk = read_fn(_CHUNK)
            if not chunk:
                break
            write_fn(chunk)
            flush_fn()
    except (OSError, ValueError) as e:
        logger.debug(f"cdp relay {label} closed: {e}")


class SandboxCdpRelay:
    """Listens on the trusted side and relays each connection into the sandbox.

    Start with :meth:`start`, which returns the CDP base URL to hand to
    browser-use. The listener thread is a daemon: it must never hold the
    agent-server open past shutdown.
    """

    def __init__(self, session_id: str, cdp_port: int) -> None:
        self._session_id = session_id
        self._cdp_port = cdp_port
        self._listener: socket.socket | None = None
        self._stop = threading.Event()

    def start(self) -> str:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Bind the SAME port Chromium advertises — see the module docstring.
        # If this is taken, a previous relay leaked and reusing it would send
        # CDP traffic into the wrong sandbox, so the failure must be loud.
        try:
            listener.bind(("127.0.0.1", self._cdp_port))
        except OSError as e:
            listener.close()
            raise RuntimeError(
                f"cdp relay cannot bind 127.0.0.1:{self._cdp_port} on the "
                f"trusted side ({e}). The port must match the one Chromium "
                f"advertises in webSocketDebuggerUrl; a leaked relay from a "
                f"previous session is the likely cause."
            ) from e
        listener.listen(8)
        self._listener = listener

        threading.Thread(
            target=self._accept_loop,
            name=f"cdp-relay-{self._session_id}",
            daemon=True,
        ).start()
        logger.info(
            f"cdp relay listening on 127.0.0.1:{self._cdp_port} "
            f"→ sandbox {self._session_id}:{self._cdp_port}"
        )
        return f"http://127.0.0.1:{self._cdp_port}"

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except OSError:
                break  # listener closed by stop()
            threading.Thread(
                target=self._serve, args=(conn,), daemon=True
            ).start()

    def _serve(self, conn: socket.socket) -> None:
        """Bridge one TCP connection to one `sandbox exec socat` process."""
        proc = None
        try:
            proc = subprocess.Popen(
                [
                    SANDBOX_BIN,
                    "exec",
                    self._session_id,
                    "--",
                    "/bin/sh",
                    "-c",
                    "export PATH=/usr/local/bin:/usr/bin:/bin; "
                    f"exec socat STDIO TCP:127.0.0.1:{self._cdp_port}",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert proc.stdin and proc.stdout

            up = threading.Thread(
                target=_pump,
                args=(conn.recv, proc.stdin.write, proc.stdin.flush, "→sandbox"),
                daemon=True,
            )
            up.start()
            _pump(proc.stdout.read1, conn.sendall, lambda: None, "←sandbox")
            up.join(timeout=5)
        except Exception as e:
            logger.warning(f"cdp relay connection failed: {e}")
        finally:
            try:
                conn.close()
            except OSError:
                pass
            if proc is not None:
                # socat exits when either side closes; only kill a straggler.
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                if proc.returncode not in (0, None):
                    err = (proc.stderr.read().decode(errors="replace")[:300]
                           if proc.stderr else "")
                    if err:
                        logger.debug(f"cdp relay socat exit "
                                     f"{proc.returncode}: {err}")

    def stop(self) -> None:
        self._stop.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
