#!/usr/bin/env python3
"""Handle gcloud auth login interactively."""
import subprocess
import sys
import threading
import time

def main():
    code = sys.argv[1] if len(sys.argv) > 1 else None
    if not code:
        print("Usage: python gcloud_auth.py <verification_code>")
        sys.exit(1)

    proc = subprocess.Popen(
        ["gcloud", "auth", "login", "--no-launch-browser"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    # Read output in a thread to avoid deadlock
    out_lines = []
    err_lines = []

    def read_output():
        for line in proc.stdout:
            out_lines.append(line)
        for line in proc.stderr:
            err_lines.append(line)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()

    # Wait for the prompt, then send the code
    time.sleep(3)
    proc.stdin.write(code + "\n")
    proc.stdin.flush()
    proc.stdin.close()

    proc.wait(timeout=30)
    reader.join(timeout=5)

    print("=== STDOUT ===")
    print("".join(out_lines))
    print("=== STDERR ===")
    print("".join(err_lines))
    print(f"=== EXIT CODE: {proc.returncode} ===")


if __name__ == "__main__":
    main()
