"""端到端 HTTP 接口测试：真实启动服务并走完整结账链路。"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from checkout.api import make_server
from checkout.app import load_app
from checkout.payments import ChargeOutcome, ScriptedGateway


class ApiClient:
    def __init__(self, base_url):
        self.base = base_url

    def request(self, method, path, body=None):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gw = ScriptedGateway(results={
            "bank-card-icbc": [
                ChargeOutcome("failed", "issuer-declined", "ch-1"),
                ChargeOutcome("succeeded", "", "ch-2"),
            ],
            "credit-zhaolian": [
                ChargeOutcome("failed", "issuer-timeout", "ch-c1"),
                ChargeOutcome("succeeded", "", "ch-c2"),
            ],
        })
        cls.app = load_app(gateway=cls.gw)
        cls.server: ThreadingHTTPServer = make_server(cls.app, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.api = ApiClient(f"http://127.0.0.1:{cls.port}")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_health(self):
        status, body = self.api.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "checkout-compliance")

    def test_full_checkout_select_charge_retry_flow(self):
        at = "2026-10-20T12:00:00+08:00"
        status, plan = self.api.request(
            "GET",
            "/checkout?order_id=ord-http-1&region=BJ&mcc=5812&at=" + at,
        )
        self.assertEqual(status, 200)
        self.assertEqual(plan["policy_source"]["version"], 3)
        self.assertTrue(
            all(not o["selected"] for g in plan["groups"] for o in g["options"])
        )

        status, body = self.api.request("POST", "/checkout/select", {
            "order_id": "ord-http-1", "source_id": "bank-card-icbc",
            "region": "BJ", "mcc": "5812", "at": at,
        })
        self.assertEqual(status, 200)

        status, attempt = self.api.request("POST", "/payments/attempt", {
            "order_id": "ord-http-1", "amount_cents": 58800,
            "region": "BJ", "mcc": "5812", "at": at,
        })
        self.assertEqual(status, 202)
        self.assertEqual(attempt["status"], "failed")

        status, retry = self.api.request("POST", "/payments/retry", {
            "order_id": "ord-http-1", "amount_cents": 58800,
            "region": "BJ", "mcc": "5812", "at": at,
        })
        self.assertEqual(status, 200)
        self.assertEqual(retry["status"], "succeeded")
        self.assertEqual(retry["source_id"], "bank-card-icbc")

        status, report = self.api.request("GET", "/audit/orders/ord-http-1")
        self.assertEqual(status, 200)
        self.assertTrue(report["consistent"], msg=str(report["findings"]))

    def test_charge_without_selection_rejected(self):
        at = "2026-10-20T12:00:00+08:00"
        self.api.request(
            "GET", "/checkout?order_id=ord-http-2&region=BJ&mcc=5812&at=" + at
        )
        status, body = self.api.request("POST", "/payments/attempt", {
            "order_id": "ord-http-2", "amount_cents": 100,
            "region": "BJ", "mcc": "5812", "at": at,
        })
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "no-explicit-selection")

    def test_retry_blocked_when_source_frozen(self):
        at = "2026-10-20T13:00:00+08:00"
        self.api.request(
            "GET", "/checkout?order_id=ord-http-3&region=BJ&mcc=5812&at=" + at
        )
        self.api.request("POST", "/checkout/select", {
            "order_id": "ord-http-3", "source_id": "credit-zhaolian",
            "region": "BJ", "mcc": "5812", "at": at,
        })
        self.api.request("POST", "/payments/attempt", {
            "order_id": "ord-http-3", "amount_cents": 100,
            "region": "BJ", "mcc": "5812", "at": at,
        })
        self.app.catalog.freeze("credit-zhaolian", "接口测试冻结")
        status, body = self.api.request("POST", "/payments/retry", {
            "order_id": "ord-http-3", "amount_cents": 100,
            "region": "BJ", "mcc": "5812", "at": at,
        })
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "frozen")
        self.app.catalog.unfreeze("credit-zhaolian")

    def test_marketing_review_blocks_with_evidence(self):
        status, body = self.api.request("POST", "/marketing/review?region=BJ", {
            "material_id": "ad-1",
            "surface": "repayment",
            "fields": [{"name": "card", "text": "急用钱？开通额度立即到账"}],
            "promoted_category": "credit",
            "at": "2026-10-20T12:00:00+08:00",
        })
        self.assertEqual(status, 422)
        self.assertTrue(body["violations"])
        self.assertTrue(all(v["clause"] for v in body["violations"]))

    def test_policy_resolution_endpoint_respects_region_and_date(self):
        status, body = self.api.request(
            "GET",
            "/policies?region=SH&at=2026-10-01T12:00:00%2B08:00&user_id=u8",
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["resolved"], {"version": 2, "mode": "canary"})

        status, body = self.api.request(
            "GET",
            "/policies?region=BJ&at=2026-10-01T12:00:00%2B08:00&user_id=u8",
        )
        self.assertEqual(body["resolved"]["version"], 1)

    def test_concurrent_publish_conflict_via_api(self):
        def new_policy(version):
            return {
                "policy_id": "experimental",
                "version": version,
                "regions": ["HN"],
                "effective_from": "2026-12-01T00:00:00+08:00",
                "canary_percent": 100,
                "groups": [
                    {"key": "payment", "title": "支付", "order": 0,
                     "categories": ["bank-card"]}
                ],
                "categories": [
                    {"category": "bank-card", "label": "银行卡",
                     "payment_allowed": True, "checkout_visible": True,
                     "required_risk_disclosure": [],
                     "group_key": "payment", "order": 0, "marketable": True}
                ],
                "marketing": {},
            }

        # 新政策族首次发布 expected_version=null；两个并发请求只有一个赢
        results = []

        def publish():
            status, body = self.api.request("POST", "/admin/policies/publish", {
                "policy": new_policy(1),
                "expected_version": None,
            })
            results.append((status, body))

        import threading
        t1 = threading.Thread(target=publish)
        t2 = threading.Thread(target=publish)
        t1.start(); t2.start(); t1.join(); t2.join()
        statuses = sorted(s for s, _ in results)
        self.assertEqual(statuses, [201, 409])

    def test_audit_orders_endpoint_lists_fixture_orders(self):
        status, body = self.api.request("GET", "/audit/orders")
        self.assertEqual(status, 200)
        self.assertIn("order-9001", body["consistent_orders"])
        self.assertIn("order-9003", body["inconsistent_orders"])

    def test_admin_check_ok(self):
        status, body = self.api.request("GET", "/admin/check")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])


if __name__ == "__main__":
    unittest.main()
