from __future__ import annotations

import io
import json
import secrets
import time
from decimal import Decimal
from http import HTTPStatus
from urllib.parse import unquote, urlparse

from micu_api import MicuApiError, parse_cny
from recharge_import import MAX_WORKBOOK_BYTES, parse_recharge_workbook, payment_key
from server_recharge import apply_wechat_transaction, public_recharge_order, reconcile_wechat_order
from server_state import *
from server_utils import hash_password, invite_digest, utc_timestamp, verify_password
from wechat_pay import WechatPayError
from workspace_store import StorageError, parse_query, parse_urlencoded_paths

class WorkspaceHandlerMixin:
  def list_workspaces(self) -> None:
    user = self.require_user()
    self.write_json({"workspaces": WORKSPACE_STORE.list_workspaces(user)})

  def upload_api(self, method: str, path: str, query: str) -> None:
    user = self.require_user()
    parts = [unquote(part) for part in path.split("/") if part]
    if method == "POST" and len(parts) == 2:
      upload = UPLOAD_MANAGER.create(user, self.read_json())
      add_audit(user["username"], "workspace upload started", upload["id"])
      self.write_json({"upload": upload}, HTTPStatus.CREATED)
      return
    if len(parts) < 3:
      self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
      return
    upload_id = parts[2]
    action = parts[3] if len(parts) > 3 else ""
    if method == "GET" and not action:
      self.write_json({"upload": UPLOAD_MANAGER.status(upload_id, user)})
    elif method == "POST" and action == "complete":
      result = UPLOAD_MANAGER.complete(upload_id, user)
      add_audit(user["username"], "workspace upload finalization requested", upload_id)
      self.write_json({"upload": result}, HTTPStatus.ACCEPTED)
    elif method == "PUT" and action == "files" and len(parts) == 5:
      length = int(self.headers.get("Content-Length") or -1)
      offset = int((parse_query(query).get("offset") or ["0"])[0])
      result = UPLOAD_MANAGER.receive_chunk(upload_id, user, int(parts[4]), offset, self.rfile, length)
      self.write_json({"upload": result})
    elif method == "DELETE" and not action:
      result = UPLOAD_MANAGER.cancel(upload_id, user)
      add_audit(user["username"], "workspace upload cancelled", upload_id)
      self.write_json({"upload": result})
    else:
      self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

  def workspace_api(self, method: str, path: str, query: str) -> None:
    user = self.require_user()
    parts = [unquote(part) for part in path.split("/") if part]
    if len(parts) < 3:
      self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
      return
    workspace_id = parts[2]
    action = parts[3] if len(parts) > 3 else ""
    subaction = parts[4] if len(parts) > 4 else ""
    tail = parts[5] if len(parts) > 5 else ""
    subtail = parts[6] if len(parts) > 6 else ""
    if action == "chat":
      self.chat_api(method, workspace_id, subaction, tail, query, subtail)
      return
    if method == "GET" and not action:
      workspace = WORKSPACE_STORE.get_workspace(workspace_id, user)
      self.write_json({"workspace": WORKSPACE_STORE.public_workspace(workspace)})
    elif method == "PATCH" and not action:
      workspace = WORKSPACE_STORE.update_workspace(workspace_id, user, self.read_json())
      add_audit(user["username"], "workspace updated", workspace_id)
      self.write_json({"workspace": workspace})
    elif method == "DELETE" and not action:
      deleted = WORKSPACE_STORE.delete_workspace(workspace_id, user)
      add_audit(user["username"], "workspace deleted", workspace_id)
      self.write_json({"deleted": deleted})
    elif method == "POST" and action == "fork":
      payload = self.read_json()
      workspace = WORKSPACE_STORE.fork_workspace(workspace_id, user, str(payload.get("name") or "") or None)
      add_audit(user["username"], "workspace forked", f"{workspace_id} -> {workspace['id']}")
      self.write_json({"workspace": workspace}, HTTPStatus.CREATED)
    elif method == "GET" and action == "files" and not subaction:
      WORKSPACE_STORE.get_workspace(workspace_id, user)
      params = parse_query(query)
      parent = (params.get("path") or [""])[0]
      try:
        depth = int((params.get("depth") or ["3"])[0])
      except ValueError as exc:
        raise StorageError("bad_request", "file tree depth must be an integer") from exc
      self.write_json({"files": WORKSPACE_STORE.file_tree(workspace_id, parent=parent, depth=depth), "path": parent, "depth": depth})
    elif method == "POST" and action == "files" and not subaction:
      raise StorageError("upload_sessions_required", "use /api/uploads for workspace file uploads")
    elif method == "DELETE" and action == "files" and not subaction:
      params = parse_query(query)
      path_to_delete = (params.get("path") or [""])[0]
      workspace = WORKSPACE_STORE.delete_workspace_path(workspace_id, user, path_to_delete)
      add_audit(user["username"], "workspace path deleted", f"{workspace_id} {path_to_delete}")
      self.write_json({"workspace": workspace, "deletedPath": path_to_delete})
    elif method == "GET" and action == "files" and subaction == "preview":
      WORKSPACE_STORE.get_workspace(workspace_id, user)
      params = parse_query(query)
      self.write_json({"preview": WORKSPACE_STORE.preview_metadata(workspace_id, (params.get("path") or [""])[0])})
    elif method == "GET" and action == "files" and subaction == "raw":
      WORKSPACE_STORE.get_workspace(workspace_id, user)
      params = parse_query(query)
      path = (params.get("path") or [""])[0]
      file_path = WORKSPACE_STORE.workspace_file_path(workspace_id, path)
      metadata = WORKSPACE_STORE.file_metadata(workspace_id, path)
      disposition = "attachment" if (params.get("download") or [""])[0] in {"1", "true", "yes"} else "inline"
      self.write_file(file_path, metadata["contentType"], metadata["name"], disposition)
    elif method == "GET" and action == "files" and subaction == "rendered":
      WORKSPACE_STORE.get_workspace(workspace_id, user)
      params = parse_query(query)
      path = (params.get("path") or [""])[0]
      file_path = WORKSPACE_STORE.rendered_preview_path(workspace_id, path)
      self.write_file(file_path, "application/pdf", f"{Path(path).stem or 'preview'}.pdf", "inline")
    elif method == "GET" and action == "artifacts":
      WORKSPACE_STORE.get_workspace(workspace_id, user)
      self.write_json({"artifacts": WORKSPACE_STORE.workspace_artifacts(workspace_id)})
    elif method == "POST" and action == "refresh-artifacts":
      artifacts = WORKSPACE_STORE.refresh_artifacts(workspace_id, user)
      add_audit(user["username"], "workspace artifacts refreshed", workspace_id)
      self.write_json({"artifacts": artifacts})
    elif method == "GET" and action == "download":
      params = parse_query(query)
      filename, body = WORKSPACE_STORE.build_download_zip(
        workspace_id,
        user,
        (params.get("mode") or ["changes"])[0],
        parse_urlencoded_paths(params.get("paths", [])),
      )
      add_audit(user["username"], "workspace downloaded", f"{workspace_id} {filename}")
      ascii_filename = ascii_download_filename(filename, "workspace.zip")
      self.write_binary(
        body,
        "application/zip",
        headers={
          "Content-Disposition": f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{quote(filename)}",
          "Content-Transfer-Encoding": "binary",
          "Connection": "close",
        },
      )
    else:
      self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

  def chat_api(self, method: str, workspace_id: str, action: str, tail: str, query: str, subtail: str = "") -> None:
    user = self.require_user()
    if method == "GET" and action == "sessions" and not tail:
      self.write_json({"sessions": CHAT_RUNTIME.list_sessions(workspace_id, user)})
    elif method == "POST" and action == "sessions" and not tail:
      payload = self.read_json()
      session = CHAT_RUNTIME.create_session(workspace_id, user, str(payload.get("title") or "") or None)
      add_audit(user["username"], "chat session created", f"{workspace_id} {session['id']}")
      self.write_json({"session": session}, HTTPStatus.CREATED)
    elif method == "PATCH" and action == "sessions" and tail:
      payload = self.read_json()
      session = CHAT_RUNTIME.update_session(workspace_id, tail, user, str(payload.get("title") or ""))
      add_audit(user["username"], "chat session renamed", f"{workspace_id} {session['id']}")
      self.write_json({"session": session})
    elif method == "DELETE" and action == "sessions" and tail and not subtail:
      deleted = CHAT_RUNTIME.delete_session(workspace_id, tail, user)
      add_audit(user["username"], "chat session deleted", f"{workspace_id} {tail} runs={len(deleted['runIds'])}")
      self.write_json({"deleted": deleted})
    elif method == "POST" and action == "sessions" and tail and subtail == "fork":
      payload = self.read_json()
      session = CHAT_RUNTIME.fork_session(workspace_id, tail, user, str(payload.get("title") or "") or None)
      add_audit(user["username"], "chat session forked", f"{workspace_id} {tail} -> {session['id']}")
      self.write_json({"session": session}, HTTPStatus.CREATED)
    elif method == "POST" and action == "runs" and not tail:
      result = CHAT_RUNTIME.start_run(workspace_id, user, self.read_json())
      add_audit(user["username"], "chat run queued", f"{workspace_id} {result['run']['id']}")
      self.write_json(result, HTTPStatus.CREATED)
    elif method == "POST" and action == "runs" and tail:
      parts = [unquote(part) for part in urlparse(self.path).path.split("/") if part]
      if len(parts) == 7 and parts[6] == "stop":
        result = CHAT_RUNTIME.stop_run(workspace_id, parts[5], user)
        add_audit(user["username"], "chat run stop requested", f"{workspace_id} {parts[5]}")
        self.write_json(result)
      else:
        self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
    elif method == "GET" and action == "events" and not tail:
      params = parse_query(query)
      session_id = (params.get("sessionId") or [""])[0]
      after = int((params.get("after") or ["0"])[0] or 0)
      before_value = (params.get("before") or [""])[0]
      before = int(before_value) if before_value else None
      if before is not None and "after" in params:
        raise ValueError("before and after cannot be used together")
      limit = min(500, max(1, int((params.get("limit") or ["200"])[0] or 200)))
      latest = (params.get("latest") or [""])[0].lower() in {"1", "true", "yes"}
      if before is not None or latest or "limit" in params:
        events = CHAT_RUNTIME.events(workspace_id, session_id, after, user, before=before, limit=limit, latest=latest)
      else:
        events = CHAT_RUNTIME.events(workspace_id, session_id, after, user)
      public_session = CHAT_RUNTIME.public_session(session_id, include_events=False)
      latest_event_id = int(events[-1]["id"]) if events else after
      self.write_json({
        "events": events, "sessionStatus": public_session.get("status"), "session": public_session,
        "latestEventId": latest_event_id, "hasMore": len(events) == limit,
      })
    elif method == "GET" and action == "stream" and not tail:
      params = parse_query(query)
      session_id = (params.get("sessionId") or [""])[0]
      after = int((params.get("after") or ["0"])[0] or 0)
      self.write_sse(workspace_id, session_id, after, user)
    else:
      self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

