"""Risk classification for model-authored Blender Python.

## What this is, and what it is not

This is a **visibility and friction** layer, not a sandbox. It reads the model's
Python with :mod:`ast` before anything executes and decides whether the operation
runs immediately or has to be shown to the user for approval first.

It is deliberately honest about its limits, because the alternative — believing a
static classifier contains anything — is how people get hurt:

* A determined author defeats it trivially
  (``getattr(__builtins__, "ev" + "al")``, attribute chains built from strings,
  ``bpy.app.driver_namespace`` tricks, and so on).
* The official Blender MCP executes whatever code it is given. Its own
  ``weak_sandbox.py`` says it "isn't really a sandbox, more guidance that some
  things should not be done", and blocks only a handful of operators.
* Therefore the only real containment for this feature is at the operating-system
  level: run the worker and Blender as a restricted user, or in a container with
  just the project directory mounted.

What the classifier *does* buy, which is worth having:

* ordinary scene modelling runs without pestering the user,
* anything reaching the filesystem, the network, a subprocess, or the dynamic-code
  builtins is surfaced with the actual code and a plain-language reason,
* the decision is made on the platform side and recorded, so "the UI forgot to
  ask" cannot become an execution path.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Final, Iterable

#: The operation may run without asking the user.
AUTO: Final = "auto"
#: The operation must be shown to the user and explicitly approved first.
APPROVAL_REQUIRED: Final = "approval_required"
#: The operation cannot run at all (it is not valid Python).
REFUSED: Final = "refused"

DECISIONS: Final = (AUTO, APPROVAL_REQUIRED, REFUSED)

#: Modules ordinary Blender modelling code legitimately needs. Anything outside
#: this set is not forbidden — it just has to be shown to the user first.
SCENE_MODULES: Final = frozenset(
    {
        "bpy",
        "bpy_extras",
        "bmesh",
        "mathutils",
        "math",
        "cmath",
        "json",
        "re",
        "copy",
        "enum",
        "string",
        "textwrap",
        "typing",
        "dataclasses",
        "collections",
        "collections.abc",
        "itertools",
        "functools",
        "operator",
        "statistics",
        "decimal",
        "fractions",
        "random",
        "uuid",
        "unicodedata",
    }
)

#: Builtins that turn data into code, or reach outside the process.
SENSITIVE_BUILTINS: Final = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "open",
        "input",
        "breakpoint",
        "help",
    }
)

#: Builtins worth flagging even when only MENTIONED, because a bare reference can be
#: stored and invoked later (``handler = eval``), so requiring a visible call would be
#: trivially avoidable.
#:
#: ``input`` is deliberately absent. Writing shader code makes ``input`` an obvious
#: variable name — ``for input in bsdf.inputs`` — and flagging the word cost a user
#: approval prompt on a turn that never read from stdin. Calling ``input()`` is still
#: flagged by :meth:`_Walker.visit_Call`, which is the form that could actually block a
#: headless render waiting for a line that will never come.
SENSITIVE_BUILTINS_BY_REFERENCE: Final = SENSITIVE_BUILTINS - frozenset({"input"})

#: ``bpy.ops.wm.*`` operators that manage FILES and the application rather than the
#: scene. The platform owns opening and saving the project, so model code touching
#: these is always worth a look.
SENSITIVE_WM_OPERATORS: Final = frozenset(
    {
        "open_mainfile",
        "read_homefile",
        "read_factory_settings",
        "read_factory_userpref",
        "recover_last_session",
        "recover_auto_save",
        "save_as_mainfile",
        "save_homefile",
        "save_userpref",
        "quit_blender",
        "url_open",
        "path_open",
        "append",
        "link",
        "collection_export_all",
    }
)

#: Attribute names that expose the interpreter, the filesystem or the host.
SENSITIVE_ATTRIBUTES: Final = frozenset(
    {
        "binary_path",
        "binary_path_python",
        "driver_namespace",
        "execfile",
        "system",
        "popen",
        "spawn",
        "spawnl",
        "spawnv",
        "fork",
        "execv",
        "execve",
        "remove",
        # ``unlink`` is intentionally NOT here: on a Blender collection
        # (``collection.objects.unlink(obj)``) it is ordinary scene work, and static
        # analysis cannot follow a local variable back to bpy to tell the two apart.
        # The genuinely dangerous forms — ``os.unlink``, ``pathlib.Path.unlink`` — are
        # already caught by the import gate, because os/shutil/pathlib are outside
        # SCENE_MODULES. Flagging the bare method name only produced false positives on
        # normal modelling code and drove needless approval prompts.
        "rmtree",
        "rmdir",
        "chmod",
        "chown",
        "rename",
        # ``replace`` is intentionally NOT here: ``str.replace`` is ubiquitous in scene
        # code (building names, formatting) and Blender collections expose ``replace``
        # too. The dangerous ``os.replace`` is already caught by the import gate. See the
        # ``unlink`` note above — flagging the bare method name only produced false
        # positives and needless approval prompts.
        "symlink",
        "urlopen",
        "urlretrieve",
        "socket",
        "check_output",
        "check_call",
        "run",
        "call",
        "Popen",
    }
)

REASON_SYNTAX: Final = "The code is not valid Python."
REASON_IMPORT: Final = "imports {name}, which is outside ordinary Blender scene work"
REASON_BUILTIN: Final = "calls {name}(), which can run code or reach the filesystem"
REASON_WM_OPERATOR: Final = (
    "calls bpy.ops.wm.{name}(), which manages files or the application rather than "
    "the scene"
)
REASON_ATTRIBUTE: Final = "uses {name}, which can reach the filesystem, a process or the network"
REASON_ABSOLUTE_PATH: Final = "contains the absolute path {path!r}"
REASON_DUNDER: Final = "uses {name}, which can reach interpreter internals"


@dataclass(frozen=True)
class RiskFinding:
    """One reason the code was flagged, with the line it was found on."""

    reason: str
    line: int

    def describe(self) -> str:
        return f"line {self.line}: {self.reason}"


@dataclass(frozen=True)
class RiskAssessment:
    """The classifier's verdict for one piece of model-authored Python."""

    decision: str
    findings: tuple[RiskFinding, ...] = field(default_factory=tuple)

    @property
    def requires_approval(self) -> bool:
        return self.decision == APPROVAL_REQUIRED

    @property
    def refused(self) -> bool:
        return self.decision == REFUSED

    @property
    def may_run_unattended(self) -> bool:
        return self.decision == AUTO

    def reasons(self) -> tuple[str, ...]:
        return tuple(finding.describe() for finding in self.findings)

    def summary(self) -> str:
        if self.decision == AUTO:
            return "Scene-only operation."
        if self.decision == REFUSED:
            return REASON_SYNTAX
        if len(self.findings) == 1:
            return f"This step {self.findings[0].reason}."
        return "This step " + "; ".join(f.reason for f in self.findings) + "."


def _root_module(name: str) -> str:
    return name.split(".", 1)[0]


#: Blender data collections whose `remove` / `clear` calls are ORDINARY SCENE WORK.
#: `bpy.data.objects.remove(obj)` deletes an object from the scene; it has nothing to do
#: with the filesystem, and asking the user to approve it would pester them on almost
#: every plan that rebuilds something. `os.remove` and `shutil.rmtree` still flag,
#: because the check is on where the call is rooted, not on the word.
SCENE_DATA_ROOTS: Final = frozenset({"bpy", "bpy_data", "context", "scene"})

#: Names on a Blender collection that only ever touch the scene.
#:
#: ``remove`` / ``clear`` / ``rename`` / ``replace`` edit collection contents and share
#: their names with ``os.remove`` / ``shutil.rmtree``. Rooted at ``bpy``/scene data they
#: are ordinary scene work; ``os.remove`` still flags because it is rooted at ``os`` and
#: the ``os`` import is flagged too. (``unlink``/``link`` are not sensitive attributes at
#: all — see the note in SENSITIVE_ATTRIBUTES — so they need no entry here.)
SCENE_DATA_METHODS: Final = frozenset({"remove", "clear", "rename", "replace"})


#: Builtins that merely WRAP a sequence, so the scene data inside still shows through.
#: ``for o in list(bpy.data.objects)`` is the standard way to iterate a collection you
#: are about to mutate, and it must not hide that ``o`` is scene data.
_SEQUENCE_WRAPPERS: Final = frozenset(
    {"list", "tuple", "set", "sorted", "reversed", "iter", "enumerate"}
)


def _chain_root(node: ast.AST) -> Optional[str]:
    """The bare name an attribute/subscript chain is rooted at, if any.

    Returns ``None`` when the chain starts from a call, which is what keeps
    ``Path(p).unlink()`` out of the scene-work exemption.
    """
    current: ast.AST = node
    while True:
        if isinstance(current, (ast.Attribute, ast.Subscript)):
            current = current.value
            continue
        if (
            isinstance(current, ast.Call)
            and isinstance(current.func, ast.Name)
            and current.func.id in _SEQUENCE_WRAPPERS
            and current.args
        ):
            current = current.args[0]
            continue
        break
    return current.id if isinstance(current, ast.Name) else None


def scene_data_names(tree: ast.AST) -> frozenset[str]:
    """Local names that hold scene data, in addition to :data:`SCENE_DATA_ROOTS`.

    WHY THIS EXISTS

    ``remove`` is only exempt when the platform can see it is rooted at ``bpy``. That
    covers ``bpy.data.objects.remove(obj)`` and stops ``os.remove``, which is the right
    shape — but real authored code assigns first:

        tree = material.node_tree
        for node in list(tree.nodes):
            tree.nodes.remove(node)          # rooted at `tree`, not at `bpy`

    Rebuilding a material's node graph like that is the most ordinary thing in the
    world, and it was stopping to ask the user for approval every time, on a re-clad
    turn where the answer is always yes. A gate that fires on almost every rebuild
    teaches people to approve without reading, which is the one thing it must not do.

    So assignments are followed: a name bound from a scene-rooted expression is itself
    scene data. Iterated to a fixed point, because chains are built up a step at a
    time. Anything rooted at a CALL is still excluded, so a helper of unknown origin —
    ``helper = get_helper(); helper.remove(path)`` — keeps asking, exactly as before.
    """
    names = set(SCENE_DATA_ROOTS)
    # Chains in authored code are short; four passes is far more than enough and
    # bounds the work on a hostile input.
    for _ in range(4):
        discovered = False
        for node in ast.walk(tree):
            targets: list[ast.AST] = []
            source: Optional[ast.AST] = None
            if isinstance(node, ast.Assign):
                targets, source = list(node.targets), node.value
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets, source = [node.target], node.value
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                targets, source = [node.target], node.iter
            elif isinstance(node, ast.withitem):
                targets, source = (
                    [node.optional_vars] if node.optional_vars else []
                ), node.context_expr
            if source is None or _chain_root(source) not in names:
                continue
            for target in targets:
                # A tuple target (``for a, b in …``) says nothing reliable about which
                # element is scene data, so only plain names are followed.
                if isinstance(target, ast.Name) and target.id not in names:
                    names.add(target.id)
                    discovered = True
        if not discovered:
            break
    return frozenset(names)


def _is_scene_data_call(node: ast.Attribute, scene_names: frozenset[str]) -> bool:
    """True for ``bpy.…remove`` shaped attributes: scene edits, not host access.

    Rooted at a name known to hold scene data — ``bpy`` and friends, plus any local
    bound from them (see :func:`scene_data_names`) — and reached through an attribute
    or subscript chain, so ``bpy.data.objects.remove`` and
    ``bpy.data.collections['x'].objects.unlink`` qualify while ``os.remove``,
    ``shutil.rmtree`` and ``Path(p).unlink`` do not. A chain that starts from a call
    is deliberately not treated as scene work.
    """
    if node.attr not in SCENE_DATA_METHODS:
        return False
    return _chain_root(node.value) in scene_names


def _looks_absolute(value: str) -> bool:
    if not value:
        return False
    if value.startswith("~"):
        return True
    if value.startswith("/") and len(value) > 1:
        # A lone "/" and things like "/tmp" are still absolute; but bare separators
        # inside ordinary strings (for example "Wall/Left") are not.
        return "/" in value[1:] or value.count("/") == 1
    return False


class _Walker(ast.NodeVisitor):
    def __init__(self, scene_names: frozenset[str] = SCENE_DATA_ROOTS) -> None:
        self.findings: list[RiskFinding] = []
        #: Names that hold scene data in THIS program, so a collection edit reached
        #: through a local reads as scene work rather than as filesystem access.
        self.scene_names = scene_names

    def _flag(self, reason: str, node: ast.AST) -> None:
        self.findings.append(RiskFinding(reason=reason, line=getattr(node, "lineno", 0)))

    # --- imports ---------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if _root_module(alias.name) not in SCENE_MODULES:
                self._flag(REASON_IMPORT.format(name=alias.name), node)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if not module or _root_module(module) not in SCENE_MODULES:
            self._flag(REASON_IMPORT.format(name=module or "a relative module"), node)
        self.generic_visit(node)

    # --- calls -----------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id in SENSITIVE_BUILTINS:
            self._flag(REASON_BUILTIN.format(name=func.id), node)
        operator = self._wm_operator(func)
        if operator is not None and operator in SENSITIVE_WM_OPERATORS:
            self._flag(REASON_WM_OPERATOR.format(name=operator), node)
        self.generic_visit(node)

    @staticmethod
    def _wm_operator(func: ast.AST) -> str | None:
        """Return the operator name for ``bpy.ops.wm.<name>`` shaped attributes."""
        if not isinstance(func, ast.Attribute):
            return None
        name = func.attr
        parent = func.value
        if not isinstance(parent, ast.Attribute) or parent.attr != "wm":
            return None
        grandparent = parent.value
        if not isinstance(grandparent, ast.Attribute) or grandparent.attr != "ops":
            return None
        root = grandparent.value
        if isinstance(root, ast.Name) and root.id == "bpy":
            return name
        return None

    # --- attributes and names --------------------------------------------
    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in SENSITIVE_ATTRIBUTES and not _is_scene_data_call(
            node, self.scene_names
        ):
            self._flag(REASON_ATTRIBUTE.format(name=node.attr), node)
        elif node.attr.startswith("__") and node.attr.endswith("__"):
            self._flag(REASON_DUNDER.format(name=node.attr), node)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in SENSITIVE_BUILTINS_BY_REFERENCE:
            self._flag(REASON_BUILTIN.format(name=node.id), node)
        elif node.id.startswith("__") and node.id.endswith("__"):
            self._flag(REASON_DUNDER.format(name=node.id), node)
        self.generic_visit(node)

    # --- literals --------------------------------------------------------
    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and _looks_absolute(node.value):
            self._flag(REASON_ABSOLUTE_PATH.format(path=node.value), node)
        self.generic_visit(node)


def _deduplicate(findings: Iterable[RiskFinding]) -> tuple[RiskFinding, ...]:
    seen: set[tuple[str, int]] = set()
    unique: list[RiskFinding] = []
    for finding in findings:
        key = (finding.reason, finding.line)
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    unique.sort(key=lambda f: (f.line, f.reason))
    return tuple(unique)


def classify_python(code: str) -> RiskAssessment:
    """Classify model-authored Blender Python.

    Returns ``AUTO`` when the code only touches the scene, ``APPROVAL_REQUIRED``
    when it reaches further than that, and ``REFUSED`` when it does not parse.
    """
    if not isinstance(code, str) or not code.strip():
        return RiskAssessment(decision=REFUSED, findings=(RiskFinding(REASON_SYNTAX, 0),))
    try:
        tree = ast.parse(code)
    except SyntaxError as error:
        line = error.lineno or 0
        return RiskAssessment(decision=REFUSED, findings=(RiskFinding(REASON_SYNTAX, line),))

    # Which locals hold scene data has to be known BEFORE walking, because a
    # collection edit can appear earlier in the file than nothing — the assignment
    # that proves `tree` came from `bpy` may sit many lines above its use.
    walker = _Walker(scene_data_names(tree))
    walker.visit(tree)
    findings = _deduplicate(walker.findings)
    decision = APPROVAL_REQUIRED if findings else AUTO
    return RiskAssessment(decision=decision, findings=findings)
