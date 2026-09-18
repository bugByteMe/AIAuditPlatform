from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chat_runtime import ChatRuntime, CodexRunner, DockerCodexRunner
from config import SETTINGS
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
    self.assertNotIn("runs", metadata)
    self.assertNotIn("chatSessions", metadata)
    self.assertNotIn("events", metadata)
    self.assertEqual(runtime.chat_store.get_run(result["run"]["id"])["status"], "completed")
    self.assertEqual(self.users["li.review"]["usedTokens"], 10)
    self.assertTrue(any(event[0] == "assistant" for event in session["events"]))
    reloaded_workspace = self.store.list_workspaces(self.users["li.review"])[0]
    reloaded_session = next(item for item in reloaded_workspace["sessions"] if item["id"] == result["session"]["id"])
    self.assertTrue(any(event[0] == "assistant" for event in reloaded_session["events"]))
    self.assertTrue(any(event[3] == result["run"]["id"] for event in reloaded_session["events"]))

  def test_exclusive_workspace_lock_rejects_second_active_run(self) -> None:
    workspace = self.create_workspace()
    self.store.update_workspace(workspace["id"], self.users["li.review"], {"runLockEnabled": True})
    runtime = ChatRuntime(self.store, self.users, FakeRunner(delay=0.2))
    result = runtime.start_run(workspace["id"], self.users["li.review"], {"prompt": "First"})
    with self.assertRaises(StorageError) as context:
      runtime.start_run(workspace["id"], self.users["li.review"], {"prompt": "Second"})
    self.assertEqual(context.exception.code, "workspace_locked")
    self.wait_for_status(runtime, workspace["id"], result["session"]["id"], "completed")

  def test_concurrent_workspace_run_requires_confirmation(self) -> None:
    workspace = self.create_workspace()
    runtime = ChatRuntime(self.store, self.users, FakeRunner(delay=0.25), capacity=2)
    first_session = runtime.create_session(workspace["id"], self.users["li.review"], "First session")
    second_session = runtime.create_session(workspace["id"], self.users["li.review"], "Second session")
    first = runtime.start_run(workspace["id"], self.users["li.review"], {"prompt": "First", "sessionId": first_session["id"]})

    with self.assertRaises(StorageError) as context:
      runtime.start_run(workspace["id"], self.users["li.review"], {"prompt": "Second", "sessionId": second_session["id"]})
    self.assertEqual(context.exception.code, "concurrent_confirmation_required")

    time.sleep(0.1)
    second = runtime.start_run(
      workspace["id"],
      self.users["li.review"],
      {"prompt": "Second", "sessionId": second_session["id"], "confirmConcurrent": True},
    )
    active_workspace = self.store.get_workspace(workspace["id"], self.users["li.review"])
    self.assertTrue(active_workspace["locked"])
    with self.assertRaises(StorageError) as context:
      self.store.delete_workspace_path(workspace["id"], self.users["li.review"], "workpapers/income.txt")
    self.assertEqual(context.exception.code, "workspace_locked")
    with self.assertRaises(StorageError) as context:
      runtime.start_run(
        workspace["id"],
        self.users["li.review"],
        {"prompt": "Same chat", "sessionId": first_session["id"], "confirmConcurrent": True},
      )
    self.assertEqual(context.exception.code, "session_run_active")

    self.wait_for_status(runtime, workspace["id"], first["session"]["id"], "completed")
    self.assertTrue(self.store.get_workspace(workspace["id"], self.users["li.review"])["locked"])
    self.wait_for_status(runtime, workspace["id"], second["session"]["id"], "completed")
    self.assertFalse(self.store.get_workspace(workspace["id"], self.users["li.review"])["locked"])

  def test_accessible_user_can_rename_chat_session(self) -> None:
    workspace = self.create_workspace()
    runtime = ChatRuntime(self.store, self.users, FakeRunner())
    session = runtime.create_session(workspace["id"], self.users["li.review"], "Original title")

    renamed = runtime.update_session(workspace["id"], session["id"], self.users["li.review"], "  Revised title  ")

    self.assertEqual(renamed["title"], "Revised title")
    self.assertEqual(runtime.list_sessions(workspace["id"], self.users["li.review"])[0]["title"], "Revised title")

  def test_chat_session_rename_rejects_empty_title(self) -> None:
    workspace = self.create_workspace()
    runtime = ChatRuntime(self.store, self.users, FakeRunner())
    session = runtime.create_session(workspace["id"], self.users["li.review"], "Original title")

    with self.assertRaises(StorageError) as context:
      runtime.update_session(workspace["id"], session["id"], self.users["li.review"], "   ")

    self.assertEqual(context.exception.code, "bad_request")

  def test_fork_session_persists_distinct_history_and_excludes_credentials(self) -> None:
    workspace = self.create_workspace()
    runtime = ChatRuntime(self.store, self.users, FakeRunner(delay=0.2))
    source = runtime.create_session(workspace["id"], self.users["li.review"], "Original")
    stored = runtime.chat_store.get_session(source["id"])
    stored["codexSessionId"] = "native-source"
    stored["codexNativeResumable"] = True
    stored["codexHomeUser"] = self.users["li.review"]["username"]
    runtime.chat_store.save_session(stored)
    runtime.append_event(source["id"], "user", "Earlier question", "run_old")
    runtime.append_event(source["id"], "assistant", "Earlier answer", "run_old")
    source_home = runtime.codex_preparer.session_home("li.review", source["id"])
    (source_home / "sessions").mkdir(parents=True)
    (source_home / "sessions" / "rollout.jsonl").write_text("conversation", encoding="utf-8")
    (source_home / "auth.json").write_text("secret", encoding="utf-8")
    (source_home / "config.toml").write_text("secret config", encoding="utf-8")

    fork = runtime.fork_session(workspace["id"], source["id"], self.users["li.review"], "Branch")

    self.assertNotEqual(fork["id"], source["id"])
    self.assertEqual(fork["forkedFromSessionId"], source["id"])
    self.assertEqual([event[1] for event in fork["events"]], ["Earlier question", "Earlier answer"])
    self.assertEqual(runtime.list_sessions(workspace["id"], self.users["li.review"])[1]["id"], fork["id"])
    fork_home = runtime.codex_preparer.session_home("li.review", fork["id"])
    self.assertEqual((fork_home / "sessions" / "rollout.jsonl").read_text(encoding="utf-8"), "conversation")
    self.assertFalse((fork_home / "auth.json").exists())
    self.assertFalse((fork_home / "config.toml").exists())

    run = runtime.start_run(workspace["id"], self.users["li.review"], {"prompt": "Branch question", "sessionId": fork["id"]})
    self.assertTrue(run["run"]["codexFork"])
    self.assertEqual(run["run"]["codexSessionId"], "native-source")
    self.wait_for_status(runtime, workspace["id"], fork["id"], "completed")
    source_after = next(item for item in runtime.list_sessions(workspace["id"], self.users["li.review"]) if item["id"] == source["id"])
    self.assertEqual([event[1] for event in source_after["events"]], ["Earlier question", "Earlier answer"])

  def test_active_session_fork_uses_last_completed_turn(self) -> None:
    workspace = self.create_workspace()
    runtime = ChatRuntime(self.store, self.users, FakeRunner(delay=0.2))
    source = runtime.create_session(workspace["id"], self.users["li.review"], "Original")
    stored = runtime.chat_store.get_session(source["id"])
    stored["codexSessionId"] = "native-source"
    stored["codexNativeResumable"] = True
    runtime.chat_store.save_session(stored)
    runtime.append_event(source["id"], "user", "Completed question", "run_old")
    runtime.append_event(source["id"], "assistant", "Completed answer", "run_old")
    source_home = runtime.codex_preparer.session_home("li.review", source["id"])
    (source_home / "sessions").mkdir(parents=True)
    (source_home / "sessions" / "rollout.jsonl").write_text("completed turn", encoding="utf-8")

    active = runtime.start_run(workspace["id"], self.users["li.review"], {"prompt": "In-progress question", "sessionId": source["id"]})
    fork = runtime.fork_session(workspace["id"], source["id"], self.users["li.review"], "Stable branch")

    messages = [event[1] for event in fork["events"]]
    self.assertEqual(messages, ["Completed question", "Completed answer"])
    fork_home = runtime.codex_preparer.session_home("li.review", fork["id"])
    self.assertEqual((fork_home / "sessions" / "rollout.jsonl").read_text(encoding="utf-8"), "completed turn")
    self.wait_for_status(runtime, workspace["id"], active["session"]["id"], "completed")

  def test_fork_rejects_missing_codex_checkpoint(self) -> None:
    workspace = self.create_workspace()
    runtime = ChatRuntime(self.store, self.users, FakeRunner())
    source = runtime.create_session(workspace["id"], self.users["li.review"], "Legacy")
    stored = runtime.chat_store.get_session(source["id"])
    stored["codexSessionId"] = "native-source"
    stored["codexNativeResumable"] = True
    runtime.chat_store.save_session(stored)
    runtime.append_event(source["id"], "user", "Earlier question", "run_old")

    with self.assertRaises(StorageError) as context:
      runtime.fork_session(workspace["id"], source["id"], self.users["li.review"], "Branch")

    self.assertEqual(context.exception.code, "session_not_forkable")

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
    self.assertEqual(runtime.chat_store.sessions(), {})
    self.assertEqual(runtime.chat_store.runs(), {})

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

  def test_docker_runner_copies_configured_skill_path_into_codex_home(self) -> None:
    runner = DockerCodexRunner()
    runner.codex_home_root = Path(self.tempdir.name) / "codex_homes"
    runner.skill_path = Path(self.tempdir.name) / "source_skills"
    (runner.skill_path / "audit-skill").mkdir(parents=True)
    (runner.skill_path / "audit-skill" / "SKILL.md").write_text("# Audit Skill\n", encoding="utf-8")
    run = {
      "id": "run_1",
      "user": "li.review",
      "sessionId": "chat_1",
      "model": "gpt-5-codex",
      "reasoning": "high",
      "codexSettings": {"baseUrl": "https://codex.example/v1", "apiKey": "sk-test"},
    }
    codex_home = runner.prepare_codex_home(run)
    self.assertEqual((codex_home / "skills" / "audit-skill" / "SKILL.md").read_text(encoding="utf-8"), "# Audit Skill\n")

  def test_docker_runner_removes_stale_codex_tmp_before_run(self) -> None:
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
    tmp_file = runner.codex_home_root / "li.review" / "chat_1" / "tmp" / "arg0" / "codex-arg00lqWw7" / "apply_patch"
    tmp_file.parent.mkdir(parents=True)
    tmp_file.write_text("stale", encoding="utf-8")
    codex_home = runner.prepare_codex_home(run)
    self.assertFalse((codex_home / "tmp").exists())

  def test_docker_runner_uses_native_resume_for_followup_runs(self) -> None:
    runner = DockerCodexRunner()
    first = {"model": "gpt-5.6-sol", "prompt": "first", "codexResume": False}
    followup = {"model": "gpt-5.6-sol", "prompt": "next", "codexResume": True, "codexSessionId": "019abc"}
    fallback = {"model": "gpt-5.6-sol", "prompt": "next", "codexResume": True, "codexSessionId": None}
    self.assertEqual(
      runner.codex_command_args(first)[:7],
      ["codex", "--ask-for-approval", "never", "--sandbox", "danger-full-access", "exec", "--json"],
    )
    self.assertEqual(
      runner.codex_command_args(followup)[:7],
      ["codex", "--ask-for-approval", "never", "--sandbox", "danger-full-access", "exec", "resume"],
    )
    self.assertIn("resume", runner.codex_command_args(followup))
    self.assertIn("019abc", runner.codex_command_args(followup))
    self.assertIn("--last", runner.codex_command_args(fallback))
    fork = {"model": "gpt-5.6-sol", "prompt": "branch", "codexFork": True, "codexSessionId": "019abc"}
    self.assertIn("fork", runner.codex_command_args(fork))
    self.assertIn("019abc", runner.codex_command_args(fork))

  def test_docker_runner_parses_completed_agent_message_item(self) -> None:
    runner = DockerCodexRunner()
    event = runner.parse_json_event(
      json.dumps(
        {
          "type": "item.completed",
          "item": {
            "id": "item_0",
            "text": "Hello. What would you like to work on?",
            "type": "agent_message",
          },
        }
      )
    )
    self.assertEqual(event["type"], "assistant")
    self.assertEqual(event["message"], "Hello. What would you like to work on?")
    self.assertEqual(event["raw"]["type"], "item.completed")

  def test_docker_runner_parses_turn_completed_usage_without_double_counting_cached_input(self) -> None:
    runner = DockerCodexRunner()
    event = runner.parse_json_event(
      json.dumps(
        {
          "type": "turn.completed",
          "usage": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 25},
        }
      )
    )
    self.assertEqual(event["type"], "usage")
    self.assertEqual(event["tokens"], 125)
    self.assertEqual(event["inputTokens"], 100)
    self.assertEqual(event["cachedInputTokens"], 80)
    self.assertEqual(event["outputTokens"], 25)

  def test_duplicate_terminal_usage_only_counts_positive_delta(self) -> None:
    workspace = self.create_workspace()
    usage = {"type": "usage", "message": "usage", "tokens": 125, "inputTokens": 100, "cachedInputTokens": 80, "outputTokens": 25}
    runtime = ChatRuntime(self.store, self.users, FakeRunner([usage, usage]))
    result = runtime.start_run(workspace["id"], self.users["li.review"], {"prompt": "Count usage"})
    self.wait_for_status(runtime, workspace["id"], result["session"]["id"], "completed")
    run = runtime.chat_store.get_run(result["run"]["id"])
    self.assertEqual(self.users["li.review"]["usedTokens"], 125)
    self.assertEqual(run["tokens"], 125)
    self.assertEqual(run["cachedInputTokens"], 80)

  def test_run_is_rejected_when_group_disk_limit_is_reached(self) -> None:
    user = self.users["li.review"]
    user.update({"id": "usr_1", "groupId": "grp_1"})
    groups = {"grp_1": {"id": "grp_1", "name": "Audit", "diskLimitBytes": len(b"initial income")}}
    self.store.set_account_provider(lambda: (self.users, groups))
    workspace = self.create_workspace()
    runtime = ChatRuntime(self.store, self.users, FakeRunner())
    with self.assertRaises(StorageError) as context:
      runtime.start_run(workspace["id"], user, {"prompt": "No capacity"})
    self.assertEqual(context.exception.code, "group_disk_quota_exceeded")

  def test_docker_runner_parses_command_execution_events(self) -> None:
    runner = DockerCodexRunner()
    item_event = runner.parse_json_event(json.dumps({"type": "item.started", "item": {"id": "call_1", "type": "command_execution", "command": "python3 -m unittest"}}))
    top_level_event = runner.parse_json_event(json.dumps({"type": "exec_command", "command": ["ls", "-la"]}))
    self.assertEqual(item_event["type"], "command")
    self.assertEqual(item_event["message"], "python3 -m unittest")
    self.assertEqual(item_event["status"], "executing")
    self.assertEqual(item_event["toolCallId"], "call_1")
    self.assertEqual(top_level_event["type"], "command")
    self.assertEqual(top_level_event["message"], "ls -la")

  def test_docker_runner_parses_web_search_events(self) -> None:
    runner = DockerCodexRunner()
    event = runner.parse_json_event(json.dumps({"type": "web_search_call", "query": "audit sampling guidance"}))
    self.assertEqual(event["type"], "websearch")
    self.assertEqual(event["message"], "audit sampling guidance")

  def test_public_session_coalesces_tool_start_and_result(self) -> None:
    workspace = self.create_workspace()
    runtime = ChatRuntime(
      self.store,
      self.users,
      FakeRunner(
        [
          {"type": "command", "message": "python3 -m unittest", "status": "executing", "toolCallId": "call_1"},
          {"type": "command", "message": "OK", "status": "completed", "toolCallId": "call_1"},
        ]
      ),
    )
    result = runtime.start_run(workspace["id"], self.users["li.review"], {"prompt": "Run tests"})
    session = self.wait_for_status(runtime, workspace["id"], result["session"]["id"], "completed")
    command_events = [event for event in session["events"] if event[0] == "command"]
    self.assertEqual(len(command_events), 1)
    self.assertEqual(command_events[0][1], "OK")
    self.assertEqual(command_events[0][5], "completed")
    self.assertEqual(command_events[0][6], "call_1")

  def test_followup_run_marks_native_resume(self) -> None:
    workspace = self.create_workspace()
    runtime = ChatRuntime(self.store, self.users, FakeRunner(delay=0.2))
    session = runtime.create_session(workspace["id"], self.users["li.review"], "Native")
    stored_session = runtime.chat_store.get_session(session["id"])
    stored_session["codexSessionId"] = "native-1"
    stored_session["codexNativeResumable"] = True
    runtime.chat_store.save_session(stored_session)
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

  def test_run_records_do_not_persist_codex_settings(self) -> None:
    workspace = self.create_workspace()
    runtime = ChatRuntime(self.store, self.users, FakeRunner())
    result = runtime.start_run(workspace["id"], self.users["li.review"], {"prompt": "Check revenue"})
    self.wait_for_status(runtime, workspace["id"], result["session"]["id"], "completed")
    stored_run = runtime.chat_store.get_run(result["run"]["id"])
    self.assertNotIn("codexSettings", stored_run)
    self.assertNotIn("codexHome", stored_run)

  def test_delete_user_resources_removes_workspace_chat_and_codex_home(self) -> None:
    workspace = self.create_workspace()
    runtime = ChatRuntime(self.store, self.users, FakeRunner())
    result = runtime.start_run(workspace["id"], self.users["li.review"], {"prompt": "Complete first"})
    self.wait_for_status(runtime, workspace["id"], result["session"]["id"], "completed")
    codex_root = Path(self.tempdir.name) / "codex_homes"
    home = codex_root / "li.review" / result["session"]["id"]
    home.mkdir(parents=True)
    (home / "config.toml").write_text("test", encoding="utf-8")
    with patch.object(SETTINGS, "codex_home_root", codex_root):
      deleted = runtime.delete_user_resources({"li.review"})
    self.assertEqual([item["id"] for item in deleted["workspaces"]], [workspace["id"]])
    self.assertIsNone(runtime.chat_store.get_session(result["session"]["id"]))
    self.assertIsNone(runtime.chat_store.get_run(result["run"]["id"]))
    self.assertFalse((codex_root / "li.review").exists())

if __name__ == "__main__":
  unittest.main()
