from __future__ import annotations

import unittest

from micu_api import MicuApiClient, parse_cny


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
  def test_cny_conversion_is_decimal_and_exact(self) -> None:
    client = FakeMicuClient()
    self.assertEqual(parse_cny("1.25"), parse_cny(1.25))
    self.assertEqual(client.quota_from_cny("1.25"), 625_000)
    self.assertEqual(client.cny_from_quota(625_000), "1.25")

  def test_add_balance_increments_existing_quota_and_reenables(self) -> None:
    client = FakeMicuClient()
    result = client.add_balance({"tokenId": 7}, "2.50")
    self.assertEqual(result["remainingCny"], "4.50")
    update = next(call for call in client.calls if call[1] == "/api/token/")
    self.assertEqual(update[2]["remain_quota"], 2_250_000)
    self.assertIn(("PUT", "/api/token/?status_only=true", {"id": 7, "status": 1}), client.calls)


if __name__ == "__main__":
  unittest.main()
