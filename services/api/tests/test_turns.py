"""Starting a design turn and polling for it.

This exists because of a real failure: the browser aborts a request after 15 seconds, a
real Astra turn takes tens of seconds to minutes, so every genuine design request died and
the model's work was abandoned. The turn is now started and polled.

What has to hold:

* the POST returns immediately, before the model has answered;
* the answer arrives later, unchanged, through the poll;
* a turn still running is not started a second time — a model call costs the user's
  ChatGPT allowance;
* a crash becomes a failed turn with a sentence, not a poll that says "thinking" forever;
* a turn id from another project is not readable.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient  # noqa: E402

from studio_agent.providers.fake_llm import FakeLlmProvider  # noqa: E402
from studio_api.app import create_app  # noqa: E402
from studio_api.dependencies import build_dependencies  # noqa: E402
from studio_api.settings import Settings  # noqa: E402

PROJECT = "proj_seed"
OTHER = "proj_other"


class SlowProvider(FakeLlmProvider):
    """A provider that answers only when the test lets it."""

    def __init__(self) -> None:
        super().__init__()
        self.released = threading.Event()
        self.entered = threading.Event()

    def respond(self, agent_input: Any):
        self.entered.set()
        # Bounded: a stuck test should fail, not hang a suite.
        self.released.wait(timeout=10)
        return super().respond(agent_input)


def build(provider: Any, tmp_path: Path, project_ids: tuple[str, ...] = (PROJECT,)):
    settings = Settings(
        environment="local",
        design_provider="fake_llm",
        database_path=str(tmp_path / "studio.sqlite3"),
        reference_root=str(tmp_path / "references"),
        artifact_root=str(tmp_path / "artifacts"),
        project_ids=project_ids,
    )
    dependencies = build_dependencies(settings, design_provider=provider)
    return TestClient(create_app(settings, dependencies)), dependencies


def start(client: TestClient, message: str = "what is here?", request_id: str = "req_1"):
    return client.post(
        f"/api/projects/{PROJECT}/design-chat",
        json={"request_id": request_id, "session_id": "sess_1", "message": message},
    )


def poll(client: TestClient, turn_id: str):
    return client.get(f"/api/projects/{PROJECT}/design-chat/{turn_id}")


def settle(client: TestClient, turn_id: str, attempts: int = 400):
    for _ in range(attempts):
        response = poll(client, turn_id)
        if response.status_code >= 400 or response.json().get("state") != "thinking":
            return response
        time.sleep(0.01)
    raise AssertionError("the turn never finished")


# --- the shape of the exchange -------------------------------------------


def test_the_post_returns_before_the_model_has_answered(tmp_path: Path):
    provider = SlowProvider()
    provider.queue_answer("There is one cube.")
    client, _ = build(provider, tmp_path)

    started = start(client)

    assert started.status_code == 202, started.text
    body = started.json()
    assert body["state"] == "thinking"
    assert body["turn_id"].startswith("turn_")
    assert body["poll_url"].endswith(body["turn_id"])
    assert body["message"], "the user needs something to read while waiting"
    # The model is genuinely still working.
    assert provider.entered.wait(timeout=5)
    assert poll(client, body["turn_id"]).json()["state"] == "thinking"

    provider.released.set()
    finished = settle(client, body["turn_id"])
    assert finished.json()["kind"] == "answer"
    assert finished.json()["message"] == "There is one cube."
    assert finished.json()["state"] == "ready"


def test_the_answer_survives_the_browser_going_away(tmp_path: Path):
    """A reload must not lose the turn: the answer is fetched by id, not awaited."""
    provider = SlowProvider()
    provider.queue_answer("Two walls and a floor.")
    client, _ = build(provider, tmp_path)

    turn_id = start(client).json()["turn_id"]
    assert provider.entered.wait(timeout=5)
    provider.released.set()

    # Whoever asks next — the same tab, a new one — gets the answer.
    assert settle(client, turn_id).json()["message"] == "Two walls and a floor."


def test_a_plan_still_reports_the_job_it_created(tmp_path: Path):
    """The 202-and-a-job-id contract the browser tracks is unchanged."""
    from studio_agent.providers.fake_llm import operation

    provider = FakeLlmProvider()
    provider.queue_plan(
        [
            operation(
                "move_object",
                {
                    "name": "Cube",
                    "desired_position_meters": {"x": 0.5, "y": 0.0, "z": 0.0},
                },
            )
        ]
    )
    client, dependencies = build(provider, tmp_path)
    # A design machine that accepts work, without a real worker: this test is about the
    # turn reporting its job, not about execution.
    from studio_api.worker_selection import WorkerSelection

    dependencies.design_chat.blender_available = lambda: True
    dependencies.design_chat.selector = type(
        "Ready", (), {"select": lambda self, job: WorkerSelection(worker_id="worker_1")}
    )()
    dependencies.design_chat.offer_job = lambda worker_id, job: None

    finished = settle(client, start(client, "move the cube").json()["turn_id"])

    assert finished.status_code == 202, finished.text
    assert finished.json()["kind"] == "plan"
    assert finished.json()["job_id"]


# --- not paying twice -----------------------------------------------------


def test_asking_twice_while_thinking_joins_the_same_turn(tmp_path: Path):
    """A double click must not buy a second model call."""
    provider = SlowProvider()
    provider.queue_answer("One cube.")
    client, _ = build(provider, tmp_path)

    first = start(client).json()
    assert provider.entered.wait(timeout=5)
    second = start(client).json()

    assert second["turn_id"] == first["turn_id"]
    provider.released.set()
    settle(client, first["turn_id"])
    assert len(provider.seen) == 1, "the model was asked twice for one message"


def test_a_repeat_after_the_turn_finished_is_a_new_turn(tmp_path: Path):
    """Once answered, the same request id goes through the service again.

    That is what keeps the service's own rules visible — job idempotency reporting a
    duplicate, a decision that was already made being refused — instead of hiding them
    behind a replayed answer.
    """
    provider = FakeLlmProvider()
    provider.queue_answer("First.")
    provider.queue_answer("Second.")
    client, _ = build(provider, tmp_path)

    first = settle(client, start(client).json()["turn_id"])
    second = settle(client, start(client).json()["turn_id"])

    assert first.json()["message"] == "First."
    assert second.json()["message"] == "Second."
    assert len(provider.seen) == 2


# --- failures -------------------------------------------------------------


def test_a_provider_that_raises_becomes_a_failed_turn_not_a_silent_one(tmp_path: Path):
    class Exploding(FakeLlmProvider):
        def respond(self, agent_input: Any):
            raise RuntimeError("the model exploded")

    client, _ = build(Exploding(), tmp_path)
    turn_id = start(client).json()["turn_id"]

    failed = settle(client, turn_id)

    assert failed.status_code == 500, failed.text
    body = failed.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    # A sentence, and no leaked internals.
    assert "exploded" not in body["error"]["message"]
    assert "Traceback" not in failed.text


def test_a_refusal_keeps_its_own_status_and_message(tmp_path: Path):
    """A turn that legitimately fails reports exactly what it used to."""
    provider = FakeLlmProvider()
    provider.unavailable = "Astra is not signed in."
    client, _ = build(provider, tmp_path)

    failed = settle(client, start(client).json()["turn_id"])

    assert failed.status_code >= 400
    assert failed.json()["error"]["message"]


def test_an_unknown_turn_id_is_refused(tmp_path: Path):
    client, _ = build(FakeLlmProvider(), tmp_path)
    response = poll(client, "turn_doesnotexist")
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_a_turn_belongs_to_its_project(tmp_path: Path):
    """A turn id from one project must not be readable through another."""
    provider = FakeLlmProvider()
    provider.queue_answer("One cube.")
    client, _ = build(provider, tmp_path, project_ids=(PROJECT, OTHER))

    turn_id = start(client).json()["turn_id"]
    settle(client, turn_id)

    leaked = client.get(f"/api/projects/{OTHER}/design-chat/{turn_id}")
    assert leaked.status_code == 404, leaked.text


def test_an_unknown_project_cannot_start_a_turn(tmp_path: Path):
    client, _ = build(FakeLlmProvider(), tmp_path)
    response = client.post(
        "/api/projects/proj_nope/design-chat",
        json={"request_id": "req_x", "session_id": "s", "message": "hello"},
    )
    assert response.status_code == 404, response.text


# --- bookkeeping ----------------------------------------------------------


def test_old_turns_are_dropped_but_never_the_transcript(tmp_path: Path):
    """The runner is a delivery buffer; the conversation itself is durable."""
    provider = FakeLlmProvider()
    client, dependencies = build(provider, tmp_path)
    dependencies.turns.history = 3

    turn_ids = []
    for index in range(5):
        provider.queue_answer(f"Answer {index}.")
        turn_ids.append(start(client, f"question {index}", f"req_{index}").json()["turn_id"])
        settle(client, turn_ids[-1])

    assert poll(client, turn_ids[0]).status_code == 404
    assert poll(client, turn_ids[-1]).status_code == 200

    # Every question and answer is still in the transcript.
    conversation = client.get(f"/api/projects/{PROJECT}/workspace").json()["conversation"]
    assert len([turn for turn in conversation if turn["role"] == "user"]) == 5
    assert "Answer 0." in [turn["text"] for turn in conversation]
