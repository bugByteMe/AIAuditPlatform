from __future__ import annotations

from pathlib import Path

from sqlalchemy import JSON, Boolean, Column, Index, MetaData, String, Table, create_engine, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine


class AccountDatabase:
  """Row-addressable account persistence for SQLite and PostgreSQL."""

  def __init__(self, root: Path, database_url: str = ""):
    self.database_url = database_url or f"sqlite:///{(root / 'account.sqlite3').as_posix()}"
    connect_args = {"check_same_thread": False, "timeout": 30} if self.database_url.startswith("sqlite") else {}
    self.engine: Engine = create_engine(self.database_url, pool_pre_ping=True, connect_args=connect_args)
    self.metadata = MetaData()
    self.groups = Table(
      "account_groups", self.metadata,
      Column("id", String(160), primary_key=True),
      Column("name_key", String(255), nullable=False, unique=True),
      Column("payload", JSON, nullable=False),
    )
    self.users = Table(
      "account_users", self.metadata,
      Column("id", String(160), primary_key=True),
      Column("username", String(255), nullable=False),
      Column("username_key", String(255), nullable=False, unique=True),
      Column("group_id", String(160), nullable=False, default=""),
      Column("enabled", Boolean, nullable=False, default=True),
      Column("status", String(32), nullable=False, default="active"),
      Column("payload", JSON, nullable=False),
    )
    self.pending = Table(
      "account_invitations", self.metadata,
      Column("id", String(160), primary_key=True),
      Column("invite_token", String(255), nullable=False, unique=True),
      Column("group_id", String(160), nullable=False, default=""),
      Column("status", String(32), nullable=False, index=True),
      Column("payload", JSON, nullable=False),
    )
    self.payments = Table(
      "account_recharge_payments", self.metadata,
      Column("payment_key", String(255), primary_key=True),
      Column("user_id", String(160), nullable=False, default=""),
      Column("status", String(32), nullable=False, index=True),
      Column("payload", JSON, nullable=False),
    )
    self.orders = Table(
      "account_recharge_orders", self.metadata,
      Column("id", String(160), primary_key=True),
      Column("out_trade_no", String(255), nullable=False, unique=True),
      Column("user_id", String(160), nullable=False, default=""),
      Column("status", String(32), nullable=False, index=True),
      Column("payload", JSON, nullable=False),
    )
    Index("account_users_group", self.users.c.group_id)
    Index("account_payments_user", self.payments.c.user_id)
    Index("account_orders_user", self.orders.c.user_id)
    self.metadata.create_all(self.engine)

  @staticmethod
  def _upsert(connection, table, key_columns: list[str], values: dict) -> None:
    if connection.dialect.name == "postgresql":
      statement = pg_insert(table).values(**values)
      update_values = {key: value for key, value in values.items() if key not in key_columns}
      connection.execute(statement.on_conflict_do_update(index_elements=[table.c[key] for key in key_columns], set_=update_values))
      return
    predicate = [table.c[key] == values[key] for key in key_columns]
    existing = connection.execute(select(table).where(*predicate)).first()
    if existing:
      connection.execute(table.update().where(*predicate).values(**{key: value for key, value in values.items() if key not in key_columns}))
    else:
      connection.execute(table.insert().values(**values))

  def empty(self) -> bool:
    with self.engine.connect() as connection:
      return not any(connection.execute(select(func.count()).select_from(table)).scalar_one() for table in self.tables())

  def tables(self) -> tuple[Table, ...]:
    return self.groups, self.users, self.pending, self.payments, self.orders

  def load(self) -> dict:
    with self.engine.connect() as connection:
      return {
        "groups": {row.id: dict(row.payload) for row in connection.execute(select(self.groups))},
        "users": {row.username: dict(row.payload) for row in connection.execute(select(self.users))},
        "pendingAccounts": {row.id: dict(row.payload) for row in connection.execute(select(self.pending))},
        "rechargePayments": {row.payment_key: dict(row.payload) for row in connection.execute(select(self.payments))},
        "rechargeOrders": {row.id: dict(row.payload) for row in connection.execute(select(self.orders))},
      }

  def save_group(self, group: dict) -> None:
    values = {"id": str(group["id"]), "name_key": str(group.get("name") or "").casefold(), "payload": dict(group)}
    with self.engine.begin() as connection:
      self._upsert(connection, self.groups, ["id"], values)

  def save_user(self, user: dict) -> None:
    values = {
      "id": str(user["id"]), "username": str(user["username"]),
      "username_key": str(user["username"]).casefold(), "group_id": str(user.get("groupId") or ""),
      "enabled": bool(user.get("enabled", True)), "status": str(user.get("status") or "active"),
      "payload": dict(user),
    }
    with self.engine.begin() as connection:
      self._upsert(connection, self.users, ["id"], values)

  def save_pending(self, account: dict) -> None:
    values = {
      "id": str(account["id"]), "invite_token": str(account.get("inviteToken") or f"revoked:{account['id']}"),
      "group_id": str(account.get("groupId") or ""), "status": str(account.get("status") or "pending"),
      "payload": dict(account),
    }
    with self.engine.begin() as connection:
      self._upsert(connection, self.pending, ["id"], values)

  def save_payment(self, key: str, payment: dict) -> None:
    values = {"payment_key": key, "user_id": str(payment.get("userId") or ""), "status": str(payment.get("status") or ""), "payload": dict(payment)}
    with self.engine.begin() as connection:
      self._upsert(connection, self.payments, ["payment_key"], values)

  def save_order(self, order: dict) -> None:
    values = {
      "id": str(order["id"]), "out_trade_no": str(order.get("outTradeNo") or order["id"]),
      "user_id": str(order.get("userId") or ""), "status": str(order.get("status") or ""), "payload": dict(order),
    }
    with self.engine.begin() as connection:
      self._upsert(connection, self.orders, ["id"], values)

  def save_batch(self, group: dict | None, accounts: list[dict]) -> None:
    with self.engine.begin() as connection:
      if group:
        self._upsert(connection, self.groups, ["id"], {
          "id": str(group["id"]), "name_key": str(group.get("name") or "").casefold(), "payload": dict(group),
        })
      for account in accounts:
        self._upsert(connection, self.pending, ["id"], {
          "id": str(account["id"]), "invite_token": str(account["inviteToken"]),
          "group_id": str(account.get("groupId") or ""), "status": str(account.get("status") or "pending"),
          "payload": dict(account),
        })

  def activate(self, pending_id: str, user: dict) -> None:
    values = {
      "id": str(user["id"]), "username": str(user["username"]),
      "username_key": str(user["username"]).casefold(), "group_id": str(user.get("groupId") or ""),
      "enabled": bool(user.get("enabled", True)), "status": str(user.get("status") or "active"),
      "payload": dict(user),
    }
    with self.engine.begin() as connection:
      connection.execute(delete(self.pending).where(self.pending.c.id == pending_id))
      self._upsert(connection, self.users, ["id"], values)

  def save_payment_and_user(self, key: str, payment: dict, user: dict | None) -> None:
    with self.engine.begin() as connection:
      self._upsert(connection, self.payments, ["payment_key"], {
        "payment_key": key, "user_id": str(payment.get("userId") or ""),
        "status": str(payment.get("status") or ""), "payload": dict(payment),
      })
      if user:
        self._upsert(connection, self.users, ["id"], {
          "id": str(user["id"]), "username": str(user["username"]),
          "username_key": str(user["username"]).casefold(), "group_id": str(user.get("groupId") or ""),
          "enabled": bool(user.get("enabled", True)), "status": str(user.get("status") or "active"),
          "payload": dict(user),
        })

  def delete_rows(self, table: Table, column, values: set[str]) -> None:
    if not values:
      return
    with self.engine.begin() as connection:
      connection.execute(delete(table).where(column.in_(values)))

  def replace_all(self, state: dict) -> None:
    if not self.empty():
      raise ValueError("target account tables are not empty")
    with self.engine.begin() as connection:
      for group in state.get("groups", {}).values():
        connection.execute(self.groups.insert().values(id=group["id"], name_key=str(group.get("name") or "").casefold(), payload=group))
      for user in state.get("users", {}).values():
        connection.execute(self.users.insert().values(
          id=user["id"], username=user["username"], username_key=str(user["username"]).casefold(),
          group_id=str(user.get("groupId") or ""), enabled=bool(user.get("enabled", True)),
          status=str(user.get("status") or "active"), payload=user,
        ))
      for account in state.get("pendingAccounts", {}).values():
        connection.execute(self.pending.insert().values(
          id=account["id"], invite_token=str(account.get("inviteToken") or f"revoked:{account['id']}"),
          group_id=str(account.get("groupId") or ""), status=str(account.get("status") or "pending"), payload=account,
        ))
      for key, payment in state.get("rechargePayments", {}).items():
        connection.execute(self.payments.insert().values(payment_key=key, user_id=str(payment.get("userId") or ""), status=str(payment.get("status") or ""), payload=payment))
      for order in state.get("rechargeOrders", {}).values():
        connection.execute(self.orders.insert().values(
          id=order["id"], out_trade_no=str(order.get("outTradeNo") or order["id"]), user_id=str(order.get("userId") or ""),
          status=str(order.get("status") or ""), payload=order,
        ))

  def sync(self, state: dict) -> None:
    """Compatibility persistence for infrequent multi-row administration changes."""
    with self.engine.begin() as connection:
      sections = [
        (self.groups, "id", state.get("groups", {}), lambda key, item: {
          "id": key, "name_key": str(item.get("name") or "").casefold(), "payload": item,
        }),
        (self.users, "id", {str(item["id"]): item for item in state.get("users", {}).values()}, lambda key, item: {
          "id": key, "username": item["username"], "username_key": str(item["username"]).casefold(),
          "group_id": str(item.get("groupId") or ""), "enabled": bool(item.get("enabled", True)),
          "status": str(item.get("status") or "active"), "payload": item,
        }),
        (self.pending, "id", state.get("pendingAccounts", {}), lambda key, item: {
          "id": key, "invite_token": str(item.get("inviteToken") or f"revoked:{key}"),
          "group_id": str(item.get("groupId") or ""), "status": str(item.get("status") or "pending"), "payload": item,
        }),
        (self.payments, "payment_key", state.get("rechargePayments", {}), lambda key, item: {
          "payment_key": key, "user_id": str(item.get("userId") or ""), "status": str(item.get("status") or ""), "payload": item,
        }),
        (self.orders, "id", state.get("rechargeOrders", {}), lambda key, item: {
          "id": key, "out_trade_no": str(item.get("outTradeNo") or key), "user_id": str(item.get("userId") or ""),
          "status": str(item.get("status") or ""), "payload": item,
        }),
      ]
      for table, key_name, items, values_for in sections:
        incoming = set(items)
        existing = set(connection.execute(select(table.c[key_name])).scalars())
        if existing - incoming:
          connection.execute(delete(table).where(table.c[key_name].in_(existing - incoming)))
        for key, item in items.items():
          self._upsert(connection, table, [key_name], values_for(key, item))
