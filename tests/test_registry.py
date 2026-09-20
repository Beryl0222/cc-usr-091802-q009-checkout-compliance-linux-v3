"""登记处：生效窗口、资质到期、商户/地区范围、错误分类更正留痕。"""

import unittest

from checkout.models import Category, FundingSource, License, MarketRole, CategoryCorrection
from checkout.registry import DuplicateRegistration, Registry

LICENSE = License("L-1", "金融许可证", "监管机关", "2020-01-01T00:00:00+08:00")
EXPIRED_LICENSE = License(
    "L-2", "金融许可证", "监管机关",
    "2020-01-01T00:00:00+08:00", "2026-09-01T00:00:00+08:00",
)


def _source(**over):
    base = dict(
        source_id="s1", display_name="测试渠道", legal_category=Category.CREDIT,
        provider_id="p1", provider_name="提供方", provider_role=MarketRole.CONSUMER_FINANCE,
        license=LICENSE, effective_from="2026-01-01T00:00:00+08:00",
    )
    base.update(over)
    return FundingSource(**base)


class RegistryTest(unittest.TestCase):
    def test_duplicate_id_rejected(self):
        reg = Registry()
        reg.register(_source())
        with self.assertRaises(DuplicateRegistration):
            reg.register(_source())

    def test_effective_window_left_closed_right_open(self):
        reg = Registry()
        reg.register(_source(effective_from="2026-09-30T00:00:00+08:00",
                             effective_until="2026-10-31T00:00:00+08:00"))
        self.assertTrue(reg.get("s1").effective_at("2026-09-30T00:00:00+08:00"))
        self.assertTrue(reg.get("s1").effective_at("2026-10-30T23:59:59+08:00"))
        self.assertFalse(reg.get("s1").effective_at("2026-10-31T00:00:00+08:00"))

    def test_license_expiry_blocks_use(self):
        reg = Registry()
        reg.register(_source(source_id="exp", license=EXPIRED_LICENSE))
        source = reg.get("exp")
        ok, reason = source.usable_for("m1", "CN-GD", "2026-09-29T12:00:00+08:00")
        self.assertFalse(ok)
        self.assertEqual(reason, "license-invalid-or-expired")

    def test_merchant_and_region_scope(self):
        reg = Registry()
        reg.register(_source(
            merchant_scope=frozenset({"m1"}), region_scope=frozenset({"CN-SH"})))
        ok, reason = reg.get("s1").usable_for("m2", "CN-SH", "2026-06-01T00:00:00+08:00")
        self.assertFalse(ok)
        self.assertEqual(reason, "merchant-out-of-scope")
        ok, reason = reg.get("s1").usable_for("m1", "CN-GD", "2026-06-01T00:00:00+08:00")
        self.assertEqual(reason, "region-out-of-scope")
        ok, reason = reg.get("s1").usable_for("m1", "CN-SH", "2026-06-01T00:00:00+08:00")
        self.assertTrue(ok)

    def test_category_correction_keeps_history_and_bumps_revision(self):
        reg = Registry()
        reg.register(_source(legal_category=Category.PAYMENT_BALANCE))
        before = reg.revision
        correction = CategoryCorrection(
            source_id="s1", claimed=Category.PAYMENT_BALANCE, corrected=Category.CREDIT,
            reason="实质为消费信贷", at="2026-09-29T23:40:00+08:00")
        reg.correct_category(correction)
        self.assertIs(reg.get("s1").legal_category, Category.CREDIT)
        self.assertEqual(reg.get("s1").corrections[-1].claimed, Category.PAYMENT_BALANCE)
        self.assertEqual(reg.get("s1").corrections[-1].reason, "实质为消费信贷")
        self.assertGreater(reg.revision, before)


if __name__ == "__main__":
    unittest.main()
