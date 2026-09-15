"""No API key exists anywhere in this product.

Astra is reached through the locally installed Codex client using Christian's ChatGPT
sign-in. That is a hard product requirement (Requirement 20.2), so it is enforced by a
test over the real source tree rather than by intention.

The two allowed mentions are declared explicitly below, and both exist to *prevent* key
use rather than to enable it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

SEARCH_ROOTS = ("services", "apps", "packages", "scripts", "infra", "database")

SOURCE_SUFFIXES = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".env",
    ".example",
    ".css",
    ".md",
}

SKIP_DIRECTORIES = {
    "node_modules",
    "__pycache__",
    ".next",
    ".venv",
    "build",
    "dist",
    ".pytest_cache",
}

#: Forbidden anywhere: these only appear when something intends to USE an API key.
FORBIDDEN_TOKENS = (
    "OPENAI_API_KEY",
    "openai_api_key",
    "OPENAI_SECRET",
    "sk-proj-",
    "--with-api-key",
    "with_api_key",
)

#: Files permitted to mention a forbidden token, each with the reason.
ALLOWED_MENTIONS = {
    # Strips OPENAI_* from the Codex child environment so a key exported in the shell
    # cannot be used even accidentally.
    "services/agent/studio_agent/codex.py": "strips the variable from the child environment",
    # Asserts the stripping actually happens.
    "services/agent/tests/test_codex_client.py": "asserts the variable never reaches Codex",
}


def _source_files() -> list[Path]:
    files: list[Path] = []
    for root_name in SEARCH_ROOTS:
        root = REPO_ROOT / root_name
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRECTORIES for part in path.parts):
                continue
            if path.suffix not in SOURCE_SUFFIXES:
                continue
            files.append(path)
    return files


def test_the_search_actually_found_the_source_tree() -> None:
    """A guard that silently scans nothing is worse than no guard."""
    files = _source_files()
    assert len(files) > 100, f"expected to scan the source tree, found {len(files)} files"


def test_no_source_file_uses_an_openai_api_key() -> None:
    offenders: list[str] = []
    for path in _source_files():
        relative = path.relative_to(REPO_ROOT).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):  # pragma: no cover - binary or unreadable
            continue
        for token in FORBIDDEN_TOKENS:
            if token in text and relative not in ALLOWED_MENTIONS:
                offenders.append(f"{relative}: {token}")
    assert not offenders, "API key usage found:\n" + "\n".join(offenders)


def test_the_allowed_mentions_still_exist_and_still_only_prevent_key_use() -> None:
    """If an allowance stops being needed, it must be removed rather than left open."""
    for relative, reason in ALLOWED_MENTIONS.items():
        path = REPO_ROOT / relative
        assert path.exists(), f"{relative} is allow-listed but does not exist ({reason})"
        text = path.read_text(encoding="utf-8")
        assert "OPENAI_API_KEY" in text, f"{relative} no longer mentions the variable"
        # The allowance is only defensible while the mention is about REMOVING the key.
        assert (
            "OPENAI_API_KEY" in text and ("not in" in text or "never" in text or "strip" in text)
        ), f"{relative} mentions the key but no longer looks like a guard"


def test_no_frontend_environment_variable_exposes_a_key() -> None:
    """A NEXT_PUBLIC_* variable ships to the browser, so it can never hold a secret.

    The check matches the sensitive word inside the VARIABLE NAME, so prose warning
    against this practice (which `lib/config.ts` deliberately carries) does not trip it.
    """
    pattern = re.compile(r"NEXT_PUBLIC_[A-Z0-9_]*(KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL)")
    offenders: list[str] = []
    for path in _source_files():
        if "apps/web" not in path.as_posix():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            if pattern.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}: {line.strip()}")
    assert not offenders, "frontend secret exposure:\n" + "\n".join(offenders)


def test_no_api_key_input_exists_in_the_browser() -> None:
    """There is deliberately no place for a user to paste a key."""
    offenders: list[str] = []
    web = REPO_ROOT / "apps" / "web"
    if not web.exists():  # pragma: no cover
        pytest.skip("no web app")
    for path in web.rglob("*"):
        if not path.is_file() or path.suffix not in {".ts", ".tsx", ".js", ".jsx"}:
            continue
        if any(part in SKIP_DIRECTORIES for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        if "api key" in text or "apikey" in text:
            offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert not offenders, "an API key field appears in the browser:\n" + "\n".join(offenders)
