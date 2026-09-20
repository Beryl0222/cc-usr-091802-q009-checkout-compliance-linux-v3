"""只追加事件台账与审计复原。

台账接收两类来源：

- 服务自身在结账/支付流程中写入的事件（``checkout.presented``、
  ``checkout.selected``、``payment.attempt/result/blocked``）；
- 外部渠道与页面回执（JSONL 导入），这些回执横跨 9 月 30 日政策切换，
  其中夹有**错误分类**与**迟到确认**。台账不静默丢弃任何回执：无法识别
  或自相矛盾的记录原样保留并打上 ``anomalies`` 标记，由审计人员定夺。

审计复原按订单重建：结账时呈现的布局（分组/顺序/风险信息/可选状态）、
用户的明确选择、政策来源（版本与解析模式）、后续扣款与渠道确认，
并核对：服务端从未预选、重试未换渠道、扣款时刻重新核验过可用性、
每个时刻只有一个有效政策版本。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .models import ComplianceError, iso, parse_dt
from .policy import PolicyStore

# 渠道确认超过此时差视为迟到确认
LATE_CONFIRM_THRESHOLD = timedelta(minutes=5)

# 内部事件的必要字段；缺字段即结构错误（区别于分类错误）
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "checkout.presented": ("plan_id", "layout"),
    "checkout.selected": ("plan_id", "source_id", "selection"),
    "payment.attempt": ("attempt_no", "source_id", "amount_cents"),
    "payment.result": ("attempt_no", "source_id", "status"),
    "payment.blocked": ("source_id", "reason_code"),
    "channel.confirmation": ("attempt_no", "channel_ref", "status"),
}

_KNOWN_TYPES = frozenset(_REQUIRED_FIELDS) | {"page.view", "unknown"}

# 标记某条记录“实际是什么”的载荷特征：带这些字段的记录本质是支付/确认
_PAYMENT_MARKERS = frozenset({"attempt_no", "channel_ref", "amount_cents"})


@dataclass(frozen=True, slots=True)
class Event:
    seq: int
    event_type: str            # 入账时采用的类型（可能经纠分类）
    order_id: str
    occurred_at: datetime      # 业务发生时间
    received_at: datetime      # 台账收到时间（迟到确认两者不同）
    payload: dict[str, Any]
    origin: str                # self / channel / page
    anomalies: tuple[str, ...] = ()
    anomaly_detail: tuple[tuple[str, str], ...] = ()
    declared_type: str = ""    # 回执原始声明类型（错误分类时保留）

    def to_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "event_type": self.event_type,
            "declared_type": self.declared_type,
            "order_id": self.order_id,
            "occurred_at": iso(self.occurred_at),
            "received_at": iso(self.received_at),
            "origin": self.origin,
            "anomalies": list(self.anomalies),
            "anomaly_detail": dict(self.anomaly_detail),
            "payload": self.payload,
        }


class LedgerError(ComplianceError):
    pass


class EventLedger:
    """线程安全、只追加（append-only）的事件台账。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._events: list[Event] = []

    def append(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        order_id: str,
        occurred_at: datetime,
        received_at: datetime | None = None,
        origin: str = "self",
    ) -> Event:
        if event_type not in _KNOWN_TYPES:
            raise LedgerError("unknown-event-type", f"内部事件类型非法: {event_type}")
        for key in _REQUIRED_FIELDS.get(event_type, ()):  # type: ignore[arg-type]
            if key not in payload:
                raise LedgerError(
                    "bad-payload", f"事件 {event_type} 缺少字段 {key}"
                )
        with self._lock:
            seq = len(self._events) + 1
            event = Event(
                seq=seq,
                event_type=event_type,
                order_id=order_id,
                occurred_at=occurred_at,
                received_at=received_at or occurred_at,
                payload=dict(payload),
                origin=origin,
            )
            self._events.append(event)
        return event

    # ---- 外部回执导入（含错误分类/迟到识别） -----------------------

    def ingest_receipt(self, record: dict[str, Any]) -> Event:
        """导入一条渠道/页面回执。

        回执不可信：声明类型可能错误、确认可能迟到、状态可能矛盾。
        本方法做**非破坏性归类**：纠正类型但保留 ``declared_type``，
        异常全部体现在 ``anomalies`` 中，原始载荷不修改。
        """
        declared = str(record.get("type", "unknown"))
        order_id = str(record.get("order_id", ""))
        occurred_at = parse_dt(record["occurred_at"])
        received_at = (
            parse_dt(record["received_at"]) if record.get("received_at") else occurred_at
        )
        payload = dict(record.get("payload", {}))
        origin = str(record.get("origin", "channel"))
        anomalies: list[str] = []
        detail: list[tuple[str, str]] = []

        event_type = self._reclassify(declared, payload, anomalies, detail)
        self._check_structure(event_type, payload, anomalies, detail)
        if received_at - occurred_at > LATE_CONFIRM_THRESHOLD:
            anomalies.append("late-arrival")
            detail.append(
                (
                    "delay_seconds",
                    str(int((received_at - occurred_at).total_seconds())),
                )
            )
        if event_type == "channel.confirmation" and (
            received_at - occurred_at > LATE_CONFIRM_THRESHOLD
        ):
            anomalies.append("late-confirmation")

        with self._lock:
            # 同一渠道参考号的重复/矛盾确认
            if event_type == "channel.confirmation":
                self._check_confirmation_locked(payload, anomalies, detail)
            seq = len(self._events) + 1
            event = Event(
                seq=seq,
                event_type=event_type,
                order_id=order_id,
                occurred_at=occurred_at,
                received_at=received_at,
                payload=payload,
                origin=origin,
                anomalies=tuple(anomalies),
                anomaly_detail=tuple(detail),
                declared_type=declared,
            )
            self._events.append(event)
        return event

    @staticmethod
    def _reclassify(
        declared: str,
        payload: dict[str, Any],
        anomalies: list[str],
        detail: list[tuple[str, str]],
    ) -> str:
        """根据载荷特征纠正回执的声明类型。"""
        if declared in _KNOWN_TYPES:
            return declared
        # 声明成营销/页面类，却带着支付特征字段 → 错误分类
        markers = _PAYMENT_MARKERS & payload.keys()
        if markers:
            corrected = (
                "channel.confirmation"
                if "channel_ref" in payload and "status" in payload
                else "payment.result"
            )
            anomalies.append("misclassified")
            detail.append(("declared_type", declared))
            detail.append(("corrected_type", corrected))
            detail.append(("evidence", ",".join(sorted(markers))))
            return corrected
        anomalies.append("unknown-type")
        detail.append(("declared_type", declared))
        return "unknown"

    @staticmethod
    def _check_structure(
        event_type: str,
        payload: dict[str, Any],
        anomalies: list[str],
        detail: list[tuple[str, str]],
    ) -> None:
        required = _REQUIRED_FIELDS.get(event_type)
        if not required:
            return
        missing = [k for k in required if k not in payload]
        if missing:
            anomalies.append("malformed")
            detail.append(("missing_fields", ",".join(missing)))
        status = payload.get("status")
        if "status" in required and status not in ("succeeded", "failed"):
            anomalies.append("bad-status")
            detail.append(("status", str(status)))

    def _check_confirmation_locked(
        self,
        payload: dict[str, Any],
        anomalies: list[str],
        detail: list[tuple[str, str]],
    ) -> None:
        ref = payload.get("channel_ref")
        for prior in self._events:
            if prior.event_type != "channel.confirmation":
                continue
            if prior.payload.get("channel_ref") == ref:
                anomalies.append("duplicate-confirmation")
                detail.append(("first_seq", str(prior.seq)))
                return

    def load_jsonl(self, path: str | Path) -> list[Event]:
        events = []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                events.append(self.ingest_receipt(json.loads(line)))
        return events

    # ---- 查询 -------------------------------------------------------

    def all(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def for_order(self, order_id: str) -> list[Event]:
        with self._lock:
            return [e for e in self._events if e.order_id == order_id]

    def order_ids(self) -> list[str]:
        with self._lock:
            return sorted({e.order_id for e in self._events if e.order_id})

    def anomalies(self) -> list[Event]:
        with self._lock:
            return [e for e in self._events if e.anomalies]


# ---- 审计复原 ------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class OrderAudit:
    order_id: str
    presentation: dict[str, Any] | None
    selections: tuple[dict[str, Any], ...]
    attempts: tuple[dict[str, Any], ...]
    confirmations: tuple[dict[str, Any], ...]
    receipts: tuple[dict[str, Any], ...]
    findings: tuple[str, ...]
    notices: tuple[str, ...]
    consistent: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "consistent": self.consistent,
            "presentation": self.presentation,
            "selections": list(self.selections),
            "attempts": list(self.attempts),
            "confirmations": list(self.confirmations),
            "receipts": list(self.receipts),
            "findings": list(self.findings),
            "notices": list(self.notices),
        }


class Auditor:
    """依据台账事件复原结账现场并出具一致性结论。"""

    def __init__(self, ledger: EventLedger, policies: PolicyStore, policy_id: str):
        self._ledger = ledger
        self._policies = policies
        self._policy_id = policy_id

    def audit_order(self, order_id: str) -> OrderAudit:
        events = sorted(self._ledger.for_order(order_id), key=lambda e: e.occurred_at)
        findings: list[str] = []

        presentation_event = next(
            (e for e in events if e.event_type == "checkout.presented"), None
        )
        presentation = presentation_event.payload["layout"] if presentation_event else None

        if presentation is not None:
            self._audit_layout(presentation, findings)
            self._audit_policy_source(presentation_event, presentation, findings)

        selection_events = [e for e in events if e.event_type == "checkout.selected"]
        selections = tuple(
            {
                "occurred_at": iso(e.occurred_at),
                "source_id": e.payload["source_id"],
                "plan_id": e.payload["plan_id"],
                "selection": e.payload["selection"],
            }
            for e in selection_events
        )

        attempt_events = [
            e for e in events if e.event_type in ("payment.attempt", "payment.result", "payment.blocked")
        ]
        attempts = tuple(self._summarize(e) for e in attempt_events)
        confirm_events = [e for e in events if e.event_type == "channel.confirmation"]
        confirmations = tuple(self._summarize(e) for e in confirm_events)

        self._audit_selection_and_retry(
            presentation, selection_events, attempt_events, findings
        )
        self._audit_confirmations(attempt_events, confirm_events, findings)

        # 回执层面的异常分两级：
        # - notices：迟到、错误分类（已纠正）等不改变交易事实的信息项；
        # - findings：畸形、状态非法、无法对应扣款的“幽灵确认”等矛盾项。
        receipts = tuple(self._summarize(e) for e in events if e.origin != "self")
        notices: list[str] = []
        notice_only = frozenset(
            {"late-arrival", "late-confirmation", "misclassified", "duplicate-confirmation"}
        )
        for e in events:
            for anomaly in e.anomalies:
                line = f"seq{e.seq} {anomaly}: {e.event_type or e.declared_type}"
                (notices if anomaly in notice_only else findings).append(line)

        return OrderAudit(
            order_id=order_id,
            presentation=presentation,
            selections=selections,
            attempts=attempts,
            confirmations=confirmations,
            receipts=receipts,
            findings=tuple(findings),
            notices=tuple(notices),
            consistent=not findings,
        )

    def audit_all(self) -> dict[str, Any]:
        reports = {oid: self.audit_order(oid) for oid in self._ledger.order_ids()}
        return {
            "orders": {oid: r.to_json() for oid, r in reports.items()},
            "anomalous_events": [e.to_json() for e in self._ledger.anomalies()],
            "consistent_orders": sorted(
                oid for oid, r in reports.items() if r.consistent
            ),
            "inconsistent_orders": sorted(
                oid for oid, r in reports.items() if not r.consistent
            ),
        }

    # ---- 具体核对项 -------------------------------------------------

    @staticmethod
    def _audit_layout(layout: dict[str, Any], findings: list[str]) -> None:
        if layout.get("preselection") != "forbidden":
            findings.append("布局未声明禁止预选")
        for group in layout.get("groups", []):
            for option in group.get("options", []):
                if option.get("selected"):
                    findings.append(
                        f"方案存在服务端预选项: {option['source_id']}"
                    )

    def _audit_policy_source(
        self,
        event: Event,
        layout: dict[str, Any],
        findings: list[str],
    ) -> None:
        source = layout["policy_source"]
        pid = source["policy_id"]
        version = source["version"]
        mode = source.get("mode", "full")
        policy = self._policies.get(pid, version)
        if policy is None:
            findings.append(f"方案引用的政策版本不存在: {pid} v{version}")
            return
        if mode == "fallback":
            # 灰度期未命中分桶而回退的旧全量版本：其窗口已闭合正是回退
            # 的前提，不构成异常。只需确认当时该地区确有进行中的灰度，
            # 且所回退的是灰度版本的直接父版本。
            active_canary = [
                p
                for p in self._policies.list_versions(pid)
                if p.canary_percent < 100
                and p.applies_to(layout["region"], event.occurred_at)
            ]
            if not active_canary:
                findings.append(
                    f"方案标记为 fallback 回退到 v{version}，"
                    "但当时该地区并无进行中的灰度版本"
                )
            elif all(p.parent_version != version for p in active_canary):
                findings.append(
                    f"fallback 回退版本 v{version} 不是进行中灰度版本的父版本"
                )
        elif not policy.window.contains(event.occurred_at):
            findings.append(
                f"呈现时刻 {iso(event.occurred_at)} 不在政策 v{version} "
                f"生效区间内（模式 {mode}）"
            )
        if mode == "full":
            effective = self._policies.effective(
                pid, layout["region"], event.occurred_at
            )
            if effective is not None and effective.version != version:
                findings.append(
                    f"全量方案版本 v{version} 与当时唯一有效版本 "
                    f"v{effective.version} 不一致"
                )
        # 金融产品分组必须带风险披露
        for group in layout.get("groups", []):
            if not group.get("risk_disclosures") and any(
                o["category"] in ("credit", "asset-management", "installment")
                for o in group.get("options", [])
            ):
                findings.append(f"金融产品分组 {group['key']} 缺少风险信息")

    @staticmethod
    def _audit_selection_and_retry(
        presentation,
        selection_events: list[Event],
        attempt_events: list[Event],
        findings: list[str],
    ) -> None:
        presented_ids = set(presentation["source_ids"]) if presentation else set()
        for event in selection_events:
            if event.payload["source_id"] not in presented_ids:
                findings.append(
                    f"seq{event.seq} 选择了未在方案中呈现的资金源"
                )
            if event.payload.get("selection") != "explicit-user-action":
                findings.append(f"seq{event.seq} 选择不是用户显式动作")

        # 扣款前必须有显式选择；每次尝试的渠道必须等于最近一次选择；
        # 相邻尝试（重试）渠道必须相同
        current_selection = None
        result_by_attempt: dict[int, str] = {}
        for event in sorted(
            selection_events + attempt_events, key=lambda e: e.occurred_at
        ):
            if event.event_type == "checkout.selected":
                current_selection = event.payload["source_id"]
            elif event.event_type == "payment.attempt":
                source = event.payload["source_id"]
                if current_selection is None:
                    findings.append(
                        f"seq{event.seq} 扣款前无用户显式选择（服务端预选嫌疑）"
                    )
                elif source != current_selection:
                    findings.append(
                        f"seq{event.seq} 扣款渠道 {source} 与用户选择 "
                        f"{current_selection} 不一致"
                    )
                retry_of = event.payload.get("retry_of")
                if retry_of is not None:
                    prior = next(
                        (
                            e
                            for e in attempt_events
                            if e.event_type == "payment.attempt"
                            and e.payload["attempt_no"] == retry_of
                        ),
                        None,
                    )
                    if prior and prior.payload["source_id"] != source:
                        findings.append(
                            f"seq{event.seq} 重试静默更换渠道: "
                            f"{prior.payload['source_id']} -> {source}"
                        )
            elif event.event_type == "payment.result":
                result_by_attempt[event.payload["attempt_no"]] = event.payload["status"]

    def _audit_confirmations(
        self,
        attempt_events: list[Event],
        confirm_events: list[Event],
        findings: list[str],
    ) -> None:
        results = {
            e.payload["attempt_no"]: e.payload
            for e in attempt_events
            if e.event_type == "payment.result"
        }
        for event in confirm_events:
            no = event.payload.get("attempt_no")
            if no not in results:
                findings.append(f"seq{event.seq} 确认对应不上任何扣款尝试 #{no}")
                continue
            if event.payload["status"] != results[no]["status"]:
                findings.append(
                    f"seq{event.seq} 迟到确认状态 {event.payload['status']} "
                    f"与原扣款结果 {results[no]['status']} 矛盾"
                )

    @staticmethod
    def _summarize(event: Event) -> dict[str, Any]:
        return {
            "seq": event.seq,
            "type": event.event_type,
            "occurred_at": iso(event.occurred_at),
            "received_at": iso(event.received_at),
            "origin": event.origin,
            "anomalies": list(event.anomalies),
            "payload": event.payload,
        }
