"""营销审核链：各类阻断与具体命中依据，以及政策版本选择。"""

import unittest

from checkout.app import load_app
from checkout.marketing import (
    MarketingMaterial,
    MaterialField,
)
from checkout.models import Category, cst


def material(
    mid,
    text,
    *,
    surface="home-banner",
    category=Category.CREDIT,
    publisher="bank",
    total_cost_disclosed=False,
    fee_elements=(),
):
    return MarketingMaterial(
        material_id=mid,
        surface=surface,
        fields=(MaterialField("body", text),),
        publisher_type=publisher,
        promoted_category=category,
        total_cost_disclosed=total_cost_disclosed,
        fee_elements=frozenset(fee_elements),
    )


class MarketingBlockRulesTest(unittest.TestCase):
    def setUp(self):
        self.app = load_app()
        self.moment = cst(2026, 10, 20)

    def review(self, mat, region="BJ", moment=None, version=None):
        return self.app.reviewer.review(
            mat, region=region, moment=moment or self.moment, policy_version=version
        )

    def test_prohibited_phrase_with_position_and_clause(self):
        result = self.review(material("m", "这款产品属于低风险理财",
                                      category=Category.ASSET_MANAGEMENT))
        rule_ids = {v.rule_id for v in result.violations}
        self.assertIn("prohibited-phrase", rule_ids)
        hit = next(v for v in result.violations if v.rule_id == "prohibited-phrase")
        self.assertEqual(hit.matched, "低风险")
        self.assertEqual(hit.field, "body")
        self.assertIsNotNone(hit.start)
        self.assertIn("第十一条", hit.clause)

    def test_guaranteed_return_for_money_fund_blocked(self):
        result = self.review(
            material("m", "余额宝保本保息，刚性兑付",
                     category=Category.ASSET_MANAGEMENT)
        )
        patterns = {
            v.matched for v in result.violations if v.rule_id == "guaranteed-return"
        }
        self.assertIn("保本保息", patterns)
        self.assertIn("刚性兑付", patterns)

    def test_first_installment_only_without_total_cost(self):
        result = self.review(
            material("m", "手机分期月供低至 99 元", category=Category.INSTALLMENT)
        )
        v = next(
            v for v in result.violations if v.rule_id == "first-installment-only"
        )
        self.assertEqual(v.matched, "月供低至")
        missing = dict(v.extra)["missing_elements"]
        self.assertIn("apr", missing)
        self.assertIn("total_fees", missing)

    def test_first_installment_allowed_when_total_cost_disclosed(self):
        mat = material(
            "m", "分期购：首期0元",
            category=Category.INSTALLMENT,
            total_cost_disclosed=True,
            fee_elements=("apr", "total_fees"),
        )
        result = self.review(mat)
        self.assertNotIn(
            "first-installment-only", {v.rule_id for v in result.violations}
        )
        # 但分期整体仍不可营销
        self.assertIn(
            "non-marketable-category", {v.rule_id for v in result.violations}
        )

    def test_borrowing_inducement_benefit_co_occurrence(self):
        result = self.review(material("m", "急用钱？借钱立减 20 元"))
        v = next(v for v in result.violations if v.rule_id == "borrowing-inducement")
        self.assertEqual(v.matched, "借钱")
        self.assertEqual(dict(v.extra)["co_occurring_benefit"], "立减")
        self.assertIn("第十条", v.clause)

    def test_repayment_surface_financial_ad_blocked(self):
        result = self.review(
            material("m", "账单分期，开通额度享好礼", surface="repayment")
        )
        surface_hits = [
            v for v in result.violations
            if v.rule_id == "payment-traffic-diversion" and v.field == "body"
        ]
        self.assertTrue(surface_hits)
        self.assertEqual(surface_hits[0].surface, "repayment")

    def test_payment_institution_diverting_financial_product_blocked(self):
        result = self.review(
            material("m", "点击查看分期服务", publisher="payment-institution")
        )
        v = next(
            v for v in result.violations
            if v.rule_id == "payment-traffic-diversion" and v.field is None
        )
        self.assertEqual(dict(v.extra)["publisher_type"], "payment-institution")

    def test_payment_institution_can_advertise_bank_card_payment(self):
        result = self.review(
            material("m", "刷工行卡支付立享积分",
                     category=Category.BANK_CARD, publisher="payment-institution")
        )
        self.assertTrue(result.approved)

    def test_credit_payable_but_not_marketable(self):
        result = self.review(
            material("m", "正常的消费信贷产品介绍文案，不含任何诱导词")
        )
        v = next(v for v in result.violations if v.rule_id == "non-marketable-category")
        self.assertEqual(dict(v.extra)["payment_allowed"], "True")
        self.assertEqual(dict(v.extra)["marketable"], "False")

    def test_clean_bank_card_copy_approved(self):
        result = self.review(
            material("m", "工商银行储蓄卡支付安全便捷", category=Category.BANK_CARD)
        )
        self.assertTrue(result.approved)


class MarketingPolicyVersionTest(unittest.TestCase):
    def setUp(self):
        self.app = load_app()

    def test_review_uses_effective_policy_at_submission_time(self):
        # 9-29 提交：旧规仅启用禁用词/保本规则，诱导借款不阻断
        mat = material("old", "借钱立减 20 元")
        old = self.app.reviewer.review(mat, region="BJ", moment=cst(2026, 9, 29))
        self.assertEqual(old.policy_version, 1)
        self.assertTrue(old.approved)
        # 同一文案 10-20 提交：新规阻断
        new = self.app.reviewer.review(mat, region="BJ", moment=cst(2026, 10, 20))
        self.assertEqual(new.policy_version, 3)
        self.assertFalse(new.approved)

    def test_explicit_policy_version_review(self):
        mat = material("fix", "月供低至 99 元", category=Category.INSTALLMENT)
        old = self.app.reviewer.review(
            mat, region="BJ", moment=cst(2026, 10, 20), policy_version=1
        )
        # v1 未启用首期费用规则、且分期可营销 → 通过
        self.assertTrue(old.approved)
        new = self.app.reviewer.review(
            mat, region="BJ", moment=cst(2026, 10, 20), policy_version=3
        )
        self.assertFalse(new.approved)

    def test_review_ledger_is_append_only(self):
        mat = material("ledger", "保本")
        first = self.app.reviewer.review(mat, region="BJ", moment=cst(2026, 10, 20))
        self.app.reviewer.review(mat, region="BJ", moment=cst(2026, 10, 21))
        records = self.app.reviewer.ledger.for_material("ledger")
        self.assertEqual(len(records), 2)
        self.assertEqual([r.policy_version for r in records], [3, 3])
        self.assertFalse(first.approved)

    def test_violation_serialization_is_concrete(self):
        result = self.app.reviewer.review(
            material("json", "低风险", category=Category.ASSET_MANAGEMENT),
            region="BJ",
            moment=cst(2026, 10, 20),
        )
        payload = result.to_json()
        self.assertEqual(payload["decision"], "blocked")
        v = next(
            x for x in payload["violations"]
            if x["rule_id"] == "prohibited-phrase"
        )
        self.assertTrue(v["clause"])
        self.assertIsNotNone(v["position"])
        self.assertEqual(v["matched"], "低风险")


if __name__ == "__main__":
    unittest.main()
