from __future__ import annotations

from email.message import Message
from io import BytesIO
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from micu_api import MicuApiClient, MicuApiError, parse_cny


class FakeMicuClient(MicuApiClient):
  def __init__(self):
    super().__init__("https://provider.example", "https://inference.example/v1", "", "", "vip_2", 500_000, 2)
    self.calls = []
    self.stored = {
      "id": 7,
      "name": "usr_example",
      "status": 3,
      "remain_quota": 1_000_000,
      "unlimited_quota": False,
      "expired_time": -1,
      "model_limits_enabled": False,
      "model_limits": "",
      "allow_ips": "",
      "group": "vip_2",
      "cross_group_retry": False,
    }

  def token(self, token_id: int) -> dict:
    return dict(self.stored)

  def _request(self, method: str, path: str, payload=None, *, api_key: str = "") -> dict:
    self.calls.append((method, path, payload))
    if method == "PUT" and path == "/api/token/":
      self.stored.update(payload)
    return {"success": True, "data": {}}


class MicuApiClientTest(unittest.TestCase):
  def test_management_requests_use_provider_compatible_user_agent(self) -> None:
    client = MicuApiClient("https://provider.example", "https://inference.example/v1", "secret", "78836", "vip_2", 500_000, 2)
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"success":true,"data":{"items":[],"total":0}}'
    with patch("micu_api.urlopen", return_value=response) as request_call:
      client.all_tokens()
    request = request_call.call_args.args[0]
    self.assertEqual(request.get_header("User-agent"), "AI-AuditPlatform/1.0")

  def test_cny_conversion_is_decimal_and_exact(self) -> None:
    client = FakeMicuClient()
    self.assertEqual(parse_cny("1.25"), parse_cny(1.25))
    self.assertEqual(client.quota_from_cny("1.25"), 625_000)
    self.assertEqual(client.cny_from_quota(625_000), "1.25")
    self.assertEqual(client.remaining_percent(3, 1), 75.0)
    self.assertEqual(client.remaining_percent(0, 0), 0.0)

  def test_add_balance_increments_existing_quota_and_reenables(self) -> None:
    client = FakeMicuClient()
    result = client.add_balance({"tokenId": 7}, "2.50")
    self.assertEqual(result["remainingCny"], "4.50")
    self.assertEqual(result["remainingPercent"], 100.0)
    update = next(call for call in client.calls if call[1] == "/api/token/")
    self.assertEqual(update[2]["remain_quota"], 2_250_000)
    self.assertIn(("PUT", "/api/token/?status_only=true", {"id": 7, "status": 1}), client.calls)

  def test_align_binding_name_renames_legacy_token_and_repairs_binding(self) -> None:
    client = FakeMicuClient()
    client.stored["name"] = "usr_legacy_id"
    client.find_token = MagicMock(return_value=None)
    binding = {"tokenId": 7, "tokenName": "usr_legacy_id"}

    result = client.align_binding_name(binding, "alice")

    self.assertEqual(result, "alice")
    self.assertEqual(binding["tokenName"], "alice")
    update = next(call for call in client.calls if call[1] == "/api/token/")
    self.assertEqual(update[2]["name"], "alice")

  def test_align_binding_name_only_repairs_stale_local_name_when_provider_is_correct(self) -> None:
    client = FakeMicuClient()
    client.stored["name"] = "alice"
    binding = {"tokenId": 7, "tokenName": "usr_legacy_id"}

    client.align_binding_name(binding, "alice")

    self.assertEqual(binding["tokenName"], "alice")
    self.assertFalse(any(call[0] == "PUT" for call in client.calls))

  def test_http_error_reports_status_and_type_without_exposing_html(self) -> None:
    client = MicuApiClient("https://provider.example", "https://inference.example/v1", "secret", "78836", "vip_2", 500_000, 2)
    headers = Message()
    headers["Content-Type"] = "text/html; charset=utf-8"
    response = HTTPError(
      "https://provider.example/api/token/",
      403,
      "Forbidden",
      headers,
      BytesIO(b"<html>sensitive proxy page</html>"),
    )
    with patch("micu_api.urlopen", side_effect=response):
      with self.assertRaisesRegex(MicuApiError, r"HTTP 403, text/html") as raised:
        client.all_tokens()
    self.assertNotIn("sensitive proxy page", str(raised.exception))

  def test_json_http_error_keeps_sanitized_provider_message_and_diagnostics(self) -> None:
    client = MicuApiClient("https://provider.example", "https://inference.example/v1", "secret", "78836", "vip_2", 500_000, 2)
    headers = Message()
    headers["Content-Type"] = "application/json; charset=utf-8"
    response = HTTPError(
      "https://provider.example/api/token/",
      429,
      "Too Many Requests",
      headers,
      BytesIO(b'{"message":"  rate limit\\nreached  "}'),
    )
    with patch("micu_api.urlopen", side_effect=response):
      with self.assertRaisesRegex(MicuApiError, r"rate limit reached \(HTTP 429, application/json\)"):
        client.all_tokens()


if __name__ == "__main__":
  unittest.main()
