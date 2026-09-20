"""营销审核链：七类阻断、否定语境放行、规则集随政策版本切换、命中依据完整。"""

import unittest

from checkout.marketing import (
    MarketingMaterial, RULESET_V1, RULESET_V2, review_material,
)
from checkout.models import Category


def _mat(**over):
    base = dict(
        material_id="m", text="", target_category=Category.CREDIT,
        publisher_role="fund-manager", surface="fund-detail")
    base.update(over)
    return MarketingMaterial(**base)


class MarketingReviewTest(unittest.TestCase):
    AT = "2026-09-30T08:00:00+08:00"

    def test_r100_prohibited_terms(self):
        result = review_material(
            _mat(text="低风险理财，放心买", target_category=Category.ASSET_MANAGEMENT),
            RULESET_V2, self.AT)
        self.assertEqual(result.decision.value, "blocked")
        hit = result.hits[0]
        self.assertEqual(hit.rule_id, "R100")
        self.assertEqual(hit.snippet, "低风险")
        self.assertIsNotNone(hit.start)
        self.assertEqual("低风险理财，放心买"[hit.start:hit.end], "低风险")
        self.assertIn("low-risk", hit.clause)

    def test_r200_principal_guarantee_hint(self):
        result = review_material(
            _mat(text="和存款一样安心", target_category=Category.ASSET_MANAGEMENT),
            RULESET_V2, self.AT)
        self.assertTrue(any(h.rule_id == "R200" for h in result.hits))

    def test_negative_disclosure_is_not_a_guarantee(self):
        result = review_material(
            _mat(text="本产品不保本、不保证收益，投资有风险",
                 target_category=Category.ASSET_MANAGEMENT),
            RULESET_V2, self.AT)
        self.assertEqual(result.decision.value, "approved",
                         [h.to_dict() for h in result.hits])

    def test_r300_first_installment_only_without_total_cost(self):
        blocked = review_material(
            _mat(text="分期首期0元带走", target_category=Category.INSTALLMENT,
                 publisher_role="merchant-credit", surface="checkout"),
            RULESET_V2, self.AT)
        self.assertTrue(any(h.rule_id == "R300" for h in blocked.hits))
        disclosed = review_material(
            _mat(text="分期首期0元，总费用36元，综合年化12%",
                 target_category=Category.INSTALLMENT,
                 publisher_role="merchant-credit", surface="checkout"),
            RULESET_V2, self.AT)
        self.assertFalse(any(h.rule_id == "R300" for h in disclosed.hits))

    def test_r400_payment_institution_diversion(self):
        result = review_material(
            _mat(text="付款时开通信用付", publisher_role="payment-institution",
                 surface="checkout", target_category=Category.CREDIT),
            RULESET_V2, self.AT)
        self.assertTrue(any(h.rule_id == "R400" for h in result.hits))
        # 基金管理人在自有详情页介绍产品不构成支付导流
        ok = review_material(
            _mat(text="信用付产品说明（非支付链路）",
                 publisher_role="consumer-finance", surface="product-detail"),
            RULESET_V2, self.AT)
        self.assertFalse(any(h.rule_id == "R400" for h in ok.hits))

    def test_r500_default_check(self):
        result = review_material(
            _mat(text="开通信用付", default_checked=True), RULESET_V2, self.AT)
        self.assertTrue(any(h.rule_id == "R500" for h in result.hits))

    def test_r600_repayment_page_ad(self):
        result = review_material(
            _mat(text="再借一笔轻松还", surface="repayment-page",
                 target_category=Category.INSTALLMENT,
                 publisher_role="consumer-finance"),
            RULESET_V2, self.AT)
        self.assertTrue(any(h.rule_id == "R600" for h in result.hits))

    def test_r700_incentive_only_borrowing_requires_risk_confirmation(self):
        blocked = review_material(
            _mat(text="开通信用付", incentive="借款立减20"),
            RULESET_V2, self.AT)
        self.assertTrue(any(h.rule_id == "R700" for h in blocked.hits))
        confirmed = review_material(
            _mat(text="开通信用付", incentive="借款立减20", risk_confirmed=True),
            RULESET_V2, self.AT)
        self.assertFalse(any(h.rule_id == "R700" for h in confirmed.hits))

    def test_ruleset_versions_differ(self):
        # "几乎无风险"：旧词表不禁，新词表禁
        old = review_material(
            _mat(text="几乎无风险的现金管理", target_category=Category.ASSET_MANAGEMENT),
            RULESET_V1, "2026-09-29T10:00:00+08:00")
        new = review_material(
            _mat(text="几乎无风险的现金管理", target_category=Category.ASSET_MANAGEMENT),
            RULESET_V2, self.AT)
        self.assertEqual(old.decision.value, "approved")
        self.assertTrue(any(h.rule_id == "R100" for h in new.hits))

    def test_every_hit_documents_basis(self):
        result = review_material(
            _mat(text="低风险，保本保息，首期0元", target_category=Category.INSTALLMENT,
                 publisher_role="payment-institution", surface="checkout"),
            RULESET_V2, self.AT)
        self.assertEqual(result.decision.value, "blocked")
        for hit in result.hits:
            self.assertTrue(hit.rule_id and hit.clause and hit.title)
            self.assertIsNotNone(hit.snippet)
            self.assertEqual(result.ruleset_id, RULESET_V2.ruleset_id)


if __name__ == "__main__":
    unittest.main()
