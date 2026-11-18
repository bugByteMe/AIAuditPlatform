from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from worker_agent import WorkerState


class WorkerStateSerializationTest(unittest.TestCase):
  def test_state_and_events_escape_surrogate_characters(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      root = Path(tempdir)
      state = object.__new__(WorkerState)
      state.state_dir = root
      state.events_dir = root / "events"
      state.events_dir.mkdir()
      state.runs_path = root / "runs.json"
      state.runs = {"run-1": {"id": "run-1", "error": "bad\udcff-name"}}
      state.events = {"run-1": []}
      state.condition = threading.Condition(threading.RLock())

      state.save_runs()
      state.append_event("run-1", {"type": "error", "message": "bad\udcff-name"})

      self.assertEqual(json.loads(state.runs_path.read_text(encoding="utf-8"))["run-1"]["error"], "bad\udcff-name")
      event = json.loads(state.event_path("run-1").read_text(encoding="utf-8"))
      self.assertEqual(event["message"], "bad\udcff-name")


if __name__ == "__main__":
  unittest.main()
