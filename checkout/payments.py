"""支付发起与重试。

铁律：

- 扣款只能引用用户在本次结账会话中**明确选择**的资金源，服务端从未
  预选（方案中 ``selected`` 恒为 false）；
- 发起支付与每次重试都要向目录**重新核验可用性**——展示时可用不代表
  扣款时可用（风控冻结、资质到期、跨过生效区间都会发生）；
- 重试只能继续使用用户已选择的渠道，**不得静默换渠道**；用户改选
  必须留下新的显式选择事件；
- 所有动作写入只追加事件台账（:mod:`checkout.ledger`），渠道侧迟到的
  回执/确认也带“发生时间 + 到达时间”入账，供审计复原。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Protocol

from .catalog import Catalog
from .ledger import EventLedger
from .models import CST, ComplianceError, iso


class PaymentError(ComplianceError):
    """支付前置条件不满足（区别于渠道侧扣款失败）。"""


@dataclass(frozen=True, slots=True)
class ChargeOutcome:
    """渠道（或桩）返回的扣款结果。"""

    status: str            # succeeded | failed
    reason_code: str = ""
    channel_ref: str = ""
    occurred_at: datetime | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"


class Gateway(Protocol):
    """渠道网关协议。生产环境对接各收单机构；测试用 :class:`ScriptedGateway`。"""

    def charge(
        self,
        *,
        order_id: str,
        attempt_no: int,
        source_id: str,
        amount_cents: int,
        moment: datetime,
    ) -> ChargeOutcome: ...


class ScriptedGateway:
    """按脚本返回结果的测试网关。

    - ``results``：source_id -> 结果队列（依次弹出）；
    - 队列耗尽后用 ``default``；
    - 每次结果可附带异步延迟确认（由测试随后通过台账导入迟到确认）。
    """

    def __init__(
        self,
        results: dict[str, list[ChargeOutcome]] | None = None,
        default: ChargeOutcome | None = None,
    ) -> None:
        self._results = {k: list(v) for k, v in (results or {}).items()}
        self._default = default or ChargeOutcome(status="succeeded", channel_ref="ch-ok")
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def charge(self, *, order_id, attempt_no, source_id, amount_cents, moment):
        with self._lock:
            self.calls.append(
                {
                    "order_id": order_id,
                    "attempt_no": attempt_no,
                    "source_id": source_id,
                    "amount_cents": amount_cents,
                    "moment": moment,
                }
            )
            queue = self._results.get(source_id, [])
            if queue:
                outcome = queue.pop(0)
                if outcome.occurred_at is None:
                    object.__setattr__(outcome, "occurred_at", moment)
                return outcome
            return ChargeOutcome(
                status=self._default.status,
                reason_code=self._default.reason_code,
                channel_ref=self._default.channel_ref or f"ch-{order_id}-{attempt_no}",
                occurred_at=moment,
            )


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    order_id: str
    attempt_no: int
    source_id: str
    amount_cents: int
    status: str
    reason_code: str
    channel_ref: str
    moment: datetime

    def to_json(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "attempt_no": self.attempt_no,
            "source_id": self.source_id,
            "amount_cents": self.amount_cents,
            "status": self.status,
            "reason_code": self.reason_code,
            "channel_ref": self.channel_ref,
            "moment": iso(self.moment),
        }


class PaymentService:
    """显式选源、每次重新核验、重试不换渠道的支付服务。"""

    def __init__(
        self,
        catalog: Catalog,
        ledger: EventLedger,
        gateway: Gateway,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._catalog = catalog
        self._ledger = ledger
        self._gateway = gateway
        self._clock = clock
        self._lock = threading.RLock()
        # 订单状态：展示方案 -> 显式选择 -> 逐笔尝试
        self._presented: dict[str, str] = {}       # order_id -> plan_id
        self._plan_sources: dict[str, frozenset[str]] = {}  # plan_id -> 源集合
        self._selection: dict[str, str] = {}       # order_id -> source_id（最新显式选择）
        self._attempts: dict[str, list[AttemptRecord]] = {}
        # 幂等：client_attempt_id -> AttemptRecord
        self._idem: dict[str, AttemptRecord] = {}

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(tz=CST)

    # ---- 结账会话事件 ----------------------------------------------

    def present(self, order_id: str, plan, moment: datetime | None = None) -> None:
        """记录收银台实际呈现给用户的方案（审计复原布局的依据）。"""
        moment = moment or plan.moment
        with self._lock:
            self._presented[order_id] = plan.plan_id
            self._plan_sources[plan.plan_id] = frozenset(plan.source_ids)
        self._ledger.append(
            "checkout.presented",
            payload={"plan_id": plan.plan_id, "layout": plan.to_json()},
            order_id=order_id,
            occurred_at=moment,
        )

    def select(
        self,
        order_id: str,
        source_id: str,
        *,
        region: str,
        mcc: str,
        moment: datetime | None = None,
    ) -> None:
        """登记用户的明确选择。服务端自身永远不会产生此事件。"""
        moment = moment or self._now()
        with self._lock:
            plan_id = self._presented.get(order_id)
            plan_sources = (
                self._plan_sources.get(plan_id, frozenset()) if plan_id else frozenset()
            )
        if plan_id is None:
            raise PaymentError(
                "no-presentation", f"订单 {order_id} 没有已呈现的收银台方案"
            )
        if source_id not in plan_sources:
            raise PaymentError(
                "source-not-presented",
                f"所选资金源 {source_id} 不在当次方案 {plan_id} 中，拒绝扣款",
            )
        # 展示时可选不代表此刻可选——选择当下也要重新核验
        availability = self._catalog.check_availability(
            source_id, region=region, mcc=mcc, moment=moment
        )
        if not availability.available:
            raise PaymentError(
                availability.reason_code,
                f"资金源 {source_id} 当前不可选: {availability.reason}",
            )
        with self._lock:
            self._selection[order_id] = source_id
        self._ledger.append(
            "checkout.selected",
            payload={
                "plan_id": plan_id,
                "source_id": source_id,
                "selection": "explicit-user-action",
            },
            order_id=order_id,
            occurred_at=moment,
        )

    # ---- 扣款与重试 -------------------------------------------------

    def attempt(
        self,
        order_id: str,
        *,
        amount_cents: int,
        region: str,
        mcc: str,
        moment: datetime | None = None,
        client_attempt_id: str | None = None,
    ) -> AttemptRecord:
        """按用户已明确选择的渠道发起扣款（首扣或用户改选后的新尝试）。"""
        moment = moment or self._now()
        with self._lock:
            if client_attempt_id and client_attempt_id in self._idem:
                # 幂等重放：不重复扣款
                return self._idem[client_attempt_id]
            source_id = self._selection.get(order_id)
        if source_id is None:
            raise PaymentError(
                "no-explicit-selection",
                f"订单 {order_id} 缺少用户明确选择的资金源，服务端不得预选",
            )
        record = self._charge(
            order_id, source_id, amount_cents, region, mcc, moment,
            client_attempt_id=client_attempt_id,
        )
        return record

    def retry(
        self,
        order_id: str,
        *,
        amount_cents: int,
        region: str,
        mcc: str,
        moment: datetime | None = None,
        client_attempt_id: str | None = None,
    ) -> AttemptRecord:
        """失败后重试：沿用用户明确选择的渠道，重新核验可用性。

        试图换渠道不算重试——用户必须重新走显式选择（:meth:`select`）。
        """
        moment = moment or self._now()
        with self._lock:
            source_id = self._selection.get(order_id)
            prior = list(self._attempts.get(order_id, ()))
        if source_id is None:
            raise PaymentError(
                "no-explicit-selection", f"订单 {order_id} 无已选渠道，无法重试"
            )
        if not prior:
            raise PaymentError("no-prior-attempt", f"订单 {order_id} 尚无扣款尝试")
        if prior[-1].status == "succeeded":
            raise PaymentError(
                "already-succeeded", f"订单 {order_id} 已扣款成功，禁止重复扣款"
            )
        record = self._charge(
            order_id, source_id, amount_cents, region, mcc, moment,
            client_attempt_id=client_attempt_id, retry_of=prior[-1].attempt_no,
        )
        return record

    def _charge(
        self,
        order_id: str,
        source_id: str,
        amount_cents: int,
        region: str,
        mcc: str,
        moment: datetime,
        *,
        client_attempt_id: str | None,
        retry_of: int | None = None,
    ) -> AttemptRecord:
        if amount_cents <= 0:
            raise PaymentError("bad-amount", "扣款金额必须为正")
        # 每次扣款/重试都重新核验，绝不沿用展示时刻的结论
        availability = self._catalog.check_availability(
            source_id, region=region, mcc=mcc, moment=moment
        )
        if not availability.available:
            # 重试时渠道不可用：阻断并留痕，而不是静默换一个渠道
            self._ledger.append(
                "payment.blocked",
                payload={
                    "source_id": source_id,
                    "reason_code": availability.reason_code,
                    "reason": availability.reason,
                    "retry_of": retry_of,
                },
                order_id=order_id,
                occurred_at=moment,
            )
            raise PaymentError(
                availability.reason_code,
                f"重新核验未通过，拒绝{'重试' if retry_of else '扣款'}: "
                f"{availability.reason}",
            )

        with self._lock:
            attempt_no = len(self._attempts.get(order_id, ())) + 1

        self._ledger.append(
            "payment.attempt",
            payload={
                "attempt_no": attempt_no,
                "source_id": source_id,
                "amount_cents": amount_cents,
                "retry_of": retry_of,
            },
            order_id=order_id,
            occurred_at=moment,
        )
        outcome = self._gateway.charge(
            order_id=order_id,
            attempt_no=attempt_no,
            source_id=source_id,
            amount_cents=amount_cents,
            moment=moment,
        )
        record = AttemptRecord(
            order_id=order_id,
            attempt_no=attempt_no,
            source_id=source_id,
            amount_cents=amount_cents,
            status=outcome.status,
            reason_code=outcome.reason_code,
            channel_ref=outcome.channel_ref,
            moment=outcome.occurred_at or moment,
        )
        with self._lock:
            self._attempts.setdefault(order_id, []).append(record)
            if client_attempt_id:
                self._idem[client_attempt_id] = record
        self._ledger.append(
            "payment.result",
            payload={
                "attempt_no": attempt_no,
                "source_id": source_id,
                "status": record.status,
                "reason_code": record.reason_code,
                "channel_ref": record.channel_ref,
                "retry_of": retry_of,
            },
            order_id=order_id,
            occurred_at=record.moment,
        )
        return record

    # ---- 查询 -------------------------------------------------------

    def attempts(self, order_id: str) -> list[AttemptRecord]:
        with self._lock:
            return list(self._attempts.get(order_id, ()))

    def selected_source(self, order_id: str) -> str | None:
        with self._lock:
            return self._selection.get(order_id)
