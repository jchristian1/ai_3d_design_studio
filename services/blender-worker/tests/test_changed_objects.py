"""Working out what a plan changed, so the preview can show THAT.

A preview framed on the whole site is honest and nearly useless for judging a change.
Warm downlights added over a reception desk, framed with eighty metres of clinic around
them, came out as a bright postage stamp — reported, reasonably, as "I did not see any
change". The picture was correct; it was about the wrong thing.

These are the comparisons that decide what the picture is about.
"""

from __future__ import annotations

from typing import Any, Optional

from blender_worker.capability_executor import changed_object_ids


def obj(
    object_id: str,
    position: tuple[float, float, float] = (0.0, 0.0, 0.0),
    dimensions: tuple[float, float, float] = (1.0, 1.0, 1.0),
    material: Optional[str] = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "studio_object_id": object_id,
        "world_position_meters": dict(zip("xyz", position)),
        "dimensions_meters": dict(zip("xyz", dimensions)),
    }
    if material:
        entry["material"] = {"name": material}
    return entry


def scene(*objects: dict[str, Any]) -> dict[str, Any]:
    return {"objects": list(objects)}


def test_an_untouched_scene_has_nothing_to_show() -> None:
    before = scene(obj("a"), obj("b", position=(5.0, 0.0, 0.0)))
    assert changed_object_ids(before, before) == ()


def test_a_moved_object_is_reported() -> None:
    before = scene(obj("a"), obj("b", position=(5.0, 0.0, 0.0)))
    after = scene(obj("a"), obj("b", position=(5.5, 0.0, 0.0)))
    assert changed_object_ids(before, after) == ("b",)


def test_a_resized_object_is_reported() -> None:
    before = scene(obj("a"))
    after = scene(obj("a", dimensions=(2.0, 1.0, 1.0)))
    assert changed_object_ids(before, after) == ("a",)


def test_a_newly_created_object_is_reported() -> None:
    before = scene(obj("a"))
    after = scene(obj("a"), obj("desk"))
    assert changed_object_ids(before, after) == ("desk",)


def test_recladding_a_surface_is_reported() -> None:
    """Material matters as much as geometry here.

    Re-cladding changes nothing about an object's position or size, and it is exactly
    the kind of turn worth a close look — it is the whole point of asking for polished
    stone instead of oak.
    """
    before = scene(obj("floor", material="StudioMat_oak_floor_2.4"))
    after = scene(obj("floor", material="StudioMat_polished_stone_1.4"))
    assert changed_object_ids(before, after) == ("floor",)


def test_a_movement_below_tolerance_is_not_a_change() -> None:
    """Float noise must not drag the camera around."""
    before = scene(obj("a"))
    after = scene(obj("a", position=(1e-9, 0.0, 0.0)))
    assert changed_object_ids(before, after) == ()


def test_a_deleted_object_is_not_focused_on() -> None:
    """Nothing can be framed where an object used to be.

    Deletion is a real change, but pointing a camera at an absence would frame empty
    air. A plan that only deletes therefore falls back to the site view, which is the
    honest picture of it.
    """
    before = scene(obj("a"), obj("b", position=(5.0, 0.0, 0.0)))
    after = scene(obj("a"))
    assert changed_object_ids(before, after) == ()


def test_the_first_read_of_a_project_reports_everything() -> None:
    before = None
    after = scene(obj("a"), obj("b"))
    assert changed_object_ids(before, after) == ("a", "b")


def test_objects_without_a_stable_id_are_skipped() -> None:
    """There is nothing to point the preview at without an id."""
    after = {"objects": [{"name": "Anonymous"}, obj("a")]}
    assert changed_object_ids(scene(), after) == ("a",)


def test_the_result_is_ordered_so_a_retry_frames_identically() -> None:
    before = scene()
    after = scene(obj("z"), obj("a"), obj("m"))
    assert changed_object_ids(before, after) == ("a", "m", "z")


def test_several_changes_are_all_reported() -> None:
    """A plan usually touches a group, and the framing should cover the group."""
    before = scene(
        obj("desk", material="StudioMat_dark_wood_2"),
        obj("floor", material="StudioMat_oak_floor_2.4"),
        obj("wall"),
    )
    after = scene(
        obj("desk", material="StudioMat_pale_veneer_1.6"),
        obj("floor", material="StudioMat_polished_stone_1.4"),
        obj("wall"),
    )
    assert changed_object_ids(before, after) == ("desk", "floor")
