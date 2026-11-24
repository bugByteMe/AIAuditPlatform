"""Shared validation for the built-in interactive MCP tool and website."""
from __future__ import annotations

MAX_TEXT_BYTES = 16 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
SERVER_NAME = "ai_audit_input"
TOOL_TIMEOUT = 2147483647
TOOL_SCHEMA = {
  "type": "object", "additionalProperties": False,
  "required": ["type", "title"],
  "properties": {
    "type": {"type": "string", "enum": ["yes_no", "multiple_choice", "text"]},
    "title": {"type": "string"}, "text": {"type": "string"},
    "images": {"type": "array", "maxItems": 4, "items": {"type": "string"}},
    "multiple": {"type": "boolean"},
    "options": {"type": "array", "minItems": 2, "maxItems": 20, "items": {
      "type": "object", "additionalProperties": False, "required": ["id", "label"],
      "properties": {"id": {"type": "string"}, "label": {"type": "string"}},
    }},
  },
}


def bounded_text(value, label, *, required=False):
  if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_TEXT_BYTES:
    raise ValueError(f"{label} must be text of at most 16 KiB")
  if required and not value.strip():
    raise ValueError(f"{label} must not be empty")
  return value


def validate_question(value):
  if not isinstance(value, dict) or set(value) - set(TOOL_SCHEMA["properties"]):
    raise ValueError("invalid question fields")
  kind = value.get("type")
  if kind not in {"yes_no", "multiple_choice", "text"}:
    raise ValueError("invalid question type")
  result = {"type": kind, "title": bounded_text(value.get("title"), "title", required=True),
            "text": bounded_text(value.get("text", ""), "text")}
  if len((result["title"] + result["text"]).encode("utf-8")) > MAX_TEXT_BYTES:
    raise ValueError("question text exceeds 16 KiB")
  images = value.get("images", [])
  if not isinstance(images, list) or len(images) > 4:
    raise ValueError("at most four images are allowed")
  result["images"] = [bounded_text(path, "image path", required=True) for path in images]
  if kind == "multiple_choice":
    options = value.get("options")
    if not isinstance(options, list) or not 2 <= len(options) <= 20:
      raise ValueError("multiple choice requires 2–20 options")
    result["options"] = []
    for option in options:
      if not isinstance(option, dict) or set(option) != {"id", "label"}:
        raise ValueError("each option requires an id and label")
      result["options"].append({key: bounded_text(option[key], key, required=True) for key in ("id", "label")})
    if len({option["id"] for option in result["options"]}) != len(options):
      raise ValueError("option ids must be unique")
    if not isinstance(value.get("multiple", False), bool):
      raise ValueError("multiple must be boolean")
    result["multiple"] = value.get("multiple", False)
  elif "options" in value or "multiple" in value:
    raise ValueError("options and multiple apply only to multiple choice")
  return result


def validate_answer(question, value):
  if not isinstance(value, dict) or set(value) - {"status", "value"}:
    raise ValueError("invalid answer fields")
  status = value.get("status")
  if status == "skipped":
    if "value" in value:
      raise ValueError("skipped answers must not contain a value")
    return {"status": "skipped"}
  if status != "answered":
    raise ValueError("answer status must be answered or skipped")
  answer = value.get("value")
  if question["type"] == "yes_no":
    if not isinstance(answer, bool):
      raise ValueError("yes/no requires a boolean")
  elif question["type"] == "text":
    bounded_text(answer, "answer", required=True)
  else:
    ids = [option["id"] for option in question["options"]]
    if not isinstance(answer, list) or not answer or any(not isinstance(item, str) for item in answer):
      raise ValueError("choose at least one option")
    if len(set(answer)) != len(answer) or any(item not in ids for item in answer):
      raise ValueError("invalid selected options")
    if not question["multiple"] and len(answer) != 1:
      raise ValueError("choose exactly one option")
    answer = [item for item in ids if item in answer]
  return {"status": status, "value": answer}
