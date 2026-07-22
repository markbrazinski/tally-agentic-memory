"""Layer-compliance check (CLAUDE.md's hexagonal architecture, recon requirement).

"src/core — pure functions on Pydantic models, zero external deps" -
this test statically checks that no file under src/core imports anything
from src/external or src/platform (the two layers core must never depend
on), and separately that it doesn't import any known third-party network/
DB library directly. Runs via `make test` today; the moment CI exists,
this test is what "layer-compliance check in CI" refers to (bundle-0.md
goal #6) - no separate mechanism needed.

Currently src/core has no modules yet (Bundle 0 hasn't populated it) -
this test passes vacuously today and becomes load-bearing the instant
the first src/core/*.py file is added, which is the point: the rule is
enforced from the first commit, not bolted on after a violation ships.
"""

from __future__ import annotations

import ast
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORE_DIR = os.path.join(REPO_ROOT, "src", "core")

FORBIDDEN_INTERNAL_PREFIXES = ("src.external", "src.platform")

# Known network/DB/LLM libraries core must never import directly - it
# operates on plain Python/Pydantic values passed in by the layers that
# do own I/O.
FORBIDDEN_EXTERNAL_MODULES = (
    "psycopg",
    "boto3",
    "botocore",
    "httpx",
    "fastapi",
    "anthropic",
)


def _iter_core_python_files():
    for root, _dirs, files in os.walk(CORE_DIR):
        for filename in files:
            if filename.endswith(".py"):
                yield os.path.join(root, filename)


def _imported_module_names(source: str) -> list[str]:
    tree = ast.parse(source)
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_core_has_no_forbidden_internal_layer_imports():
    violations = []
    for path in _iter_core_python_files():
        with open(path) as f:
            source = f.read()
        for module in _imported_module_names(source):
            if module.startswith(FORBIDDEN_INTERNAL_PREFIXES):
                violations.append((path, module))
    assert violations == [], f"src/core importing forbidden internal layers: {violations}"


def test_core_has_no_forbidden_external_library_imports():
    violations = []
    for path in _iter_core_python_files():
        with open(path) as f:
            source = f.read()
        for module in _imported_module_names(source):
            top_level = module.split(".")[0]
            if top_level in FORBIDDEN_EXTERNAL_MODULES:
                violations.append((path, module))
    assert violations == [], f"src/core importing forbidden external libraries: {violations}"


def test_core_directory_exists_so_this_check_is_not_silently_vacuous():
    assert os.path.isdir(CORE_DIR)
