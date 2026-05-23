#!/usr/bin/env python3
"""Complete gcloud auth login automation."""
import subprocess
import threading
import time
import queue

def stream_reader(stream, q, prefix):
    for line in iter(stream.readline, ''):
        if line:
            q.put((prefix, line))
            print(f"[{prefix}] {line.rstrip()}")
    stream.close()

def main():
    print("=" * 60)
    print("Starting gcloud auth login with --no-launch-browser")
    print("=" * 60)

    proc = subprocess.Popen(
        ["gcloud", "auth", "login", "--no-launch-browser"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1  # line buffering
    )

    q = queue.Queue()

    stdout_thread = threading.Thread(target=stream_reader, args=(proc.stdout, q, "STDOUT"), daemon=True)
    stderr_thread = threading.Thread(target=stream_reader, args=(proc.stderr, q, "STDERR"), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    # Wait for URL prompt
    auth_url = None
    waiting_for_code = False
    timeout = 30
    start_time = time.time()

    while time.time() - start_time < timeout:
        try:
            prefix, line = q.get(timeout=1)
            if "https://" in line:
                auth_url = line.strip()
                print(f"\n*** AUTH URL FOUND: {auth_url} ***\n")
            if "enter the verification code" in line.lower() or "Once finished" in line:
                waiting_for_code = True
                print("\n=== gcloud is waiting for verification code ===")
        except queue.Empty:
            pass

        if auth_url and waiting_for_code:
            break

    if not auth_url:
        print("WARNING: Could not detect auth URL automatically")
    else:
        # Open browser
        import webbrowser
        print(f"\nOpening browser with: {auth_url}")
        webbrowser.open(auth_url)

    # Wait for user to paste code
    print("\n" + "=" * 60)
    print("Please complete authorization in your browser.")
    print("Copy the verification code and paste it here, then press Enter:")
    print("=" * 60)

    try:
        code = input("\nVerification Code: ").strip()
    except EOFError:
        code = None

    if not code:
        print("No code entered, exiting.")
        proc.terminate()
        return

    # Send code to gcloud
    print(f"\nSending verification code to gcloud...")
    proc.stdin.write(code + "\n")
    proc.stdin.flush()

    # Wait for completion
    print("Waiting for gcloud to process...")
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        print("WARNING: gcloud taking too long, will still read output")

    # Let reader threads catch up
    time.sleep(2)

    print("\n" + "=" * 60)
    print(f"gcloud exited with code: {proc.returncode}")
    print("=" * 60)


if __name__ == "__main__":
    main()
