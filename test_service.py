"""核对基础服务和收银分类。"""

import json
import unittest
from pathlib import Path

from service import SERVICE_ID, health_payload


class BaselineContractTest(unittest.TestCase):
    def test_service_identity(self):
        self.assertEqual(health_payload()["service"], SERVICE_ID)

    def test_fixture_separates_credit_from_payment(self):
        data = json.loads(Path("fixtures/sample.json").read_text(encoding="utf-8"))
        self.assertIn("payment-tool", data["categories"])
        self.assertIn("credit", data["categories"])
        self.assertNotEqual(data["categories"].index("payment-tool"), data["categories"].index("credit"))


if __name__ == "__main__":
    unittest.main()
