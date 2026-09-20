"""端到端审计复原：横跨 2026-09-30，错误分类、迟到确认、唯一版本。"""

import json
import unittest
from pathlib import Path

from checkout.demo import build_world, run_scenario, FIXTURES_DIR


class ScenarioAuditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = run_scenario(build_world(corrected=False))

    def test_policy_versions_either_side_of_boundary(self):
        versions = self.report["effective_version_at"]
        self.assertEqual(versions["2026-09-29T22:15:00+08:00"],
                         "financial-marketing-2025")
        self.assertEqual(versions["2026-09-30T00:00:00+08:00"],
                         "financial-marketing-2026")

    def test_c1_layout_pre_boundary(self):
        c1 = self.report["c1_before_boundary"]
        layout = c1["presented_layout"]
        self.assertEqual(layout["policy_version"], "financial-marketing-2025")
        groups = {g["group_id"]: g["source_ids"] for g in layout["groups"]}
        # 更正发生在 23:40，结账在 22:15：当时 biz-paylater 仍在支付工具组呈现
        self.assertIn("biz-paylater", groups["payment-tools"])
        self.assertIsNone(layout["preselected_source"])
        self.assertTrue(layout["selection_required"])
        # 用户明确选择了 cf-credit
        self.assertEqual(c1["user_selection"]["source_id"], "cf-credit")
        self.assertTrue(c1["user_selection"]["explicit"])

    def test_c1_subsequent_charges_timeout_then_retry_same_channel(self):
        types_ = [c["event_type"] for c in self.report["c1_before_boundary"]["charges"]]
        self.assertEqual(types_, ["payment.attempted", "payment.failed",
                                  "payment.retried", "payment.succeeded"])
        succeeded = self.report["c1_before_boundary"]["charges"][-1]
        self.assertTrue(succeeded["reuses_user_selected_channel"])
        self.assertEqual(succeeded["source_id"], "cf-credit")

    def test_c2_layout_post_boundary_reflects_correction(self):
        c2 = self.report["c2_after_boundary"]
        groups = {g["group_id"]: g["source_ids"]
                  for g in c2["presented_layout"]["groups"]}
        self.assertEqual(c2["presented_layout"]["policy_version"],
                         "financial-marketing-2026")
        # 更正后的 biz-paylater 必须出现在金融产品组
        self.assertIn("biz-paylater", groups["financial-products"])
        self.assertNotIn("biz-paylater", groups["payment-tools"])
        self.assertEqual(c2["user_selection"]["source_id"], "icbc-debit")

    def test_late_and_misclassified_receipts_identified(self):
        receipts = self.report["c1_before_boundary"]["receipts"]
        by_ref = {r["channel_ref"]: r for r in receipts}
        # 迟到确认：09-30 09:12 才收到 09-29 22:25 的成功回执
        self.assertTrue(by_ref["CH-7002"]["late_confirmation"])
        self.assertFalse(by_ref["CH-7002"]["category_mismatch"])
        # 错误分类回执：自称 payment-tool，权威分类为 credit
        dup = by_ref["CH-7002-DUP"]
        self.assertTrue(dup["category_mismatch"])
        self.assertEqual(dup["claimed_category"], "payment-tool")
        self.assertEqual(dup["authoritative_category"], "credit")

    def test_receipt_fixture_matches_scenario(self):
        fixtures = json.loads((FIXTURES_DIR / "receipts.json").read_text(encoding="utf-8"))
        refs = {r["channel_ref"] for r in fixtures["receipts"]}
        recv = {r["channel_ref"] for r in self.report["c1_before_boundary"]["receipts"]}
        self.assertEqual(refs, recv)

    def test_published_timeline_is_contiguous_and_non_overlapping(self):
        timeline = self.report["policy_timeline"]
        self.assertEqual([r["version"] for r in timeline],
                         ["financial-marketing-2025", "financial-marketing-2026"])
        # 旧版结束点 == 新版生效点
        self.assertEqual(timeline[0]["effective_until"], timeline[1]["effective_from"])

    def test_marketing_blocks_all_four_required_categories(self):
        by_id = {m["material_id"]: m for m in self.report["marketing_reviews"]}
        required = {
            "禁用表述": "AD-NEW-LOW-RISK",
            "保本暗示": "AD-NEW-GUARANTEE",
            "只突出首期费用": "AD-NEW-FIRST-ONLY",
            "支付机构导流": "AD-NEW-DIVERSION",
        }
        for label, material_id in required.items():
            self.assertEqual(by_id[material_id]["decision"], "blocked", label)
            self.assertTrue(by_id[material_id]["hits"], label)


if __name__ == "__main__":
    unittest.main()
