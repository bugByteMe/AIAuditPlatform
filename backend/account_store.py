from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path


class AccountStore:
  def __init__(self, path: Path, seed_users: dict[str, dict]):
    self.path = path
    self.seed_users = deepcopy(seed_users)
    self.users = self.load_or_seed()

  def load_or_seed(self) -> dict[str, dict]:
    self.path.parent.mkdir(parents=True, exist_ok=True)
    if self.path.exists():
      return json.loads(self.path.read_text(encoding="utf-8"))
    self.save(self.seed_users)
    return deepcopy(self.seed_users)

  def save(self, users: dict[str, dict] | None = None) -> None:
    users = users or self.users
    self.path.parent.mkdir(parents=True, exist_ok=True)
    tmp = self.path.with_suffix(".tmp")
    tmp.write_text(json.dumps(users, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(self.path)
    os.chmod(self.path, 0o600)
