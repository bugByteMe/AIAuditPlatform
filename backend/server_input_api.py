from __future__ import annotations

from http import HTTPStatus
from urllib.parse import parse_qs


def handle_input_api(handler, runtime, method, workspace_id, request_id, query, user):
  params = parse_qs(query)
  session_id = (params.get("sessionId") or [""])[0]
  if method == "GET" and not request_id:
    handler.write_json({"requests": runtime.input_requests(workspace_id, session_id, user)})
  elif method == "POST" and request_id:
    request = runtime.answer_input(workspace_id, session_id, request_id, user, handler.read_json())
    handler.write_json({"request": request})
  elif method == "GET" and request_id and "image" in params:
    path, mime, name = runtime.input_image(workspace_id, session_id, request_id, int(params["image"][0]), user)
    handler.write_file(path, mime, name, "inline")
  else:
    handler.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
