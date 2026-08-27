"""Sandbox-backed file editor.

Routes the FileEditor's filesystem I/O (read, write, exists, stat, list)
through ``sandbox exec`` so every file the agent views or edits lives
INSIDE the Cloud Run sandbox session — the trusted agent-server never
touches the workspace filesystem directly.

All string-level logic (str_replace matching, insert splicing, snippet
formatting, edit history) is inherited unchanged from FileEditor; only the
I/O primitives are overridden. Binary transport is base64 so content is
byte-exact in both directions.
"""

import base64
import os
import subprocess
from pathlib import Path, PurePosixPath

from openhands.sdk.logger import get_logger
from openhands.tools.file_editor.editor import FileEditor
from openhands.tools.file_editor.exceptions import ToolError


logger = get_logger(__name__)

SANDBOX_BIN = "/usr/local/gcp/bin/sandbox"


def _sandbox_exec(session_id: str, args: list[str], timeout: float = 60.0) -> str:
    """Run one command inside the sandbox session; return stdout.

    The sandbox exec environment has NO PATH set, so bare command
    names (mkdir, test, stat, mv, ...) fail with exit 1. Wrap every
    call in /bin/sh -c with an explicit PATH export. Args are passed
    as shell-quoted positional parameters ($1..) so no quoting bugs.

    Raises RuntimeError on non-zero exit (no silent fallbacks).
    """
    n = len(args)
    params = " ".join(f'"${i}"' for i in range(1, n + 1))
    sh_cmd = f"export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; {args[0]} {params}"
    cmd = [SANDBOX_BIN, "exec", session_id, "--", "/bin/sh", "-c", sh_cmd, "sandbox_exec", *[str(a) for a in args[1:]]]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(
            f"sandbox exec failed (exit {result.returncode}): "
            f"{result.stderr.strip()[:500]}"
        )
    return result.stdout


class SandboxFileEditor(FileEditor):
    """FileEditor whose I/O runs inside a Cloud Run sandbox session.

    The path space is the sandbox session's filesystem (the agent's
    workspace). Paths are treated as POSIX absolute paths inside the
    session; host-side Path objects are used only as opaque identifiers.
    """

    _session_id: str

    def __init__(
        self,
        workspace_root: str | None = None,
        max_file_size_mb: int | None = None,
        sandbox_session_id: str | None = None,
    ):
        # FileEditor.__init__ builds a FileHistoryManager against a temp dir
        # on the HOST — that is correct: edit history is trusted-side state
        # (undo support), not workspace content.
        super().__init__(workspace_root=workspace_root, max_file_size_mb=max_file_size_mb)
        self._session_id = sandbox_session_id or "agent-workspace"

    # ── sandbox I/O primitives ─────────────────────────────────────────

    def _sb(self, *args: str, timeout: float = 60.0) -> str:
        return _sandbox_exec(self._session_id, list(args), timeout=timeout)

    def _sb_ok(self, *args: str) -> bool:
        """Run a command; True on exit 0."""
        try:
            self._sb(*args)
            return True
        except RuntimeError:
            return False

    def _read_bytes(self, path: Path) -> bytes:
        out = self._sb("base64", "-w0", str(path))
        return base64.b64decode(out.strip())

    def _write_bytes(self, path: Path, data: bytes) -> None:
        b64 = base64.b64encode(data).decode("ascii")
        # mkdir -p the parent, then decode the payload into the file.
        parent = str(PurePosixPath(str(path)).parent)
        self._sb("mkdir", "-p", parent)
        # Payload via stdin-style heredoc is fragile through exec; use
        # printf with the base64 payload (safe charset: A-Za-z0-9+/=).
        self._sb("/bin/sh", "-c", f"export PATH=/usr/local/bin:/usr/bin:/bin; printf '%s' '{b64}' | base64 -d > '{path}'")  # noqa: G004 — b64 charset is shell-safe

    # ── overridden I/O surface ─────────────────────────────────────────

    def read_file(
        self,
        path: Path,
        start_line: int | None = None,
        end_line: int | None = None,
        encoding: str = "utf-8",
    ) -> str:
        """Read file content from the sandbox session."""
        # validate_file checks size + binary-ness; replicate via stat/file.
        self._validate_file_sandbox(path)
        try:
            data = self._read_bytes(path)
            content = data.decode(encoding)
            if start_line is not None and end_line is not None:
                lines = content.splitlines(keepends=True)
                return "".join(lines[start_line - 1 : end_line])
            if start_line is not None or end_line is not None:
                raise ValueError("Both start_line and end_line must be provided together")
            return content
        except UnicodeDecodeError as e:
            raise ToolError(f"Ran into {e} while trying to read {path}") from None
        except RuntimeError as e:
            raise ToolError(f"Ran into {e} while trying to read {path}") from None

    def write_file(self, path: Path, file_text: str, encoding: str = "utf-8") -> None:
        """Write file content into the sandbox session (atomic via temp+mv)."""
        self._validate_file_sandbox(path, for_write=True)
        try:
            data = file_text.encode(encoding)
            tmp = f"{path}.vura-tmp"
            self._write_bytes(Path(tmp), data)
            self._sb("mv", tmp, str(path))
        except (RuntimeError, UnicodeEncodeError) as e:
            raise ToolError(f"Ran into {e} while trying to write to {path}") from None

    # ── FS predicates routed to the sandbox ────────────────────────────

    def _exists(self, path: Path) -> bool:
        return self._sb_ok("test", "-e", str(path))

    def _is_file(self, path: Path) -> bool:
        return self._sb_ok("test", "-f", str(path))

    def _is_dir(self, path: Path) -> bool:
        return self._sb_ok("test", "-d", str(path))

    def _size(self, path: Path) -> int:
        out = self._sb("stat", "-c", "%s", str(path))
        return int(out.strip())

    def _validate_file_sandbox(self, path: Path, for_write: bool = False) -> None:
        """Size + binary checks against the sandbox filesystem."""
        if not self._exists(path):
            return  # create command / view of missing file handled upstream
        if self._is_dir(path):
            return
        size = self._size(path)
        if size > self._max_file_size:
            from openhands.tools.file_editor.exceptions import FileValidationError

            raise FileValidationError(
                path=str(path),
                reason=(
                    f"File is too large ({size / 1024 / 1024:.1f}MB). "
                    f"Maximum allowed size is {int(self._max_file_size / 1024 / 1024)}MB."
                ),
            )
        # Binary check: NUL byte in the first 4KB.
        # Binary check: NUL byte in the first 4KB via shell pipeline.
        is_bin = self._sb_ok(
            "/bin/sh", "-c", f"export PATH=/usr/local/bin:/usr/bin:/bin; head -c 4096 '{path}' | grep -qP '\\x00'"
        )
        ext = path.suffix.lower()
        from openhands.tools.file_editor.editor import IMAGE_EXTENSIONS

        if is_bin and ext not in IMAGE_EXTENSIONS:
            from openhands.tools.file_editor.exceptions import FileValidationError

            raise FileValidationError(
                path=str(path),
                reason=(
                    "File appears to be binary and this file type cannot be read "
                    "or edited by this tool."
                ),
            )

    # ── directory listing for view ─────────────────────────────────────

    def _list_directory_for_view(self, path: Path) -> list[str]:
        """List a directory (inside the sandbox) as formatted entries."""
        # find with -maxdepth 1 mirrors the original's single-level listing
        # plus the root; formatting is inherited via _format_directory_entry
        # which needs Path objects — build them from the sandbox's listing.
        out = self._sb("find", str(path), "-maxdepth", "1", "-mindepth", "0")
        names = [line for line in out.splitlines() if line.strip()]
        entries = [Path(n) for n in names]
        return [self._format_directory_entry(Path(str(path)), e) for e in entries]

    def _count_hidden_children(self, path: Path) -> int:
        out = self._sb(
            "/bin/sh", "-c",
            f"export PATH=/usr/local/bin:/usr/bin:/bin; find '{path}' -maxdepth 1 -name '.*' -mindepth 1 | wc -l",
        )
        try:
            return int(out.strip())
        except ValueError:
            return 0

    # ── validate_path: exists() checks against the sandbox ─────────────

    def validate_path(self, command, path: Path) -> None:
        """Same contract as FileEditor.validate_path, with sandbox exists()."""
        from openhands.sdk.utils.path import is_host_absolute_path
        from openhands.tools.file_editor.exceptions import EditorToolParameterInvalidError

        if not is_host_absolute_path(path):
            raise EditorToolParameterInvalidError(
                "path",
                str(path),
                "The path should be an absolute path.",
            )
        if command == "create" and self._exists(path):
            raise EditorToolParameterInvalidError(
                "path",
                str(path),
                f"File already exists at: {path}. Cannot overwrite files using "
                f"command `create`.",
            )
        if command in ("str_replace", "insert", "undo_edit") and not self._exists(path):
            raise EditorToolParameterInvalidError(
                "path",
                str(path),
                f"File does not exist. Cannot run command `{command}` on a "
                f"non-existent file.",
            )
        # view works on both existing and missing paths (missing → ToolError
        # in view()); no extra checks here.

    def view(self, path: Path, view_range=None):
        """view with sandbox is_dir/exists predicates."""
        if self._is_dir(path):
            if view_range:
                from openhands.tools.file_editor.exceptions import (
                    EditorToolParameterInvalidError,
                )

                raise EditorToolParameterInvalidError(
                    "view_range",
                    str(view_range),
                    "The `view_range` parameter is not allowed when `path` points "
                    "to a directory.",
                )
            try:
                hidden_count = self._count_hidden_children(path)
                formatted_paths = self._list_directory_for_view(path)
            except RuntimeError as e:
                raise ToolError(f"Ran into {e} while trying to list {path}") from None
            from openhands.tools.file_editor.editor import DIRECTORY_CONTENT_TRUNCATED_NOTICE
            from openhands.tools.file_editor.utils.constants import (
                MAX_RESPONSE_LEN_CHAR,
            )
            from openhands.sdk.utils.truncate import maybe_truncate

            content = "\n".join(formatted_paths) + "\n"
            if hidden_count > 0:
                content += (
                    f"\n({hidden_count} hidden files/directories not shown)\n"
                )
            content = maybe_truncate(content, MAX_RESPONSE_LEN_CHAR)
            return self._observation_from_view(content, path)
        if not self._exists(path):
            raise ToolError(f"File or directory not found: {path}")
        return self._view_file(path, view_range)

    def _observation_from_view(self, content: str, path: Path):
        from openhands.tools.file_editor.definition import FileEditorObservation

        return FileEditorObservation.from_text(
            text=content, command="view", path=str(path)
        )

    def _view_file(self, path: Path, view_range):
        """View a file: full content or a line range (sandbox read)."""
        if view_range:
            if len(view_range) != 2:
                from openhands.tools.file_editor.exceptions import (
                    EditorToolParameterInvalidError,
                )

                raise EditorToolParameterInvalidError(
                    "view_range",
                    str(view_range),
                    "It should be a list of two integers.",
                )
            content = self.read_file(path, start_line=view_range[0], end_line=view_range[1])
        else:
            content = self.read_file(path)
        from openhands.tools.file_editor.editor import TEXT_FILE_CONTENT_TRUNCATED_NOTICE
        from openhands.tools.file_editor.utils.constants import (
            MAX_RESPONSE_LEN_CHAR,
        )
        from openhands.sdk.utils.truncate import maybe_truncate

        content = maybe_truncate(content, MAX_RESPONSE_LEN_CHAR)
        return self._observation_from_view(content, path)
