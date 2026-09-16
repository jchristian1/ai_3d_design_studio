"""The classifier decides what runs unattended and what the user must see.

These tests are the specification of that boundary. They deliberately include the
cases that defeat the classifier, asserted as *known limitations*, so nobody later
mistakes it for a sandbox.
"""

from __future__ import annotations

import pytest

from blender_worker.capability import classifier


def assess(code: str) -> classifier.RiskAssessment:
    return classifier.classify_python(code)


# --- ordinary scene work runs unattended ---------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "import bpy\nbpy.ops.mesh.primitive_cube_add(size=2.0)\n",
        "import bpy\nobj = bpy.data.objects['Cube']\nobj.location = (0.5, 0.0, 0.0)\n",
        "import bpy\nimport math\nobj = bpy.data.objects['Cube']\n"
        "obj.rotation_euler = (0.0, 0.0, math.pi / 4)\n",
        "import bpy, bmesh\nmesh = bpy.data.meshes.new('m')\n",
        "import bpy\nfrom mathutils import Vector\nv = Vector((1.0, 0.0, 0.0))\n",
        "import bpy\nbpy.ops.wm.save_mainfile()\n",
        "import bpy\nresult = {'count': len(bpy.data.objects)}\n",
    ],
)
def test_scene_only_code_runs_without_approval(code: str) -> None:
    assessment = assess(code)
    assert assessment.decision == classifier.AUTO
    assert assessment.may_run_unattended
    assert assessment.findings == ()


def test_wall_building_loop_is_scene_only() -> None:
    code = (
        "import bpy\n"
        "import math\n"
        "walls = [((0.0, 0.0), (4.0, 0.0)), ((4.0, 0.0), (4.0, 3.0))]\n"
        "for index, (start, end) in enumerate(walls):\n"
        "    dx = end[0] - start[0]\n"
        "    dy = end[1] - start[1]\n"
        "    length = math.hypot(dx, dy)\n"
        "    bpy.ops.mesh.primitive_cube_add(size=1.0)\n"
        "    obj = bpy.context.active_object\n"
        "    obj.name = 'Wall_%d' % index\n"
        "    obj.scale = (length, 0.12, 2.4)\n"
        "bpy.ops.wm.save_mainfile()\n"
    )
    assert assess(code).decision == classifier.AUTO


# --- reaching further than the scene needs approval ----------------------


@pytest.mark.parametrize(
    "code, expected_fragment",
    [
        ("import os\nos.remove('/tmp/x')\n", "imports os"),
        ("import subprocess\nsubprocess.run(['ls'])\n", "imports subprocess"),
        ("import shutil\nshutil.rmtree('/home')\n", "imports shutil"),
        ("import socket\ns = socket.socket()\n", "imports socket"),
        ("from urllib.request import urlopen\n", "imports urllib.request"),
        ("import bpy\neval('1 + 1')\n", "calls eval()"),
        ("import bpy\nexec('x = 1')\n", "calls exec()"),
        ("import bpy\n__import__('os')\n", "calls __import__()"),
        ("import bpy\nf = open('/etc/passwd')\n", "calls open()"),
        ("import bpy\nbpy.ops.wm.read_factory_settings()\n", "read_factory_settings"),
        ("import bpy\nbpy.ops.wm.quit_blender()\n", "quit_blender"),
        (
            "import bpy\nbpy.ops.wm.save_as_mainfile(filepath='/tmp/elsewhere.blend')\n",
            "save_as_mainfile",
        ),
        ("import bpy\nprint(bpy.app.binary_path)\n", "binary_path"),
        ("import bpy\nname = '/home/christian/secret.txt'\n", "absolute path"),
    ],
)
def test_reaching_outside_the_scene_requires_approval(
    code: str, expected_fragment: str
) -> None:
    assessment = assess(code)
    assert assessment.decision == classifier.APPROVAL_REQUIRED
    assert assessment.requires_approval
    assert any(expected_fragment in reason for reason in assessment.reasons()), (
        f"expected a finding mentioning {expected_fragment!r}, got {assessment.reasons()}"
    )


def test_the_home_directory_deletion_attempt_requires_approval() -> None:
    """The exact scenario from the product's security requirement."""
    code = (
        "import shutil\n"
        "import os\n"
        "shutil.rmtree(os.path.expanduser('~'))\n"
    )
    assessment = assess(code)
    assert assessment.requires_approval
    reasons = " ".join(assessment.reasons())
    assert "shutil" in reasons
    assert "os" in reasons
    # The user sees a plain-language explanation, not an AST dump.
    assert assessment.summary().startswith("This step ")


def test_findings_carry_line_numbers_and_are_ordered() -> None:
    code = "import bpy\nimport os\nimport socket\n"
    assessment = assess(code)
    lines = [finding.line for finding in assessment.findings]
    assert lines == sorted(lines)
    assert lines[0] == 2
    assert all(reason.startswith("line ") for reason in assessment.reasons())


def test_duplicate_findings_are_collapsed() -> None:
    code = "import bpy\nos_calls = 0\nimport os\nimport os\n"
    assessment = assess(code)
    import_findings = [f for f in assessment.findings if "imports os" in f.reason]
    # Same reason on different lines stays distinct; the same reason on the same line
    # is collapsed. Two separate import lines therefore yield two findings.
    assert len(import_findings) == 2


# --- invalid code cannot run at all -------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "import bpy\nobj = bpy.data.objects[\n",
        "def (\n",
        "if True\n    pass\n",
        "",
        "   ",
    ],
)
def test_unparseable_code_is_refused(code: str) -> None:
    assessment = assess(code)
    assert assessment.decision == classifier.REFUSED
    assert assessment.refused
    assert not assessment.may_run_unattended
    assert assessment.summary() == classifier.REASON_SYNTAX


def test_non_string_code_is_refused() -> None:
    assert classifier.classify_python(None).refused  # type: ignore[arg-type]
    assert classifier.classify_python(42).refused  # type: ignore[arg-type]


# --- honest limitations, asserted so they stay documented ----------------


def test_obfuscated_dynamic_code_is_caught_by_the_dunder_rule() -> None:
    """``getattr(__builtins__, "ev" + "al")`` still trips the dunder finding.

    This is not because the classifier understands the trick, but because reaching
    interpreter internals is itself flagged. Recorded so the coverage is deliberate.
    """
    code = "import bpy\nfn = getattr(__builtins__, 'ev' + 'al')\nfn('1')\n"
    assert assess(code).requires_approval


def test_a_relative_path_write_is_not_flagged_and_this_is_a_known_gap() -> None:
    """A relative filename does not trip the absolute-path rule.

    ``open`` is flagged, so this specific example still requires approval, but a
    filesystem write reached through a Blender operator with a relative path would
    not be. The classifier is friction and visibility, not containment: the real
    boundary is running the worker as a restricted OS user.
    """
    flagged_by_open = assess("import bpy\nopen('notes.txt', 'w')\n")
    assert flagged_by_open.requires_approval

    not_flagged = assess("import bpy\nname = 'notes.txt'\n")
    assert not_flagged.decision == classifier.AUTO



# ---------------------------------------------------------------------------
# Deleting an object is scene work, not filesystem access
# ---------------------------------------------------------------------------
#
# From a real Astra turn: a plan that rebuilds a coffee table started by removing the
# objects it was about to replace, with `bpy.data.objects.remove(obj)`. That stopped for
# approval because the word "remove" was on the sensitive list. It is ordinary modelling —
# and a gate that fires on almost every rebuild teaches people to approve without reading,
# which is the one thing the gate must not do.


@pytest.mark.parametrize(
    "code",
    [
        "import bpy\nbpy.data.objects.remove(bpy.data.objects['Cube'], do_unlink=True)\n",
        "import bpy\nbpy.data.materials.remove(bpy.data.materials['Old'])\n",
        "import bpy\nbpy.data.meshes.remove(bpy.data.meshes['Mesh'])\n",
        "import bpy\nbpy.data.collections.clear()\n",
        "import bpy\nfor o in list(bpy.data.objects):\n    bpy.data.objects.remove(o, do_unlink=True)\n",
    ],
)
def test_removing_blender_data_runs_without_asking(code: str) -> None:
    assessment = classifier.classify_python(code)
    assert assessment.may_run_unattended, assessment.reasons()


@pytest.mark.parametrize(
    "code",
    [
        "import os\nos.remove('/tmp/x')\n",
        "import shutil\nshutil.rmtree('/tmp/x')\n",
        "from pathlib import Path\nPath('/tmp/x').unlink()\n",
        "import os\nos.rename('/tmp/a', '/tmp/b')\n",
        # Not rooted at bpy, so the platform has no reason to believe it is scene work.
        "import bpy\nhelper = get_helper()\nhelper.remove('/tmp/x')\n",
    ],
)
def test_removing_anything_else_still_needs_approval(code: str) -> None:
    assessment = classifier.classify_python(code)
    assert assessment.requires_approval, assessment.reasons()


def test_the_real_plan_that_prompted_this_now_runs_unattended() -> None:
    """The shape Astra actually produced: clear by id, then rebuild."""
    code = (
        "import bpy, math\n"
        "def clear_id(oid):\n"
        "    for o in list(bpy.data.objects):\n"
        "        if o.get('object_id') == oid:\n"
        "            bpy.data.objects.remove(o, do_unlink=True)\n"
        "clear_id('obj_tabletop')\n"
        "bpy.ops.mesh.primitive_cylinder_add(vertices=96, radius=0.6, depth=0.08,"
        " location=(0, 0, 0.41))\n"
    )
    assessment = classifier.classify_python(code)
    assert assessment.may_run_unattended, assessment.reasons()
