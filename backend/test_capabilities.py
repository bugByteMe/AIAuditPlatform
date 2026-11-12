from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from capabilities import CapabilityCatalog, CapabilityConfigError


class CapabilityCatalogTest(unittest.TestCase):
  def setUp(self) -> None:
    self.tempdir = tempfile.TemporaryDirectory()
    self.root = Path(self.tempdir.name)
    self.skills = self.root / "skills"
    self.skills.mkdir()
    for name in ["audit-base", "audit-detail"]:
      (self.skills / name).mkdir()
      (self.skills / name / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    self.mcp_path = self.root / "mcp.json"
    self.mcp_path.write_text(json.dumps({
      "mcpServers": {
        "documents": {"command": "node", "args": ["documents.js"]},
        "disabled": {"command": "node", "enabled": False},
      }
    }), encoding="utf-8")

  def tearDown(self) -> None:
    self.tempdir.cleanup()

  def write_catalog(self, capabilities: list[dict]) -> Path:
    path = self.root / "capabilities.json"
    path.write_text(json.dumps({"capabilities": capabilities}), encoding="utf-8")
    return path

  def test_defaults_localization_and_union_are_normalized(self) -> None:
    catalog = CapabilityCatalog(self.write_catalog([
      {
        "id": "audit",
        "name": {"zh": "审计", "en": "Audit"},
        "description": "Common audit tools",
        "default": True,
        "skills": ["audit-base"],
        "mcpServers": ["documents", "disabled"],
      },
      {
        "id": "detail",
        "name": {"en": "Detail testing"},
        "default": True,
        "skills": ["audit-base", "audit-detail"],
        "mcpServers": ["documents"],
      },
    ]), self.skills, self.mcp_path)

    self.assertEqual(catalog.default_ids(), ["audit", "detail"])
    self.assertEqual(catalog.public_capabilities()[1]["name"], {"zh": "Detail testing", "en": "Detail testing"})
    self.assertEqual(catalog.resolve(["detail", "audit"]), {
      "capabilityIds": ["audit", "detail"],
      "skillIds": ["audit-base", "audit-detail"],
      "mcpServerIds": ["documents", "disabled"],
    })

  def test_invalid_references_and_duplicate_ids_fail_loading(self) -> None:
    with self.assertRaisesRegex(CapabilityConfigError, "missing skill"):
      CapabilityCatalog(self.write_catalog([
        {"id": "audit", "name": "Audit", "skills": ["missing"]},
      ]), self.skills, self.mcp_path)
    with self.assertRaisesRegex(CapabilityConfigError, "unknown MCP"):
      CapabilityCatalog(self.write_catalog([
        {"id": "audit", "name": "Audit", "mcpServers": ["missing"]},
      ]), self.skills, self.mcp_path)
    with self.assertRaisesRegex(CapabilityConfigError, "duplicate id"):
      CapabilityCatalog(self.write_catalog([
        {"id": "audit", "name": "Audit"},
        {"id": "audit", "name": "Audit again"},
      ]), self.skills, self.mcp_path)

  def test_explicit_empty_and_legacy_defaults_are_distinct(self) -> None:
    catalog = CapabilityCatalog(self.write_catalog([
      {"id": "audit", "name": "Audit", "default": True},
    ]), self.skills, self.mcp_path)
    self.assertEqual(catalog.effective_session_ids({}), ["audit"])
    self.assertEqual(catalog.effective_session_ids({"capabilityIds": []}), [])
    with self.assertRaisesRegex(CapabilityConfigError, "Unknown capabilities"):
      catalog.resolve(["removed"])


if __name__ == "__main__":
  unittest.main()
