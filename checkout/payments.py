"""显式选择、扣款与重试。

规则：

- 扣款只能凭**用户明确选择**后颁发的选择令牌；服务端从不预选渠道，
  也不会在用户未选择时指定默认渠道。
- 首次扣款与每次重试都在扣款时刻**重新核验**渠道可用性（生效区间、
  资质有效期、商户与地区范围、分类更正后的当前视图）。
- 重试必须继续使用用户当初明确选择的同一渠道；核验不通过时拒绝，
  绝不替换成其他渠道。
- 渠道回执可能迟到、可能带错误分类：原样登记为 ``channel.receipt``，
  分类以登记处权威记录为准，由审计链交叉核对。
"""

import hmac
import secrets
import threading
from dataclasses import dataclass
from typing import Optional

from . import timeutil


class PaymentError(Exception):
    """拒绝码通过 code 暴露给调用方。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Selection:
    token: str
    checkout_id: str
    source_id: str
    merchant_id: str
    region: str
    plan_id: str
    selected_category: str          # 选择时呈现的类别（用于检测事后更正）
    selected_at: str
    attempts: int = 0
    settled: bool = False


class PaymentService:
    def __init__(self, registry, audit_log, *, gateway=None):
        self.registry = registry
        self.audit = audit_log
        # 渠道网关钩子：None 表示始终成功；否则以 (source, attempt_no) 调用，
        # 抛 PaymentError 表示渠道侧失败（可用性核验已在此之前完成）。
        self._gateway = gateway
        self._lock = threading.RLock()
        self._selections: dict[str, Selection] = {}

    # ---------------------------------------------------------- 用户选择
    def record_selection(self, *, checkout_id: str, source_id: str, merchant_id: str,
                         region: str, plan: dict, explicit: bool, at) -> Selection:
        """登记用户在某方案下的明确选择，颁发一次性的选择令牌。"""
        at = timeutil.parse(at) if not hasattr(at, "tzinfo") else at
        if not explicit:
            raise PaymentError("selection-not-explicit",
                               "缺少用户明确选择，服务端不得预选或默认勾选")
        presented = {item["source_id"]: item
                     for group in plan["groups"] for item in group["items"]}
        if source_id not in presented:
            raise PaymentError("source-not-presented",
                               f"{source_id} 不在结账方案 {plan.get('plan_id')} 的可选项中")
        if plan.get("preselected_source") is not None:
            # 任何带预选的方案都不得进入支付链路
            raise PaymentError("plan-has-preselection", "方案包含服务端预选，拒绝受理")

        token = secrets.token_urlsafe(18)
        selection = Selection(
            token=token,
            checkout_id=checkout_id,
            source_id=source_id,
            merchant_id=merchant_id,
            region=region,
            plan_id=plan["plan_id"],
            selected_category=presented[source_id]["category"],
            selected_at=timeutil.iso(at),
        )
        with self._lock:
            self._selections[token] = selection
        self.audit.append("user.selection", {
            "checkout_id": checkout_id,
            "source_id": source_id,
            "category_at_selection": presented[source_id]["category"],
            "plan_id": plan["plan_id"],
            "policy_version": plan["policy"]["version"],
            "explicit": True,
            "selection_token_fingerprint": self._fingerprint(token),
        }, at)
        return selection

    # -------------------------------------------------------------- 扣款
    def charge(self, token: str, amount: str, at, *, is_retry: bool = False,
               channel_ref: Optional[str] = None) -> dict:
        at = timeutil.parse(at) if not hasattr(at, "tzinfo") else at
        with self._lock:
            selection = self._selections.get(token)
        if selection is None:
            raise PaymentError("unknown-selection-token", "选择令牌无效或不存在")
        with self._lock:
            if selection.settled:
                raise PaymentError("already-settled", "该选择已完成扣款，请勿重复提交")
            selection.attempts += 1
            attempt_no = selection.attempts

        # 关键：每次（含重试）都按扣款时刻重新核验当前视图。
        source = self.registry.get(selection.source_id)  # 含分类更正后的当前视图
        ok, reason = source.usable_for(selection.merchant_id, selection.region, at)

        attempt_payload = {
            "checkout_id": selection.checkout_id,
            "source_id": selection.source_id,
            "amount": amount,
            "attempt": attempt_no,
            "is_retry": is_retry,
            "reuses_user_selected_channel": is_retry,
            "reverified_at": timeutil.iso(at),
            "channel_ref": channel_ref,
        }
        self.audit.append(
            "payment.retried" if is_retry else "payment.attempted",
            attempt_payload, at,
        )

        if not ok:
            self.audit.append("payment.failed", {
                **attempt_payload,
                "reason": reason,
                "substituted_source": None,  # 明确：不替换渠道
            }, at)
            raise PaymentError(
                reason,
                f"用户选择的渠道 {selection.source_id} 此刻不可用（{reason}）；"
                "按要求继续使用原渠道重试，不得替换",
            )

        # 类别在选择后被更正也要留痕（仍按用户选择的同一资金源扣款）。
        category_now = source.legal_category.value
        if category_now != selection.selected_category:
            self.audit.append("source.category-corrected-before-charge", {
                "checkout_id": selection.checkout_id,
                "source_id": selection.source_id,
                "category_at_selection": selection.selected_category,
                "authoritative_category_now": category_now,
            }, at)

        if self._gateway is not None:
            try:
                self._gateway(source, attempt_no)
            except PaymentError as exc:
                self.audit.append("payment.failed", {
                    **attempt_payload,
                    "reason": exc.code,
                    "substituted_source": None,  # 失败也不替换渠道
                }, at)
                raise

        with self._lock:
            selection.settled = True
        self.audit.append("payment.succeeded", {
            **attempt_payload,
            "provider_id": source.provider_id,
            "authoritative_category": category_now,
        }, at)
        return {
            "status": "succeeded",
            "checkout_id": selection.checkout_id,
            "source_id": selection.source_id,
            "attempt": attempt_no,
            "amount": amount,
            "charged_at": timeutil.iso(at),
        }

    def retry(self, token: str, amount: str, at, *, channel_ref: Optional[str] = None) -> dict:
        """重试：沿用用户明确选择的渠道，并在当前时刻重新核验。"""
        return self.charge(token, amount, at, is_retry=True, channel_ref=channel_ref)

    # ------------------------------------------------------ 渠道回执接入
    def ingest_receipt(self, *, checkout_id: str, source_id: str, status: str,
                       claimed_category: Optional[str], channel_ref: str,
                       attempt_at: str, received_at) -> dict:
        """登记渠道回执。迟到、错误分类都原样收下，审计层负责比对纠正。"""
        received_at = timeutil.parse(received_at)
        event = self.audit.append("channel.receipt", {
            "checkout_id": checkout_id,
            "source_id": source_id,
            "status": status,
            "claimed_category": claimed_category,  # 渠道自称分类，可能错误
            "channel_ref": channel_ref,
            "attempt_at": attempt_at,
        }, received_at)
        return {"receipt_seq": event.seq, "received_at": event.at}

    @staticmethod
    def _fingerprint(token: str) -> str:
        return hmac.new(b"audit-only", token.encode("utf-8"), "sha256").hexdigest()[:12]
