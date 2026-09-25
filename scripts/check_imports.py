"""Scan src/ and tests/ for import statements that require rns/lxmf.

Run: venv/bin/python scripts/check_imports.py

Pure-logic modules must not import RNS/LXMF (or the bridge integration
modules that wrap them). Integration modules are excluded by their full
package path so they keep access to the stack.
"""
import ast
import os
import sys

# Real stack dependencies, as imported in the codebase (uppercase).
BANNED_MODULES = {"RNS", "LXMF"}

# Integration modules that legitimately import RNS/LXMF. A file is excluded
# only if it is one of these (matched by full module path), so a module that
# happens to import a banned symbol for a different reason is still caught.
EXCLUDED_MODULES = {
    # src: the bridge integration layer and its entrypoint
    "hermes_reticulum.core.bridge",
    "hermes_reticulum.core.bridge_liveness",
    "hermes_reticulum.core.profiler",
    "hermes_reticulum.cli",
    "hermes_reticulum.plugin.adapter",
    # The transport IS the RNS/LXMF integration; registration probes the
    # installed stack for platform enablement. Both were added after this
    # guard and need declaring, not exempting by accident.
    "hermes_reticulum.plugin.transport",
    "hermes_reticulum.plugin.registration",
    # tests: integration test files that drive the live bridge/RNS loop
    "test_loopback_lxmf",
    "test_core",
    # Identity tests decode a real announce payload via RNS's msgpack vendor
    # shim (no radio involved).
    "test_reticulum_identity",
}


def file_module_path(path, root):
    """Return the dotted module path for a .py file under src/ (or '')."""
    rel = os.path.relpath(path, root)
    rel = rel[:-3] if rel.endswith(".py") else rel
    if rel.endswith("__init__"):
        rel = rel[: -len("__init__")]
    return rel.replace(os.sep, ".")


def _guarded_import_linenos(tree):
    """Line numbers of banned imports that sit inside a try/except ImportError.

    A module may legitimately import the stack *optionally* — inside a
    ``try: import LXMF / except ImportError:`` with a working fallback — so
    the module stays usable where the stack is absent. That is not the
    invariant this guard protects (a hard dependency in pure logic); it is
    the opposite, and flagging it would push authors to delete the fallback.

    Only the try/except-ImportError shape qualifies. A bare import in a
    try/except-Exception still counts as banned, since the fallback there is
    not about the dependency being optional.
    """
    guarded = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        catches_importerror = any(
            (isinstance(h.type, ast.Name) and h.type.id == "ImportError")
            or (isinstance(h.type, ast.Tuple) and any(
                isinstance(e, ast.Name) and e.id == "ImportError" for e in h.type.elts
            ))
            for h in node.handlers
            if h.type is not None
        )
        if not catches_importerror:
            continue
        for sub in ast.walk(node):
            if isinstance(sub, (ast.Import, ast.ImportFrom)):
                guarded.add(sub.lineno)
    return guarded


def scan_file(path, root):
    """Return list of (lineno, import_name) for banned imports."""
    module = file_module_path(path, root)
    base = os.path.basename(path)[:-3]
    excluded = (
        module in EXCLUDED_MODULES
        or base in EXCLUDED_MODULES
        or any(module.startswith(exc + ".") for exc in EXCLUDED_MODULES)
    )
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    if excluded:
        return []
    guarded = _guarded_import_linenos(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if (
                    top in BANNED_MODULES
                    and node.lineno not in guarded
                    and not excluded
                ):
                    found.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top = node.module.split(".")[0]
                if top in BANNED_MODULES and not excluded:
                    found.append((node.lineno, node.module))
    return found


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    src_dir = os.path.join(root, "src")
    tests_dir = os.path.join(root, "tests")
    total_banned = 0
    for base in (src_dir, tests_dir):
        if not os.path.isdir(base):
            continue
        for dirpath, _, files in os.walk(base):
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(dirpath, fname)
                found = scan_file(fpath, base)
                for lineno, name in found:
                    print(f"  {fpath}:{lineno}: {name}")
                total_banned += len(found)
    if total_banned:
        print(f"\nFAIL: {total_banned} banned import(s) found")
        sys.exit(1)
    else:
        print("OK: no banned imports in src/ or tests/")


if __name__ == "__main__":
    main()
