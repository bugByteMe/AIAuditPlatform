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


class ChatRuntimeTestBase(unittest.TestCase):
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

  def wait_for_status(
    self, runtime: ChatRuntime, workspace_id: str, session_id: str, status: str, user: dict | None = None
  ) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
      sessions = runtime.list_sessions(workspace_id, user or self.users["li.review"])
      session = next(item for item in sessions if item["id"] == session_id)
      if session["status"] == status:
        return runtime.public_session(session_id)
      time.sleep(0.05)
    self.fail(f"session did not reach {status}")

