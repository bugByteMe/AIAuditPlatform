"""Durable central provisioning journal and local identity uniqueness."""
import hashlib
import fcntl
from pathlib import Path
from contextlib import contextmanager

from sqlalchemy import Column, JSON, MetaData, String, Table, select, text

from oidc_client import IdentityError


class IdentityDatabase:
  def __init__(self, engine):
    self.engine = engine
    metadata = MetaData()
    self.customers = Table("identity_customers", metadata,
      Column("id", String(160), primary_key=True), Column("payload", JSON, nullable=False))
    self.operations = Table("identity_operations", metadata,
      Column("id", String(255), primary_key=True), Column("payload", JSON, nullable=False))
    self.bindings = Table("identity_bindings", metadata,
      Column("subject", String(255), primary_key=True),
      Column("username_key", String(255), nullable=False, unique=True),
      Column("payload", JSON, nullable=False))
    self.links = Table("identity_local_links", metadata,
      Column("issuer", String(512), primary_key=True), Column("subject", String(255), primary_key=True),
      Column("user_id", String(160), nullable=False, unique=True))
    metadata.create_all(engine)

  def get(self, table, key):
    column = list(table.primary_key.columns)[0]
    with self.engine.connect() as connection:
      return connection.execute(select(table.c.payload).where(column == key)).scalar_one_or_none()

  def put(self, table, key, payload, **extra):
    from account_database import AccountDatabase
    column = list(table.primary_key.columns)[0].name
    with self.engine.begin() as connection:
      AccountDatabase._upsert(connection, table, [column], {column: key, "payload": payload, **extra})

  @contextmanager
  def exclusive(self, key):
    # Session/file locks survive journal commits but release on process death.
    digest = hashlib.sha256(key.encode()).digest()
    if self.engine.dialect.name == "postgresql":
      number = int.from_bytes(digest[:8], "big", signed=True)
      with self.engine.connect() as connection:
        if not connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": number}).scalar():
          raise IdentityError("Identity operation busy; retry")
        try:
          yield
        finally:
          connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": number})
      return
    root = Path(self.engine.url.database).parent / "identity-locks"
    root.mkdir(mode=0o700, exist_ok=True)
    with (root / digest.hex()).open("a") as handle:
      try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
      except BlockingIOError:
        raise IdentityError("Identity operation busy; retry") from None
      try:
        yield
      finally:
        fcntl.flock(handle, fcntl.LOCK_UN)

  def link(self, issuer, subject, user_id):
    with self.engine.begin() as connection:
      existing = connection.execute(select(self.links.c.user_id).where(
        self.links.c.issuer == issuer, self.links.c.subject == subject)).scalar_one_or_none()
      if existing:
        if existing != user_id:
          raise IdentityError("Identity already linked")
        return
      connection.execute(self.links.insert().values(issuer=issuer, subject=subject, user_id=user_id))

  def linked_user_id(self, issuer, subject):
    with self.engine.connect() as connection:
      return connection.execute(select(self.links.c.user_id).where(
        self.links.c.issuer == issuer, self.links.c.subject == subject)).scalar_one_or_none()

  def customer_list(self):
    with self.engine.connect() as connection:
      return list(connection.execute(select(self.customers.c.payload)).scalars())

  def require_customer(self, customer):
    record = self.get(self.customers, customer)
    if not record or not record.get("enabled", True):
      raise IdentityError("Customer is not registered or is disabled")
    return record
