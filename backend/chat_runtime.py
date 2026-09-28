from __future__ import annotations

import queue
import threading
from pathlib import Path

from chat_common import KeyedLockPool, RUNNING_STATES, TERMINAL_STATES
from chat_execution import ChatExecutionMixin
from chat_sessions import ChatSessionMixin
from chat_store import ChatStore
from codex_runner import CodexRunner, DockerCodexRunner, RunnerError, safe_segment, toml_string
from compute_nodes import WorkerRegistry
from config import SETTINGS, parse_memory_bytes
from workspace_store import WorkspaceStore


class ChatRuntime(ChatSessionMixin, ChatExecutionMixin):
  def __init__(
    self,
    store: WorkspaceStore,
    users: dict[str, dict],
    runner: CodexRunner | None = None,
    capacity: int = 1,
    chat_store: ChatStore | None = None,
    save_users=None,
    worker_registry: WorkerRegistry | None = None,
    groups: dict[str, dict] | None = None,
    budget_checker=None,
  ):
    self.store = store
    self.users = users
    self.groups = groups if groups is not None else {}
    self.runner = runner or DockerCodexRunner()
    self.chat_store = chat_store or ChatStore(store.root / "chat", SETTINGS.database_url)
    for username, user in self.users.items():
      user["usedTokens"] = self.chat_store.ensure_user_usage(username, int(user.get("usedTokens") or 0))
    self.save_users = save_users
    self.budget_checker = budget_checker
    self.capacity = max(1, capacity)
    self.requested_cpu = float(SETTINGS.run_cpus)
    self.requested_memory_bytes = parse_memory_bytes(SETTINGS.run_memory)
    self.worker_registry = worker_registry or (WorkerRegistry(SETTINGS) if SETTINGS.compute_nodes else None)
    self.codex_preparer = self.runner if isinstance(self.runner, DockerCodexRunner) else DockerCodexRunner()
    if runner is not None and not isinstance(runner, DockerCodexRunner):
      self.codex_preparer.codex_root = store.root / "codex"
      self.codex_preparer.codex_home_root = store.root / "codex" / "homes"
    self.scheduler_poll_seconds = max(0.01, SETTINGS.scheduler_poll_seconds)
    self.lock = threading.RLock()
    self.condition = threading.Condition(self.lock)
    self.lifecycle_locks = KeyedLockPool()
    self.queue: queue.Queue[str] = queue.Queue()
    self.active_runs: set[str] = set()
    self.stop_requested: set[str] = set()
    self.event_callback = None
    self.shutdown = False
    self.store.set_chat_session_provider(self.public_workspace_sessions)
    self.recover_persisted_runs()
    self.scheduler = threading.Thread(target=self.scheduler_loop, name="ai-audit-chat-scheduler", daemon=True)
    self.scheduler.start()
