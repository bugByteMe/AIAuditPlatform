#!/usr/bin/env python3
"""Generate a private, reviewable public-customer mapping without changing accounts."""
import argparse
import json
import os
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import JSON, Column, MetaData, String, Table, create_engine, select
from sqlalchemy.exc import SQLAlchemyError

from config import SETTINGS


def build_mapping(users, invitations):
  rows = []
  for user in sorted(users, key=lambda item: item["id"]):
    if user.get("customerId") not in (None, "", "public"):
      raise ValueError("An account already belongs to another customer; prepare a reviewed mapping manually")
    row = {"localUserId": user["id"], "customerId": "public"}
    if user.get("oidcSubject"):
      row["subject"] = user["oidcSubject"]
    rows.append(row)
  if not rows:
    raise ValueError("No local accounts found; verify database and storage configuration")
  pending = []
  for invitation in sorted(invitations, key=lambda item: item["id"]):
    if invitation.get("status") != "pending":
      continue
    if invitation.get("customerId") not in (None, "", "public"):
      raise ValueError("A pending invitation already belongs to another customer; prepare a reviewed mapping manually")
    pending.append({"invitationId": invitation["id"], "customerId": "public"})
  return {"users": rows, "invitations": pending}


def read_accounts(database_url, storage_dir):
  if not database_url:
    path = (Path(storage_dir) / "account.sqlite3").resolve()
    if not path.is_file():
      raise ValueError("Account database not found; configure the existing deployment database/storage")
    database_url = "sqlite:///file:" + quote(path.as_posix(), safe="/") + "?mode=ro&uri=true"
  engine = create_engine(database_url)
  try:
    metadata = MetaData()
    # Declare existing columns only: never initialize schemas or normalize accounts.
    users = Table("account_users", metadata, Column("id", String), Column("payload", JSON))
    invitations = Table("account_invitations", metadata, Column("id", String), Column("payload", JSON))
    with engine.connect() as connection:
      return (
        [dict(row.payload, id=row.id) for row in connection.execute(select(users))],
        [dict(row.payload, id=row.id) for row in connection.execute(select(invitations))],
      )
  finally:
    engine.dispose()


def write_mapping(path, mapping):
  # Exclusive creation also refuses symlinks; permission is private from creation.
  encoded = json.dumps(mapping, indent=2, ensure_ascii=False) + "\n"
  descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
  with os.fdopen(descriptor, "w", encoding="utf-8") as output:
    output.write(encoded)


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--output", type=Path, required=True)
  args = parser.parse_args()
  try:
    if os.environ.get("AI_AUDIT_DEPLOYMENT_CUSTOMER_ID", "").strip():
      raise ValueError("Public mapping is for the main platform, not a dedicated customer deployment")
    if os.path.lexists(args.output):
      raise ValueError("Output already exists; choose a new path to preserve reviewed mappings")
    users, invitations = read_accounts(SETTINGS.database_url, SETTINGS.workspace_storage_dir)
    mapping = build_mapping(users, invitations)
    write_mapping(args.output, mapping)
  except ValueError as exc:
    parser.exit(1, "Mapping not generated: " + str(exc) + "\n")
  except (OSError, SQLAlchemyError, KeyError, TypeError):
    # Do not print database URLs, payloads, credentials, or driver exception text.
    parser.exit(1, "Mapping not generated: check database configuration, schema, and output permissions.\n")
  print(json.dumps({"accounts": len(mapping["users"]), "invitations": len(mapping["invitations"])}))


if __name__ == "__main__":
  main()
