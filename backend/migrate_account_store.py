from __future__ import annotations

import argparse
import json
import os
from copy import deepcopy
from pathlib import Path

from account_database import AccountDatabase
from account_store import AccountStore, SCHEMA_VERSION


def normalized_state(path: Path) -> dict:
  if not path.is_file():
    raise SystemExit(f"account JSON not found: {path}")
  raw = json.loads(path.read_text(encoding="utf-8"))
  helper = AccountStore.__new__(AccountStore)
  helper.users, helper.pending_accounts, helper.groups = {}, {}, {}
  helper.recharge_payments, helper.recharge_orders = {}, {}
  if isinstance(raw, dict) and raw.get("schemaVersion") in {2, 3, 4, 5, 6}:
    state = helper.migrate_versioned(raw)
  elif isinstance(raw, dict) and raw.get("schemaVersion") == SCHEMA_VERSION:
    state = deepcopy(raw)
  else:
    state = helper.migrate_legacy(raw)
  helper.users.update(deepcopy(state.get("users") or {}))
  helper.pending_accounts.update(deepcopy(state.get("pendingAccounts") or {}))
  helper.groups.update(deepcopy(state.get("groups") or {}))
  helper.recharge_payments.update(deepcopy(state.get("rechargePayments") or {}))
  helper.recharge_orders.update(deepcopy(state.get("rechargeOrders") or {}))
  helper._normalize_provider_state()
  return helper.state()


def main() -> None:
  parser = argparse.ArgumentParser(description="Import accounts.json into SQL account tables")
  parser.add_argument("--storage-root", required=True, type=Path)
  parser.add_argument("--database-url", default=os.environ.get("AI_AUDIT_DATABASE_URL", ""))
  args = parser.parse_args()
  if not args.database_url:
    raise SystemExit("AI_AUDIT_DATABASE_URL or --database-url is required")
  source = normalized_state(args.storage_root / "accounts.json")
  database = AccountDatabase(args.storage_root, args.database_url)
  database.replace_all(source)
  target = database.load()
  counts = {key: len(source.get(key) or {}) for key in ["users", "pendingAccounts", "groups", "rechargePayments", "rechargeOrders"]}
  target_counts = {key: len(target.get(key) or {}) for key in counts}
  report = {"source": counts, "target": target_counts, "contentVerified": all(source.get(key, {}) == target.get(key, {}) for key in counts)}
  print(json.dumps(report, ensure_ascii=False, sort_keys=True))
  if counts != target_counts or not report["contentVerified"]:
    raise SystemExit("account migration validation failed")


if __name__ == "__main__":
  main()
