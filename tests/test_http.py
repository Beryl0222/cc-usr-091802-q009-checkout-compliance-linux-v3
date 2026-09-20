"""HTTP 端到端：方案—选择—扣款/重试—审计，以及营销阻断接口。"""

import json
import threading
import unittest
import urllib.request
import urllib.error

from checkout.app import make_server


class HttpIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json; charset=utf-8"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health(self):
        status, body = self._call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "checkout-compliance")

    def test_checkout_select_charge_retry_flow(self):
        plan_body = {
            "checkout_id": "W1", "merchant_id": "m1001", "region": "CN-GD",
            "subject_key": "u-http", "amount": "368.00",
            "at": "2026-09-30T10:00:00+08:00",
        }
        status, plan = self._call("POST", "/checkout/plan", plan_body)
        self.assertEqual(status, 200)
        self.assertIsNone(plan["preselected_source"])
        for group in plan["groups"]:
            for item in group["items"]:
                self.assertFalse(item["selected"])

        # 未显式选择 → 422
        status, err = self._call("POST", "/checkout/select",
                                 {"checkout_id": "W1", "source_id": "cf-credit",
                                  "explicit": False})
        self.assertEqual(status, 422)
        self.assertEqual(err["error"], "selection-not-explicit")

        status, sel = self._call("POST", "/checkout/select",
                                 {"checkout_id": "W1", "source_id": "cf-credit",
                                  "explicit": True})
        self.assertEqual(status, 200)
        token = sel["selection_token"]

        # 首次渠道超时
        status, err = self._call("POST", "/payments/charge",
                                 {"selection_token": token, "amount": "368.00",
                                  "at": "2026-09-30T10:02:00+08:00",
                                  "channel_ref": "CH-H1"})
        self.assertEqual(status, 422)
        self.assertEqual(err["error"], "channel-timeout")

        # 重试沿用同一渠道并重新核验 → 成功
        status, charged = self._call("POST", "/payments/charge",
                                     {"selection_token": token, "amount": "368.00",
                                      "is_retry": True,
                                      "at": "2026-09-30T10:05:00+08:00",
                                      "channel_ref": "CH-H2"})
        self.assertEqual(status, 200)
        self.assertEqual(charged["source_id"], "cf-credit")
        self.assertEqual(charged["attempt"], 2)

    def test_marketing_review_blocks_with_evidence(self):
        status, body = self._call("POST", "/marketing/review", {
            "material_id": "WEB-1", "text": "保本保息，和存款一样",
            "target_category": "asset-management",
            "publisher_role": "fund-manager", "surface": "fund-detail",
            "at": "2026-09-30T08:00:00+08:00",
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["decision"], "blocked")
        self.assertTrue(body["hits"])
        for hit in body["hits"]:
            self.assertTrue(hit["clause"])
            self.assertIsNotNone(hit["snippet"])

    def test_audit_reconstruction_available(self):
        # 自包含：先建方案与选择，再复原（不依赖其他用例的执行顺序）
        self._call("POST", "/checkout/plan", {
            "checkout_id": "WAUD", "merchant_id": "m1001", "region": "CN-GD",
            "subject_key": "u-aud", "amount": "88.00",
            "at": "2026-09-30T11:00:00+08:00"})
        self._call("POST", "/checkout/select",
                   {"checkout_id": "WAUD", "source_id": "icbc-debit",
                    "explicit": True})
        status, body = self._call("GET", "/audit/checkouts/WAUD")
        self.assertEqual(status, 200)
        self.assertEqual(body["user_selection"]["source_id"], "icbc-debit")
        self.assertTrue(body["integrity"]["no_server_preselection"])
        self.assertTrue(body["integrity"]["charges_match_selection"])

    def test_unknown_route_404(self):
        status, _ = self._call("GET", "/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
