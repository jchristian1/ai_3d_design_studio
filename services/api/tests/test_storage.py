"""Durable local state: the contracts that make a restart harmless."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from studio_api.job_records import JobRecord, record_from_job
from studio_api.storage import (
    APPROVED,
    PDF,
    PDF_PAGE,
    PENDING,
    REJECTED,
    ApprovalRecord,
    ClarificationRecord,
    ConversationTurnRecord,
    ProjectRecord,
    ReferenceFileStore,
    ReferenceRecord,
    ReferenceStorageError,
    SqliteJobRecordStore,
    StudioDatabase,
    StudioRepositories,
    checksum_of,
    media_type_for,
    sanitise_display_name,
)
from studio_api.storage.database import LATEST_VERSION


@pytest.fixture()
def database(tmp_path: Path) -> StudioDatabase:
    return StudioDatabase(tmp_path / "studio.sqlite3")


@pytest.fixture()
def repositories(database: StudioDatabase) -> StudioRepositories:
    return StudioRepositories(database)


# --- migrations -----------------------------------------------------------


def test_a_fresh_database_is_migrated_to_the_latest_version(database) -> None:
    assert database.version == LATEST_VERSION


def test_migrating_twice_is_harmless(database) -> None:
    assert database.migrate() == LATEST_VERSION
    assert database.migrate() == LATEST_VERSION


def test_an_existing_database_is_reopened_without_losing_data(tmp_path: Path) -> None:
    path = tmp_path / "studio.sqlite3"
    first = StudioDatabase(path)
    StudioRepositories(first).projects.ensure("proj_a", "Beach House")
    first.close()

    second = StudioDatabase(path)
    project = StudioRepositories(second).projects.get("proj_a")
    assert project is not None
    assert project.display_name == "Beach House"


# --- projects -------------------------------------------------------------


def test_a_project_is_created_once_and_then_returned(repositories) -> None:
    first = repositories.projects.ensure("proj_a", "Beach House")
    second = repositories.projects.ensure("proj_a", "Ignored Rename")
    assert second.created_at == first.created_at
    assert second.display_name == "Beach House"


def test_the_latest_scene_version_is_remembered(repositories) -> None:
    repositories.projects.ensure("proj_a", "Beach House")
    repositories.projects.record_scene_version("proj_a", "sha256:abc")
    assert repositories.projects.get("proj_a").latest_scene_version == "sha256:abc"


# --- references -----------------------------------------------------------


def _reference(**overrides: object) -> ReferenceRecord:
    base: dict[str, object] = {
        "reference_id": "ref_plan01",
        "project_id": "proj_a",
        "kind": PDF,
        "display_name": "floor-plan.pdf",
        "media_type": "application/pdf",
        "size_bytes": 1024,
        "sha256": "a" * 64,
        "stored_name": "ref_plan01.pdf",
    }
    base.update(overrides)
    return ReferenceRecord(**base)  # type: ignore[arg-type]


def test_references_are_listed_without_their_derived_pages(repositories) -> None:
    repositories.projects.ensure("proj_a", "Beach House")
    repositories.references.add(_reference(page_count=2))
    for page in (1, 2):
        repositories.references.add(
            _reference(
                reference_id=f"ref_page{page}",
                kind=PDF_PAGE,
                display_name="floor-plan.pdf",
                media_type="image/png",
                stored_name=f"ref_page{page}.png",
                parent_reference_id="ref_plan01",
                page_number=page,
            )
        )

    top_level = repositories.references.list_for_project("proj_a")
    assert [record.reference_id for record in top_level] == ["ref_plan01"]

    everything = repositories.references.list_for_project("proj_a", include_pages=True)
    assert len(everything) == 3

    pages = repositories.references.pages_of("proj_a", "ref_plan01")
    assert [page.page_number for page in pages] == [1, 2]


def test_a_reference_snapshot_carries_no_filesystem_path(repositories) -> None:
    record = _reference()
    snapshot = record.snapshot()
    assert "stored_name" not in snapshot
    assert "path" not in snapshot
    assert "sha256" not in snapshot
    serialized = json.dumps(snapshot)
    assert "runtime" not in serialized
    assert "/home" not in serialized


def test_a_page_label_mentions_its_page_number() -> None:
    page = _reference(kind=PDF_PAGE, page_number=2)
    assert page.label == "floor-plan.pdf (page 2)"
    assert page.is_image


def test_identical_bytes_are_found_by_hash_so_work_is_not_repeated(repositories) -> None:
    repositories.projects.ensure("proj_a", "Beach House")
    repositories.references.add(_reference())
    found = repositories.references.find_by_hash("proj_a", "a" * 64)
    assert found is not None and found.reference_id == "ref_plan01"
    assert repositories.references.find_by_hash("proj_a", "b" * 64) is None


def test_a_reference_from_another_project_is_not_reachable(repositories) -> None:
    repositories.projects.ensure("proj_a", "A")
    repositories.projects.ensure("proj_b", "B")
    repositories.references.add(_reference())
    assert repositories.references.get("proj_b", "ref_plan01") is None
    assert repositories.references.resolve_many("proj_b", ["ref_plan01"]) == ()


def test_deleting_a_pdf_also_removes_its_pages(repositories) -> None:
    repositories.projects.ensure("proj_a", "A")
    repositories.references.add(_reference(page_count=1))
    repositories.references.add(
        _reference(
            reference_id="ref_page1",
            kind=PDF_PAGE,
            stored_name="ref_page1.png",
            parent_reference_id="ref_plan01",
            page_number=1,
        )
    )
    removed = repositories.references.delete("proj_a", "ref_plan01")
    assert set(removed) == {"ref_plan01.pdf", "ref_page1.png"}
    assert repositories.references.list_for_project("proj_a", include_pages=True) == ()


# --- design facts ---------------------------------------------------------


def test_a_fact_survives_being_restated(repositories) -> None:
    repositories.projects.ensure("proj_a", "A")
    repositories.facts.set("proj_a", "ceiling_height_m", "2.4")
    repositories.facts.set("proj_a", "ceiling_height_m", "2.7")
    assert repositories.facts.get("proj_a", "ceiling_height_m").value == "2.7"
    assert repositories.facts.as_mapping("proj_a") == {"ceiling_height_m": "2.7"}


def test_facts_are_scoped_to_their_project(repositories) -> None:
    repositories.projects.ensure("proj_a", "A")
    repositories.projects.ensure("proj_b", "B")
    repositories.facts.set("proj_a", "ceiling_height_m", "2.4")
    assert repositories.facts.as_mapping("proj_b") == {}


def test_facts_record_who_stated_them(repositories) -> None:
    repositories.projects.ensure("proj_a", "A")
    repositories.facts.set("proj_a", "plan_scale", "1:50", source="agent")
    assert repositories.facts.get("proj_a", "plan_scale").source == "agent"


# --- conversation ---------------------------------------------------------


def test_recent_conversation_is_bounded_and_ordered_oldest_first(repositories) -> None:
    repositories.projects.ensure("proj_a", "A")
    for index in range(20):
        repositories.conversation.append(
            ConversationTurnRecord(
                project_id="proj_a", session_id="s", role="user", text=f"message {index}"
            )
        )
    recent = repositories.conversation.recent("proj_a", limit=5)
    assert [turn.text for turn in recent] == [
        "message 15",
        "message 16",
        "message 17",
        "message 18",
        "message 19",
    ]
    assert repositories.conversation.count("proj_a") == 20


# --- clarifications -------------------------------------------------------


def test_an_open_clarification_is_found_and_can_be_resolved_once(repositories) -> None:
    repositories.projects.ensure("proj_a", "A")
    repositories.clarifications.add(
        ClarificationRecord(
            clarification_id="clr_1",
            project_id="proj_a",
            session_id="s",
            question="What is the ceiling height?",
            missing_information=("ceiling_height_m",),
        )
    )
    open_question = repositories.clarifications.open_for_project("proj_a")
    assert open_question is not None
    assert open_question.missing_information == ("ceiling_height_m",)
    assert not open_question.resolved

    repositories.clarifications.resolve("proj_a", "clr_1")
    assert repositories.clarifications.open_for_project("proj_a") is None
    assert repositories.clarifications.get("proj_a", "clr_1").resolved


# --- approvals ------------------------------------------------------------


def _approval(**overrides: object) -> ApprovalRecord:
    base: dict[str, object] = {
        "approval_id": "apr_1",
        "project_id": "proj_a",
        "session_id": "s",
        "code": "import os\nprint(os.getcwd())\n",
        "summary": "This step imports os.",
        "reasons": ("line 1: imports os",),
        "operations": ({"capability": "execute_blender_python"},),
    }
    base.update(overrides)
    return ApprovalRecord(**base)  # type: ignore[arg-type]


def test_an_approval_starts_pending_and_keeps_the_code_verbatim(repositories) -> None:
    repositories.projects.ensure("proj_a", "A")
    repositories.approvals.add(_approval())
    stored = repositories.approvals.get("proj_a", "apr_1")
    assert stored is not None
    assert stored.is_pending
    assert stored.code == "import os\nprint(os.getcwd())\n"
    assert stored.reasons == ("line 1: imports os",)
    assert stored.operations[0]["capability"] == "execute_blender_python"


def test_an_approval_can_be_decided_exactly_once(repositories) -> None:
    repositories.projects.ensure("proj_a", "A")
    repositories.approvals.add(_approval())

    decided = repositories.approvals.decide("proj_a", "apr_1", True)
    assert decided is not None and decided.decision == APPROVED

    again = repositories.approvals.decide("proj_a", "apr_1", False)
    assert again is None, "a decided approval must not be decidable again"
    assert repositories.approvals.get("proj_a", "apr_1").decision == APPROVED


def test_a_rejection_is_recorded_and_the_approval_leaves_the_open_list(repositories) -> None:
    repositories.projects.ensure("proj_a", "A")
    repositories.approvals.add(_approval())
    assert len(repositories.approvals.open_for_project("proj_a")) == 1

    repositories.approvals.decide("proj_a", "apr_1", False)
    assert repositories.approvals.open_for_project("proj_a") == ()
    assert repositories.approvals.get("proj_a", "apr_1").decision == REJECTED


def test_an_approval_in_another_project_is_not_decidable(repositories) -> None:
    repositories.projects.ensure("proj_a", "A")
    repositories.projects.ensure("proj_b", "B")
    repositories.approvals.add(_approval())
    assert repositories.approvals.decide("proj_b", "apr_1", True) is None
    assert repositories.approvals.get("proj_a", "apr_1").is_pending


# --- analysis cache -------------------------------------------------------


def test_an_analysis_is_cached_by_content_so_credits_are_not_burned(repositories) -> None:
    repositories.projects.ensure("proj_a", "A")
    fingerprint = repositories.analyses.fingerprint("what do you see?", ["hash_a", "hash_b"])
    assert repositories.analyses.get("proj_a", fingerprint) is None

    repositories.analyses.put("proj_a", fingerprint, {"plan_type": "floor_plan"})
    cached = repositories.analyses.get("proj_a", fingerprint)
    assert cached is not None
    assert cached.analysis["plan_type"] == "floor_plan"


def test_the_analysis_fingerprint_ignores_reference_order(repositories) -> None:
    first = repositories.analyses.fingerprint("q", ["a", "b"])
    second = repositories.analyses.fingerprint("q", ["b", "a"])
    assert first == second


def test_a_different_question_is_a_different_fingerprint(repositories) -> None:
    assert repositories.analyses.fingerprint("q1", ["a"]) != repositories.analyses.fingerprint(
        "q2", ["a"]
    )


# --- artifact index -------------------------------------------------------


def test_the_latest_artifact_of_each_type_is_one_query(repositories) -> None:
    repositories.projects.ensure("proj_a", "A")
    repositories.artifacts.record(
        "proj_a", "preview_1", "preview_image", "image/png", created_at="2026-01-01T00:00:00Z"
    )
    repositories.artifacts.record(
        "proj_a", "model_1", "model_glb", "model/gltf-binary", created_at="2026-01-01T00:01:00Z"
    )
    repositories.artifacts.record(
        "proj_a", "model_2", "model_glb", "model/gltf-binary", created_at="2026-01-01T00:02:00Z"
    )

    latest_model = repositories.artifacts.latest("proj_a", "model_glb")
    assert latest_model is not None and latest_model["artifact_id"] == "model_2"
    latest_preview = repositories.artifacts.latest("proj_a", "preview_image")
    assert latest_preview is not None and latest_preview["artifact_id"] == "preview_1"
    assert repositories.artifacts.latest("proj_a", "nothing_like_this") is None


# --- the job store --------------------------------------------------------


def _job_wire(job_id: str = "job_1", request_id: str = "req_1", index: int = 0) -> dict:
    return {
        "job_id": job_id,
        "job_type": "move_object",
        "project_id": "proj_a",
        "session_id": "sess_1",
        "user_id": "user_dev_local",
        "payload": {"target": {"name": "Cube"}, "delta_meters": {"x": 0.5, "y": 0.0, "z": 0.0}},
        "origin": {"request_id": request_id, "operation_index": index},
        "status": "queued",
        "idempotency_key": f"idem_{request_id}_{index}",
        "created_at": "2026-01-01T00:00:00Z",
    }


@pytest.fixture()
def job_store(database: StudioDatabase) -> SqliteJobRecordStore:
    return SqliteJobRecordStore(database)


def test_the_sqlite_store_satisfies_the_job_record_store_protocol(job_store) -> None:
    from studio_api.job_records import JobRecordStore

    assert isinstance(job_store, JobRecordStore)


def test_submitting_the_same_mutation_identity_twice_returns_the_first_record(job_store) -> None:
    first, duplicate_first = job_store.submit(record_from_job(_job_wire()))
    assert duplicate_first is False

    second, duplicate_second = job_store.submit(record_from_job(_job_wire()))
    assert duplicate_second is True, "the second submission must be reported as duplicate"
    assert second.job_id == first.job_id
    assert len(job_store.all_records()) == 1, "a duplicate must not create a second record"


def test_a_job_is_only_reachable_with_its_project(job_store) -> None:
    job_store.submit(record_from_job(_job_wire()))
    assert job_store.get("proj_a", "job_1") is not None
    assert job_store.get("proj_other", "job_1") is None


def test_updates_are_persisted(job_store) -> None:
    record, _ = job_store.submit(record_from_job(_job_wire()))
    record.job_status = "succeeded"
    record.worker_id = "worker_1"
    record.result = {"applied": True}
    record.preview = {"artifact_id": "preview_1"}
    job_store.save(record)

    reloaded = job_store.get("proj_a", "job_1")
    assert reloaded is not None
    assert reloaded.job_status == "succeeded"
    assert reloaded.worker_id == "worker_1"
    assert reloaded.result == {"applied": True}
    assert reloaded.preview == {"artifact_id": "preview_1"}


def test_job_records_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "studio.sqlite3"
    first = StudioDatabase(path)
    SqliteJobRecordStore(first).submit(record_from_job(_job_wire()))
    first.close()

    reopened = SqliteJobRecordStore(StudioDatabase(path))
    record = reopened.get("proj_a", "job_1")
    assert record is not None
    assert record.job["payload"]["target"]["name"] == "Cube"


def test_a_resubmitted_request_after_a_restart_is_still_a_duplicate(tmp_path: Path) -> None:
    """The property that stops a restart causing a second Blender mutation."""
    path = tmp_path / "studio.sqlite3"
    first = StudioDatabase(path)
    SqliteJobRecordStore(first).submit(record_from_job(_job_wire()))
    first.close()

    store = SqliteJobRecordStore(StudioDatabase(path))
    _, duplicate = store.submit(record_from_job(_job_wire()))
    assert duplicate is True


def test_jobs_for_one_request_come_back_in_execution_order(job_store) -> None:
    for index in (2, 0, 1):
        job_store.submit(
            record_from_job(_job_wire(job_id=f"job_{index}", request_id="req_multi", index=index))
        )
    ordered = job_store.for_request("proj_a", "req_multi")
    assert [record.operation_index for record in ordered] == [0, 1, 2]


# --- reference byte storage ----------------------------------------------


@pytest.fixture()
def file_store(tmp_path: Path) -> ReferenceFileStore:
    return ReferenceFileStore(tmp_path / "references")


def test_bytes_round_trip_under_a_platform_derived_name(file_store) -> None:
    stored_name = file_store.build_stored_name("ref_abc123", "floor plan.png")
    assert stored_name == "ref_abc123.png"

    file_store.write("proj_a", stored_name, b"image-bytes")
    assert file_store.read("proj_a", stored_name) == b"image-bytes"
    assert file_store.exists("proj_a", stored_name)


def test_the_stored_name_never_contains_the_uploaded_filename(file_store) -> None:
    stored_name = file_store.build_stored_name("ref_abc123", "../../etc/passwd.png")
    assert stored_name == "ref_abc123.png"
    assert ".." not in stored_name


def test_an_unknown_extension_is_dropped_rather_than_trusted(file_store) -> None:
    assert file_store.build_stored_name("ref_abc123", "payload.exe") == "ref_abc123"
    assert file_store.build_stored_name("ref_abc123", "archive.zip") == "ref_abc123"


@pytest.mark.parametrize(
    "stored_name", ["../escape.png", "nested/file.png", "", ".", "..", "back\\slash.png"]
)
def test_a_traversing_stored_name_is_refused(file_store, stored_name: str) -> None:
    with pytest.raises(ReferenceStorageError):
        file_store.write("proj_a", stored_name, b"x")
    assert file_store.read("proj_a", stored_name) is None


@pytest.mark.parametrize("project_id", ["../other", "with/slash", "", ".", "a" * 200])
def test_an_unsafe_project_id_is_refused(file_store, project_id: str) -> None:
    with pytest.raises(ReferenceStorageError):
        file_store.project_directory(project_id)


@pytest.mark.parametrize("reference_id", ["Ref_Upper", "ref with space", "..", "ab", "ref/slash"])
def test_an_unsafe_reference_id_is_refused(file_store, reference_id: str) -> None:
    with pytest.raises(ReferenceStorageError):
        file_store.build_stored_name(reference_id, "x.png")


def test_reading_a_missing_reference_returns_nothing_rather_than_raising(file_store) -> None:
    assert file_store.read("proj_a", "ref_missing.png") is None
    assert file_store.path_for("proj_a", "ref_missing.png") is None
    assert file_store.delete("proj_a", "ref_missing.png") is False


def test_a_partial_write_is_never_visible(file_store) -> None:
    stored_name = file_store.build_stored_name("ref_abc123", "x.png")
    file_store.write("proj_a", stored_name, b"first")
    file_store.write("proj_a", stored_name, b"second-and-longer")
    assert file_store.read("proj_a", stored_name) == b"second-and-longer"
    leftovers = list((file_store.project_directory("proj_a")).glob("*.partial"))
    assert leftovers == []


# --- filename sanitisation ----------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("floor plan.pdf", "floor plan.pdf"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\plan.png", "plan.png"),
        ("  spaced  .png  ", "spaced .png"),
        ("", "reference"),
        ("...", "reference"),
        ("a\u0000b.png", "ab.png"),
        ("plan\u202egnp.exe", "plangnp.exe"),
        ("many    spaces.png", "many spaces.png"),
    ],
)
def test_display_names_are_sanitised(raw: str, expected: str) -> None:
    assert sanitise_display_name(raw) == expected


def test_a_very_long_display_name_is_truncated() -> None:
    assert len(sanitise_display_name("x" * 500)) <= 120


def test_media_types_are_recognised_only_for_supported_extensions() -> None:
    assert media_type_for("plan.pdf") == "application/pdf"
    assert media_type_for("photo.JPG") == "image/jpeg"
    assert media_type_for("notes.md") == "text/markdown"
    assert media_type_for("payload.exe") is None
    assert media_type_for("archive.zip") is None


def test_the_checksum_is_a_sha256_hex_digest() -> None:
    digest = checksum_of(b"hello")
    assert len(digest) == 64
    assert digest == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
