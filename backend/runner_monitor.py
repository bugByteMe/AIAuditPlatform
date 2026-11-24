from __future__ import annotations

import queue
import threading
import time
from collections import deque


def monitor_runner(runner, process, run, workspace, bridge):
  output = queue.Queue(maxsize=1024)
  stderr = deque(maxlen=100)
  finished = threading.Event()

  def read_stdout():
    try:
      for line in process.stdout:
        while not finished.is_set():
          try:
            output.put(line, timeout=0.1)
            break
          except queue.Full:
            pass
    finally:
      while not finished.is_set():
        try:
          output.put(None, timeout=0.1)
          break
        except queue.Full:
          pass

  def read_stderr():
    for line in process.stderr:
      stderr.append(line[-4096:])

  readers = [threading.Thread(target=target, daemon=True) for target in (read_stdout, read_stderr)]
  for reader in readers:
    reader.start()
  seen = set()
  elapsed = 0.0
  previous = time.monotonic()
  paused = False
  eof = False
  try:
    while not eof or process.poll() is None:
      now = time.monotonic()
      if not paused:
        elapsed += now - previous
      previous = now
      if elapsed > runner.timeout_seconds:
        runner.stop(run)
        raise RuntimeError("Run timed out.")
      yield from bridge.requests(workspace, workspace.parent.parent / "blobs" / "sha256", seen)
      paused = bridge.pending(seen)
      try:
        line = output.get(timeout=0.1)
      except queue.Empty:
        continue
      if line is None:
        eof = True
      else:
        yield runner.parse_json_event(line)
    returncode = process.wait(timeout=runner.process_wait_timeout_seconds)
    for reader in readers:
      reader.join(timeout=1)
    if returncode != 0 and run.get("status") != "stopping":
      raise RuntimeError("".join(stderr).strip() or f"Codex exited with status {returncode}")
  finally:
    finished.set()
