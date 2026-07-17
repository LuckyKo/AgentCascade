import subprocess, sys, os

os.chdir(r"N:\work\WD\AgentCascade_unified")
result = subprocess.run(
    ["pytest", "tests/", "--tb=line"],
    capture_output=True, text=True, timeout=120
)
output = result.stdout + result.stderr
with open("test_results_default.txt", "w") as f:
    f.write(output)
print(output)
sys.exit(result.returncode)