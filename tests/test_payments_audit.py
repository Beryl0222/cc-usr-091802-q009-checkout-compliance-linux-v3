"""支付：显式选源、每次重新核验、重试不换渠道；台账复原跨 9·30 回执。"""

import tempfile
import unittest
from pathlib import Path

from checkout.app import load_app
from checkout.ledger import EventLedger
from checkout.models import cst
from checkout.payments import ChargeOutcome, ScriptedGateway


def gateway(results=None, default=None):
    return ScriptedGateway(results=results, default=default)


class PaymentSelectionTest(unittest.TestCase):
    def setUp(self):
        self.gw = gateway()
        self.app = load_app(gateway=self.gw)
        self.moment = cst(2026, 10, 20, 12)
        self.plan = self.app.orchestrator.build_plan(
            region="BJ", mcc="5812", moment=self.moment
        )
        self.app.payments.present("o1", self.plan, self.moment)

    def test_cannot_charge_without_explicit_selection(self):
        from checkout.models import ComplianceError

        with self.assertRaises(ComplianceError) as ctx:
            self.app.payments.attempt(
                "o1", amount_cents=1000, region="BJ", mcc="5812",
                moment=self.moment,
            )
        self.assertEqual(ctx.exception.code, "no-explicit-selection")

    def test_cannot_select_source_not_in_presented_plan(self):
        from checkout.models import ComplianceError

        with self.assertRaises(ComplianceError) as ctx:
            self.app.payments.select(
                "o1", "credit-mashang", region="BJ", mcc="5812",
                moment=self.moment,
            )
        self.assertEqual(ctx.exception.code, "source-not-presented")

    def test_selection_rechecks_availability_at_select_time(self):
        from checkout.models import ComplianceError

        self.app.catalog.freeze("credit-zhaolian", "冻结")
        with self.assertRaises(ComplianceError) as ctx:
            self.app.payments.select(
                "o1", "credit-zhaolian", region="BJ", mcc="5812",
                moment=self.moment,
            )
        self.assertEqual(ctx.exception.code, "frozen")

    def test_explicit_selection_then_charge_succeeds(self):
        self.app.payments.select(
            "o1", "bank-card-icbc", region="BJ", mcc="5812", moment=self.moment
        )
        record = self.app.payments.attempt(
            "o1", amount_cents=8800, region="BJ", mcc="5812",
            moment=self.moment,
        )
        self.assertEqual(record.status, "succeeded")
        self.assertEqual(record.source_id, "bank-card-icbc")
        self.assertEqual(self.app.payments.selected_source("o1"), "bank-card-icbc")


class RetryTest(unittest.TestCase):
    def setUp(self):
        self.gw = gateway(
            results={
                "bank-card-icbc": [
                    ChargeOutcome("failed", "issuer-declined", "ch-1"),
                    ChargeOutcome("succeeded", "", "ch-2"),
                ]
            }
        )
        self.app = load_app(gateway=self.gw)
        self.moment = cst(2026, 10, 20, 12)
        self.plan = self.app.orchestrator.build_plan(
            region="BJ", mcc="5812", moment=self.moment
        )
        self.app.payments.present("o2", self.plan, self.moment)
        self.app.payments.select(
            "o2", "bank-card-icbc", region="BJ", mcc="5812", moment=self.moment
        )

    def test_retry_uses_same_channel(self):
        first = self.app.payments.attempt(
            "o2", amount_cents=58800, region="BJ", mcc="5812", moment=self.moment
        )
        self.assertEqual(first.status, "failed")
        second = self.app.payments.retry(
            "o2", amount_cents=58800, region="BJ", mcc="5812", moment=self.moment
        )
        self.assertEqual(second.status, "succeeded")
        self.assertEqual(first.source_id, second.source_id)
        self.assertEqual(second.attempt_no, 2)

    def test_retry_rechecks_availability_and_blocks_when_frozen(self):
        from checkout.models import ComplianceError

        self.app.payments.attempt(
            "o2", amount_cents=58800, region="BJ", mcc="5812", moment=self.moment
        )
        self.app.catalog.freeze("bank-card-icbc", "发卡行系统维护")
        with self.assertRaises(ComplianceError) as ctx:
            self.app.payments.retry(
                "o2", amount_cents=58800, region="BJ", mcc="5812",
                moment=self.moment,
            )
        self.assertEqual(ctx.exception.code, "frozen")
        # 阻断事件留痕，且没有静默换到其他渠道
        blocked = [
            e for e in self.app.ledger.for_order("o2")
            if e.event_type == "payment.blocked"
        ]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0].payload["source_id"], "bank-card-icbc")
        self.assertEqual(len(self.gw.calls), 1)

    def test_retry_without_prior_attempt_rejected(self):
        from checkout.models import ComplianceError

        app = load_app(gateway=gateway())
        plan = app.orchestrator.build_plan(
            region="BJ", mcc="5812", moment=self.moment
        )
        app.payments.present("o3", plan, self.moment)
        app.payments.select(
            "o3", "bank-card-icbc", region="BJ", mcc="5812", moment=self.moment
        )
        with self.assertRaises(ComplianceError) as ctx:
            app.payments.retry(
                "o3", amount_cents=1, region="BJ", mcc="5812", moment=self.moment
            )
        self.assertEqual(ctx.exception.code, "no-prior-attempt")

    def test_no_double_charge_after_success(self):
        from checkout.models import ComplianceError

        self.app.payments.attempt(
            "o2", amount_cents=58800, region="BJ", mcc="5812", moment=self.moment
        )
        # 让第二次也失败后第三次成功
        self.gw._results["bank-card-icbc"] = [
            ChargeOutcome("failed", "timeout", "ch-x"),
            ChargeOutcome("succeeded", "", "ch-y"),
        ]
        self.app.payments.retry(
            "o2", amount_cents=58800, region="BJ", mcc="5812", moment=self.moment
        )
        self.app.payments.retry(
            "o2", amount_cents=58800, region="BJ", mcc="5812", moment=self.moment
        )
        with self.assertRaises(ComplianceError) as ctx:
            self.app.payments.retry(
                "o2", amount_cents=58800, region="BJ", mcc="5812",
                moment=self.moment,
            )
        self.assertEqual(ctx.exception.code, "already-succeeded")

    def test_idempotent_attempt_replay_does_not_recharge(self):
        rec1 = self.app.payments.attempt(
            "o2", amount_cents=58800, region="BJ", mcc="5812", moment=self.moment,
            client_attempt_id="cli-1",
        )
        rec2 = self.app.payments.attempt(
            "o2", amount_cents=58800, region="BJ", mcc="5812", moment=self.moment,
            client_attempt_id="cli-1",
        )
        self.assertIs(rec1, rec2)
        self.assertEqual(len(self.gw.calls), 1)


class ReceiptIngestTest(unittest.TestCase):
    def setUp(self):
        self.ledger = EventLedger()
        self.app = load_app()

    def _ingest(self, record):
        return self.ledger.ingest_receipt(record)

    def test_misclassified_receipt_is_corrected_but_preserved(self):
        event = self._ingest({
            "type": "ad.click.attribution",
            "order_id": "x",
            "origin": "channel",
            "occurred_at": "2026-10-02T11:00:27+08:00",
            "payload": {"attempt_no": 1, "channel_ref": "ch-x", "status": "failed"},
        })
        self.assertEqual(event.event_type, "channel.confirmation")
        self.assertEqual(event.declared_type, "ad.click.attribution")
        self.assertIn("misclassified", event.anomalies)
        detail = dict(event.anomaly_detail)
        self.assertEqual(detail["evidence"], "attempt_no,channel_ref")

    def test_late_confirmation_marked_with_delay(self):
        event = self._ingest({
            "type": "channel.confirmation",
            "order_id": "x",
            "origin": "channel",
            "occurred_at": "2026-10-01T20:01:12+08:00",
            "received_at": "2026-10-03T08:30:00+08:00",
            "payload": {"attempt_no": 2, "channel_ref": "ch-late",
                        "status": "succeeded"},
        })
        self.assertIn("late-arrival", event.anomalies)
        self.assertIn("late-confirmation", event.anomalies)
        self.assertGreater(int(dict(event.anomaly_detail)["delay_seconds"]), 86400)

    def test_bad_status_flagged(self):
        event = self._ingest({
            "type": "risk.event.callback",
            "order_id": "x",
            "origin": "channel",
            "occurred_at": "2026-10-04T09:05:00+08:00",
            "payload": {"attempt_no": 7, "channel_ref": "ch-ghost",
                        "status": "pending"},
        })
        self.assertIn("bad-status", event.anomalies)
        self.assertIn("misclassified", event.anomalies)

    def test_duplicate_confirmation_flagged(self):
        record = {
            "type": "channel.confirmation", "order_id": "x", "origin": "channel",
            "occurred_at": "2026-10-01T20:01:12+08:00",
            "payload": {"attempt_no": 1, "channel_ref": "dup", "status": "succeeded"},
        }
        self._ingest(record)
        second = self._ingest({**record, "occurred_at": "2026-10-01T20:02:00+08:00"})
        self.assertIn("duplicate-confirmation", second.anomalies)

    def test_unknown_type_without_markers_is_not_silently_dropped(self):
        event = self._ingest({
            "type": "some-new-webhook",
            "order_id": "x",
            "origin": "channel",
            "occurred_at": "2026-10-04T09:00:00+08:00",
            "payload": {"note": "未知新回调"},
        })
        self.assertEqual(event.event_type, "unknown")
        self.assertIn("unknown-type", event.anomalies)


class AuditorReconstructionTest(unittest.TestCase):
    def setUp(self):
        self.app = load_app()

    def test_clean_orders_across_policy_boundary(self):
        # order-9001：9-29 旧规下信贷支付；order-9002：10-01 灰度新规，
        # 失败后同渠道重试成功；两者均含迟到确认，但事实一致
        for oid, policy_version in (("order-9001", 1), ("order-9002", 2)):
            report = self.app.auditor.audit_order(oid)
            self.assertTrue(
                report.consistent,
                msg=f"{oid} 不应有矛盾: {report.findings}",
            )
            self.assertEqual(
                report.presentation["policy_source"]["version"], policy_version
            )

    def test_reconstruction_shows_layout_selection_and_charges(self):
        report = self.app.auditor.audit_order("order-9002")
        layout = report.presentation
        self.assertEqual(layout["region"], "SH")
        self.assertEqual(layout["policy_source"]["mode"], "canary")
        # 复原分组顺序
        self.assertEqual(
            [g["key"] for g in layout["groups"]],
            ["bank-card", "payment-tool", "credit", "asset-management",
             "installment"],
        )
        # 用户明确选择了银行卡
        self.assertEqual(report.selections[-1]["source_id"], "bank-card-icbc")
        # 后续扣款：失败一笔、成功一笔，渠道一致
        statuses = [
            (e["payload"]["attempt_no"], e["payload"]["status"],
             e["payload"]["source_id"])
            for e in report.attempts if e["type"] == "payment.result"
        ]
        self.assertEqual(statuses, [
            (1, "failed", "bank-card-icbc"),
            (2, "succeeded", "bank-card-icbc"),
        ])
        # 迟到但一致的确认只进 notices
        self.assertTrue(
            any("late-confirmation" in n for n in report.notices)
        )

    def test_contradictory_late_confirmation_is_finding(self):
        # order-9003：原扣款 succeeded，迟到的错误分类确认声称 failed
        report = self.app.auditor.audit_order("order-9003")
        self.assertFalse(report.consistent)
        self.assertTrue(
            any("矛盾" in f for f in report.findings),
            msg=str(report.findings),
        )
        self.assertTrue(
            any("misclassified" in n for n in report.notices)
        )

    def test_ghost_confirmation_without_attempt_is_finding(self):
        report = self.app.auditor.audit_order("order-9004")
        self.assertFalse(report.consistent)
        self.assertTrue(any("对应不上任何扣款尝试" in f for f in report.findings))

    def test_audit_all_partitions_orders(self):
        summary = self.app.auditor.audit_all()
        self.assertIn("order-9001", summary["consistent_orders"])
        self.assertIn("order-9002", summary["consistent_orders"])
        self.assertEqual(
            sorted(summary["inconsistent_orders"]),
            ["order-9003", "order-9004"],
        )
        # 异常事件清单完整保留
        anomalous = {(e["seq"], tuple(e["anomalies"])) for e in
                     summary["anomalous_events"]}
        self.assertTrue(any("late-confirmation" in a for _, a in anomalous))

    def test_audit_flags_preselected_layout(self):
        ledger = EventLedger()
        bad_layout = {
            "plan_id": "p1", "region": "BJ", "mcc": "0000",
            "policy_source": {"policy_id": "financial-marketing", "version": 1,
                              "mode": "full", "window": {}},
            "preselection": "forbidden",
            "source_ids": ["bank-card-icbc"],
            "groups": [{"key": "payment", "options": [
                {"source_id": "bank-card-icbc", "category": "bank-card",
                 "selected": True}
            ]}],
        }
        ledger.append(
            "checkout.presented",
            {"plan_id": "p1", "layout": bad_layout},
            order_id="bad", occurred_at=cst(2026, 9, 29),
        )
        from checkout.ledger import Auditor

        report = Auditor(ledger, self.app.policies, "financial-marketing").audit_order("bad")
        self.assertTrue(any("预选" in f for f in report.findings))


    def test_canary_miss_uses_old_full_during_coexistence(self):
        # 夹具时间线：v1 旧全量持续至 10-15、v2 在上海灰度。未命中灰度的
        # 上海用户仍适用窗口内的旧全量 v1，审计应判一致。
        app = load_app()
        moment = cst(2026, 10, 1, 12)
        plan = app.orchestrator.build_plan(
            region="SH", mcc="5812", moment=moment, canary_unit="u1"
        )
        self.assertEqual((plan.policy_version, plan.policy_mode), (1, "full"))
        app.payments.present("order-fallback", plan, moment)
        app.payments.select(
            "order-fallback", "bank-card-icbc",
            region="SH", mcc="5812", moment=moment,
        )
        app.payments.attempt(
            "order-fallback", amount_cents=100,
            region="SH", mcc="5812", moment=moment,
        )
        report = app.auditor.audit_order("order-fallback")
        self.assertTrue(report.consistent, msg=str(report.findings))

    def test_fallback_to_closed_predecessor_audits_consistent(self):
        # 另一种合法时间线：旧全量 9-30 已闭合、新规仅上海灰度，未命中
        # 用户回退旧版（mode=fallback）。审计不得把已闭合的回退版本误判越界。
        import json as _json
        from pathlib import Path as _Path
        from checkout.policy import PolicyStore, policy_from_dict
        from checkout.orchestrator import CheckoutOrchestrator
        from checkout.ledger import Auditor

        policies = _json.loads(
            _Path("fixtures/policies.json").read_text(encoding="utf-8")
        )
        store = PolicyStore()
        v1_data = {**policies[0], "effective_to": "2026-09-30T00:00:00+08:00"}
        store.register(policy_from_dict(v1_data))
        store.register(policy_from_dict(policies[1]))  # v2 SH canary

        r = store.resolve("financial-marketing", "SH", cst(2026, 10, 1, 12), "u1")
        self.assertEqual((r.policy.version, r.mode), (1, "fallback"))

        app = load_app()
        app.policies = store
        app.orchestrator = CheckoutOrchestrator(
            store, app.catalog, "financial-marketing"
        )
        app.auditor = Auditor(app.ledger, store, "financial-marketing")

        moment = cst(2026, 10, 1, 12)
        plan = app.orchestrator.build_plan(
            region="SH", mcc="5812", moment=moment, canary_unit="u1"
        )
        self.assertEqual(plan.policy_mode, "fallback")
        app.payments.present("order-fb2", plan, moment)
        app.payments.select(
            "order-fb2", "bank-card-icbc",
            region="SH", mcc="5812", moment=moment,
        )
        app.payments.attempt(
            "order-fb2", amount_cents=100,
            region="SH", mcc="5812", moment=moment,
        )
        report = app.auditor.audit_order("order-fb2")
        self.assertTrue(report.consistent, msg=str(report.findings))


class JsonlLoadTest(unittest.TestCase):
    def test_fixture_file_loads_and_marks_all_anomaly_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "receipts.jsonl"
            p.write_text(
                Path("fixtures/receipts.jsonl").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            ledger = EventLedger()
            events = ledger.load_jsonl(p)
            self.assertEqual(len(events), 20)
            anomalies = {a for e in events for a in e.anomalies}
            self.assertIn("misclassified", anomalies)
            self.assertIn("late-confirmation", anomalies)
            self.assertIn("bad-status", anomalies)


if __name__ == "__main__":
    unittest.main()
