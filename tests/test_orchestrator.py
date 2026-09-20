"""收银台编排：分组隔离、绝不预选、风险信息、缓存到期、政策切换。"""

import unittest
from datetime import timedelta

from checkout import timeutil
from checkout.demo import build_world
from checkout.orchestrator import CheckoutRequest


class OrchestratorTest(unittest.TestCase):
    def setUp(self):
        self.w = build_world(corrected=True)

    def _plan(self, at, region="CN-SH", merchant="m1001", subject="u1"):
        return self.w.orchestrator.build_plan(CheckoutRequest(
            merchant_id=merchant, region=region, subject_key=subject,
            amount="100.00", at=at, checkout_id="X1"))

    def test_groups_separate_payment_tools_from_financial_products(self):
        plan = self._plan("2026-09-30T00:20:00+08:00")
        groups = {g["group_id"]: {i["source_id"] for i in g["items"]}
                  for g in plan["groups"]}
        self.assertIn("icbc-debit", groups["payment-tools"])
        self.assertIn("pay-balance", groups["payment-tools"])
        self.assertIn("cf-credit", groups["financial-products"])
        self.assertIn("biz-paylater", groups["financial-products"])  # 更正后归类
        self.assertNotIn("cf-credit", groups["payment-tools"])
        self.assertNotIn("icbc-debit", groups["financial-products"])

    def test_no_server_preselection_ever(self):
        for at in ("2026-09-29T22:15:00+08:00", "2026-09-30T00:20:00+08:00"):
            plan = self._plan(at)
            self.assertIsNone(plan["preselected_source"])
            self.assertTrue(plan["selection_required"])
            self.assertEqual(plan["selection_mode"], "user-explicit-only")
            for group in plan["groups"]:
                for item in group["items"]:
                    self.assertFalse(item["selected"])
                    self.assertFalse(item["default"])
                    self.assertFalse(item["recommended"])

    def test_financial_products_carry_required_risk_notice(self):
        plan = self._plan("2026-09-30T00:20:00+08:00")
        for group in plan["groups"]:
            for item in group["items"]:
                if item["group_kind"] == "financial-product":
                    self.assertIn("required_risk_notice", item)
                    notice = item["required_risk_notice"]
                    self.assertTrue(notice["text"])
                    self.assertIn("clause", notice)
                else:
                    self.assertNotIn("required_risk_notice", item)

    def test_plan_switches_policy_version_at_boundary(self):
        before = self._plan("2026-09-29T23:59:59+08:00")
        after = self._plan("2026-09-30T00:00:00+08:00")
        self.assertEqual(before["policy"]["version"], "financial-marketing-2025")
        self.assertEqual(after["policy"]["version"], "financial-marketing-2026")
        self.assertIn("legal_source", before["policy"])

    def test_region_scoped_source_excluded_with_reason(self):
        # mmf-fund 仅限 CN-GD/CN-SH；北京用户看不到，且排除原因可追溯
        plan = self._plan("2026-09-30T00:20:00+08:00", region="CN-BJ")
        presented = {i["source_id"] for g in plan["groups"] for i in g["items"]}
        self.assertNotIn("mmf-fund", presented)
        excluded = {e["source_id"]: e["reason"] for e in plan["excluded"]}
        self.assertEqual(excluded.get("mmf-fund"), "region-out-of-scope")

    def test_cache_expires_at_policy_boundary(self):
        # 09-29 23:59 生成的方案，过期点不得晚于 09-30 00:00 生效边界
        plan = self._plan("2026-09-29T23:59:00+08:00")
        self.assertLessEqual(
            timeutil.parse(plan["expires_at"]),
            timeutil.parse("2026-09-30T00:00:00+08:00"))

    def test_expired_cache_entry_is_not_served(self):
        req = CheckoutRequest(
            merchant_id="m1001", region="CN-SH", subject_key="u1",
            amount="100.00", at="2026-09-29T23:59:00+08:00", checkout_id="X2")
        first = self.w.orchestrator.build_plan(req)
        self.assertFalse(first["from_cache"])
        # 跨过 expires_at 后同键再取：必须重新渲染
        at = timeutil.parse(first["expires_at"]) + timedelta(seconds=1)
        second = self.w.orchestrator.build_plan(
            CheckoutRequest(merchant_id="m1001", region="CN-SH", subject_key="u1",
                            amount="100.00", at=at, checkout_id="X2"))
        self.assertFalse(second["from_cache"])
        self.assertEqual(second["policy"]["version"], "financial-marketing-2026")


if __name__ == "__main__":
    unittest.main()
