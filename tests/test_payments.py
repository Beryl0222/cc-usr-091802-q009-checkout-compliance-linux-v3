"""显式选择、扣款与重试：无选择不扣款、失败不替换渠道、重试重新核验。"""

import unittest

from checkout.demo import build_world
from checkout.orchestrator import CheckoutRequest
from checkout.payments import PaymentError
from checkout.models import Category, FundingSource, License, MarketRole


class PaymentRetryTest(unittest.TestCase):
    def setUp(self):
        self.w = build_world(corrected=True)
        self.plan = self.w.orchestrator.build_plan(CheckoutRequest(
            merchant_id="m1001", region="CN-GD", subject_key="u9",
            amount="368.00", at="2026-09-30T10:00:00+08:00", checkout_id="P1"))

    def _select(self, source_id="icbc-debit", at="2026-09-30T10:01:00+08:00"):
        return self.w.payments.record_selection(
            checkout_id="P1", source_id=source_id, merchant_id="m1001",
            region="CN-GD", plan=self.plan, explicit=True, at=at)

    def test_charge_requires_explicit_selection(self):
        with self.assertRaises(PaymentError) as ctx:
            self.w.payments.record_selection(
                checkout_id="P1", source_id="icbc-debit", merchant_id="m1001",
                region="CN-GD", plan=self.plan, explicit=False,
                at="2026-09-30T10:01:00+08:00")
        self.assertEqual(ctx.exception.code, "selection-not-explicit")

    def test_cannot_select_source_not_in_plan(self):
        with self.assertRaises(PaymentError) as ctx:
            self.w.payments.record_selection(
                checkout_id="P1", source_id="not-exist", merchant_id="m1001",
                region="CN-GD", plan=self.plan, explicit=True,
                at="2026-09-30T10:01:00+08:00")
        self.assertEqual(ctx.exception.code, "source-not-presented")

    def test_unknown_token_rejected(self):
        with self.assertRaises(PaymentError) as ctx:
            self.w.payments.charge("bogus", "1.00", "2026-09-30T10:02:00+08:00")
        self.assertEqual(ctx.exception.code, "unknown-selection-token")

    def test_retry_keeps_channel_and_succeeds_after_timeout(self):
        # cf-credit 的模拟网关：首次超时，重试成功
        selection = self.w.payments.record_selection(
            checkout_id="P1", source_id="cf-credit", merchant_id="m1001",
            region="CN-GD", plan=self.plan, explicit=True,
            at="2026-09-30T10:01:00+08:00")
        with self.assertRaises(PaymentError) as ctx:
            self.w.payments.charge(selection.token, "368.00",
                                   "2026-09-30T10:02:00+08:00", channel_ref="CH-1")
        self.assertEqual(ctx.exception.code, "channel-timeout")
        retry = self.w.payments.retry(selection.token, "368.00",
                                      "2026-09-30T10:05:00+08:00", channel_ref="CH-2")
        self.assertEqual(retry["status"], "succeeded")
        self.assertEqual(retry["source_id"], "cf-credit")  # 仍是用户选择的同一渠道
        self.assertEqual(retry["attempt"], 2)
        # 已结清不得重复扣款
        with self.assertRaises(PaymentError) as ctx:
            self.w.payments.charge(selection.token, "368.00",
                                   "2026-09-30T10:06:00+08:00")
        self.assertEqual(ctx.exception.code, "already-settled")

    def test_retry_reverifies_and_refuses_when_channel_unusable(self):
        # 构造一个生效区间即将结束的资金源：首次扣款落在窗外失败，
        reg = self.w.registry
        reg.register(FundingSource(
            source_id="flash-credit", display_name="临时期白",
            legal_category=Category.CREDIT,
            provider_id="cfco", provider_name="普惠消费金融",
            provider_role=MarketRole.CONSUMER_FINANCE,
            license=License("CF-XFJ-2024-03", "消费金融牌照", "监管机关",
                            "2024-03-01T00:00:00+08:00"),
            effective_from="2026-09-01T00:00:00+08:00",
            effective_until="2026-09-30T12:00:00+08:00",
            risk_notice="信贷有成本",
        ))
        early_plan = self.w.orchestrator.build_plan(CheckoutRequest(
            merchant_id="m1001", region="CN-GD", subject_key="u9",
            amount="50.00", at="2026-09-30T11:59:00+08:00", checkout_id="P2"))
        sel = self.w.payments.record_selection(
            checkout_id="P2", source_id="flash-credit", merchant_id="m1001",
            region="CN-GD", plan=early_plan, explicit=True,
            at="2026-09-30T11:59:10+08:00")
        # 模拟首次渠道失败（不置 settled）：直接调用内部网关外的失败路径
        with self.assertRaises(PaymentError):
            # flash-credit 走默认网关本应成功；这里通过把首次调用推到窗口外实现
            self.w.payments.charge(sel.token, "50.00",
                                   "2026-09-30T12:00:30+08:00")
        # 选择令牌仍未结清；继续沿用同一渠道重试 → 重新核验拒绝，且不替换
        with self.assertRaises(PaymentError) as ctx:
            self.w.payments.retry(sel.token, "50.00",
                                  "2026-09-30T12:05:00+08:00")
        self.assertEqual(ctx.exception.code, "outside-effective-window")
        charges = [e for e in self.w.audit.events()
                   if e.payload.get("checkout_id") == "P2"]
        failed = [e for e in charges if e.event_type == "payment.failed"]
        self.assertTrue(failed)
        self.assertIsNone(failed[-1].payload["substituted_source"])

    def test_category_correction_recorded_on_charge(self):
        w = build_world(corrected=False)
        plan = w.orchestrator.build_plan(CheckoutRequest(
            merchant_id="m1001", region="CN-GD", subject_key="u9",
            amount="368.00", at="2026-09-29T22:15:00+08:00", checkout_id="P3"))
        sel = w.payments.record_selection(
            checkout_id="P3", source_id="biz-paylater", merchant_id="m1001",
            region="CN-GD", plan=plan, explicit=True,
            at="2026-09-29T22:16:00+08:00")
        # 此时 biz-paylater 被当作 payment-tool 呈现；之后更正分类
        from checkout.models import CategoryCorrection
        w.registry.correct_category(CategoryCorrection(
            source_id="biz-paylater", claimed=Category.PAYMENT_BALANCE,
            corrected=Category.CREDIT, reason="实质为消费信贷",
            at="2026-09-29T23:40:00+08:00"))
        result = w.payments.charge(sel.token, "368.00",
                                   "2026-09-30T09:00:00+08:00")
        self.assertEqual(result["source_id"], "biz-paylater")  # 仍是用户选的渠道
        notes = [e for e in w.audit.events(event_type="source.category-corrected-before-charge")
                 if e.payload.get("checkout_id") == "P3"]
        self.assertEqual(notes[0].payload["authoritative_category_now"], "credit")


if __name__ == "__main__":
    unittest.main()
