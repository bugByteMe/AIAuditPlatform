from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chat_runtime import ChatRuntime, CodexRunner
from workspace_store import StorageError, UploadedFile, WorkspaceStore


OWNER = {"username": "li.review", "role": "group_admin", "group": "审计一组", "budgetTokens": 2_000_000, "usedTokens": 0}


class FakeRunner(CodexRunner):
  def __init__(self, events: list[dict] | None = None, delay: float = 0):
    self.events = events or [{"type": "assistant", "message": "done"}, {"type": "usage", "message": "usage", "tokens": 10}]
    self.delay = delay
    self.stopped = False

  def start(self, run: dict, workspace_path: Path):
    run["container"] = "fake-container"
    for event in self.events:
      if self.delay:
        time.sleep(self.delay)
      if self.stopped:
        break
      yield event

  def stop(self, run: dict) -> None:
    self.stopped = True


class ChatRuntimeTest(unittest.TestCase):
  def setUp(self) -> None:
    self.tempdir = tempfile.TemporaryDirectory()
    self.store = WorkspaceStore(Path(self.tempdir.name) / "workspace_storage")
    self.users = {"li.review": dict(OWNER)}

  def tearDown(self) -> None:
    self.tempdir.cleanup()

  def create_workspace(self) -> dict:
    return self.store.create_workspace(
      self.users["li.review"],
      "Audit Upload",
      True,
      [UploadedFile("workpapers/income.txt", b"initial income")],
    )

  def wait_for_status(self, runtime: ChatRuntime, workspace_id: str, session_id: str, status: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
      sessions = runtime.list_sessions(workspace_id, self.users["li.review"])
      session = next(item for item in sessions if item["id"] == session_id)
      if session["status"] == status:
        return session
      time.sleep(0.05)
    self.fail(f"session did not reach {status}")

  def test_run_completes_and_releases_workspace_lock(self) -> None:
    workspace = self.create_workspace()
    runtime = ChatRuntime(self.store, self.users, FakeRunner())
    result = runtime.start_run(workspace["id"], self.users["li.review"], {"prompt": "Check revenue"})
    session = self.wait_for_status(runtime, workspace["id"], result["session"]["id"], "completed")
    metadata = self.store.load_metadata()
    self.assertFalse(metadata["workspaces"][workspace["id"]]["locked"])
    self.assertEqual(metadata["runs"][result["run"]["id"]]["status"], "completed")
    self.assertEqual(self.users["li.review"]["usedTokens"], 10)
    self.assertTrue(any(event[0] == "assistant" for event in session["events"]))

  def test_workspace_lock_rejects_second_active_run(self) -> None:
    workspace = self.create_workspace()
    runtime = ChatRuntime(self.store, self.users, FakeRunner(delay=0.2))
    result = runtime.start_run(workspace["id"], self.users["li.review"], {"prompt": "First"})
    with self.assertRaises(StorageError) as context:
      runtime.start_run(workspace["id"], self.users["li.review"], {"prompt": "Second"})
    self.assertEqual(context.exception.code, "workspace_locked")
    self.wait_for_status(runtime, workspace["id"], result["session"]["id"], "completed")

  def test_budget_exhaustion_rejects_run(self) -> None:
    workspace = self.create_workspace()
    self.users["li.review"]["usedTokens"] = self.users["li.review"]["budgetTokens"]
    runtime = ChatRuntime(self.store, self.users, FakeRunner())
    with self.assertRaises(StorageError) as context:
      runtime.start_run(workspace["id"], self.users["li.review"], {"prompt": "Check revenue"})
    self.assertEqual(context.exception.code, "budget_exhausted")

  def test_stop_transitions_run_to_stopped(self) -> None:
    workspace = self.create_workspace()
    runner = FakeRunner([{"type": "progress", "message": "working"} for _ in range(10)], delay=0.1)
    runtime = ChatRuntime(self.store, self.users, runner)
    result = runtime.start_run(workspace["id"], self.users["li.review"], {"prompt": "Long task"})
    runtime.stop_run(workspace["id"], result["run"]["id"], self.users["li.review"])
    session = self.wait_for_status(runtime, workspace["id"], result["session"]["id"], "stopped")
    self.assertTrue(any(event[0] == "stopped" for event in session["events"]))


if __name__ == "__main__":
  unittest.main()
