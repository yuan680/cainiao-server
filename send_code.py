"""Send verification code to gcloud auth login."""
import subprocess
import sys

code = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read().strip()
if not code:
    print("ERROR: No verification code provided")
    sys.exit(1)

print(f"Starting gcloud auth login with code: {code[:40]}...")
proc = subprocess.Popen(
    ["gcloud", "auth", "login", "--no-launch-browser"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True
)

try:
    stdout, stderr = proc.communicate(input=code.strip() + "\n", timeout=60)
    print("STDOUT:", stdout)
    if stderr:
        print("STDERR:", stderr)
    print(f"Exit code: {proc.returncode}")
except subprocess.TimeoutExpired:
    proc.kill()
    stdout, stderr = proc.communicate()
    print("TIMEOUT - STDOUT:", stdout)
    print("TIMEOUT - STDERR:", stderr)
