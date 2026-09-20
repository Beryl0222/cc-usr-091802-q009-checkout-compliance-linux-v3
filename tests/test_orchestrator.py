"""收银台方案编排：分组/顺序/风险信息/可选状态/禁止预选/缓存失效。"""

import unittest

from checkout.app import load_app
from checkout.models import cst


class CheckoutLayoutTest(unittest.TestCase):
    def setUp(self):
        self.app = load_app()

    def plan(self, region="BJ", mcc="5812", moment=None, unit=None):
        return self.app.orchestrator.build_plan(
            region=region,
            mcc=mcc,
            moment=moment or cst(2026, 10, 20, 12),
            canary_unit=unit,
            use_cache=False,
        )

    def test_old_policy_mixed_payment_group(self):
        plan = self.app.orchestrator.build_plan(
            region="BJ", mcc="5812", moment=cst(2026, 9, 29), use_cache=False
        )
        keys = [g.key for g in plan.groups]
        self.assertEqual(keys, ["payment", "credit-finance", "wealth"])
        payment_cats = {
            o.source.category.value
            for g in plan.groups if g.key == "payment" for o in g.options
        }
        self.assertEqual(payment_cats, {"bank-card", "payment-tool"})

    def test_new_policy_each_financial_category_is_its_own_group(self):
        plan = self.plan()
        groups = {g.key: g for g in plan.groups}
        # 银行卡与余额也是独立组，但更重要的是信贷/资管/分期互不混排
        self.assertEqual(
            sorted(groups),
            [
                "asset-management",
                "bank-card",
                "credit",
                "installment",
                "payment-tool",
            ],
        )
        for key in ("credit", "asset-management", "installment"):
            cats = {o.source.category.value for o in groups[key].options}
            self.assertEqual(cats, {key})

    def test_groups_follow_policy_order(self):
        plan = self.plan()
        self.assertEqual(
            [g.order for g in plan.groups],
            sorted(g.order for g in plan.groups),
        )

    def test_financial_groups_carry_required_risk_disclosures(self):
        plan = self.plan()
        groups = {g.key: g for g in plan.groups}
        self.assertTrue(groups["credit"].risk_disclosures)
        self.assertTrue(groups["asset-management"].risk_disclosures)
        self.assertTrue(groups["installment"].risk_disclosures)
        joined = " ".join(groups["asset-management"].risk_disclosures)
        self.assertIn("不保证本金", joined)

    def test_no_option_is_ever_preselected(self):
        for moment in (cst(2026, 9, 29), cst(2026, 10, 20)):
            plan = self.app.orchestrator.build_plan(
                region="BJ", mcc="5812", moment=moment, use_cache=False
            )
            self.assertEqual(plan.to_json()["preselection"], "forbidden")
            self.assertTrue(
                all(
                    not o.selected and o.to_json()["selected"] is False
                    for g in plan.groups
                    for o in g.options
                )
            )

    def test_credit_and_moneyfund_remain_payable(self):
        # “可用于付款”：新规下信贷与货基仍出现在方案中且可选
        plan = self.plan()
        ids = {
            o.source.source_id for g in plan.groups for o in g.options if o.selectable
        }
        self.assertIn("credit-zhaolian", ids)
        self.assertIn("fund-tianhong-moneyfund", ids)

    def test_mcc_restricted_source_filtered_out(self):
        # 马上消金只开放 5311/5411/5732：餐饮商户方案里不出现
        plan = self.plan(mcc="5812")
        ids = {o.source.source_id for g in plan.groups for o in g.options}
        self.assertNotIn("credit-mashang", ids)
        plan = self.plan(mcc="5411")
        ids = {o.source.source_id for g in plan.groups for o in g.options}
        self.assertIn("credit-mashang", ids)

    def test_frozen_source_shown_unavailable_with_reason(self):
        self.app.catalog.freeze("credit-zhaolian", "风控临时冻结")
        plan = self.plan()
        option = plan.find_option("credit-zhaolian")
        self.assertFalse(option.selectable)
        self.assertEqual(option.to_json()["state"], "unavailable")
        self.assertEqual(
            option.to_json()["unavailable_reason"]["reason_code"], "frozen"
        )

    def test_plan_records_policy_source(self):
        plan = self.plan()
        self.assertEqual(
            plan.to_json()["policy_source"],
            {"policy_id": "financial-marketing", "version": 3, "mode": "full",
             "window": plan.policy_window},
        )

    def test_canary_layout_differs_from_full(self):
        canary = self.app.orchestrator.build_plan(
            region="SH", mcc="5812", moment=cst(2026, 10, 1),
            canary_unit="u8", use_cache=False,
        )
        fallback = self.app.orchestrator.build_plan(
            region="SH", mcc="5812", moment=cst(2026, 10, 1),
            canary_unit="u1", use_cache=False,
        )
        self.assertEqual(canary.policy_version, 2)
        self.assertEqual(fallback.policy_version, 1)
        self.assertNotEqual(
            [g.key for g in canary.groups], [g.key for g in fallback.groups]
        )


class PlanCacheExpiryTest(unittest.TestCase):
    def _app_with_timer(self):
        clock_value = [cst(2026, 9, 29, 23, 59, 50)]

        def clock():
            return clock_value[0]

        monotonic = [0.0]

        def timer():
            return monotonic[0]

        app = load_app(clock=clock)
        # 替换编排器为可控 timer
        from checkout.orchestrator import CheckoutOrchestrator

        app.orchestrator = CheckoutOrchestrator(
            app.policies, app.catalog, app.policy_id,
            ttl_seconds=30.0, clock=clock, timer=timer,
        )
        return app, clock_value, monotonic

    def test_cached_plan_expires_after_ttl(self):
        app, clock_value, monotonic = self._app_with_timer()
        plan1 = app.orchestrator.build_plan(region="BJ", mcc="5812")
        self.assertEqual(plan1.policy_version, 1)
        # TTL 内命中缓存
        clock_value[0] = cst(2026, 9, 29, 23, 59, 59)
        monotonic[0] = 5.0
        cached = app.orchestrator.build_plan(region="BJ", mcc="5812")
        self.assertIs(cached, plan1)
        # TTL 到期后重算（旧方案不超期服役）
        monotonic[0] = 31.0
        again = app.orchestrator.build_plan(region="BJ", mcc="5812")
        self.assertIsNot(again, plan1)

    def test_cached_plan_invalidated_by_freeze(self):
        app, clock_value, monotonic = self._app_with_timer()
        plan1 = app.orchestrator.build_plan(region="BJ", mcc="5812")
        app.catalog.freeze("bank-card-icbc", "测试冻结")
        monotonic[0] = 1.0
        plan2 = app.orchestrator.build_plan(region="BJ", mcc="5812")
        self.assertIsNot(plan1, plan2)
        option = plan2.find_option("bank-card-icbc")
        self.assertFalse(option.selectable)

    def test_plan_cannot_serve_past_window_boundary(self):
        # 构造一个资金源 10 秒后到期的场景，验证边界截断 TTL
        app, clock_value, monotonic = self._app_with_timer()
        from checkout.catalog import FundingSource, Provider
        from checkout.models import Category, Qualification, TimeWindow

        cf = Provider("pv-cf", "测试消金", Qualification.CONSUMER_FINANCE, "X-TEST")
        app.catalog.register_provider(cf)
        app.catalog.register_source(
            FundingSource(
                source_id="short-lived-credit",
                display_name="短期信贷",
                category=Category.CREDIT,
                provider=cf,
                window=TimeWindow(cst(2020, 1, 1), cst(2026, 9, 29, 23, 59, 55)),
                regions=frozenset({"CN"}),
                risk_notes=("借款需谨慎",),
            )
        )
        plan = app.orchestrator.build_plan(region="BJ", mcc="5812")
        self.assertIn("short-lived-credit", plan.source_ids)
        # 5 秒后资金源窗口闭合，缓存必须已失效并重算
        clock_value[0] = cst(2026, 9, 29, 23, 59, 56)
        monotonic[0] = 6.0
        plan2 = app.orchestrator.build_plan(region="BJ", mcc="5812")
        self.assertNotIn("short-lived-credit", plan2.source_ids)


if __name__ == "__main__":
    unittest.main()
