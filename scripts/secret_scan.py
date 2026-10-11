"""Refuse to push if a tracked file holds anything that looks like a key, or any real
value from the env file in use. Prints file names and key NAMES only, never a value.

    python scripts/secret_scan.py        # exit 0 clean, exit 1 found something
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from second_opinion import config  # noqa: E402

# Built from pieces so this file does not match itself.
SHAPES = re.compile("|".join([
    "AI" + "za[0-9A-Za-z_-]{20}",
    "sk" + "-[A-Za-z0-9]{20}",
    "sk" + "-ant-",
    "gh" + "p_[A-Za-z0-9]{20}",
    r"api[_-]?key\s*=\s*['\"][A-Za-z0-9]{12}",
    r"[a-z0-9-]+\.(?:cognitiveservices|openai)\.azure\.com",
]))


def main() -> int:
    root = config.ROOT
    files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=root, capture_output=True,
                           text=True, check=True).stdout.split("\n")
    values = {name: v for name, v in config.load_env().items() if v and len(v) >= 8}
    for v in list(values.values()):
        if v.startswith("http"):
            values["host of a configured URL"] = v.split("//", 1)[-1].split("/", 1)[0]
    bad = 0
    for rel in filter(None, files):
        path = root / rel
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if SHAPES.search(line):
                print(f"FOUND key-shaped text: {rel}:{lineno}")
                bad += 1
        for name, value in values.items():
            if value in text:
                print(f"FOUND the live value of {name} in {rel}")
                bad += 1
    print(f"scanned {len(files) - 1} files, {len(values)} live values checked: " + ("CLEAN" if not bad else f"{bad} PROBLEMS"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
