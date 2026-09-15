from __future__ import annotations

import sys
import tempfile
import time
import unittest
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chat_runtime import ChatRuntime, CodexRunner, DockerCodexRunner
from workspace_store import StorageError, UploadedFile, WorkspaceStore


OWNER = {
  "username": "li.review",
  "role": "group_admin",
  "group": "审计一组",
  "budgetTokens": 2_000_000,
  "usedTokens": 0,
  "codex": {"baseUrl": "https://codex.example/v1", "apiKey": "sk-test"},
}


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
    self.users = {"li.review": deepcopy(OWNER)}

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

  def test_missing_codex_api_key_rejects_run(self) -> None:
    workspace = self.create_workspace()
    self.users["li.review"]["codex"]["apiKey"] = ""
    runtime = ChatRuntime(self.store, self.users, FakeRunner())
    with self.assertRaises(StorageError) as context:
      runtime.start_run(workspace["id"], self.users["li.review"], {"prompt": "Check revenue"})
    self.assertEqual(context.exception.code, "codex_auth_required")

  def test_stop_transitions_run_to_stopped(self) -> None:
    workspace = self.create_workspace()
    runner = FakeRunner([{"type": "progress", "message": "working"} for _ in range(10)], delay=0.1)
    runtime = ChatRuntime(self.store, self.users, runner)
    result = runtime.start_run(workspace["id"], self.users["li.review"], {"prompt": "Long task"})
    runtime.stop_run(workspace["id"], result["run"]["id"], self.users["li.review"])
    session = self.wait_for_status(runtime, workspace["id"], result["session"]["id"], "stopped")
    self.assertTrue(any(event[0] == "stopped" for event in session["events"]))

  def test_docker_runner_writes_session_scoped_codex_home(self) -> None:
    runner = DockerCodexRunner()
    runner.codex_home_root = Path(self.tempdir.name) / "codex_homes"
    run = {
      "id": "run_1",
      "user": "li.review",
      "sessionId": "chat_1",
      "model": "gpt-5-codex",
      "reasoning": "high",
      "codexSettings": {"baseUrl": "https://codex.example/v1", "apiKey": "sk-test"},
    }
    codex_home = runner.prepare_codex_home(run)
    self.assertEqual(codex_home, runner.codex_home_root / "li.review" / "chat_1")
    self.assertIn('base_url = "https://codex.example/v1"', (codex_home / "config.toml").read_text(encoding="utf-8"))
    self.assertIn('"OPENAI_API_KEY": "sk-test"', (codex_home / "auth.json").read_text(encoding="utf-8"))

  def test_docker_runner_uses_native_resume_for_followup_runs(self) -> None:
    runner = DockerCodexRunner()
    first = {"model": "gpt-5.6-sol", "prompt": "first", "codexResume": False}
    followup = {"model": "gpt-5.6-sol", "prompt": "next", "codexResume": True, "codexSessionId": "019abc"}
    fallback = {"model": "gpt-5.6-sol", "prompt": "next", "codexResume": True, "codexSessionId": None}
    self.assertEqual(runner.codex_command_args(first)[:5], ["codex", "--ask-for-approval", "never", "exec", "--json"])
    self.assertIn("resume", runner.codex_command_args(followup))
    self.assertIn("019abc", runner.codex_command_args(followup))
    self.assertIn("--last", runner.codex_command_args(fallback))

  def test_followup_run_marks_native_resume(self) -> None:
    workspace = self.create_workspace()
    runtime = ChatRuntime(self.store, self.users, FakeRunner(delay=0.2))
    session = runtime.create_session(workspace["id"], self.users["li.review"], "Native")
    metadata = self.store.load_metadata()
    metadata["chatSessions"][session["id"]]["codexSessionId"] = "native-1"
    metadata["chatSessions"][session["id"]]["codexNativeResumable"] = True
    self.store.save_metadata(metadata)
    second = runtime.start_run(workspace["id"], self.users["li.review"], {"prompt": "Second", "sessionId": session["id"]})
    self.assertTrue(second["run"]["codexResume"])
    self.assertEqual(second["run"]["codexSessionId"], "native-1")
    self.wait_for_status(runtime, workspace["id"], session["id"], "completed")

  def test_default_model_is_gpt_56_sol(self) -> None:
    workspace = self.create_workspace()
    runtime = ChatRuntime(self.store, self.users, FakeRunner())
    result = runtime.start_run(workspace["id"], self.users["li.review"], {"prompt": "Check revenue"})
    self.assertEqual(result["run"]["model"], "gpt-5.6-sol")
    self.wait_for_status(runtime, workspace["id"], result["session"]["id"], "completed")


if __name__ == "__main__":
  unittest.main()
