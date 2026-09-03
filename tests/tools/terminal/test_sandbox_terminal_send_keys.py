"""SandboxTerminal.send_keys must paste the command, not a quoted copy of it.

Observed 2026-09-03 in a Cloud Run ticket run: EVERY multi-word command the
agent typed came back as

    bash: cd ./api && npm install --no-audit --no-fund: No such file or directory
    [The command completed with exit code 127.]

send_keys wrapped the text in shell quotes before handing it to tmux::

    quoted = "'" + text.replace("'", "'\\''") + "'"
    self._tmux("send-keys", "-t", pane, "-l", quoted)

but ``_sandbox_exec`` runs ``subprocess.run([...], ...)`` with a LIST and no
shell, and ``sandbox exec`` passes argv straight to execve. Nothing on that
path strips quotes, so tmux pasted the quote characters themselves and bash
saw one enormous word.

The message reads as a MISSING FILE, so the agent concluded its toolchain was
absent and went looking for it — ``which npm``, then ``find / -name npm``,
both of which failed the same way. About eleven minutes of a ticket's budget
went into it.

``SandboxTerminal`` had no tests at all, which is how a defect that broke
every agent command reached deployment. These are the first.
"""

from unittest.mock import patch

from openhands.tools.terminal.terminal.sandbox_terminal import SandboxTerminal


def _terminal() -> SandboxTerminal:
    """A SandboxTerminal that believes it is initialized, without a sandbox."""
    term = SandboxTerminal.__new__(SandboxTerminal)
    term._initialized = True
    term._pane_id = "%1"
    term._session_id = "ticket-current"
    return term


def test_command_is_pasted_verbatim_not_shell_quoted() -> None:
    """The exact regression: a multi-word command must reach tmux unwrapped."""
    term = _terminal()
    command = "cd ./api && npm install --no-audit --no-fund"

    with patch.object(term, "_tmux") as tmux:
        term.send_keys(command)

    literal_calls = [c.args for c in tmux.call_args_list if "-l" in c.args]
    assert literal_calls, "the command should be sent with tmux's literal flag"
    sent = literal_calls[0][-1]

    assert sent == command, f"expected the raw command, got {sent!r}"
    # The specific damage: a leading quote makes bash treat the whole line as
    # one filename and report "No such file or directory" (exit 127).
    assert not sent.startswith("'"), "pre-quoting is what caused exit 127"


def test_a_command_containing_quotes_is_still_untouched() -> None:
    """Quotes the AGENT typed are its own; we must not escape or double them.

    The old code's `'\\''` dance mangled these on top of everything else.
    """
    term = _terminal()
    command = "echo 'hello world' && grep -r \"needle\" ."

    with patch.object(term, "_tmux") as tmux:
        term.send_keys(command)

    sent = [c.args for c in tmux.call_args_list if "-l" in c.args][0][-1]
    assert sent == command


def test_enter_is_sent_after_the_command() -> None:
    """Pasting alone runs nothing — the pane needs the newline."""
    term = _terminal()

    with patch.object(term, "_tmux") as tmux:
        term.send_keys("ls")

    assert any(
        "Enter" in c.args for c in tmux.call_args_list
    ), "send_keys must follow the paste with Enter"


def test_named_special_keys_are_not_pasted_as_text() -> None:
    """C-c must interrupt, not type the letters 'C-c' into the shell."""
    term = _terminal()

    with patch.object(term, "_tmux") as tmux:
        term.send_keys("C-c")

    for call in tmux.call_args_list:
        assert "-l" not in call.args, "a control key must not be sent literally"
