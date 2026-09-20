"""只追加审计事件链与结账现场复原。

审计要回答四件事：结账时**呈现了什么布局**、**用户选了什么**、
**依据哪一版政策**、之后**发生了哪些扣款/确认**。因此事件链只追加、
不可改写；复原以结账会话为主线，把方案快照、显式选择、扣款尝试、
渠道回执（含错误分类与迟到确认）串起来，并与登记处当前权威分类
交叉核对。
"""

import itertools
import threading
from dataclasses import dataclass

from . import timeutil


@dataclass(frozen=True)
class Event:
    seq: int
    at: str
    event_type: str
    payload: dict

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "at": self.at,
            "event_type": self.event_type,
            "payload": self.payload,
        }


class AuditLog:
    def __init__(self):
        self._lock = threading.RLock()
        self._events: list[Event] = []
        self._seq = itertools.count(1)

    def append(self, event_type: str, payload: dict, at) -> Event:
        at = timeutil.parse(at) if not hasattr(at, "tzinfo") else at
        with self._lock:
            event = Event(next(self._seq), timeutil.iso(at), event_type, dict(payload))
            self._events.append(event)
            return event

    def events(self, *, event_type: str | None = None) -> list[Event]:
        with self._lock:
            out = list(self._events)
        if event_type is not None:
            out = [e for e in out if e.event_type == event_type]
        return out

    # -------------------------------------------------------------- 复原
    def reconstruct_checkout(self, checkout_id: str, registry) -> dict:
        """复原单个结账会话的完整现场。"""
        plans = []
        selections = []
        charges = []
        receipts = []
        for event in self.events():
            p = event.payload
            if p.get("checkout_id") != checkout_id:
                continue
            if event.event_type == "plan.rendered":
                plans.append(event)
            elif event.event_type == "user.selection":
                selections.append(event)
            elif event.event_type in {
                "payment.attempted", "payment.succeeded", "payment.failed", "payment.retried"
            }:
                charges.append(event)
            elif event.event_type == "channel.receipt":
                receipts.append(event)

        if not plans:
            raise LookupError(f"审计链中不存在结账会话 {checkout_id}")
        plan_event = plans[-1]  # 以该会话最后一次方案快照为呈现依据
        plan = plan_event.payload["plan"]

        selection = selections[-1].payload if selections else None

        charge_rows = []
        for event in charges:
            row = {"seq": event.seq, "at": event.at,
                   "event_type": event.event_type, **event.payload}
            charge_rows.append(row)

        receipt_rows = []
        for event in receipts:
            p = event.payload
            claimed = p.get("claimed_category")
            source_id = p.get("source_id")
            actual = None
            mismatch = False
            try:
                actual = registry.get(source_id).legal_category.value
                mismatch = claimed is not None and claimed != actual
            except KeyError:
                actual = None
            attempt_at = p.get("attempt_at")
            late = False
            if attempt_at is not None:
                late = timeutil.parse(event.at) > timeutil.parse(attempt_at)
            receipt_rows.append({
                "seq": event.seq,
                "received_at": event.at,
                "channel_ref": p.get("channel_ref"),
                "source_id": source_id,
                "claimed_category": claimed,
                "authoritative_category": actual,
                "category_mismatch": mismatch,
                "attempt_at": attempt_at,
                "late_confirmation": late,
                "status": p.get("status"),
            })

        return {
            "checkout_id": checkout_id,
            "presented_layout": {
                "generated_at": plan.get("generated_at"),
                "policy_version": plan["policy"]["version"],
                "legal_source": plan["policy"]["legal_source"],
                "groups": [
                    {
                        "group_id": g["group_id"],
                        "source_ids": [i["source_id"] for i in g["items"]],
                    }
                    for g in plan["groups"]
                ],
                "preselected_source": plan.get("preselected_source"),
                "selection_required": plan.get("selection_required"),
                "expires_at": plan.get("expires_at"),
            },
            "user_selection": selection,
            "charges": charge_rows,
            "receipts": receipt_rows,
            "integrity": self._integrity_summary(plan, selection, charge_rows),
        }

    def _integrity_summary(self, plan, selection, charges) -> dict:
        presented = {i["source_id"] for g in plan["groups"] for i in g["items"]}
        result = {
            "no_server_preselection": plan.get("preselected_source") is None,
            "selection_was_explicit": selection is not None
                and bool(selection.get("explicit", False)),
            "selection_was_presented": (
                selection is not None and selection.get("source_id") in presented
            ),
            "charges_match_selection": all(
                c.get("source_id") == selection.get("source_id")
                for c in charges
                if c["event_type"] in {"payment.attempted", "payment.succeeded"}
            ) if selection else False,
        }
        return result

    def checkouts(self) -> list[str]:
        ids = []
        seen = set()
        for event in self.events():
            cid = event.payload.get("checkout_id")
            if cid and cid not in seen:
                seen.add(cid)
                ids.append(cid)
        return ids

    def effective_version_timeline(self) -> list[dict]:
        """发布事件时间线：供审计确认每个区间恰有一个有效版本。"""
        rows = []
        for event in self.events(event_type="policy.published"):
            p = event.payload
            rows.append({
                "version": p["version"],
                "effective_from": p["effective_from"],
                "effective_until": p.get("effective_until"),
                "legal_source": p.get("legal_source"),
                "published_at": event.at,
            })
        rows.sort(key=lambda r: r["effective_from"])
        return rows
