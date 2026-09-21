"""Scan src/ and tests/ for import statements that require rns/lxmf.

Run: venv/bin/python scripts/check_imports.py
"""
import ast
import os
import sys

BANNED = {"rns", "lxmf", "reticulum", "mesh_tools", "hermes_reticulum.core.bridge",
          "hermes_reticulum.core.bridge_liveness", "hermes_reticulum.plugin.adapter"}

def scan_file(path):
    """Return list of (lineno, import_name) for banned imports."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in BANNED:
                    found.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top = node.module.split(".")[0]
                if top in BANNED:
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
                found = scan_file(fpath)
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
