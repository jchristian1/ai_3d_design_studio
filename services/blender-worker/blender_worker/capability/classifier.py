"""Risk classification for model-authored Blender Python, re-exported.

The implementation lives in :mod:`studio_validation.code_risk` because BOTH ends need
it and neither may import the other:

* the control plane classifies before creating jobs, so it can ask the user for
  approval — the worker cannot pause and wait for a human;
* the worker classifies again before execution, so a job that reached it without a
  matching approval token still cannot run.

Classifying in both places is deliberate, not redundant. The control-plane check is
what produces a good user experience; the worker check is what makes the boundary real.
"""

from __future__ import annotations

from studio_validation.code_risk import (  # noqa: F401  (re-exported)
    APPROVAL_REQUIRED,
    AUTO,
    DECISIONS,
    REASON_ABSOLUTE_PATH,
    REASON_ATTRIBUTE,
    REASON_BUILTIN,
    REASON_DUNDER,
    REASON_IMPORT,
    REASON_SYNTAX,
    REASON_WM_OPERATOR,
    REFUSED,
    SCENE_MODULES,
    SENSITIVE_ATTRIBUTES,
    SENSITIVE_BUILTINS,
    SENSITIVE_WM_OPERATORS,
    RiskAssessment,
    RiskFinding,
    classify_python,
)

__all__ = [
    "APPROVAL_REQUIRED",
    "AUTO",
    "DECISIONS",
    "REASON_ABSOLUTE_PATH",
    "REASON_ATTRIBUTE",
    "REASON_BUILTIN",
    "REASON_DUNDER",
    "REASON_IMPORT",
    "REASON_SYNTAX",
    "REASON_WM_OPERATOR",
    "REFUSED",
    "RiskAssessment",
    "RiskFinding",
    "SCENE_MODULES",
    "SENSITIVE_ATTRIBUTES",
    "SENSITIVE_BUILTINS",
    "SENSITIVE_WM_OPERATORS",
    "classify_python",
]
