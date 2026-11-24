from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path

from chat_store import ChatStore
from input_bridge import InputBridge, snapshot_image
from user_input_mcp import ask
from user_input_schema import MAX_IMAGE_BYTES, validate_answer, validate_question
from workspace_store import StorageError


class InputValidationTest(unittest.TestCase):
  def test_all_question_and_answer_types(self):
    for kind, value in [("yes_no", False), ("text", "Please use the prior year")]:
      question = validate_question({"type": kind, "title": "Question"})
      self.assertEqual(validate_answer(question, {"status": "answered", "value": value})["value"], value)
      self.assertEqual(validate_answer(question, {"status": "skipped"}), {"status": "skipped"})
    question = validate_question({"type": "multiple_choice", "title": "Choose", "multiple": True,
                                  "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]})
    self.assertEqual(validate_answer(question, {"status": "answered", "value": ["b", "a"]})["value"], ["a", "b"])
    question["multiple"] = False
    with self.assertRaises(ValueError):
      validate_answer(question, {"status": "answered", "value": ["a", "b"]})

  def test_invalid_payloads(self):
    for value in [None, {}, {"type": "unknown", "title": "Q"}, {"type": "text", "title": "Q", "text": "中" * 6000},
                  {"type": "text", "title": "Q", "images": ["x"] * 5}]:
      with self.assertRaises(ValueError):
        validate_question(value)
    with self.assertRaises(ValueError):
      validate_answer({"type": "yes_no"}, {"status": "answered", "value": "false"})
    with self.assertRaises(ValueError):
      validate_answer({"type": "text"}, {"status": "answered", "value": " "})


class InputStoreTest(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.store = ChatStore(Path(self.temp.name))
    self.store.save_session({"id": "s", "workspaceId": "w", "title": "Chat", "status": "running", "updated": "now"})
    self.run = {"id": "run_1", "workspaceId": "w", "sessionId": "s", "user": "owner", "status": "running", "updated": "now"}
    self.store.save_run(self.run)
    self.request_id = "a" * 32
    self.event = {"type": "user_input_requested", "message": "Proceed?", "inputRequest": {
      "id": self.request_id, "question": {"type": "yes_no", "title": "Proceed?"}, "images": [],
    }}
    self.store.commit_runner_event("run_1", self.event, source_event_id=1)

  def tearDown(self):
    self.store.engine.dispose()
    self.temp.cleanup()

  def test_answer_is_durable_idempotent_and_delivered_via_outbox(self):
    answer = {"status": "answered", "value": False}
    request, event = self.store.input_requests.resolve(self.request_id, "owner", answer)
    self.assertEqual(request["result"], {"requestId": self.request_id, **answer})
    self.assertEqual(event["type"], "user_input_resolved")
    self.assertEqual(len(self.store.input_requests.outbox()), 1)
    _, duplicate = self.store.input_requests.resolve(self.request_id, "owner", answer)
    self.assertIsNone(duplicate)
    replay = self.store.commit_runner_event("run_1", self.event, source_event_id=1)
    self.assertFalse(replay["inserted"])
    reopened = ChatStore(Path(self.temp.name))
    self.assertEqual(reopened.input_requests.get(self.request_id)["result"]["value"], False)
    reopened.engine.dispose()
    self.store.input_requests.delivered(self.request_id)
    self.assertEqual(self.store.input_requests.outbox(), [])
    self.assertIn("inputRequest", self.store.events("s")[-1])
    self.assertIn("inputRequest", self.store.public_session("s")["events"][-1][7])

  def test_only_initiator_can_answer_and_conflicting_answers_fail(self):
    with self.assertRaises(StorageError) as denied:
      self.store.input_requests.resolve(self.request_id, "collaborator", {"status": "skipped"})
    self.assertEqual(denied.exception.code, "forbidden")
    self.store.input_requests.resolve(self.request_id, "owner", {"status": "answered", "value": True})
    with self.assertRaises(StorageError) as conflict:
      self.store.input_requests.resolve(self.request_id, "owner", {"status": "answered", "value": False})
    self.assertEqual(conflict.exception.code, "input_conflict")

  def test_terminal_and_cancelled_requests_cannot_be_answered(self):
    self.store.save_run({**self.run, "status": "stopped"})
    events = self.store.input_requests.cancel_run("run_1")
    self.assertEqual(events[0]["inputRequest"]["status"], "cancelled")
    self.assertEqual(self.store.input_requests.cancel_run("run_1"), [])
    with self.assertRaises(StorageError):
      self.store.input_requests.resolve(self.request_id, "owner", {"status": "skipped"})

  def test_concurrent_answers_accept_only_one(self):
    barrier = threading.Barrier(2)
    accepted, rejected = [], []
    def answer(value):
      barrier.wait()
      try:
        accepted.append(self.store.input_requests.resolve(self.request_id, "owner", {"status": "answered", "value": value}))
      except StorageError as exc:
        rejected.append(exc.code)
    threads = [threading.Thread(target=answer, args=(value,)) for value in (True, False)]
    for thread in threads:
      thread.start()
    for thread in threads:
      thread.join(timeout=5)
    self.assertEqual(len(accepted), 1)
    self.assertEqual(rejected, ["input_conflict"])


class InputBridgeTest(unittest.TestCase):
  def test_mcp_waits_and_returns_the_exact_answer(self):
    async def scenario(root):
      bridge = InputBridge(root, "run_1")
      bridge.path.mkdir(parents=True)
      task = asyncio.create_task(ask({"type": "text", "title": "Which year?"}, bridge.path))
      while not list(bridge.path.glob("*.request.json")):
        await asyncio.sleep(0.01)
      request_id = next(bridge.path.glob("*.request.json")).name.split(".")[0]
      self.assertFalse(task.done())
      result = {"requestId": request_id, "status": "answered", "value": "2026"}
      bridge.respond(request_id, result)
      bridge.respond(request_id, result)
      self.assertEqual(await asyncio.wait_for(task, 2), result)
      with self.assertRaises(ValueError):
        bridge.respond(request_id, {**result, "value": "2025"})
      with self.assertRaises(ValueError):
        InputBridge(root, "run_2").respond(request_id, result)
    with tempfile.TemporaryDirectory() as temp:
      asyncio.run(scenario(Path(temp)))

  def test_images_are_immutable_and_paths_are_confined(self):
    with tempfile.TemporaryDirectory() as temp:
      root = Path(temp)
      workspace = root / "workspace"
      workspace.mkdir()
      image = workspace / "image.png"
      original = b"\x89PNG\r\n\x1a\noriginal"
      image.write_bytes(original)
      descriptor = snapshot_image(workspace, "/workspace/image.png", root / "blobs")
      image.write_bytes(b"changed")
      digest = descriptor["blob"]
      self.assertEqual((root / "blobs" / digest[:2] / digest[2:4] / digest).read_bytes(), original)
      (root / "secret.png").write_bytes(original)
      (workspace / "link").symlink_to(root, target_is_directory=True)
      for path in ["../secret.png", "/etc/passwd", "link/secret.png"]:
        with self.assertRaises((ValueError, OSError)):
          snapshot_image(workspace, path, root / "blobs")
      image.write_bytes(b"x" * (MAX_IMAGE_BYTES + 1))
      with self.assertRaises(ValueError):
        snapshot_image(workspace, "image.png", root / "blobs")


if __name__ == "__main__":
  unittest.main()
