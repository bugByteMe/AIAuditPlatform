from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from chat_input import ChatInputMixin
from chat_runtime_test_support import ChatRuntimeTestBase, ChatRuntime, CodexRunner
from input_bridge import InputBridge, atomic_json
from runner_monitor import monitor_runner
from worker_agent import WorkerState
from workspace_store import StorageError


class WaitingRunner(CodexRunner):
  def __init__(self):
    self.ready = threading.Event()
    self.result = None
    self.stopped = False

  def start(self, run, workspace_path):
    yield {"type": "user_input_requested", "message": "Proceed?", "inputRequest": {
      "id": "c" * 32, "question": {"type": "yes_no", "title": "Proceed?"}, "images": [],
    }}
    self.ready.wait(3)
    if self.result:
      yield {"type": "assistant", "message": json.dumps(self.result)}

  def answer_input(self, run, request_id, result):
    self.result = result
    self.ready.set()

  def stop(self, run):
    self.stopped = True
    self.ready.set()


class InputRuntimeTest(ChatRuntimeTestBase):
  def create_workspace(self):
    # The shared legacy fixture requests group visibility without a group id.
    return self.store.create_workspace(self.users["li.review"], "Input test", False, [])

  def test_website_answer_reaches_original_waiting_run(self):
    runner = WaitingRunner()
    runtime = ChatRuntime(self.store, self.users, runner)
    try:
      workspace = self.create_workspace()
      started = runtime.start_run(workspace["id"], self.users["li.review"], {"prompt": "Ask me"})
      session_id = started["session"]["id"]
      deadline = time.monotonic() + 3
      while not runtime.input_requests(workspace["id"], session_id, self.users["li.review"]):
        self.assertLess(time.monotonic(), deadline)
        time.sleep(0.01)
      self.assertTrue(runtime.public_session(session_id)["waitingForInput"])
      with self.assertRaises(StorageError):
        runtime.require_input(workspace["id"], "wrong-session", "c" * 32, self.users["li.review"])
      result = runtime.answer_input(workspace["id"], session_id, "c" * 32, self.users["li.review"], {"status": "answered", "value": False})
      self.assertEqual(result["status"], "answered")
      self.wait_for_status(runtime, workspace["id"], session_id, "completed")
      self.assertEqual(runner.result, {"requestId": "c" * 32, "status": "answered", "value": False})
      self.assertFalse(runtime.public_session(session_id)["waitingForInput"])
    finally:
      runtime.shutdown = True
      runner.ready.set()

  def test_stop_cancels_waiting_question(self):
    runner = WaitingRunner()
    runtime = ChatRuntime(self.store, self.users, runner)
    try:
      workspace = self.create_workspace()
      started = runtime.start_run(workspace["id"], self.users["li.review"], {"prompt": "Ask me"})
      session_id = started["session"]["id"]
      deadline = time.monotonic() + 3
      while not runtime.input_requests(workspace["id"], session_id, self.users["li.review"]):
        self.assertLess(time.monotonic(), deadline)
        time.sleep(0.01)
      runtime.stop_run(workspace["id"], started["run"]["id"], self.users["li.review"])
      self.assertEqual(runtime.input_requests(workspace["id"], session_id, self.users["li.review"])[0]["status"], "cancelled")
      with self.assertRaises(StorageError):
        runtime.answer_input(workspace["id"], session_id, "c" * 32, self.users["li.review"], {"status": "skipped"})
      self.wait_for_status(runtime, workspace["id"], session_id, "stopped")
    finally:
      runtime.shutdown = True
      runner.ready.set()


class RunnerMonitoringTest(unittest.TestCase):
  def runner(self, process, timeout):
    return Mock(timeout_seconds=timeout, process_wait_timeout_seconds=2,
                stop=lambda run: process.terminate(), parse_json_event=lambda line: {"type": "assistant", "message": line.strip()})

  def test_silent_process_times_out(self):
    with tempfile.TemporaryDirectory() as temp:
      workspace = Path(temp) / "active" / "w"
      workspace.mkdir(parents=True)
      bridge = InputBridge(temp, "run_1")
      bridge.path.mkdir(parents=True)
      process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
      try:
        with self.assertRaisesRegex(RuntimeError, "timed out"):
          list(monitor_runner(self.runner(process, 0.1), process, {"id": "run_1"}, workspace, bridge))
      finally:
        process.terminate()
        process.communicate(timeout=2)

  def test_wait_time_is_excluded_and_stderr_is_drained(self):
    with tempfile.TemporaryDirectory() as temp:
      workspace = Path(temp) / "active" / "w"
      workspace.mkdir(parents=True)
      bridge = InputBridge(temp, "run_1")
      bridge.path.mkdir(parents=True)
      request_id = "d" * 32
      atomic_json(bridge.path / f"{request_id}.request.json", {"type": "text", "title": "Question"})
      response = bridge.path / f"{request_id}.response.json"
      script = f"import pathlib,time,sys; p=pathlib.Path({str(response)!r}); exec('while not p.exists(): time.sleep(0.01)'); sys.stderr.write('x'*200000); print('done')"
      process = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
      timer = threading.Timer(0.4, lambda: bridge.respond(request_id, {"requestId": request_id, "status": "skipped"}))
      timer.start()
      try:
        events = list(monitor_runner(self.runner(process, 0.25), process, {"id": "run_1"}, workspace, bridge))
        self.assertEqual(events[0]["type"], "user_input_requested")
        self.assertEqual(events[-1]["message"], "done")
      finally:
        timer.join()
        if process.poll() is None:
          process.terminate()
        process.communicate(timeout=2)


class WorkerInputDeliveryTest(unittest.TestCase):
  def test_worker_validates_request_and_rejects_cross_run_delivery(self):
    with tempfile.TemporaryDirectory() as temp:
      state = object.__new__(WorkerState)
      state.root = Path(temp)
      state.lock = threading.RLock()
      state.runs = {"run_1": {"id": "run_1", "status": "running"}}
      request_id = "e" * 32
      state.events = {"run_1": [{"type": "user_input_requested", "inputRequest": {
        "id": request_id, "question": {"type": "yes_no", "title": "Continue?"},
      }}]}
      bridge = InputBridge(temp, "run_1")
      bridge.path.mkdir(parents=True)
      atomic_json(bridge.path / f"{request_id}.request.json", {})
      result = {"requestId": request_id, "status": "answered", "value": True}
      self.assertEqual(state.answer_input("run_1", request_id, result), {"accepted": True})
      self.assertEqual(state.answer_input("run_1", request_id, result), {"accepted": True})
      with self.assertRaises(ValueError):
        state.answer_input("run_1", "f" * 32, result)
      state.runs["run_1"]["status"] = "stopping"
      with self.assertRaises(ValueError):
        state.answer_input("run_1", request_id, result)


class InputDeliveryRetryTest(unittest.TestCase):
  def test_recovery_cancels_terminal_questions_but_keeps_live_remote_questions(self):
    runtime = object.__new__(ChatRuntime)
    runtime.chat_store = Mock()
    runtime.chat_store.input_requests.pending_run_ids.return_value = ["terminal", "live"]
    runtime.chat_store.get_run.side_effect = [{"id": "terminal", "status": "failed"}, {"id": "live", "status": "running", "workerId": "worker"}]
    runtime.chat_store.input_requests.cancel_run.return_value = []
    runtime.reconcile_input_requests()
    runtime.chat_store.input_requests.cancel_run.assert_called_once_with("terminal")

  def test_transient_delivery_failure_retries_same_saved_result(self):
    runtime = object.__new__(ChatRuntime)
    runtime.shutdown = False
    runtime.worker_registry = None
    runtime.runner = Mock()
    runtime.runner.answer_input.side_effect = [OSError("temporary failure"), None]
    runtime.chat_store = Mock()
    request = {"id": "f" * 32, "runId": "run_1", "result": {"requestId": "f" * 32, "status": "skipped"}}
    runtime.chat_store.input_requests.outbox.return_value = [request]
    runtime.chat_store.get_run.return_value = {"id": "run_1", "status": "running"}
    attempts = []
    def tick(_delay):
      attempts.append(True)
      if len(attempts) == 2:
        runtime.shutdown = True
    with patch("chat_input.time.sleep", side_effect=tick):
      runtime.input_delivery_loop()
    self.assertEqual(runtime.runner.answer_input.call_count, 2)
    self.assertEqual(runtime.runner.answer_input.call_args_list[0], runtime.runner.answer_input.call_args_list[1])
    runtime.chat_store.input_requests.delivered.assert_called_once_with(request["id"])


if __name__ == "__main__":
  unittest.main()
