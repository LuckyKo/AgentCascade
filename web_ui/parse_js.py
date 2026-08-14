#!/usr/bin/env python3
"""Use esbuild or simple parsing to check JS syntax."""
import subprocess
import sys

try:
    # Try using esbuild via node
    result = subprocess.run(
        ['node', '-e', 'require("esbuild").transformSync(require("fs").readFileSync("N:/work/WD/AgentCascade/web_ui/app.js", "utf8"), {jsx: false, loader: "js"})'],
        capture_output=True,
        text=True,
        timeout=10
    )
    print("STDOUT:", result.stdout[:2000])
    print("STDERR:", result.stderr[:2000])
except Exception as e:
    print(f"Error: {e}")
