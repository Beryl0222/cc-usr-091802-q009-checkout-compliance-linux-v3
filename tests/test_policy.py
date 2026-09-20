"""政策版本存储：生效区间、地区、灰度与并发发布唯一有效版本。"""

import threading
import unittest

from checkout.models import Category, ComplianceError, TimeWindow, cst
from checkout.policy import (
    CategoryRule,
    GroupSpec,
    MarketingRule,
    PolicyConflict,
    PolicyStore,
    PolicyVersion,
    policy_from_dict,
)

EMPTY_MARKETING = MarketingRule(
    prohibited_phrases=frozenset(),
    guaranteed_return_patterns=frozenset(),
    first_installment_only_patterns=frozenset(),
    forbidden_traffic_surfaces=frozenset(),
    financial_keywords=frozenset(),
    enabled_rules=frozenset(),
    clause_refs={},
)


def make_policy(
    version: int,
    regions=("*",),
    start=cst(2020, 1, 1),
    end=None,
    canary=100,
    policy_id="financial-marketing",
    parent=None,
):
    return PolicyVersion(
        policy_id=policy_id,
        version=version,
        regions=frozenset(regions),
        window=TimeWindow(start, end),
        group_specs=(
            GroupSpec("payment", "支付", (Category.BANK_CARD,), 0),
            GroupSpec("credit", "信贷", (Category.CREDIT,), 1),
        ),
        category_rules=(
            CategoryRule(
                Category.BANK_CARD, "银行卡", True, True, (), "payment", 0, True
            ),
            CategoryRule(
                Category.CREDIT,
                "消费信贷",
                True,
                True,
                ("借款有风险",),
                "credit",
                0,
                False,
            ),
        ),
        marketing=EMPTY_MARKETING,
        published_at=start,
        canary_percent=canary,
        parent_version=parent,
    )


class PolicyFixtureTest(unittest.TestCase):
    """直接核对夹具表达的法规时间线。"""

    def test_fixture_versions_load(self):
        from checkout.app import load_app

        app = load_app()
        versions = app.policies.list_versions("financial-marketing")
        self.assertEqual([p.version for p in versions], [1, 2, 3])
        self.assertEqual(app.head_version(), 3)

    def test_old_rule_before_sep30_full_everywhere(self):
        from checkout.app import load_app

        app = load_app()
        r = app.policies.resolve("financial-marketing", "BJ", cst(2026, 9, 29, 23, 59))
        self.assertEqual((r.policy.version, r.mode), (1, "full"))

    def test_new_rule_after_national_rollout(self):
        from checkout.app import load_app

        app = load_app()
        for region in ("BJ", "SH", "GD"):
            r = app.policies.resolve(
                "financial-marketing", region, cst(2026, 10, 15)
            )
            self.assertEqual(r.policy.version, 3)
            self.assertEqual(r.mode, "full")

    def test_canary_shanghai_only_during_window(self):
        from checkout.app import load_app

        app = load_app()
        # 同一用户在上海命中灰度，在北京永远不会命中（地区优先）
        hit_region = set()
        for region in ("SH", "BJ"):
            r = app.policies.resolve(
                "financial-marketing", region, cst(2026, 10, 1), "u8"
            )
            hit_region.add((region, r.policy.version, r.mode))
        self.assertIn(("SH", 2, "canary"), hit_region)
        self.assertIn(("BJ", 1, "full"), hit_region)

    def test_canary_miss_falls_back_to_old_full(self):
        from checkout.app import load_app

        app = load_app()
        r = app.policies.resolve(
            "financial-marketing", "SH", cst(2026, 10, 1), "u1"
        )
        self.assertEqual((r.policy.version, r.mode), (1, "full"))

    def test_canary_does_not_apply_before_effective_date(self):
        from checkout.app import load_app

        app = load_app()
        # 9-30 00:00 之前，即使用户哈希命中，也看不到灰度版本
        r = app.policies.resolve(
            "financial-marketing", "SH", cst(2026, 9, 29, 12), "u8"
        )
        self.assertEqual(r.policy.version, 1)

    def test_credit_still_payable_but_not_marketable_under_v2(self):
        from checkout.app import load_app

        app = load_app()
        v2 = app.policies.get("financial-marketing", 2)
        for cat in (Category.CREDIT, Category.ASSET_MANAGEMENT, Category.INSTALLMENT):
            rule = v2.rule_for(cat)
            self.assertTrue(rule.payment_allowed)
            self.assertFalse(rule.marketable)
            self.assertTrue(rule.required_risk_disclosure)
        self.assertTrue(v2.rule_for(Category.BANK_CARD).marketable)


class PolicyStoreInvariantTest(unittest.TestCase):
    def test_two_full_versions_overlapping_rejected(self):
        store = PolicyStore()
        store.register(make_policy(1, end=cst(2026, 11, 1)))
        with self.assertRaises(PolicyConflict) as ctx:
            store.register(
                make_policy(2, start=cst(2026, 10, 1), end=cst(2026, 11, 1))
            )
        self.assertEqual(ctx.exception.code, "overlapping-window")

    def test_adjacent_windows_are_allowed(self):
        store = PolicyStore()
        store.register(make_policy(1, end=cst(2026, 9, 30)))
        store.register(make_policy(2, start=cst(2026, 9, 30)))
        self.assertEqual(
            store.effective("financial-marketing", "BJ", cst(2026, 9, 29)).version, 1
        )
        self.assertEqual(
            store.effective("financial-marketing", "BJ", cst(2026, 9, 30)).version, 2
        )

    def test_canary_may_overlap_full(self):
        store = PolicyStore()
        store.register(make_policy(1, end=cst(2026, 10, 15)))
        store.register(
            make_policy(2, regions=("SH",), start=cst(2026, 9, 30),
                        end=cst(2026, 10, 15), canary=20)
        )
        self.assertEqual(
            store.effective("financial-marketing", "BJ", cst(2026, 10, 1)).version, 1
        )

    def test_two_canaries_overlapping_rejected(self):
        store = PolicyStore()
        store.register(
            make_policy(2, regions=("SH",), canary=20,
                        start=cst(2026, 9, 30), end=cst(2026, 10, 15))
        )
        with self.assertRaises(PolicyConflict):
            store.register(
                make_policy(3, regions=("SH",), canary=50,
                            start=cst(2026, 10, 1), end=cst(2026, 10, 15))
            )

    def test_financial_product_requires_disclosure(self):
        bad = PolicyVersion(
            policy_id="financial-marketing",
            version=1,
            regions=frozenset({"*"}),
            window=TimeWindow(cst(2026, 9, 30)),
            group_specs=(GroupSpec("credit", "信贷", (Category.CREDIT,), 0),),
            category_rules=(
                CategoryRule(
                    Category.CREDIT, "信贷", True, True, (), "credit", 0, False
                ),
            ),
            marketing=EMPTY_MARKETING,
            published_at=cst(2026, 9, 1),
        )
        with self.assertRaises(ComplianceError) as ctx:
            PolicyStore().register(bad)
        self.assertEqual(ctx.exception.code, "missing-disclosure")


class ConcurrentPublishTest(unittest.TestCase):
    def test_cas_publish_only_one_winner(self):
        store = PolicyStore()
        v1 = make_policy(1, end=cst(2026, 10, 15))
        store.publish(v1, None)

        v2a = make_policy(2, start=cst(2026, 10, 15))
        v2b = make_policy(2, start=cst(2026, 10, 15), parent=1)

        results = []

        def publish(policy):
            try:
                store.publish(policy, expected_version=1)
                results.append("ok")
            except PolicyConflict as exc:
                results.append(exc.code)

        t1 = threading.Thread(target=publish, args=(v2a,))
        t2 = threading.Thread(target=publish, args=(v2b,))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(sorted(results), ["ok", "policy-conflict"])
        self.assertEqual(store.head("financial-marketing"), 2)

    def test_stale_cas_cannot_publish_version_skipping(self):
        store = PolicyStore()
        store.publish(make_policy(1, end=cst(2026, 10, 15)), None)
        store.publish(make_policy(2, start=cst(2026, 10, 15)), 1)
        with self.assertRaises(PolicyConflict):
            store.publish(
                make_policy(3, start=cst(2026, 10, 20)), expected_version=1
            )

    def test_concurrent_promote_single_effective_full(self):
        store = PolicyStore()
        store.register(make_policy(1, end=cst(2026, 10, 15)))
        store.register(
            make_policy(2, regions=("SH",), canary=20,
                        start=cst(2026, 9, 30), end=cst(2026, 10, 15), parent=1)
        )
        # 第一个提升成功：SH 地区由新全量遮蔽旧全量，其他地区仍是 v1
        promoted = store.promote("financial-marketing", 2, expected_canary_percent=20)
        self.assertEqual(promoted.canary_percent, 100)
        self.assertTrue(promoted.promoted)
        # 提升后 SH 唯一生效版本是 v2，其他地区仍为 v1
        self.assertEqual(
            store.effective("financial-marketing", "SH", cst(2026, 10, 1)).version, 2
        )
        self.assertEqual(
            store.effective("financial-marketing", "BJ", cst(2026, 10, 1)).version, 1
        )
        # 并发的第二个提升（基于旧灰度比例）冲突；重复提升同样拒绝
        with self.assertRaises(PolicyConflict):
            store.promote("financial-marketing", 2, expected_canary_percent=20)
        with self.assertRaises(PolicyConflict):
            store.promote("financial-marketing", 2)

    def test_promote_to_national_region_rejected_without_handoff(self):
        # 灰度只在 SH，却试图把一个覆盖全国（*）的提升与旧全国全量重叠：
        # 必须显式满足地区子集关系；同地区（* vs *）且非父子则拒绝
        store = PolicyStore()
        store.register(make_policy(1, end=cst(2026, 10, 15)))
        store.register(
            make_policy(2, regions=("SH",), canary=20,
                        start=cst(2026, 9, 30), end=cst(2026, 10, 15), parent=1)
        )
        # 手工制造一个“假提升”：promoted 但 parent 不匹配，必须被拒绝
        from dataclasses import replace

        fake = replace(
            store.get("financial-marketing", 2),
            regions=frozenset({"*"}),
            canary_percent=100,
            promoted=True,
            parent_version=99,  # 父版本不存在
        )
        with self.assertRaises(PolicyConflict):
            store.register(fake)

    def test_effective_always_single_version(self):
        store = PolicyStore()
        store.register(make_policy(1, end=cst(2026, 10, 15)))
        store.register(
            make_policy(2, regions=("SH",), canary=20,
                        start=cst(2026, 9, 30), end=cst(2026, 10, 15))
        )
        # 全量基线在任何时刻都只返回一个版本
        for day in (29, 30):
            store.effective("financial-marketing", "SH", cst(2026, 9, day))
        store.effective("financial-marketing", "SH", cst(2026, 10, 1))


class PolicyFromDictTest(unittest.TestCase):
    def test_enabled_rules_default_is_all(self):
        minimal = {
            "policy_id": "p", "version": 1, "regions": ["*"],
            "effective_from": "2026-09-30T00:00:00+08:00",
            "groups": [], "categories": [], "marketing": {},
        }
        policy = policy_from_dict(minimal)
        self.assertIn("payment-traffic-diversion", policy.marketing.enabled_rules)


if __name__ == "__main__":
    unittest.main()
