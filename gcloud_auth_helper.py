#!/usr/bin/env python3
"""
Two-phase gcloud auth helper.
Phase 1: Start gcloud, capture URL, write to state file.
Phase 2: Read code from code file, feed to gcloud, get result.
"""
import subprocess
import threading
import queue
import time
import sys
import os
import json

GCLOUD_CMD = r"C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
STATE_DIR = r"F:\cainiao_track"
URL_FILE = os.path.join(STATE_DIR, "gcloud_url.txt")
CODE_FILE = os.path.join(STATE_DIR, "gcloud_code.txt")
RESULT_FILE = os.path.join(STATE_DIR, "gcloud_result.json")

def reader_thread(stream, q, label):
    try:
        for line in iter(stream.readline, ''):
            if line:
                q.put((label, line))
    except ValueError:
        pass

def main():
    # Clean up old files
    for f in [URL_FILE, CODE_FILE, RESULT_FILE]:
        if os.path.exists(f):
            os.remove(f)

    print("Starting gcloud auth login...")

    proc = subprocess.Popen(
        [GCLOUD_CMD, "auth", "login", "--no-launch-browser"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1
    )

    q = queue.Queue()
    t1 = threading.Thread(target=reader_thread, args=(proc.stdout, q, "OUT"), daemon=True)
    t2 = threading.Thread(target=reader_thread, args=(proc.stderr, q, "ERR"), daemon=True)
    t1.start()
    t2.start()

    auth_url = None
    code_prompt = False
    all_output = []
    start_time = time.time()

    # Phase 1: Read until we get URL and prompt
    while time.time() - start_time < 30:
        try:
            label, line = q.get(timeout=2)
            line = line.rstrip()
            all_output.append((label, line))
            print(f"[{label}] {line}")

            if "https://accounts.google.com" in line and "oauth2/auth" in line:
                auth_url = line.strip()
            if "verification code" in line.lower() or "once finished" in line.lower():
                code_prompt = True
        except queue.Empty:
            if proc.poll() is not None:
                print(f"Process exited early with code {proc.returncode}")
                break

        if auth_url and code_prompt:
            break

    if not auth_url:
        print("ERROR: Could not find auth URL in output.")
        for label, line in all_output:
            print(f"  [{label}] {line}")
        proc.terminate()
        return 1

    # Save URL to file
    with open(URL_FILE, "w") as f:
        f.write(auth_url)
    print(f"\n✓ Auth URL saved to {URL_FILE}")
    print(f"  URL: {auth_url[:80]}...")

    # Phase 2: Wait for code file
    print(f"\nWaiting for code file at {CODE_FILE}...")
    print("Please authorize in the browser and send me the verification code.")
    print("I will write the code to the code file.\n")

    code = None
    wait_start = time.time()
    while time.time() - wait_start < 180:  # 3 min timeout
        if os.path.exists(CODE_FILE):
            with open(CODE_FILE, "r") as f:
                code = f.read().strip()
            if code:
                print(f"Code file detected! Feeding code to gcloud...")
                os.remove(CODE_FILE)
                break
            else:
                os.remove(CODE_FILE)
        time.sleep(1)

    if not code:
        print("ERROR: No code provided within timeout.")
        proc.terminate()
        result = {"success": False, "error": "timeout", "output": all_output}
        with open(RESULT_FILE, "w") as f:
            json.dump(result, f)
        return 1

    # Feed code to gcloud
    print(f"Feeding verification code: {code[:40]}...")
    proc.stdin.write(code + "\n")
    proc.stdin.flush()

    # Read remaining output
    time.sleep(2)
    final_output = []
    while True:
        try:
            label, line = q.get(timeout=10)
            line = line.rstrip()
            final_output.append((label, line))
            print(f"[{label}] {line}")
        except queue.Empty:
            if proc.poll() is not None:
                break
            time.sleep(1)

    remaining = []
    while not q.empty():
        try:
            remaining.append(q.get_nowait())
        except queue.Empty:
            break

    proc.wait(timeout=5)

    # Save result
    success = proc.returncode == 0
    result = {
        "success": success,
        "returncode": proc.returncode,
        "output": [(l, t) for l, t in all_output + final_output + remaining]
    }
    with open(RESULT_FILE, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\n{'='*50}")
    if success:
        print("✓ AUTHENTICATION SUCCESSFUL!")
        subprocess.run([GCLOUD_CMD, "auth", "list"], check=False)
    else:
        print(f"✗ Authentication failed (exit code: {proc.returncode})")

    return 0 if success else 1

if __name__ == "__main__":
    sys.exit(main())
