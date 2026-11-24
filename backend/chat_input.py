from __future__ import annotations

import hashlib
import re
import threading
import time

from chat_common import TERMINAL_STATES
from input_bridge import read_regular
from user_input_schema import MAX_IMAGE_BYTES
from workspace_store import StorageError


class ChatInputMixin:
  def input_access(self, workspace_id, session_id, user):
    self.store.get_workspace(workspace_id, user)
    session = self.chat_store.get_session(session_id)
    if not session or session["workspaceId"] != workspace_id:
      raise StorageError("not_found", "chat session not found")

  def input_requests(self, workspace_id, session_id, user):
    self.input_access(workspace_id, session_id, user)
    return [self.chat_store.input_requests.public(item) for item in self.chat_store.input_requests.list(session_id)]

  def require_input(self, workspace_id, session_id, request_id, user):
    self.input_access(workspace_id, session_id, user)
    request = self.chat_store.input_requests.get(request_id)
    if not request or request["sessionId"] != session_id or request["workspaceId"] != workspace_id:
      raise StorageError("not_found", "input request not found")
    return request

  def answer_input(self, workspace_id, session_id, request_id, user, answer):
    self.require_input(workspace_id, session_id, request_id, user)
    with self.lifecycle_locks.hold(f"run:{self.chat_store.input_requests.get(request_id)['runId']}"):
      request, event = self.chat_store.input_requests.resolve(request_id, user["username"], answer)
    if event:
      self.notify_input_event(session_id, event)
    return self.chat_store.input_requests.public(request)

  def input_image(self, workspace_id, session_id, request_id, index, user):
    request = self.require_input(workspace_id, session_id, request_id, user)
    images = request.get("images", [])
    if index < 0 or index >= len(images):
      raise StorageError("not_found", "input image not found")
    image = images[index]
    return self.store.blob_path(image["blob"]), image["contentType"], image["name"]

  def validate_input_event(self, event):
    incoming = event.get("inputRequest")
    if not isinstance(incoming, dict) or not re.fullmatch(r"[a-f0-9]{32}", str(incoming.get("id", ""))):
      raise ValueError("invalid input request")
    images = incoming.get("images", [])
    if not isinstance(images, list) or len(images) > 4:
      raise ValueError("invalid input images")
    for image in images:
      digest = image.get("blob", "")
      if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("invalid input image digest")
      if image.get("contentType") not in {"image/png", "image/jpeg", "image/webp"}:
        raise ValueError("invalid input image type")
      data = read_regular(self.store.blob_path(digest), MAX_IMAGE_BYTES)
      if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("input image checksum mismatch")

  def notify_input_event(self, session_id, event):
    if self.event_callback:
      try:
        self.event_callback(session_id, event)
      except Exception:
        pass
    with self.condition:
      self.condition.notify_all()

  def input_delivery_loop(self):
    while not self.shutdown:
      try:
        requests = self.chat_store.input_requests.outbox()
        for request in requests:
          run = self.chat_store.get_run(request["runId"])
          if not run or run["status"] in TERMINAL_STATES or run["status"] == "stopping":
            self.chat_store.input_requests.delivered(request["id"])
            continue
          try:
            if run.get("workerId") and self.worker_registry:
              self.worker_registry.client(run["workerId"]).answer_input(run["id"], request["id"], request["result"])
            else:
              self.runner.answer_input(run, request["id"], request["result"])
            self.chat_store.input_requests.delivered(request["id"])
          except Exception:
            # The durable outbox is retried independently of the browser.
            continue
      except Exception:
        pass
      time.sleep(0.5)

  def start_input_delivery(self):
    threading.Thread(target=self.input_delivery_loop, name="ai-audit-input-delivery", daemon=True).start()

  def reconcile_input_requests(self):
    # Repair a crash between saving terminal run state and publishing its
    # cancellation events. Live remote runs retain their pending questions.
    for run_id in self.chat_store.input_requests.pending_run_ids():
      run = self.chat_store.get_run(run_id)
      if not run or run["status"] in TERMINAL_STATES or run["status"] == "stopping":
        for event in self.chat_store.input_requests.cancel_run(run_id):
          request = event["inputRequest"]
          self.notify_input_event(request["sessionId"], event)
