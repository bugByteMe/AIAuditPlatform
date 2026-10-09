from chat_runtime_test_support import *
from sqlalchemy import event as sqlalchemy_event
from unittest.mock import Mock


class CodexRunnerTest(ChatRuntimeTestBase):
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
    self.assertIn('model_reasoning_effort = "high"', (codex_home / "config.toml").read_text(encoding="utf-8"))
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

  def test_remote_runner_event_is_committed_once_with_cursor_and_usage(self) -> None:
    workspace = self.create_workspace()
    save_users = Mock()
    usage = {
      "type": "usage", "message": "usage", "tokens": 125,
      "inputTokens": 100, "cachedInputTokens": 80, "outputTokens": 25,
      "_workerEventId": 7,
    }
    runtime = ChatRuntime(self.store, self.users, FakeRunner([usage, usage]), save_users=save_users)
    result = runtime.start_run(workspace["id"], self.users["li.review"], {"prompt": "Count remote usage"})
    self.wait_for_status(runtime, workspace["id"], result["session"]["id"], "completed")

    run = runtime.chat_store.get_run(result["run"]["id"])
    events = runtime.chat_store.events(result["session"]["id"])
    usage_events = [event for event in events if event["type"] == "usage"]
    self.assertEqual(len(usage_events), 1)
    self.assertEqual(run["workerEventCursor"], 7)
    self.assertEqual(run["tokens"], 125)
    self.assertEqual(self.users["li.review"]["usedTokens"], 125)
    self.assertNotIn("tokens", usage_events[0])
    save_users.assert_not_called()

  def test_runner_event_transaction_rolls_back_as_one_unit(self) -> None:
    workspace = self.create_workspace()
    runtime = ChatRuntime(self.store, self.users, FakeRunner())
    session = runtime.create_session(workspace["id"], self.users["li.review"], "Atomic event")
    run = {
      "id": "run_atomic", "workspaceId": workspace["id"], "sessionId": session["id"],
      "user": "li.review", "groupId": "", "status": "running", "updated": "before",
      "workerEventCursor": 0, "tokens": 0,
    }
    runtime.chat_store.save_run(run)

    with patch.object(runtime.chat_store, "_update_session", side_effect=RuntimeError("forced rollback")):
      with self.assertRaisesRegex(RuntimeError, "forced rollback"):
        runtime.record_runner_event(
          run["id"], {"type": "usage", "message": "usage", "tokens": 10, "_workerEventId": 1}
        )

    self.assertEqual(runtime.chat_store.events(session["id"]), [])
    self.assertEqual(runtime.chat_store.get_run(run["id"])["workerEventCursor"], 0)
    self.assertEqual(runtime.chat_store.ensure_user_usage("li.review"), 0)

  def test_runner_event_uses_one_transaction_and_notifies_after_commit(self) -> None:
    workspace = self.create_workspace()
    runtime = ChatRuntime(self.store, self.users, FakeRunner())
    session = runtime.create_session(workspace["id"], self.users["li.review"], "One transaction")
    run = {
      "id": "run_transaction", "workspaceId": workspace["id"], "sessionId": session["id"],
      "user": "li.review", "groupId": "", "status": "running", "updated": "before",
      "workerEventCursor": 0, "tokens": 0,
    }
    runtime.chat_store.save_run(run)
    transactions = {"begin": 0, "commit": 0, "rollback": 0}
    notified = []

    def count_begin(_connection):
      transactions["begin"] += 1

    def count_commit(_connection):
      transactions["commit"] += 1

    def count_rollback(_connection):
      transactions["rollback"] += 1

    engine = runtime.chat_store.engine
    sqlalchemy_event.listen(engine, "begin", count_begin)
    sqlalchemy_event.listen(engine, "commit", count_commit)
    sqlalchemy_event.listen(engine, "rollback", count_rollback)
    runtime.set_event_callback(lambda _session_id, _event: notified.append(transactions["commit"]))
    try:
      runtime.record_runner_event(
        run["id"], {"type": "assistant", "message": "done", "_workerEventId": 1}
      )
    finally:
      sqlalchemy_event.remove(engine, "begin", count_begin)
      sqlalchemy_event.remove(engine, "commit", count_commit)
      sqlalchemy_event.remove(engine, "rollback", count_rollback)

    self.assertEqual(transactions, {"begin": 1, "commit": 1, "rollback": 0})
    self.assertEqual(notified, [1])

  def test_database_usage_total_hydrates_users_after_restart(self) -> None:
    workspace = self.create_workspace()
    first_runtime = ChatRuntime(self.store, self.users, FakeRunner([
      {"type": "usage", "message": "usage", "tokens": 25},
    ]))
    result = first_runtime.start_run(workspace["id"], self.users["li.review"], {"prompt": "Persist usage"})
    self.wait_for_status(first_runtime, workspace["id"], result["session"]["id"], "completed")

    reloaded_users = {"li.review": {**deepcopy(OWNER), "usedTokens": 0}}
    ChatRuntime(self.store, reloaded_users, FakeRunner())
    self.assertEqual(reloaded_users["li.review"]["usedTokens"], 25)

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

  def test_workspace_session_summaries_do_not_embed_history(self) -> None:
    workspace = self.create_workspace()
    runtime = ChatRuntime(self.store, self.users, FakeRunner())
    result = runtime.start_run(workspace["id"], self.users["li.review"], {"prompt": "Check revenue"})
    self.wait_for_status(runtime, workspace["id"], result["session"]["id"], "completed")
    public = self.store.public_workspace(self.store.get_workspace(workspace["id"], self.users["li.review"]))
    self.assertTrue(public["sessions"])
    self.assertEqual(public["sessions"][0]["events"], [])

  def test_event_cursor_reads_are_bounded_and_incremental(self) -> None:
    workspace = self.create_workspace()
    runtime = ChatRuntime(self.store, self.users, FakeRunner())
    session = runtime.create_session(workspace["id"], self.users["li.review"])
    for index in range(12):
      runtime.append_event(session["id"], "progress", f"event-{index}", None)
    page = runtime.events(workspace["id"], session["id"], 5, self.users["li.review"], limit=3)
    self.assertEqual([event["id"] for event in page], [6, 7, 8])
    latest = runtime.events(workspace["id"], session["id"], 0, self.users["li.review"], limit=3, latest=True)
    self.assertEqual([event["id"] for event in latest], [10, 11, 12])
    older = runtime.events(workspace["id"], session["id"], 0, self.users["li.review"], before=10, limit=3)
    self.assertEqual([event["id"] for event in older], [7, 8, 9])

  def test_event_cursor_reads_do_not_load_the_workspace_catalog(self) -> None:
    workspace = self.create_workspace()
    runtime = ChatRuntime(self.store, self.users, FakeRunner())
    session = runtime.create_session(workspace["id"], self.users["li.review"])
    runtime.append_event(session["id"], "progress", "indexed event", None)
    with patch.object(self.store, "load_metadata", side_effect=AssertionError("catalog load on event read")):
      events = runtime.events(workspace["id"], session["id"], 0, self.users["li.review"])
    self.assertEqual([event["message"] for event in events], ["indexed event"])

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
