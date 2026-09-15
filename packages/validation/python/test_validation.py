"""Tests for the Python validation mirror (Spec 001, Task 1)."""

from studio_types import Vec3
from studio_validation import (
    is_finite_meters,
    is_finite_vec3,
    is_non_empty_string,
    validate_chat_request,
    validate_meter_deltas,
)


def test_valid_chat_request_passes():
    r = validate_chat_request(
        {
            "project_id": "proj_1",
            "session_id": "sess_1",
            "message": "Move Cube 50 cm to the right",
        }
    )
    assert r.valid is True
    assert r.errors == []


def test_missing_project_id_rejected():
    r = validate_chat_request({"session_id": "s", "message": "hi"})
    assert r.valid is False
    assert any(e.field == "project_id" for e in r.errors)


def test_whitespace_project_id_rejected():
    r = validate_chat_request(
        {"project_id": "   ", "session_id": "s", "message": "hi"}
    )
    assert r.valid is False
    assert any(e.field == "project_id" for e in r.errors)


def test_non_object_body_rejected():
    assert validate_chat_request(None).valid is False
    assert validate_chat_request("nope").valid is False


def test_selected_object_id_must_be_non_empty_when_present():
    r = validate_chat_request(
        {
            "project_id": "p",
            "session_id": "s",
            "message": "m",
            "selected_object_id": "",
        }
    )
    assert r.valid is False
    assert any(e.field == "selected_object_id" for e in r.errors)


def test_accepts_object_with_attributes():
    from studio_contracts import ChatRequest

    r = validate_chat_request(
        ChatRequest(project_id="p", session_id="s", message="m")
    )
    assert r.valid is True


def test_is_finite_meters_rejects_nan_inf_bool_and_non_numbers():
    assert is_finite_meters(0.5) is True
    assert is_finite_meters(0) is True
    assert is_finite_meters(-1.25) is True
    assert is_finite_meters(float("nan")) is False
    assert is_finite_meters(float("inf")) is False
    assert is_finite_meters(float("-inf")) is False
    assert is_finite_meters("0.5") is False
    assert is_finite_meters(None) is False
    assert is_finite_meters(True) is False


def test_is_finite_vec3():
    assert is_finite_vec3(Vec3(0, 0, 0)) is True
    assert is_finite_vec3({"x": 0.5, "y": 1, "z": -2}) is True
    assert is_finite_vec3({"x": 0, "y": float("nan"), "z": 0}) is False
    assert is_finite_vec3({"x": 0, "y": 0}) is False
    assert is_finite_vec3(None) is False


def test_validate_meter_deltas():
    assert validate_meter_deltas(delta_x_m=0.5).valid is True
    assert validate_meter_deltas().valid is True
    bad = validate_meter_deltas(delta_x_m=float("inf"), delta_z_m=float("nan"))
    assert bad.valid is False
    assert len(bad.errors) == 2


def test_is_non_empty_string():
    assert is_non_empty_string("x") is True
    assert is_non_empty_string("  ") is False
    assert is_non_empty_string(5) is False
