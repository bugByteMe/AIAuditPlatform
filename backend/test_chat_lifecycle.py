from chat_runtime_test_support import *


class ChatLifecycleTest(ChatRuntimeTestBase):
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
    reloaded_workspace = self.store.public_workspace(
      self.store.get_workspace(workspace["id"], self.users["li.review"])
    )
    reloaded_session = next(item for item in reloaded_workspace["sessions"] if item["id"] == result["session"]["id"])
    self.assertEqual(reloaded_session["events"], [])

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

  def test_group_live_run_limit_applies_across_workspaces(self) -> None:
    first_user = {**deepcopy(OWNER), "groupId": "grp_a"}
    second_user = {**deepcopy(OWNER), "username": "second.review", "groupId": "grp_a"}
    users = {first_user["username"]: first_user, second_user["username"]: second_user}
    groups = {"grp_a": {"id": "grp_a", "name": "Audit", "liveRunLimit": 1}}
    first_workspace = self.store.create_workspace(
      first_user, "First workspace", False, [UploadedFile("first.txt", b"first")]
    )
    second_workspace = self.store.create_workspace(
      second_user, "Second workspace", False, [UploadedFile("second.txt", b"second")]
    )
    runtime = ChatRuntime(self.store, users, FakeRunner(delay=0.25), capacity=2, groups=groups)
    first = runtime.start_run(first_workspace["id"], first_user, {"prompt": "First"})

    with self.assertRaises(StorageError) as context:
      runtime.start_run(second_workspace["id"], second_user, {"prompt": "Second"})
    self.assertEqual(context.exception.code, "group_live_run_limit_reached")

    self.wait_for_status(runtime, first_workspace["id"], first["session"]["id"], "completed")
    second = runtime.start_run(second_workspace["id"], second_user, {"prompt": "Second"})
    self.assertEqual(second["run"]["groupId"], "grp_a")
    self.wait_for_status(runtime, second_workspace["id"], second["session"]["id"], "completed", second_user)

  def test_zero_group_live_run_limit_pauses_new_runs(self) -> None:
    user = {**deepcopy(OWNER), "groupId": "grp_paused"}
    workspace = self.store.create_workspace(user, "Paused workspace", False, [UploadedFile("file.txt", b"data")])
    runtime = ChatRuntime(
      self.store,
      {user["username"]: user},
      FakeRunner(),
      groups={"grp_paused": {"id": "grp_paused", "name": "Paused", "liveRunLimit": 0}},
    )

    with self.assertRaises(StorageError) as context:
      runtime.start_run(workspace["id"], user, {"prompt": "Blocked"})
    self.assertEqual(context.exception.code, "group_live_run_limit_reached")

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

  def test_delete_session_removes_terminal_history_and_allows_empty_workspace(self) -> None:
    workspace = self.create_workspace()
    runtime = ChatRuntime(self.store, self.users, FakeRunner())
    session = runtime.create_session(workspace["id"], self.users["li.review"], "Delete me")
    runtime.append_event(session["id"], "assistant", "Completed", "run_delete")
    runtime.chat_store.save_run({
      "id": "run_delete",
      "workspaceId": workspace["id"],
      "sessionId": session["id"],
      "user": "li.review",
      "status": "completed",
    })
    home = runtime.codex_preparer.session_home("li.review", session["id"])
    home.mkdir(parents=True)
    fork_base = runtime.codex_preparer.fork_base_home("run_delete")
    fork_base.mkdir(parents=True)

    deleted = runtime.delete_session(workspace["id"], session["id"], self.users["li.review"])

    self.assertEqual(deleted, {"id": session["id"], "runIds": ["run_delete"]})
    self.assertEqual(runtime.list_sessions(workspace["id"], self.users["li.review"]), [])
    self.assertIsNone(runtime.chat_store.get_session(session["id"]))
    self.assertIsNone(runtime.chat_store.get_run("run_delete"))
    self.assertFalse(runtime.chat_store.events_path(session["id"]).exists())
    self.assertFalse(home.exists())
    self.assertFalse(fork_base.exists())

  def test_delete_session_requires_owner_or_system_admin(self) -> None:
    workspace = self.create_workspace()
    runtime = ChatRuntime(self.store, self.users, FakeRunner())
    session = runtime.create_session(workspace["id"], self.users["li.review"], "Protected")
    collaborator = {"username": "wang.audit", "role": "user", "group": "审计一组"}
    administrator = {"username": "root.admin", "role": "system_admin", "group": ""}

    with self.assertRaises(StorageError) as context:
      runtime.delete_session(workspace["id"], session["id"], collaborator)
    self.assertEqual(context.exception.code, "forbidden")
    self.assertIsNotNone(runtime.chat_store.get_session(session["id"]))

    runtime.delete_session(workspace["id"], session["id"], administrator)
    self.assertIsNone(runtime.chat_store.get_session(session["id"]))

  def test_delete_session_rejects_active_run_without_mutation(self) -> None:
    workspace = self.create_workspace()
    runtime = ChatRuntime(self.store, self.users, FakeRunner())
    session = runtime.create_session(workspace["id"], self.users["li.review"], "Active")
    runtime.chat_store.save_run({
      "id": "run_active",
      "workspaceId": workspace["id"],
      "sessionId": session["id"],
      "user": "li.review",
      "status": "queued",
    })

    with self.assertRaises(StorageError) as context:
      runtime.delete_session(workspace["id"], session["id"], self.users["li.review"])

    self.assertEqual(context.exception.code, "session_run_active")
    self.assertIsNotNone(runtime.chat_store.get_session(session["id"]))
    self.assertIsNotNone(runtime.chat_store.get_run("run_active"))
    self.assertEqual(runtime.list_sessions(workspace["id"], self.users["li.review"])[0]["id"], session["id"])

  def test_delete_session_rejects_missing_or_cross_workspace_session(self) -> None:
    first_workspace = self.create_workspace()
    second_workspace = self.store.create_workspace(
      self.users["li.review"],
      "Second workspace",
      False,
      [UploadedFile("notes.txt", b"notes")],
    )
    runtime = ChatRuntime(self.store, self.users, FakeRunner())
    session = runtime.create_session(first_workspace["id"], self.users["li.review"], "First")

    with self.assertRaises(StorageError) as missing:
      runtime.delete_session(first_workspace["id"], "chat_missing", self.users["li.review"])
    self.assertEqual(missing.exception.code, "not_found")

    with self.assertRaises(StorageError) as cross_workspace:
      runtime.delete_session(second_workspace["id"], session["id"], self.users["li.review"])
    self.assertEqual(cross_workspace.exception.code, "not_found")
    self.assertIsNotNone(runtime.chat_store.get_session(session["id"]))

  def test_delete_source_session_keeps_independent_fork(self) -> None:
    workspace = self.create_workspace()
    runtime = ChatRuntime(self.store, self.users, FakeRunner())
    source = runtime.create_session(workspace["id"], self.users["li.review"], "Source")
    fork = runtime.create_session(workspace["id"], self.users["li.review"], "Fork")
    stored_fork = runtime.chat_store.get_session(fork["id"])
    stored_fork["forkedFromSessionId"] = source["id"]
    runtime.chat_store.save_session(stored_fork)

    runtime.delete_session(workspace["id"], source["id"], self.users["li.review"])

    remaining = runtime.list_sessions(workspace["id"], self.users["li.review"])
    self.assertEqual([item["id"] for item in remaining], [fork["id"]])
    self.assertEqual(remaining[0]["forkedFromSessionId"], source["id"])

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
    (source_home / "sessions" / "rollout-native-source.jsonl").write_text("conversation", encoding="utf-8")
    (source_home / "sessions" / "rollout-ancestor.jsonl").write_text("unrelated conversation", encoding="utf-8")
    (source_home / "auth.json").write_text("secret", encoding="utf-8")
    (source_home / "config.toml").write_text("secret config", encoding="utf-8")

    with patch.object(runtime.chat_store, "append_event", wraps=runtime.chat_store.append_event) as append_event:
      fork = runtime.fork_session(workspace["id"], source["id"], self.users["li.review"], "Branch")
    append_event.assert_not_called()

    self.assertNotEqual(fork["id"], source["id"])
    self.assertEqual(fork["forkedFromSessionId"], source["id"])
    self.assertEqual(fork["events"], [])
    persisted_fork = runtime.public_session(fork["id"])
    self.assertEqual([event[1] for event in persisted_fork["events"]], ["Earlier question", "Earlier answer"])
    with patch.object(runtime.chat_store, "events", side_effect=AssertionError("append must not rescan history")):
      appended = runtime.chat_store.append_event(fork["id"], {"type": "progress", "message": "Next event"})
    self.assertEqual(appended["id"], 3)
    self.assertEqual(runtime.list_sessions(workspace["id"], self.users["li.review"])[1]["id"], fork["id"])
    fork_home = runtime.codex_preparer.session_home("li.review", fork["id"])
    self.assertEqual((fork_home / "sessions" / "rollout-native-source.jsonl").read_text(encoding="utf-8"), "conversation")
    self.assertFalse((fork_home / "sessions" / "rollout-ancestor.jsonl").exists())
    self.assertFalse((fork_home / "auth.json").exists())
    self.assertFalse((fork_home / "config.toml").exists())

    run = runtime.start_run(workspace["id"], self.users["li.review"], {"prompt": "Branch question", "sessionId": fork["id"]})
    self.assertTrue(run["run"]["codexFork"])
    self.assertEqual(run["run"]["codexSessionId"], "native-source")
    self.wait_for_status(runtime, workspace["id"], fork["id"], "completed")
    source_after = runtime.public_session(source["id"])
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
    (source_home / "sessions" / "rollout-native-source.jsonl").write_text("completed turn", encoding="utf-8")

    active = runtime.start_run(workspace["id"], self.users["li.review"], {"prompt": "In-progress question", "sessionId": source["id"]})
    fork = runtime.fork_session(workspace["id"], source["id"], self.users["li.review"], "Stable branch")

    persisted_fork = runtime.public_session(fork["id"])
    messages = [event[1] for event in persisted_fork["events"]]
    self.assertEqual(messages, ["Completed question", "Completed answer"])
    fork_home = runtime.codex_preparer.session_home("li.review", fork["id"])
    self.assertEqual((fork_home / "sessions" / "rollout-native-source.jsonl").read_text(encoding="utf-8"), "completed turn")
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
    self.users["li.review"]["providerMode"] = "micu"
    self.users["li.review"]["usedTokens"] = 99_999_999

    def exhausted(_user):
      raise StorageError("budget_exhausted", "MicuAPI balance is exhausted")

    runtime = ChatRuntime(self.store, self.users, FakeRunner(), budget_checker=exhausted)
    with self.assertRaises(StorageError) as context:
      runtime.start_run(workspace["id"], self.users["li.review"], {"prompt": "Check revenue"})
    self.assertEqual(context.exception.code, "budget_exhausted")

  def test_custom_provider_does_not_use_micu_budget_checker(self) -> None:
    workspace = self.create_workspace()
    user = self.users["li.review"]
    user["providerMode"] = "custom"
    user["customCodex"] = {"baseUrl": "https://custom.example/v1", "apiKey": "key-placeholder"}
    checked = []
    runtime = ChatRuntime(self.store, self.users, FakeRunner(), budget_checker=lambda candidate: checked.append(candidate))
    result = runtime.start_run(workspace["id"], user, {"prompt": "Check revenue"})
    self.wait_for_status(runtime, workspace["id"], result["session"]["id"], "completed")
    self.assertEqual(checked, [])

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

