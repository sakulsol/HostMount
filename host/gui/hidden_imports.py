"""Prints PyInstaller --hidden-import flags for the standard-library modules the kit's scripts use.

HostMount.exe also runs the kit's Python scripts (dirserve, the installer, mkgame), which PyInstaller
does not see when it analyses HostMount.pyw; their stdlib imports must be bundled by hand.
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
mods = set()
for sub in ("host", "install"):
    for dp, _, files in os.walk(os.path.join(ROOT, sub)):
        for f in files:
            if not f.endswith(".py"):
                continue
            try:
                tree = ast.parse(open(os.path.join(dp, f), encoding="utf-8").read())
            except (SyntaxError, UnicodeDecodeError):
                continue
            for n in ast.walk(tree):
                names = [a.name for a in n.names] if isinstance(n, ast.Import) else \
                        [n.module] if isinstance(n, ast.ImportFrom) and n.module and not n.level else []
                for name in names:
                    if name.split(".")[0] in sys.stdlib_module_names:
                        mods.add(name)
print(" ".join("--hidden-import=%s" % m for m in sorted(mods)))
