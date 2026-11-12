from __future__ import annotations

import json
import re
from pathlib import Path

from codex_runner import RunnerError, load_mcp_servers


CAPABILITY_ID_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")


class CapabilityConfigError(ValueError):
  pass


class CapabilityCatalog:
  def __init__(
    self,
    path: Path | None = None,
    skill_path: Path | None = None,
    mcp_server_config_path: Path | None = None,
  ) -> None:
    self.path = path
    self.skill_path = skill_path
    self.mcp_server_config_path = mcp_server_config_path
    self.capabilities = self._load()
    self.by_id = {item["id"]: item for item in self.capabilities}

  def _load(self) -> list[dict]:
    if not self.path:
      return []
    try:
      payload = json.loads(self.path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
      raise CapabilityConfigError(f"Failed to read capability configuration from {self.path}: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {"capabilities"}:
      raise CapabilityConfigError(f"Capability configuration in {self.path} must contain only a capabilities array")
    raw_capabilities = payload.get("capabilities")
    if not isinstance(raw_capabilities, list):
      raise CapabilityConfigError(f"Capability configuration in {self.path} must contain a capabilities array")

    mcp_servers = self._mcp_servers()
    capabilities = []
    seen = set()
    for index, raw in enumerate(raw_capabilities):
      if not isinstance(raw, dict):
        raise CapabilityConfigError(f"Capability {index} in {self.path} must be a JSON object")
      unexpected = set(raw) - {"id", "name", "description", "default", "skills", "mcpServers"}
      if unexpected:
        raise CapabilityConfigError(
          f"Capability {index} in {self.path} has unsupported fields: {', '.join(sorted(unexpected))}"
        )
      capability_id = str(raw.get("id") or "").strip()
      if not CAPABILITY_ID_PATTERN.fullmatch(capability_id) or capability_id in seen:
        raise CapabilityConfigError(f"Capability {index} in {self.path} has a missing, invalid, or duplicate id")
      if "default" in raw and not isinstance(raw["default"], bool):
        raise CapabilityConfigError(f"Capability {capability_id} default must be a boolean")
      skills = self._resource_ids(capability_id, "skills", raw.get("skills", []))
      mcp_ids = self._resource_ids(capability_id, "mcpServers", raw.get("mcpServers", []))
      self._validate_skills(capability_id, skills)
      missing_mcp = [server_id for server_id in mcp_ids if server_id not in mcp_servers]
      if missing_mcp:
        raise CapabilityConfigError(
          f"Capability {capability_id} references unknown MCP servers: {', '.join(missing_mcp)}"
        )
      seen.add(capability_id)
      capabilities.append({
        "id": capability_id,
        "name": self._localized(capability_id, "name", raw.get("name"), required=True),
        "description": self._localized(capability_id, "description", raw.get("description", "")),
        "default": bool(raw.get("default", False)),
        "skills": skills,
        "mcpServers": mcp_ids,
      })
    return capabilities

  def _mcp_servers(self) -> dict:
    if not self.mcp_server_config_path:
      return {}
    try:
      return load_mcp_servers(self.mcp_server_config_path)
    except RunnerError as exc:
      raise CapabilityConfigError(str(exc)) from exc

  def _localized(self, capability_id: str, field: str, value, required: bool = False) -> dict[str, str]:
    if isinstance(value, str):
      clean = value.strip()
      if required and not clean:
        raise CapabilityConfigError(f"Capability {capability_id} {field} must not be empty")
      return {"zh": clean, "en": clean}
    if not isinstance(value, dict) or set(value) - {"zh", "en"}:
      raise CapabilityConfigError(f"Capability {capability_id} {field} must be a string or a zh/en object")
    zh = value.get("zh")
    en = value.get("en")
    if (zh is not None and not isinstance(zh, str)) or (en is not None and not isinstance(en, str)):
      raise CapabilityConfigError(f"Capability {capability_id} {field} translations must be strings")
    zh = str(zh or "").strip()
    en = str(en or "").strip()
    fallback = zh or en
    if required and not fallback:
      raise CapabilityConfigError(f"Capability {capability_id} {field} must not be empty")
    return {"zh": zh or fallback, "en": en or fallback}

  def _resource_ids(self, capability_id: str, field: str, value) -> list[str]:
    if not isinstance(value, list):
      raise CapabilityConfigError(f"Capability {capability_id} {field} must be an array")
    result = []
    for item in value:
      resource_id = str(item or "").strip() if isinstance(item, str) else ""
      if not CAPABILITY_ID_PATTERN.fullmatch(resource_id):
        raise CapabilityConfigError(f"Capability {capability_id} contains an invalid {field} reference")
      if resource_id not in result:
        result.append(resource_id)
    return result

  def _validate_skills(self, capability_id: str, skills: list[str]) -> None:
    if not skills:
      return
    if not self.skill_path:
      raise CapabilityConfigError(f"Capability {capability_id} references skills but skill_path is not configured")
    for skill_id in skills:
      skill = self.skill_path / skill_id
      if not skill.is_dir() or not (skill / "SKILL.md").is_file():
        raise CapabilityConfigError(f"Capability {capability_id} references missing skill {skill_id}")

  def default_ids(self) -> list[str]:
    return [item["id"] for item in self.capabilities if item["default"]]

  def effective_session_ids(self, session: dict) -> list[str]:
    requested = session.get("capabilityIds") if "capabilityIds" in session else self.default_ids()
    return self.filter_known_ids(requested)

  def filter_known_ids(self, capability_ids) -> list[str]:
    values = capability_ids if isinstance(capability_ids, list) else []
    requested = {item for item in values if isinstance(item, str)}
    return [item["id"] for item in self.capabilities if item["id"] in requested]

  def resolve(self, capability_ids) -> dict:
    if not isinstance(capability_ids, list) or not all(isinstance(item, str) for item in capability_ids):
      raise CapabilityConfigError("capabilityIds must be an array of capability IDs")
    unknown = sorted({item for item in capability_ids if item not in self.by_id})
    if unknown:
      raise CapabilityConfigError(f"Unknown capabilities: {', '.join(unknown)}")
    selected_ids = self.filter_known_ids(capability_ids)
    skills = []
    mcp_servers = []
    for capability_id in selected_ids:
      capability = self.by_id[capability_id]
      for skill_id in capability["skills"]:
        if skill_id not in skills:
          skills.append(skill_id)
      for server_id in capability["mcpServers"]:
        if server_id not in mcp_servers:
          mcp_servers.append(server_id)
    return {"capabilityIds": selected_ids, "skillIds": skills, "mcpServerIds": mcp_servers}

  def public_capabilities(self) -> list[dict]:
    return [
      {
        "id": item["id"],
        "name": dict(item["name"]),
        "description": dict(item["description"]),
        "default": item["default"],
      }
      for item in self.capabilities
    ]
