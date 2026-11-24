from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock

from server_input_api import handle_input_api
from workspace_store import StorageError


class InputApiTest(unittest.TestCase):
  def setUp(self):
    self.handler = Mock()
    self.runtime = Mock()
    self.user = {"username": "owner"}

  def call(self, method, request_id="", query="sessionId=chat-1"):
    handle_input_api(self.handler, self.runtime, method, "workspace-1", request_id, query, self.user)

  def test_list_preserves_session_and_actor(self):
    self.runtime.input_requests.return_value = [{"id": "input-1"}]
    self.call("GET")
    self.runtime.input_requests.assert_called_once_with("workspace-1", "chat-1", self.user)
    self.handler.write_json.assert_called_once_with({"requests": [{"id": "input-1"}]})

  def test_submit_passes_answer_to_authorized_runtime(self):
    self.handler.read_json.return_value = {"status": "answered", "value": False}
    self.runtime.answer_input.return_value = {"status": "answered"}
    self.call("POST", "input-1")
    self.runtime.answer_input.assert_called_once_with("workspace-1", "chat-1", "input-1", self.user, {"status": "answered", "value": False})
    self.handler.write_json.assert_called_once_with({"request": {"status": "answered"}})

  def test_image_uses_only_the_runtime_authorized_blob(self):
    self.runtime.input_image.return_value = (Path("/test/blob"), "image/png", "question.png")
    self.call("GET", "input-1", "sessionId=chat-1&image=0")
    self.runtime.input_image.assert_called_once_with("workspace-1", "chat-1", "input-1", 0, self.user)
    self.handler.write_file.assert_called_once_with(Path("/test/blob"), "image/png", "question.png", "inline")

  def test_authorization_failure_cannot_write_an_answer_or_image(self):
    self.runtime.answer_input.side_effect = StorageError("forbidden", "not the initiator")
    with self.assertRaises(StorageError):
      self.call("POST", "input-1")
    self.handler.write_json.assert_not_called()
    self.handler.write_file.assert_not_called()


if __name__ == "__main__":
  unittest.main()
