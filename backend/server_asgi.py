from __future__ import annotations

import asyncio
import io
import json
import logging
import threading
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from config import SETTINGS
from server_api import Handler
from server_state import CHAT_RUNTIME, FRONTEND_DIR, UPLOAD_MANAGER, WECHAT_RECONCILE_STOP, reconcile_micu_accounts
from server_recharge import reconcile_wechat_orders
from server_transport import RequestStopped, SSE_BROKER
from server_utils import ascii_download_filename, static_content_type
from server_workloads import WorkloadBusy, WorkloadPools, classify_request
from workspace_store import StorageError, parse_query


LOGGER = logging.getLogger("ai_audit.workloads")
WORKLOAD_POOLS: WorkloadPools | None = None


def workload_pools() -> WorkloadPools:
  global WORKLOAD_POOLS
  if WORKLOAD_POOLS is None:
    WORKLOAD_POOLS = WorkloadPools(SETTINGS)
  return WORKLOAD_POOLS


def cors_headers(request: Request) -> dict[str, str]:
  origin = request.headers.get("origin")
  if not origin:
    return {}
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Credentials": "true",
    "Vary": "Origin",
  }


class AsgiHandler(Handler):
  """Compatibility adapter from the existing application methods to ASGI."""

  def __init__(self, request: Request, body=None):
    self.request = request
    self.path = str(request.url)
    self.headers = request.headers
    self.rfile = body if body is not None else io.BytesIO()
    self.response: Response | None = None

  def write_binary(
    self,
    body: bytes,
    content_type: str,
    status: HTTPStatus = HTTPStatus.OK,
    headers: dict | None = None,
    no_store: bool = False,
  ) -> None:
    response_headers = cors_headers(self.request)
    response_headers["Content-Type"] = content_type
    if no_store:
      response_headers["Cache-Control"] = "no-store"
    response_headers.update(headers or {})
    self.response = Response(content=body, status_code=int(status), headers=response_headers)

  def write_file(
    self, file_path: Path, content_type: str, filename: str, disposition: str, *, delete_after: bool = False,
  ) -> None:
    ascii_filename = ascii_download_filename(filename)
    headers = {
      **cors_headers(self.request),
      "Content-Disposition": f"{disposition}; filename=\"{ascii_filename}\"; filename*=UTF-8''{quote(filename)}",
      "X-Content-Type-Options": "nosniff",
      "Cache-Control": "no-store",
      "Connection": "close",
    }
    background = BackgroundTask(file_path.unlink, missing_ok=True) if delete_after else None
    self.response = FileResponse(file_path, media_type=content_type, headers=headers, background=background)

  def serve_static(self, request_path: str) -> None:
    relative = unquote(request_path.lstrip("/")) or "index.html"
    candidate = (FRONTEND_DIR / relative).resolve()
    try:
      candidate.relative_to(FRONTEND_DIR.resolve())
    except ValueError:
      candidate = FRONTEND_DIR / "index.html"
    if not candidate.is_file():
      candidate = FRONTEND_DIR / "index.html"
    self.response = FileResponse(
      candidate,
      media_type=static_content_type(candidate),
      headers={"Cache-Control": "no-store"},
    )


class AsyncRequestReader:
  """Expose an ASGI request stream as the bounded blocking reader uploads expect."""

  _EOF = object()

  def __init__(self, loop: asyncio.AbstractEventLoop, max_chunks: int = 2):
    self.loop = loop
    self.queue: asyncio.Queue = asyncio.Queue(maxsize=max_chunks)
    self.remainder = b""

  async def feed(self, block: bytes) -> None:
    if block:
      await self.queue.put(block)

  async def finish(self, error: BaseException | None = None) -> None:
    await self.queue.put(error if error is not None else self._EOF)

  def read(self, size: int = -1) -> bytes:
    if self.remainder:
      block, self.remainder = self.remainder, b""
    else:
      item = asyncio.run_coroutine_threadsafe(self.queue.get(), self.loop).result()
      if item is self._EOF:
        return b""
      if isinstance(item, BaseException):
        raise item
      block = item
    if size >= 0 and len(block) > size:
      block, self.remainder = block[:size], block[size:]
    return block


def dispatch_request(request: Request, body) -> Response:
  handler = AsgiHandler(request, body)
  parsed = urlparse(handler.path)
  if request.method == "GET" and not parsed.path.startswith("/api/"):
    handler.serve_static(parsed.path)
  else:
    handler.handle_api(request.method, parsed.path, parsed.query)
  return handler.response or Response(status_code=HTTPStatus.NO_CONTENT)


async def dispatch_streaming_upload(request: Request) -> Response:
  loop = asyncio.get_running_loop()
  reader = AsyncRequestReader(loop)
  dispatch_task = asyncio.create_task(workload_pools().upload.run(dispatch_request, request, reader))

  async def pump() -> None:
    try:
      async for block in request.stream():
        if dispatch_task.done():
          return
        await reader.feed(block)
      await reader.finish()
    except BaseException as exc:
      await reader.finish(exc)

  pump_task = asyncio.create_task(pump())
  try:
    done, _ = await asyncio.wait({dispatch_task, pump_task}, return_when=asyncio.FIRST_COMPLETED)
    if pump_task in done:
      await pump_task
    return await dispatch_task
  finally:
    if not pump_task.done():
      pump_task.cancel()
    if not dispatch_task.done():
      dispatch_task.cancel()


def is_chunk_upload(method: str, path: str) -> bool:
  parts = [part for part in path.split("/") if part]
  return method == "PUT" and len(parts) == 5 and parts[:2] == ["api", "uploads"] and parts[3] == "files"


async def sse_response(request: Request) -> Response:
  handler = AsgiHandler(request)
  parsed = urlparse(handler.path)
  try:
    user = handler.require_user()
    params = parse_query(parsed.query)
    session_id = (params.get("sessionId") or [""])[0]
    after = int((params.get("after") or ["0"])[0] or 0)
    header_last_id = int(request.headers.get("Last-Event-ID") or 0)
    last_id = max(after, header_last_id)
    await run_in_threadpool(CHAT_RUNTIME.events, parsed.path.split("/")[3], session_id, last_id, user)
  except RequestStopped:
    return handler.response or Response(status_code=HTTPStatus.UNAUTHORIZED)
  except StorageError as exc:
    handler.write_json({"error": exc.code, "message": exc.message}, handler.status_for_storage_error(exc))
    return handler.response
  except ValueError as exc:
    handler.write_json({"error": "bad_request", "message": str(exc)}, HTTPStatus.BAD_REQUEST)
    return handler.response

  async def stream():
    nonlocal last_id
    yield f"retry: {SETTINGS.sse_retry_ms}\n\n".encode("utf-8")
    idle_rounds = 0
    while idle_rounds < SETTINGS.sse_max_idle_rounds:
      signal = SSE_BROKER.prepare(session_id)
      events = await run_in_threadpool(CHAT_RUNTIME.events, parsed.path.split("/")[3], session_id, last_id, user)
      if not events:
        try:
          await asyncio.wait_for(signal.wait(), timeout=SETTINGS.sse_wait_timeout_seconds)
          continue
        except asyncio.TimeoutError:
          public_session = await run_in_threadpool(CHAT_RUNTIME.public_session, session_id, False)
          heartbeat = {
            "sessionId": session_id,
            "latestEventId": last_id,
            "sessionStatus": public_session.get("status"),
          }
          yield f"event: heartbeat\ndata: {json.dumps(heartbeat, ensure_ascii=False)}\n\n".encode("utf-8")
          if public_session.get("status") in {"completed", "stopped", "failed"}:
            return
          idle_rounds += 1
          yield b": keepalive\n\n"
          continue
      idle_rounds = 0
      for event in events:
        last_id = int(event["id"])
        payload = json.dumps(event, ensure_ascii=False)
        yield f"id: {last_id}\nevent: {event['type']}\ndata: {payload}\n\n".encode("utf-8")
      if events[-1]["type"] in {"completed", "stopped", "failed"}:
        return

  headers = {
    **cors_headers(request),
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-store",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
  }
  return StreamingResponse(stream(), headers=headers)


async def app_lifespan(_app: FastAPI):
  global WORKLOAD_POOLS
  WORKLOAD_POOLS = WorkloadPools(SETTINGS)
  SSE_BROKER.bind(asyncio.get_running_loop())
  CHAT_RUNTIME.set_event_callback(SSE_BROKER.persisted)
  threading.Thread(target=reconcile_micu_accounts, name="micu-account-reconciler", daemon=True).start()
  WECHAT_RECONCILE_STOP.clear()
  threading.Thread(target=reconcile_wechat_orders, name="wechat-pay-reconciler", daemon=True).start()
  try:
    yield
  finally:
    WECHAT_RECONCILE_STOP.set()
    UPLOAD_MANAGER.shutdown()
    pools, WORKLOAD_POOLS = WORKLOAD_POOLS, None
    if pools is not None:
      pools.shutdown()
    CHAT_RUNTIME.set_event_callback(None)
    SSE_BROKER.unbind()


app = FastAPI(lifespan=app_lifespan)


@app.options("/{request_path:path}")
async def options_route(request: Request, request_path: str) -> Response:
  return Response(
    status_code=HTTPStatus.NO_CONTENT,
    headers={
      "Access-Control-Allow-Origin": request.headers.get("origin", "*"),
      "Access-Control-Allow-Credentials": "true",
      "Access-Control-Allow-Headers": "Content-Type, Authorization",
      "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
    },
  )


@app.api_route("/{request_path:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"])
async def application_route(request: Request, request_path: str) -> Response:
  path = "/" + request_path
  if request.method == "GET" and path.startswith("/api/workspaces/") and path.endswith("/chat/stream"):
    return await sse_response(request)
  workload, route_label = classify_request(request.method, path)
  try:
    if is_chunk_upload(request.method, path):
      return await dispatch_streaming_upload(request)
    body = await request.body()
    if workload:
      return await workload_pools().pool(workload).run(dispatch_request, request, io.BytesIO(body))
    return await run_in_threadpool(dispatch_request, request, io.BytesIO(body))
  except ClientDisconnect:
    return Response(status_code=499)
  except WorkloadBusy as exc:
    LOGGER.warning("workload_rejected workload=%s route=%s", exc.workload, route_label)
    return Response(
      content=json.dumps({"error": "server_busy", "message": "server is busy; retry shortly"}),
      status_code=HTTPStatus.SERVICE_UNAVAILABLE,
      headers={**cors_headers(request), "Content-Type": "application/json; charset=utf-8", "Retry-After": "2", "Cache-Control": "no-store"},
    )
