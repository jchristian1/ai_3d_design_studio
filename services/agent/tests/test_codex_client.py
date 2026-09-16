"""The Codex client: status detection, structured completion, and no API key anywhere."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pytest

from studio_agent import codex
from studio_agent.agent_input import AgentInput, ReferenceImage
from studio_agent.codex import CodexClient, CodexError, CodexStatus
from studio_agent.outcome import AgentError, Answer, PlanProposal
from studio_agent.providers.codex_astra import CodexAstraProvider, build_prompt
from studio_agent.providers.fake_llm import operation, response_body


@dataclass
class FakeCli:
    """Stands in for the codex executable, recording how it was invoked."""

    version_text: str = "codex-cli 0.154.0"
    version_code: int = 0
    login_text: str = "Logged in using ChatGPT (christian@example.com)"
    login_code: int = 0
    exec_code: int = 0
    exec_stderr: str = ""
    response: Optional[dict[str, Any]] = None
    calls: list[list[str]] = field(default_factory=list)
    stdins: list[Optional[str]] = field(default_factory=list)
    environments_seen: list[dict[str, str]] = field(default_factory=list)
    raise_timeout: bool = False

    def __call__(
        self,
        command: list[str],
        *,
        stdin: Optional[str] = None,
        timeout: Optional[float] = None,
        cwd: Optional[Path] = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        self.stdins.append(stdin)

        if "--version" in command:
            return subprocess.CompletedProcess(command, self.version_code, self.version_text, "")
        if command[1:3] == ["login", "status"]:
            return subprocess.CompletedProcess(command, self.login_code, self.login_text, "")

        if self.raise_timeout:
            raise subprocess.TimeoutExpired(command, 1.0)

        # Emulate `--output-last-message`: write the response where Codex would.
        if self.response is not None and "--output-last-message" in command:
            target = Path(command[command.index("--output-last-message") + 1])
            target.write_text(json.dumps(self.response), encoding="utf-8")
        return subprocess.CompletedProcess(command, self.exec_code, "", self.exec_stderr)

    def argument_after(self, flag: str) -> Optional[str]:
        for call in self.calls:
            if flag in call:
                return call[call.index(flag) + 1]
        return None

    @property
    def exec_call(self) -> list[str]:
        for call in self.calls:
            if "exec" in call:
                return call
        raise AssertionError("codex exec was never invoked")


def client(cli: FakeCli, **kwargs: Any) -> CodexClient:
    return CodexClient(executable="/usr/bin/true", runner=cli, **kwargs)


# --- status ---------------------------------------------------------------


def test_a_missing_codex_reports_not_installed() -> None:
    status = CodexClient(executable="/definitely/not/here").status()
    assert status.state == codex.NOT_INSTALLED
    assert not status.connected
    assert status.snapshot()["action"] == "npm install -g @openai/codex"


def test_an_old_codex_reports_update_required_and_never_claims_astra() -> None:
    status = client(FakeCli(version_text="codex-cli 0.140.0")).status()
    assert status.state == codex.UPDATE_REQUIRED
    assert "codex update" in status.message
    assert status.snapshot()["model"] is None


def test_a_logged_out_codex_reports_login_required() -> None:
    cli = FakeCli(login_text="Not logged in", login_code=1)
    status = client(cli).status()
    assert status.state == codex.LOGIN_REQUIRED
    assert status.snapshot()["action"] == "codex login"
    # The user is never asked for a password.
    assert "password" not in status.message.lower()


def test_a_logged_in_codex_reports_connected_via_chatgpt() -> None:
    status = client(FakeCli()).status()
    assert status.state == codex.CONNECTED
    assert status.connected
    snapshot = status.snapshot()
    assert snapshot["label"] == "Astra via Codex"
    assert snapshot["message"] == "Connected via ChatGPT"
    assert snapshot["model"] == codex.ASTRA_MODEL


def test_an_api_key_login_is_rejected_rather_than_accepted() -> None:
    """This product uses the ChatGPT allowance; a key login would contradict that."""
    cli = FakeCli(login_text="Logged in using an API key")
    status = client(cli).status()
    assert status.state == codex.LOGIN_REQUIRED
    assert "ChatGPT sign-in" in status.message


def test_every_state_has_a_message() -> None:
    for state in codex.STATES:
        assert codex.STATE_MESSAGES[state]


# --- structured completion -----------------------------------------------


def test_completion_returns_the_parsed_last_message() -> None:
    cli = FakeCli(response=response_body("answer", message="Three walls."))
    result = client(cli).complete("hello", schema={"type": "object"})
    assert result["kind"] == "answer"
    assert result["message"] == "Three walls."


def test_completion_uses_the_supported_structured_flags() -> None:
    cli = FakeCli(response=response_body("answer", message="ok"))
    client(cli).complete("hello", schema={"type": "object"})
    call = cli.exec_call
    for flag in (
        "--json",
        "--ephemeral",
        "--skip-git-repo-check",
        "--output-schema",
        "--output-last-message",
        "--sandbox",
        "--model",
    ):
        assert flag in call, f"{flag} should be used"
    assert call[call.index("--sandbox") + 1] == "read-only"
    assert call[call.index("--model") + 1] == codex.ASTRA_MODEL


def test_the_prompt_is_passed_on_stdin_not_as_an_argument() -> None:
    """A long prompt on argv would hit platform limits and leak into process lists."""
    cli = FakeCli(response=response_body("answer", message="ok"))
    client(cli).complete("a very long prompt", schema={"type": "object"})
    assert "a very long prompt" in (cli.stdins[-1] or "")
    assert "a very long prompt" not in " ".join(cli.exec_call)


def test_images_are_attached_as_real_files() -> None:
    cli = FakeCli(response=response_body("answer", message="ok"))
    client(cli).complete(
        "look",
        schema={"type": "object"},
        images=[Path("/tmp/plan-page-1.png"), Path("/tmp/room.jpg")],
    )
    call = cli.exec_call
    attached = [call[index + 1] for index, part in enumerate(call) if part == "--image"]
    assert attached == ["/tmp/plan-page-1.png", "/tmp/room.jpg"]


def test_the_schema_is_written_without_authoring_metadata() -> None:
    cli = FakeCli(response=response_body("answer", message="ok"))
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "agent-response.schema.json",
        "title": "AgentResponse",
        "type": "object",
        "properties": {},
    }
    stripped = codex.strict_schema(schema)
    assert "$schema" not in stripped and "$id" not in stripped and "title" not in stripped
    assert stripped["type"] == "object"

    client(cli).complete("hello", schema=schema)
    assert cli.argument_after("--output-schema") is not None


def test_a_failing_codex_raises_with_an_actionable_message() -> None:
    cli = FakeCli(exec_code=1, exec_stderr="error: not logged in")
    with pytest.raises(CodexError) as error:
        client(cli).complete("hello", schema={"type": "object"})
    assert "Sign in to ChatGPT" in str(error.value)


def test_a_usage_limit_is_reported_as_usage_unavailable() -> None:
    cli = FakeCli(exec_code=1, exec_stderr="you have hit your usage limit for this model")
    with pytest.raises(CodexError) as error:
        client(cli).complete("hello", schema={"type": "object"})
    assert "usage" in str(error.value).lower()


def test_a_missing_model_is_reported_as_astra_unavailable() -> None:
    cli = FakeCli(exec_code=1, exec_stderr="model gpt-6-astra not found")
    with pytest.raises(CodexError) as error:
        client(cli).complete("hello", schema={"type": "object"})
    assert "Astra is not available" in str(error.value)


def test_a_timeout_is_reported_rather_than_hanging() -> None:
    cli = FakeCli(raise_timeout=True)
    with pytest.raises(CodexError) as error:
        client(cli, timeout_seconds=5.0).complete("hello", schema={"type": "object"})
    assert "did not respond" in str(error.value)


def test_a_response_that_is_not_json_is_refused() -> None:
    cli = FakeCli()

    def runner(command, *, stdin=None, timeout=None, cwd=None):
        cli.calls.append(command)
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, cli.version_text, "")
        if command[1:3] == ["login", "status"]:
            return subprocess.CompletedProcess(command, 0, cli.login_text, "")
        Path(command[command.index("--output-last-message") + 1]).write_text(
            "I am not JSON", encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(CodexError) as error:
        CodexClient(executable="/usr/bin/true", runner=runner).complete(
            "hello", schema={"type": "object"}
        )
    assert "not valid JSON" in str(error.value)


def test_stderr_is_never_treated_as_model_output() -> None:
    cli = FakeCli(
        response=response_body("answer", message="the real answer"),
        exec_stderr="warning: something noisy on stderr",
    )
    result = client(cli).complete("hello", schema={"type": "object"})
    assert result["message"] == "the real answer"


def test_the_event_stream_parser_ignores_decorative_output() -> None:
    events = codex.parse_event_stream(
        'noise\n{"type":"item.completed"}\nmore noise\n{"broken\n{"type":"turn.done"}\n'
    )
    assert [event["type"] for event in events] == ["item.completed", "turn.done"]


# --- no API key anywhere -------------------------------------------------


def test_openai_api_variables_are_stripped_from_the_child_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Even if the shell has a key exported, Codex is not given it."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-be-used")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid")

    script = tmp_path / "fake-codex"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "codex-cli 0.154.0"; exit 0; fi\n'
        'env | grep -c "^OPENAI_" > "$STUDIO_ENV_PROBE" || true\n'
        "exit 7\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    probe = tmp_path / "probe.txt"
    monkeypatch.setenv("STUDIO_ENV_PROBE", str(probe))

    real_client = CodexClient(executable=str(script))
    with pytest.raises(CodexError):
        real_client.complete("hello", schema={"type": "object"})

    assert probe.exists()
    assert probe.read_text(encoding="utf-8").strip() == "0", (
        "no OPENAI_* variable may reach the Codex child process"
    )


# --- the provider --------------------------------------------------------


def test_the_provider_reports_provider_unavailable_when_not_connected() -> None:
    provider = CodexAstraProvider(client=client(FakeCli(login_text="Not logged in", login_code=1)))
    outcome = provider.respond(
        AgentInput(user_text="hi", user_id="u", project_id="p", session_id="s")
    )
    assert isinstance(outcome, AgentError)
    assert outcome.code == "PROVIDER_UNAVAILABLE"
    assert "codex login" in outcome.message


def test_the_provider_parses_a_plan_through_the_real_validation_path() -> None:
    cli = FakeCli(
        response=response_body(
            "plan",
            message="Moving the cube.",
            operations=[
                operation(
                    "move_object",
                    {"name": "Cube", "desired_position_meters": {"x": 0.5, "y": 0, "z": 0}},
                )
            ],
        )
    )
    provider = CodexAstraProvider(client=client(cli))
    outcome = provider.respond(
        AgentInput(user_text="move the cube right", user_id="u", project_id="p", session_id="s")
    )
    assert isinstance(outcome, PlanProposal)
    assert outcome.operations[0].capability == "move_object"
    assert outcome.metadata.model == codex.ASTRA_MODEL


def test_the_provider_passes_images_to_codex() -> None:
    cli = FakeCli(response=response_body("answer", message="I see a floor plan."))
    provider = CodexAstraProvider(client=client(cli))
    outcome = provider.respond(
        AgentInput(
            user_text="what do you see?",
            user_id="u",
            project_id="p",
            session_id="s",
            images=(
                ReferenceImage(
                    reference_id="ref_1",
                    path=Path("/tmp/plan.png"),
                    media_type="image/png",
                    label="floor plan",
                    page_number=1,
                ),
            ),
        )
    )
    assert isinstance(outcome, Answer)
    assert "--image" in cli.exec_call
    assert cli.argument_after("--image") == "/tmp/plan.png"


# --- the prompt ----------------------------------------------------------


def _input(**kwargs: Any) -> AgentInput:
    base = {"user_text": "do something", "user_id": "u", "project_id": "p", "session_id": "s"}
    base.update(kwargs)
    return AgentInput(**base)  # type: ignore[arg-type]


def test_the_prompt_states_the_canonical_units() -> None:
    prompt = build_prompt(_input())
    assert "METRES" in prompt
    assert "RADIANS" in prompt
    assert "linear sRGB" in prompt
    assert "+X is right" in prompt


def test_the_prompt_asks_for_a_missing_scale_but_not_for_the_ordinary() -> None:
    """The policy changed, deliberately, and this records what it is now.

    It used to say "NEVER INVENT ARCHITECTURE — guessing a dimension is worse than asking".
    In practice that produced three questions and no model for a user who had already said
    "invent it but make it look great". Asking is now reserved for what genuinely cannot be
    chosen — no scale at all, or contradictory numbers — and everything else has a standard
    value to use and declare.
    """
    prompt = build_prompt(_input())
    assert "ASK RARELY, AND BUILD" in prompt
    # Still asks when there is no way to know the real size of anything.
    assert "no scale at all" in prompt
    # And a declared assumption is the alternative to a question, not an excuse for silence.
    assert '"assumptions"' in prompt


def test_the_prompt_carries_confirmed_facts_so_astra_stops_asking() -> None:
    prompt = build_prompt(_input(project_facts={"ceiling_height_m": "2.4"}))
    assert "ceiling_height_m = 2.4" in prompt
    assert "do not ask" in prompt


def test_the_prompt_names_the_selected_object() -> None:
    prompt = build_prompt(_input(selected_object_id="obj_wall_3"))
    assert "obj_wall_3" in prompt
    assert '"this"' in prompt


def test_the_prompt_says_when_blender_is_offline() -> None:
    prompt = build_prompt(_input(blender_available=False))
    assert "BLENDER IS NOT CONNECTED" in prompt


def test_the_prompt_contains_no_credential_or_filesystem_path() -> None:
    prompt = build_prompt(
        _input(
            images=(
                ReferenceImage(
                    reference_id="ref_1",
                    path=Path("/home/christian/secret/plan.png"),
                    media_type="image/png",
                    label="floor plan",
                ),
            ),
        )
    )
    assert "/home/christian" not in prompt
    assert "secret" not in prompt
    # The image is described, and delivered as bytes rather than as a path.
    assert "floor plan" in prompt


def test_the_prompt_lists_only_proposable_capabilities() -> None:
    prompt = build_prompt(_input())
    assert "move_object" in prompt
    assert "create_wall" in prompt
    # Platform-initiated artifact capabilities are not offered.
    assert "export_glb" not in prompt
    assert "render_preview" not in prompt



# ---------------------------------------------------------------------------
# Correcting the model's own mistake
# ---------------------------------------------------------------------------
#
# From a real session: after ten turns of collecting dimensions, the reply was "That
# request couldn't be processed, so nothing was changed." The model had returned something
# that did not fit the capability contract — a MODEL mistake the user cannot act on, and a
# dead end after all that work. The provider now quotes the rejection back and asks once
# more, which is what a tool loop is for.


def _connected_runner(responses):
    """A Codex stand-in that is signed in and returns queued response bodies."""
    import json as _json

    calls: list[str] = []

    def runner(arguments, *, stdin=None, timeout=None, cwd=None):
        joined = " ".join(arguments)
        if "--version" in joined:
            return _completed(0, "codex-cli 0.154.0")
        if "login" in joined and "status" in joined:
            return _completed(0, "Logged in using ChatGPT")
        calls.append(stdin or "")
        body = responses.pop(0)
        destination = _output_path(arguments)
        Path(destination).write_text(_json.dumps(body), encoding="utf-8")
        return _completed(0, "")

    return runner, calls


def _output_path(arguments) -> str:
    for index, value in enumerate(arguments):
        if value in ("-o", "--output-last-message"):
            return arguments[index + 1]
    raise AssertionError("no output file was requested")


def _completed(code: int, stdout: str):
    import subprocess

    return subprocess.CompletedProcess(args=["codex"], returncode=code, stdout=stdout, stderr="")


def _answer(text: str) -> dict:
    return {
        "kind": "answer",
        "message": text,
        "question": "",
        "missing_information": [],
        "operations": [],
        "design_facts": [],
        "assumptions": [],
    }


def _bad_plan() -> dict:
    """A plan naming a capability the platform does not have."""
    return {
        "kind": "plan",
        "message": "Building the room.",
        "question": "",
        "missing_information": [],
        "operations": [
            {"capability": "create_room", "label": "build the room", "arguments_json": "{}"}
        ],
        "design_facts": [],
        "assumptions": [],
    }


def _agent_input():
    from studio_agent.agent_input import AgentInput

    return AgentInput(
        user_text="door height 83.5, window width 80",
        user_id="user_1",
        project_id="proj_1",
        session_id="sess_1",
    )


def test_a_rejected_response_is_corrected_without_bothering_the_user() -> None:
    from studio_agent.codex import CodexClient
    from studio_agent.outcome import Answer
    from studio_agent.providers.codex_astra import CodexAstraProvider

    runner, prompts = _connected_runner([_bad_plan(), _answer("Understood.")])
    provider = CodexAstraProvider(client=CodexClient(executable="/usr/bin/true", runner=runner))

    outcome = provider.respond(_agent_input())

    assert isinstance(outcome, Answer), outcome
    assert len(prompts) == 2, "the model should have been asked to correct itself"
    # The second prompt quotes the actual reason, which is what makes a correction land.
    assert "REJECTED" in prompts[1]
    assert "create_room" in prompts[1]


def test_a_second_rejection_is_reported_in_plain_language() -> None:
    from studio_agent.codex import CodexClient
    from studio_agent.outcome import AgentError
    from studio_agent.providers.codex_astra import CodexAstraProvider

    runner, prompts = _connected_runner([_bad_plan(), _bad_plan()])
    provider = CodexAstraProvider(client=CodexClient(executable="/usr/bin/true", runner=runner))

    outcome = provider.respond(_agent_input())

    assert isinstance(outcome, AgentError)
    assert len(prompts) == 2, "one correction attempt, not an unbounded loop"
    message = outcome.error.message
    assert "could not build" in message
    # Written for the user: no capability names, no codes, no paths.
    assert "create_room" not in message
    assert "VALIDATION_ERROR" not in message


def test_a_good_response_is_not_asked_twice() -> None:
    from studio_agent.codex import CodexClient
    from studio_agent.outcome import Answer
    from studio_agent.providers.codex_astra import CodexAstraProvider

    runner, prompts = _connected_runner([_answer("There is one cube.")])
    provider = CodexAstraProvider(client=CodexClient(executable="/usr/bin/true", runner=runner))

    outcome = provider.respond(_agent_input())

    assert isinstance(outcome, Answer)
    assert len(prompts) == 1, "a valid answer must cost exactly one call"


def test_codex_being_unavailable_is_not_retried_as_a_model_mistake() -> None:
    """Retrying is for the model's output. A broken Codex is reported straight away."""
    from studio_agent.codex import CodexClient
    from studio_agent.outcome import AgentError
    from studio_agent.providers.codex_astra import CodexAstraProvider

    calls: list[str] = []

    def runner(arguments, *, stdin=None, timeout=None, cwd=None):
        joined = " ".join(arguments)
        if "--version" in joined:
            return _completed(0, "codex-cli 0.154.0")
        if "login" in joined and "status" in joined:
            return _completed(0, "Logged in using ChatGPT")
        calls.append(stdin or "")
        return _completed(1, "codex exploded")

    provider = CodexAstraProvider(client=CodexClient(executable="/usr/bin/true", runner=runner))
    outcome = provider.respond(_agent_input())

    assert isinstance(outcome, AgentError)
    assert outcome.error.code == "PROVIDER_UNAVAILABLE"
    assert len(calls) == 1
