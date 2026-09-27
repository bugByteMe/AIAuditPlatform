from __future__ import annotations

import threading
from contextlib import contextmanager

RUNNING_STATES = {"queued", "starting", "running", "stopping"}
TERMINAL_STATES = {"completed", "stopped", "failed"}


class KeyedLockPool:
  def __init__(self):
    self.guard = threading.Lock()
    self.entries: dict[str, dict] = {}

  @contextmanager
  def hold(self, *keys: str):
    ordered = sorted(set(keys))
    locks = []
    with self.guard:
      for key in ordered:
        entry = self.entries.setdefault(key, {"lock": threading.RLock(), "references": 0})
        entry["references"] += 1
        locks.append((key, entry["lock"]))
    try:
      for _, lock in locks:
        lock.acquire()
      yield
    finally:
      for _, lock in reversed(locks):
        lock.release()
      with self.guard:
        for key, _ in locks:
          self.entries[key]["references"] -= 1
          if self.entries[key]["references"] == 0:
            self.entries.pop(key, None)

