"""Signing in to ChatGPT through the official Codex flow.

The platform relays what `codex login` prints. These tests pin that relay, and pin the
things it must never do: implement OAuth, read a credential file, or handle a password.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Optional

import pytest

from studio_agent import codex
from studio_agent.codex import CodexClient, CodexError

# Real output captured from `codex login` on this machine, so the parser is tested
# against what the CLI actually prints rather than a guess.
DEFAULT_FLOW_OUTPUT = """\
Starting local login server on http://localhost:1455.
If your browser did not open, navigate to this URL to authenticate:

https://auth.openai.com/oauth/authorize?response_type=code&client_id=app_EMoamEEZ73f0CkXaXp7hrann&redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback&scope=openid%20profile&state=9CQR21yYUqqkZecMQoRu

On a remote or headless machine? Use `codex login --device-auth` instead.
"""

# Device flow output, including the ANSI colour codes the CLI really emits.
DEVICE_FLOW_OUTPUT = (
    "\nWelcome to Codex [v\x1b[90m0.154.0\x1b[0m]\n"
    "\x1b[90mOpenAI's command-line coding agent\x1b[0m\n\n"
    "Follow these steps to sign in with ChatGPT using device code authorization:\n\n"
    "1. Open this link in your browser and sign in to your account\n"
    "   \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m\n\n"
    "2. Enter this one-time code \x1b[90m(expires in 15 minutes)\x1b[0m\n"
    "   \x1b[94mFIZI-U80Z5\x1b[0m\n"
)


class FakeProcess:
    """A stand-in for the `codex login` child process."""

    def __init__(self, output: str, *, exit_after_output: Optional[int] = None) -> None:
        self.stdout = _LineReader(output)
        self._exit_after_output = exit_after_output
        self.terminated = False
        self.killed = False

    def poll(self) -> Optional[int]:
        if self._exit_after_output is not None and self.stdout.exhausted:
            return self._exit_after_output
        return None

    def terminate(self) -> None:
        self.terminated = True
        self._exit_after_output = 0
        self.stdout.exhaust()

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:  # pragma: no cover - only on a hung process
        self.killed = True


class _LineReader:
    def __init__(self, text: str) -> None:
        self._lines = text.splitlines(keepends=True)
        self._index = 0

    def readline(self) -> str:
        if self._index >= len(self._lines):
            return ""
        line = self._lines[self._index]
        self._index += 1
        return line

    @property
    def exhausted(self) -> bool:
        return self._index >= len(self._lines)

    def exhaust(self) -> None:
        self._index = len(self._lines)


def build_client(
    *,
    logged_in: bool = False,
    login_output: str = DEFAULT_FLOW_OUTPUT,
    exit_after_output: Optional[int] = None,
) -> tuple[CodexClient, list[list[str]], list[FakeProcess]]:
    commands: list[list[str]] = []
    processes: list[FakeProcess] = []

    def runner(command, *, stdin=None, timeout=None, cwd=None):
        commands.append(list(command))
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, "codex-cli 0.154.0", "")
        if command[1:3] == ["login", "status"]:
            if logged_in:
                return subprocess.CompletedProcess(command, 0, "Logged in using ChatGPT", "")
            return subprocess.CompletedProcess(command, 1, "Not logged in", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    client = CodexClient(executable="/usr/bin/true", runner=runner)

    def spawn(arguments):
        commands.append(["/usr/bin/true", *arguments])
        process = FakeProcess(login_output, exit_after_output=exit_after_output)
        processes.append(process)
        return process

    client._spawn_login = spawn  # type: ignore[method-assign]
    return client, commands, processes


# --- the default flow -----------------------------------------------------


def test_the_default_flow_returns_the_authorize_url() -> None:
    client, commands, _ = build_client()
    session = client.begin_login()

    assert session.state == codex.LOGIN_WAITING
    assert session.waiting
    assert session.verification_url is not None
    assert session.verification_url.startswith("https://auth.openai.com/oauth/authorize")
    assert session.user_code is None, "the default flow has no code to type"
    assert ["/usr/bin/true", "login"] in commands


def test_the_verification_url_is_always_an_official_openai_url() -> None:
    client, _, _ = build_client()
    session = client.begin_login()
    assert session.verification_url is not None
    assert session.verification_url.startswith("https://auth.openai.com/")


# --- the device-code flow -------------------------------------------------


def test_the_device_flow_returns_a_url_and_a_one_time_code() -> None:
    client, commands, _ = build_client(login_output=DEVICE_FLOW_OUTPUT)
    session = client.begin_login(device_auth=True)

    assert session.state == codex.LOGIN_WAITING
    assert session.verification_url == "https://auth.openai.com/codex/device"
    assert session.user_code == "FIZI-U80Z5"
    assert session.device_auth is True
    assert ["/usr/bin/true", "login", "--device-auth"] in commands


def test_ansi_colour_codes_are_stripped_from_what_the_user_sees() -> None:
    client, _, _ = build_client(login_output=DEVICE_FLOW_OUTPUT)
    session = client.begin_login(device_auth=True)
    assert "\x1b[" not in (session.verification_url or "")
    assert "\x1b[" not in (session.user_code or "")


# --- state transitions ----------------------------------------------------


def test_an_already_signed_in_client_reports_complete_without_spawning() -> None:
    client, commands, processes = build_client(logged_in=True)
    session = client.begin_login()

    assert session.state == codex.LOGIN_COMPLETE
    assert processes == [], "no sign-in process is needed when already signed in"
    assert not any(command[1:2] == ["login"] and len(command) == 2 for command in commands)


def test_the_session_becomes_complete_once_codex_reports_signed_in() -> None:
    client, _, processes = build_client(exit_after_output=0)
    client.begin_login()

    # The CLI exits after the browser callback; from then on `login status` is the
    # authority on whether it worked.
    processes[0].stdout.exhaust()
    client._login_process = processes[0]  # type: ignore[attr-defined]

    def logged_in_runner(command, *, stdin=None, timeout=None, cwd=None):
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, "codex-cli 0.154.0", "")
        return subprocess.CompletedProcess(command, 0, "Logged in using ChatGPT", "")

    client.runner = logged_in_runner
    assert client.login_session().state == codex.LOGIN_COMPLETE


def test_a_finished_process_that_did_not_sign_in_reports_failure() -> None:
    client, _, processes = build_client(exit_after_output=1)
    client.begin_login()
    processes[0].stdout.exhaust()

    session = client.login_session()
    assert session.state == codex.LOGIN_FAILED
    assert "try again" in session.detail.lower()


def test_cancelling_terminates_the_callback_server() -> None:
    client, _, processes = build_client()
    client.begin_login()

    client.cancel_login()
    assert processes[0].terminated is True
    assert client.login_session().state == codex.LOGIN_CANCELLED


def test_a_second_attempt_supersedes_the_first() -> None:
    """Two callback servers would fight over the same port."""
    client, _, processes = build_client()
    client.begin_login()
    client.begin_login()

    assert len(processes) == 2
    assert processes[0].terminated is True, "the first attempt is stopped"


def test_output_with_no_url_fails_with_what_the_cli_said() -> None:
    client, _, _ = build_client(login_output="error: something went wrong\n", exit_after_output=1)
    with pytest.raises(CodexError) as error:
        client.begin_login()
    assert "something went wrong" in str(error.value)


def test_a_missing_codex_cannot_start_a_sign_in() -> None:
    client = CodexClient(executable="/definitely/not/here")
    with pytest.raises(CodexError) as error:
        client.begin_login()
    assert "not installed" in str(error.value).lower()


def test_an_idle_client_reports_an_idle_session() -> None:
    client, _, _ = build_client()
    session = client.login_session()
    assert session.state == codex.LOGIN_IDLE
    assert not session.waiting
    assert session.snapshot()["verification_url"] is None


# --- what the flow must never do -----------------------------------------


def test_the_snapshot_carries_no_credential() -> None:
    client, _, _ = build_client(login_output=DEVICE_FLOW_OUTPUT)
    snapshot = client.begin_login(device_auth=True).snapshot()

    keys = set(snapshot)
    assert not keys & {"token", "access_token", "refresh_token", "api_key", "password"}
    # The one-time code is not a credential: it is what the user types on OpenAI's own
    # page, and it expires. It is deliberately present.
    assert snapshot["user_code"] == "FIZI-U80Z5"


def test_no_openai_api_variable_reaches_the_sign_in_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-be-used")

    script = tmp_path / "fake-codex"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "codex-cli 0.154.0"; exit 0; fi\n'
        'if [ "$2" = "status" ]; then echo "Not logged in"; exit 1; fi\n'
        'env | grep -c "^OPENAI_" > "$STUDIO_ENV_PROBE" || true\n'
        'echo "https://auth.openai.com/oauth/authorize?x=1"\n'
        "sleep 5\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    probe = tmp_path / "probe.txt"
    monkeypatch.setenv("STUDIO_ENV_PROBE", str(probe))

    client = CodexClient(executable=str(script))
    try:
        session = client.begin_login()
        assert session.verification_url is not None
    finally:
        client.cancel_login()

    assert probe.exists()
    assert probe.read_text(encoding="utf-8").strip() == "0"


def test_the_login_code_never_reads_a_credential_file() -> None:
    """auth.json is Codex's business. We must not go near it."""
    source = Path(codex.__file__).read_text(encoding="utf-8")
    for credential_path in ("auth.json", "~/.codex", ".codex/auth", "CODEX_HOME"):
        assert credential_path not in source, (
            f"the Codex client must not go near {credential_path}"
        )
    # And no HTTP client: this module shells out to Codex and nothing else.
    for forbidden in ("httpx", "requests", "urllib.request", "aiohttp"):
        assert forbidden not in source, f"{forbidden} must not appear in the Codex client"
