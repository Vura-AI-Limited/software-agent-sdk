"""Sandbox-based terminal backend implementation.

Runs the interactive terminal INSIDE a Cloud Run sandbox session (via the
sandbox CLI at /usr/local/gcp/bin/sandbox), driven from the trusted
agent-server process. The pattern mirrors TmuxTerminal, but every tmux
operation is prefixed with ``sandbox exec <session> --`` so the shell, the
command under test, and all its output live inside the untrusted sandbox
(no credentials, no egress by default).

The tmux server itself runs inside the sandbox session on a dedicated
socket, so panes, history, and running commands survive across individual
``sandbox exec`` invocations (the session is stateful).
"""

import re
import subprocess
import time
import uuid
from collections.abc import Mapping

from openhands.sdk.logger import get_logger
from openhands.tools.terminal.constants import (
    HISTORY_LIMIT,
    TMUX_SESSION_HEIGHT,
    TMUX_SESSION_WIDTH,
)
from openhands.tools.terminal.env import (
    build_terminal_env,
    normalize_terminal_env,
)
from openhands.tools.terminal.metadata import CmdOutputMetadata
from openhands.tools.terminal.terminal import TerminalInterface
from openhands.tools.terminal.terminal.interface import parse_ctrl_key


logger = get_logger(__name__)

# Path to the sandbox CLI (auto-mounted by --sandbox-launcher).
SANDBOX_BIN = "/usr/local/gcp/bin/sandbox"

# Dedicated tmux socket INSIDE the sandbox session — isolated from any
# host tmux and from other sandbox sessions.
SOCKET_NAME = "openhands-sandbox"

# Map normalized special key names to tmux key names (same as TmuxTerminal).
_SANDBOX_SPECIALS: dict[str, str] = {
    "ENTER": "Enter",
    "TAB": "Tab",
    "BS": "BSpace",
    "ESC": "Escape",
    "UP": "Up",
    "DOWN": "Down",
    "LEFT": "Left",
    "RIGHT": "Right",
    "HOME": "Home",
    "END": "End",
    "PGUP": "PPage",
    "PGDN": "NPage",
    "C-L": "C-l",
    "C-D": "C-d",
    "C-C": "C-c",
}


def _sandbox_exec(session_id: str, args: list[str], timeout: float = 30.0) -> str:
    """Run one command inside the sandbox session; return stdout.

    Raises on non-zero exit so callers fail loudly (no silent fallbacks).
    """
    cmd = [SANDBOX_BIN, "exec", session_id, "--", *args]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"sandbox exec failed (exit {result.returncode}): "
            f"{result.stderr.strip()[:500]}"
        )
    return result.stdout


class SandboxTerminal(TerminalInterface):
    """Sandbox-backed terminal.

    The tmux server + shell run INSIDE the Cloud Run sandbox session; this
    class drives them from the trusted side via ``sandbox exec``. Every
    command the agent runs therefore executes in the untrusted sandbox with
    no access to credentials or the metadata server.
    """

    PS1: str
    _session_id: str
    _pane_id: str
    _env: Mapping[str, str]

    def __init__(
        self,
        work_dir: str,
        username: str | None = None,
        env: Mapping[str, str] | None = None,
        sandbox_session_id: str | None = None,
    ):
        super().__init__(work_dir, username)
        self.PS1 = CmdOutputMetadata.to_ps1_prompt()
        self._env = normalize_terminal_env(env)
        # The Cloud Run sandbox session id. Defaults to the conventional
        # per-ticket session name; the runner passes the real id explicitly.
        self._session_id = sandbox_session_id or "agent-workspace"
        self._pane_id = ""

    # ── sandbox CLI helpers ────────────────────────────────────────────

    def _tmux(self, *args: str, timeout: float = 30.0) -> str:
        """Run a tmux command against the in-sandbox tmux server."""
        return _sandbox_exec(
            self._session_id,
            ["/usr/bin/tmux", "-L", SOCKET_NAME, *args],
            timeout=timeout,
        )

    def _tmux_ok(self, *args: str, timeout: float = 30.0) -> bool:
        """Run a tmux command; return True on exit 0 (no raise)."""
        try:
            self._tmux(*args, timeout=timeout)
            return True
        except RuntimeError as e:
            logger.debug(f"tmux command failed (ok-path): {e}")
            return False

    # ── TerminalInterface ──────────────────────────────────────────────

    def initialize(self) -> None:
        """Start the tmux server + a bash pane inside the sandbox session."""
        if self._initialized:
            return

        env = build_terminal_env(self._env)
        env.setdefault("GIT_PAGER", "cat")
        env.setdefault("PAGER", "cat")

        session_name = f"openhands-{uuid.uuid4().hex[:8]}"
        # Export env vars into the in-sandbox shell via the pane command.
        env_exports = " ".join(f"{k}='{v}'" for k, v in env.items())

        # Create the session with a bash pane. The shell starts in the
        # sandbox session's workspace dir.
        self._tmux(
            "new-session",
            "-d",
            "-s", session_name,
            "-x", str(TMUX_SESSION_WIDTH),
            "-y", str(TMUX_SESSION_HEIGHT),
            f"cd '{self.work_dir}' && {env_exports} exec /bin/bash",
        )
        self._tmux("set-option", "-t", session_name, "history-limit", str(HISTORY_LIMIT))

        # New window with the configured history limit (same dance as
        # TmuxTerminal — the initial pane inherits the old default).
        self._tmux(
            "new-window",
            "-t", session_name,
            "-n", "terminal",
            "-c", self.work_dir,
            f"{env_exports} exec /bin/bash",
        )
        # Capture the active pane id (%N) for later targeting.
        pane_list = self._tmux("list-panes", "-t", session_name, "-F", "#{pane_id}")
        self._pane_id = pane_list.strip().splitlines()[-1].strip()
        self._tmux("kill-window", "-t", f"{session_name}:0")

        # Simple PS1, no PS2, no history expansion (same as TmuxTerminal).
        self._tmux(
            "send-keys",
            "-t", self._pane_id,
            f'set +H; export PROMPT_COMMAND=\'export PS1="{self.PS1}"\'; export PS2=""',
            "Enter",
        )
        time.sleep(0.1)

        self._session_name = session_name
        self._initialized: bool = True
        self.clear_screen()
        logger.debug(
            f"Sandbox terminal initialized (session={self._session_id}, "
            f"tmux={session_name}, pane={self._pane_id})"
        )

    _session_name: str

    def close(self) -> None:
        """Kill the in-sandbox tmux server (the sandbox session itself is
        managed by the ticket-runner's lifecycle, not per-terminal)."""
        if self._closed:
            return
        try:
            self._tmux_ok("kill-server")
        except Exception as e:
            logger.debug(f"Error closing sandbox tmux (may already be dead): {e}")
        self._closed: bool = True

    def send_keys(self, text: str, enter: bool = True) -> None:
        """Send text/keys to the in-sandbox tmux pane.

        Supports the same key vocabulary as TmuxTerminal: named specials,
        generic Ctrl sequences, and literal text.
        """
        if not self._initialized:
            raise RuntimeError("Sandbox terminal is not initialized")

        upper = text.strip().upper()

        # 1) Named specials
        if upper in _SANDBOX_SPECIALS:
            self._tmux("send-keys", "-t", self._pane_id, _SANDBOX_SPECIALS[upper])
            return

        # 2) Generic Ctrl-<letter>
        ctrl = parse_ctrl_key(text)
        if ctrl is not None:
            self._tmux("send-keys", "-t", self._pane_id, ctrl)
            return

        # 3) Plain text — literal paste (tmux -L inside the sandbox).
        #
        # DO NOT shell-quote. `_sandbox_exec` runs
        #     subprocess.run([SANDBOX_BIN, "exec", id, "--", *args])
        # with a LIST and no shell, and `sandbox exec` passes argv straight
        # to execve. There is no shell anywhere on this path to strip
        # quotes, so wrapping the text in `'...'` made tmux paste the quote
        # characters themselves. bash then saw one giant word:
        #
        #   bash: cd ./api && npm install --no-audit --no-fund:
        #         No such file or directory        (exit 127)
        #
        # Every multi-word command the agent typed failed that way, and the
        # message reads as a MISSING FILE, so the agent concluded its tools
        # were absent and went hunting for them — `which npm`, then
        # `find / -name npm`, which failed identically. Observed 2026-09-03:
        # ~11 minutes of a ticket's budget spent before it recovered.
        #
        # `-l` already means "literal": tmux does not split or interpret the
        # argument, which is exactly what the quoting was meant to achieve.
        # TmuxTerminal (the working backend) passes literal=True and adds no
        # quoting of its own.
        self._tmux("send-keys", "-t", self._pane_id, "-l", text)
        if enter and not text.endswith("\n"):
            self._tmux("send-keys", "-t", self._pane_id, "Enter")

    def read_screen(self) -> str:
        """Read the in-sandbox pane content (visible screen + history)."""
        if not self._initialized:
            raise RuntimeError("Sandbox terminal is not initialized")
        content = self._tmux(
            "capture-pane", "-t", self._pane_id, "-J", "-pS", "-",
        )
        # Same newline hygiene as TmuxTerminal.
        return "\n".join(line.rstrip() for line in content.splitlines())

    def clear_screen(self) -> None:
        """Clear the pane via the `clear` command (avoids C-l leakage)."""
        if not self._initialized:
            return
        self._tmux("send-keys", "-t", self._pane_id, "clear", "Enter")
        time.sleep(0.1)
        self._tmux("send-keys", "-t", self._pane_id, "Enter")

    def interrupt(self) -> bool:
        """Send C-c to the in-sandbox pane."""
        if not self._initialized:
            return False
        return self._tmux_ok("send-keys", "-t", self._pane_id, "C-c")

    def is_running(self) -> bool:
        """True while the in-sandbox tmux server + pane are alive."""
        if not self._initialized or self._closed:
            return False
        return self._tmux_ok("list-panes", "-t", self._pane_id)
