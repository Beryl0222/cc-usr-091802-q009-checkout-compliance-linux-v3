"""资金源目录：资质校验、商户/地区/生效区间与可用性重新核验。"""

import unittest

from checkout.catalog import (
    Catalog,
    FundingSource,
    Provider,
)
from checkout.models import (
    Category,
    ComplianceError,
    Qualification,
    TimeWindow,
    cst,
)


def provider(qid="pv-bank", q=Qualification.BANK, valid_until=None):
    return Provider(qid, "测试机构", q, "LICENSE-0001", valid_until)


def source(
    sid="s-card",
    category=Category.BANK_CARD,
    *,
    prov=None,
    regions=frozenset({"CN"}),
    mccs=frozenset(),
    start=cst(2020, 1, 1),
    end=None,
):
    return FundingSource(
        source_id=sid,
        display_name=sid,
        category=category,
        provider=prov or provider(),
        window=TimeWindow(start, end),
        regions=regions,
        merchant_mccs=mccs,
        risk_notes=("风险提示",) if category.is_financial_product else (),
    )


class CatalogRegistrationTest(unittest.TestCase):
    def test_qualification_must_match_category(self):
        catalog = Catalog()
        catalog.register_provider(provider(q=Qualification.BANK))
        bad = source(
            "s-credit", Category.CREDIT,
            prov=provider(q=Qualification.BANK),
        )
        catalog.register_source(bad)  # 银行可以发信贷，合法

        catalog.register_provider(provider("pv-fund", Qualification.FUND_MANAGER))
        with self.assertRaises(ComplianceError) as ctx:
            catalog.register_source(
                source("s-bad", Category.CREDIT,
                       prov=provider("pv-fund", Qualification.FUND_MANAGER))
            )
        self.assertEqual(ctx.exception.code, "qualification-mismatch")

    def test_payment_institution_cannot_issue_financial_product(self):
        catalog = Catalog()
        catalog.register_provider(provider("pv-pay", Qualification.PAYMENT_INSTITUTION))
        with self.assertRaises(ComplianceError) as ctx:
            catalog.register_source(
                source("s-credit", Category.CREDIT,
                       prov=provider("pv-pay", Qualification.PAYMENT_INSTITUTION))
            )
        self.assertEqual(ctx.exception.code, "payment-institution-issuer")
        # 但支付机构的余额工具合法
        catalog.register_source(
            source("s-balance", Category.PAYMENT_TOOL,
                   prov=provider("pv-pay", Qualification.PAYMENT_INSTITUTION))
        )

    def test_unknown_provider_rejected(self):
        with self.assertRaises(ComplianceError) as ctx:
            Catalog().register_source(source())
        self.assertEqual(ctx.exception.code, "unknown-provider")

    def test_bad_window_rejected(self):
        catalog = Catalog()
        catalog.register_provider(provider())
        with self.assertRaises(ComplianceError) as ctx:
            catalog.register_source(
                source(end=cst(2019, 1, 1))
            )
        self.assertEqual(ctx.exception.code, "bad-window")


class AvailabilityRecheckTest(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog()
        self.catalog.register_provider(provider())
        self.catalog.register_provider(
            provider("pv-pay", Qualification.PAYMENT_INSTITUTION)
        )
        self.catalog.register_source(source("card"))
        self.catalog.register_source(
            source(
                "mcc-limited",
                Category.PAYMENT_TOOL,
                prov=provider("pv-pay", Qualification.PAYMENT_INSTITUTION),
                mccs=frozenset({"5411"}),
            )
        )
        self.catalog.register_source(
            source("sh-only", regions=frozenset({"SH"}))
        )
        self.catalog.register_source(
            source("expiring", end=cst(2026, 9, 30))
        )

    def test_region_national_and_provincial(self):
        self.assertTrue(
            self.catalog.check_availability(
                "card", region="SH", mcc="0000", moment=cst(2026, 9, 29)
            ).available
        )
        self.assertFalse(
            self.catalog.check_availability(
                "sh-only", region="BJ", mcc="0000", moment=cst(2026, 9, 29)
            ).available
        )

    def test_mcc_whitelist(self):
        ok = self.catalog.check_availability(
            "mcc-limited", region="BJ", mcc="5411", moment=cst(2026, 9, 29)
        )
        bad = self.catalog.check_availability(
            "mcc-limited", region="BJ", mcc="5812", moment=cst(2026, 9, 29)
        )
        self.assertTrue(ok.available)
        self.assertFalse(bad.available)
        self.assertEqual(bad.reason_code, "merchant-unsupported")

    def test_window_boundary_is_half_open(self):
        # [start, end)：9-30 00:00 起不可用
        self.assertTrue(
            self.catalog.check_availability(
                "expiring", region="BJ", mcc="0000", moment=cst(2026, 9, 29, 23, 59)
            ).available
        )
        result = self.catalog.check_availability(
            "expiring", region="BJ", mcc="0000", moment=cst(2026, 9, 30)
        )
        self.assertFalse(result.available)
        self.assertEqual(result.reason_code, "out-of-window")

    def test_freeze_and_disable(self):
        self.catalog.freeze("card", "监管约谈")
        result = self.catalog.check_availability(
            "card", region="BJ", mcc="0000", moment=cst(2026, 9, 29)
        )
        self.assertFalse(result.available)
        self.assertEqual(result.reason_code, "frozen")
        self.catalog.unfreeze("card")
        self.assertTrue(
            self.catalog.check_availability(
                "card", region="BJ", mcc="0000", moment=cst(2026, 9, 29)
            ).available
        )
        self.catalog.set_enabled("card", False)
        self.assertEqual(
            self.catalog.check_availability(
                "card", region="BJ", mcc="0000", moment=cst(2026, 9, 29)
            ).reason_code,
            "disabled",
        )

    def test_provider_license_expiry(self):
        catalog = Catalog()
        bank = provider(valid_until=cst(2026, 9, 30))
        catalog.register_provider(bank)
        catalog.register_source(source(prov=bank))
        self.assertTrue(
            catalog.check_availability(
                "s-card", region="BJ", mcc="0000", moment=cst(2026, 9, 29)
            ).available
        )
        result = catalog.check_availability(
            "s-card", region="BJ", mcc="0000", moment=cst(2026, 10, 1)
        )
        self.assertFalse(result.available)
        self.assertEqual(result.reason_code, "provider-license-expired")


if __name__ == "__main__":
    unittest.main()
