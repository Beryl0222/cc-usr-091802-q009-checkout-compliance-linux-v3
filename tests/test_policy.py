"""政策发布：区间不重叠、并发 CAS、灰度必须带地区与生效日、任意时刻唯一版本。"""

import threading
import unittest

from checkout.policy import (
    ConcurrentPublish, GrayConfig, InvalidGrayConfig, OverlappingPolicyWindow,
    PolicyChain, PolicyVersion,
)
from checkout.marketing import RULESET_V1, RULESET_V2

CHAIN = "test-chain"


def _policy(version, frm, until=None, grays=()):
    return PolicyVersion(
        chain_id=CHAIN, version=version, effective_from=frm, effective_until=until,
        legal_source=f"src-{version}", marketing_ruleset=RULESET_V1.ruleset_id, grays=grays)


class PolicyPublishTest(unittest.TestCase):
    def test_overlapping_windows_rejected(self):
        chain = PolicyChain(CHAIN)
        token = chain.head_token()
        chain.publish(_policy("v1", "2025-01-01T00:00:00+08:00",
                              "2026-09-30T00:00:00+08:00"), token)
        with self.assertRaises(OverlappingPolicyWindow):
            chain.publish(_policy("v2-bad", "2026-09-29T00:00:00+08:00"),
                          chain.head_token())

    def test_exactly_one_effective_version_across_boundary(self):
        chain = PolicyChain(CHAIN)
        t = chain.head_token()
        chain.publish(_policy("v1", "2025-09-30T00:00:00+08:00",
                              "2026-09-30T00:00:00+08:00"), t)
        chain.publish(_policy("v2", "2026-09-30T00:00:00+08:00"), chain.head_token())
        self.assertEqual(chain.effective_policy("2026-09-29T23:59:59+08:00").version, "v1")
        # 边界点左闭右开：00:00 整已经是新版
        self.assertEqual(chain.effective_policy("2026-09-30T00:00:00+08:00").version, "v2")
        self.assertEqual(chain.effective_policy("2026-10-01T00:00:00+08:00").version, "v2")

    def test_stale_token_concurrent_publish(self):
        chain = PolicyChain(CHAIN)
        stale = chain.head_token()
        chain.publish(_policy("v1", "2025-01-01T00:00:00+08:00",
                              "2026-09-30T00:00:00+08:00"), stale)
        # 另一个发布者持有同一旧令牌，竞争失败
        with self.assertRaises(ConcurrentPublish):
            chain.publish(_policy("v2-race", "2026-09-30T00:00:00+08:00"), stale)
        # 刷新令牌后可正常发布
        chain.publish(_policy("v2", "2026-09-30T00:00:00+08:00"), chain.head_token())

    def test_concurrent_publish_only_one_wins(self):
        chain = PolicyChain(CHAIN)
        chain.publish(_policy("v1", "2025-01-01T00:00:00+08:00",
                              "2026-09-30T00:00:00+08:00"), chain.head_token())
        token = chain.head_token()  # 两个线程都基于同一链尾令牌竞争后继位置
        outcomes = []

        def publish(name):
            try:
                chain.publish(
                    PolicyVersion(
                        chain_id=CHAIN, version=name,
                        effective_from="2026-09-30T00:00:00+08:00", effective_until=None,
                        legal_source=name, marketing_ruleset=RULESET_V2.ruleset_id),
                    token)
                outcomes.append(("ok", name))
            except ConcurrentPublish:
                outcomes.append(("lost", name))

        threads = [threading.Thread(target=publish, args=(f"v{i}",)) for i in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        winners = [n for state, n in outcomes if state == "ok"]
        self.assertEqual(len(winners), 1, outcomes)
        self.assertEqual(chain.effective_policy("2026-10-01T00:00:00+08:00").version,
                         winners[0])

    def test_gray_requires_region_and_date(self):
        with self.assertRaises(InvalidGrayConfig):
            GrayConfig(feature="f", regions=frozenset(),
                       effective_from="2026-09-30T00:00:00+08:00", bucket_percent=50)
        with self.assertRaises(InvalidGrayConfig):
            GrayConfig(feature="f", regions=frozenset({"CN-SH"}),
                       effective_from="", bucket_percent=50)

    def test_gray_cannot_leave_version_window_or_region(self):
        gray = GrayConfig(feature="f", regions=frozenset({"CN-SH"}),
                          effective_from="2026-09-30T00:00:00+08:00", bucket_percent=100)
        policy = _policy("v2", "2026-09-30T00:00:00+08:00", grays=(gray,))
        # 生效日前
        self.assertFalse(policy.feature_on("f", "CN-SH",
                                           "2026-09-29T23:59:59+08:00", "u1"))
        # 地区外
        self.assertFalse(policy.feature_on("f", "CN-GD",
                                           "2026-10-01T00:00:00+08:00", "u1"))
        # 命中
        self.assertTrue(policy.feature_on("f", "CN-SH",
                                          "2026-10-01T00:00:00+08:00", "u1"))

    def test_gray_date_must_lie_inside_version_window(self):
        gray = GrayConfig(feature="f", regions=frozenset({"CN-SH"}),
                          effective_from="2027-01-01T00:00:00+08:00", bucket_percent=100)
        chain = PolicyChain(CHAIN)
        with self.assertRaises(InvalidGrayConfig):
            chain.publish(
                _policy("v1", "2026-09-30T00:00:00+08:00",
                        "2026-12-31T00:00:00+08:00", grays=(gray,)),
                chain.head_token())

    def test_server_preselection_policy_never_publishable(self):
        bad = PolicyVersion(
            chain_id=CHAIN, version="bad", effective_from="2026-01-01T00:00:00+08:00",
            effective_until=None, legal_source="x",
            marketing_ruleset=RULESET_V1.ruleset_id,
            allow_server_preselection=True)
        with self.assertRaises(ValueError):
            PolicyChain(CHAIN).publish(bad)


if __name__ == "__main__":
    unittest.main()
