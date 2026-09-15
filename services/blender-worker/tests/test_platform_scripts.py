"""Platform-owned Blender scripts: fixed source, safely encoded parameters.

The load-bearing property proved here is *structural invariance*: a hostile parameter
value cannot change the shape of the program it is substituted into. It arrives as one
Python literal in one place, so the worst a hostile value can do is be a strange
literal.
"""

from __future__ import annotations

import ast

import pytest

from blender_worker.backends.official import scripts


def parse(source: str) -> ast.AST:
    return ast.parse(source)


def structure(source: str) -> str:
    """The program's shape with every constant blanked out."""

    class Blank(ast.NodeTransformer):
        def visit_Constant(self, node: ast.Constant) -> ast.Constant:
            return ast.copy_location(ast.Constant(value=None), node)

    tree = Blank().visit(parse(source))
    ast.fix_missing_locations(tree)
    return ast.dump(tree, annotate_fields=True)


def test_every_script_renders_to_valid_python() -> None:
    minimal = {
        "read_scene": {},
        "read_object": {"object_id": "obj_1", "name": "Cube"},
        "move_object": {"name": "Cube", "desired_position_meters": {"x": 0.5, "y": 0.0, "z": 0.0}},
        "rotate_object": {"name": "Cube", "desired_rotation_radians": {"x": 0.0, "y": 0.0, "z": 0.785}},
        "scale_object": {"name": "Cube", "desired_scale": {"x": 1.0, "y": 1.0, "z": 1.0}},
        "set_object_dimensions": {
            "name": "Cube",
            "desired_dimensions_meters": {"x": 1.6, "y": 1.6, "z": 1.6},
        },
        "create_object": {
            "primitive": "cube",
            "display_name": "Box",
            "object_id": "obj_2",
            "position_meters": {"x": 0.0, "y": 0.0, "z": 0.0},
            "dimensions_meters": None,
        },
        "duplicate_object": {
            "name": "Cube",
            "display_name": "Cube2",
            "object_id": "obj_3",
            "offset_meters": None,
        },
        "delete_object": {"name": "Cube"},
        "set_material_color": {
            "name": "Cube",
            "color_linear_srgb": {"r": 0.5, "g": 0.4, "b": 0.3, "a": 1.0},
        },
        "create_wall": {
            "display_name": "Wall",
            "object_id": "obj_w",
            "start_meters": {"x": 0.0, "y": 0.0},
            "end_meters": {"x": 4.0, "y": 0.0},
            "height_meters": 2.4,
            "thickness_meters": 0.12,
            "base_elevation_meters": 0.0,
            "color_linear_srgb": None,
        },
        "create_slab": {
            "display_name": "Floor",
            "object_id": "obj_f",
            "footprint_meters": [{"x": 0.0, "y": 0.0}, {"x": 4.0, "y": 0.0}, {"x": 4.0, "y": 3.0}],
            "thickness_meters": 0.2,
            "elevation_meters": 0.0,
            "grow": "down",
            "color_linear_srgb": None,
        },
        "create_opening": {
            "wall": {"object_id": "obj_w", "name": None},
            "centre_meters": {"x": 1.0, "y": 0.0, "z": 1.0},
            "width_meters": 0.9,
            "height_meters": 2.1,
            "wall_thickness_meters": 0.12,
        },
        "create_placeholder": {
            "display_name": "Door",
            "object_id": "obj_d",
            "element_type": "door",
            "centre_meters": {"x": 1.0, "y": 0.0, "z": 1.05},
            "width_meters": 0.9,
            "height_meters": 2.1,
            "depth_meters": 0.05,
            "rotation_z_radians": 0.0,
            "color_linear_srgb": None,
        },
        "export_glb": {"output_path": "/tmp/out.glb"},
    }
    assert set(minimal) == set(scripts.SCRIPTS), "every script needs a smoke case"
    for name, params in minimal.items():
        rendered = scripts.render(name, params)
        parse(rendered)  # raises SyntaxError if the template is malformed
        assert scripts.PARAMS_PLACEHOLDER not in rendered


def test_a_hostile_object_name_cannot_change_the_program_structure() -> None:
    benign = scripts.render("move_object", {
        "name": "Cube",
        "object_id": None,
        "desired_position_meters": {"x": 0.0, "y": 0.0, "z": 0.0},
    })
    hostile_names = [
        "'; import os; os.system('rm -rf ~') #",
        '"}\nimport os\n#',
        "Cube\\n__import__('os').system('x')",
        "Cube' + str(__import__('os')) + '",
        "\n\nimport shutil\nshutil.rmtree('/')\n",
    ]
    for name in hostile_names:
        hostile = scripts.render("move_object", {
            "name": name,
            "object_id": None,
            "desired_position_meters": {"x": 0.0, "y": 0.0, "z": 0.0},
        })
        parse(hostile)
        assert structure(hostile) == structure(benign), (
            f"hostile name changed the program structure: {name!r}"
        )
        # The hostile text exists only as inert string data. Nothing it contains
        # becomes an import, a call, or any other executable node.
        tree = parse(hostile)
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert imported == {"bpy", "math"}, f"unexpected imports {imported}"
        assert name in {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }, "the hostile value should survive as a string constant"


def test_rendered_scripts_contain_no_dynamic_source_construction() -> None:
    """No template may build Python from anything at runtime."""
    for name in scripts.SCRIPTS:
        source = scripts.SCRIPTS[name]
        tree = parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"eval", "exec", "compile", "__import__"}, (
                    f"{name} uses {node.func.id}"
                )
            if isinstance(node, ast.JoinedStr):
                raise AssertionError(f"{name} builds a string with an f-string")


@pytest.mark.parametrize(
    "value",
    [
        object(),
        {1: "non-string key"},
        [object()],
        float("inf"),
        float("nan"),
        {"nested": {"deeper": object()}},
    ],
)
def test_unencodable_parameters_are_refused(value: object) -> None:
    with pytest.raises(scripts.ScriptParameterError):
        scripts.render("read_object", {"name": "Cube", "bad": value})


def test_unknown_script_names_are_refused() -> None:
    with pytest.raises(KeyError):
        scripts.render("definitely_not_a_script", {})


def test_every_mutating_script_saves_the_project() -> None:
    """A background Blender discards unsaved changes, so a save is mandatory."""
    mutating = [
        "move_object",
        "rotate_object",
        "scale_object",
        "set_object_dimensions",
        "create_object",
        "duplicate_object",
        "delete_object",
        "set_material_color",
        "create_wall",
        "create_slab",
        "create_opening",
        "create_placeholder",
    ]
    for name in mutating:
        assert "_save()" in scripts.SCRIPTS[name], f"{name} never saves"


def test_read_scripts_do_not_save() -> None:
    for name in ("read_scene", "read_object"):
        assert "_save()" not in scripts.SCRIPTS[name], f"{name} must not save"


def test_read_scene_reports_metres_as_the_canonical_length_unit() -> None:
    rendered = scripts.render("read_scene", {})
    assert '"length_unit": "m"' in rendered
