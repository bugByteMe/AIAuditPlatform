"""Explicit account administration capabilities; unknown roles fail closed."""

ROLES = frozenset({"user", "group_admin", "platform_admin", "system_admin"})
MANAGEMENT_ROLES = frozenset({"platform_admin", "system_admin"})
PLATFORM_ACCOUNT_FIELDS = frozenset({"groupId"})
PLATFORM_GROUP_FIELDS = frozenset({"name", "liveRunLimit"})


def validate_role(role: object) -> str:
  if not isinstance(role, str) or role not in ROLES:
    raise ValueError("unknown user role")
  return role
