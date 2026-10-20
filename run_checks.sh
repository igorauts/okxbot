#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

python3 -B - <<'PY'
from pathlib import Path
import ast
import json

root = Path.cwd()
manifest = json.loads((root / "engine_manifest.json").read_text())
version = manifest["current_version"]
entry = manifest["engines"][version]

assert entry["file"] == "okx_bot.py"

engine = ast.parse((root / "okx_bot.py").read_text())
versions = [
    ast.literal_eval(n.value)
    for n in engine.body
    if isinstance(n, ast.Assign)
    and any(isinstance(t, ast.Name) and t.id == "VER"
            for t in n.targets)
]
assert versions == [version], (versions, version)

runtime_dirs = [
    Path("/opt/okx-grid-live"),
    Path("/root/okx"),
    Path("/root/okx/zec0710"),
]

runtime = {
    p.name
    for directory in runtime_dirs
    for p in directory.glob("*.py")
    if p.is_file()
}

required = runtime | set(entry["dependencies"]) | {
    "okx_bot.py", "okxbot.py"
}

missing = sorted(name for name in required if not (root / name).is_file())
assert not missing, f"Missing LIVE source files: {missing}"

checked = 0
for name in sorted(required):
    path = root / name
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=name)

    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])

    missing_imports = [
        mod for mod in imports
        if mod.startswith("okx_") and not (root / (mod + ".py")).is_file()
    ]
    assert not missing_imports, (name, missing_imports)
    checked += 1

assert (root / "README.md").is_file()

print(f"PASS: manifest and engine v{version}")
print(f"PASS: {checked} LIVE source files parsed and imports checked")
print("PASS: one current README")
print("PASS: no trading code executed")
PY
