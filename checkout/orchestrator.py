"""收银台方案编排。

给定（地区、商户 MCC、时刻、灰度单元），输出该时刻依法应呈现的收银台
方案：

- 按政策版本分组（银行卡/余额等支付工具一组，信贷、资管、分期各自
  独立成组，绝不混排）；
- 组与选项的顺序由政策版本决定；
- 每个金融产品选项附政策要求的必要风险信息；
- 每个选项带可选状态（可选 / 不可选及原因码）；
- **任何资金源都不由服务端预选**：方案里没有 selected/默认项，
  扣款必须引用用户在本次会话中的明确选择。

方案可短期缓存，但缓存有效期取「TTL 与方案中所有生效区间边界的最早
值」，旧方案到期即失效；政策发布、提升、资金源上下线/冻结都会主动
清空缓存，灰度配置不可能借缓存绕开地区与生效日。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from .catalog import Availability, Catalog, FundingSource
from .models import CST, Category, ComplianceError, iso, region_covers
from .policy import PolicyStore, Resolution

# 方案缓存上限秒数；真实有效期会再被生效区间边界截断
DEFAULT_TTL_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class CheckoutOption:
    source: FundingSource
    availability: Availability

    @property
    def selectable(self) -> bool:
        return self.availability.available

    @property
    def selected(self) -> bool:
        """服务端永不预选：恒为 False。"""
        return False

    @property
    def category(self) -> Category:
        return self.source.category

    def to_json(self) -> dict[str, Any]:
        return {
            "source_id": self.source.source_id,
            "display_name": self.source.display_name,
            "category": self.source.category.value,
            "provider": self.source.provider.to_json(),
            "selectable": self.selectable,
            # 服务端永远不预选：字段固定为 false，客户端不得渲染默认勾选
            "selected": False,
            "state": "selectable" if self.selectable else "unavailable",
            "unavailable_reason": None
            if self.selectable
            else self.availability.to_json(),
            "risk_disclosures": list(self.source.risk_notes),
        }


@dataclass(frozen=True, slots=True)
class CheckoutGroup:
    key: str
    title: str
    order: int
    risk_disclosures: tuple[str, ...]
    options: tuple[CheckoutOption, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "order": self.order,
            "risk_disclosures": list(self.risk_disclosures),
            "options": [o.to_json() for o in self.options],
        }


@dataclass(frozen=True, slots=True)
class CheckoutPlan:
    """一次收银台方案的完整快照（也是缓存与审计的载体）。"""

    plan_id: str
    policy_id: str
    policy_version: int
    policy_mode: str
    policy_window: dict[str, Any]
    region: str
    mcc: str
    moment: datetime
    generated_at: datetime
    groups: tuple[CheckoutGroup, ...]
    # 全局必要提示（如“借贷有风险”），与组内提示分开
    global_disclosures: tuple[str, ...]
    source_ids: tuple[str, ...]

    def find_option(self, source_id: str) -> CheckoutOption | None:
        for g in self.groups:
            for o in g.options:
                if o.source.source_id == source_id:
                    return o
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "policy_source": {
                "policy_id": self.policy_id,
                "version": self.policy_version,
                "mode": self.policy_mode,
                "window": self.policy_window,
            },
            "region": self.region,
            "mcc": self.mcc,
            "moment": iso(self.moment),
            "generated_at": iso(self.generated_at),
            "global_disclosures": list(self.global_disclosures),
            "preselection": "forbidden",
            "groups": [g.to_json() for g in self.groups],
            "source_ids": list(self.source_ids),
        }


_CacheEntry = tuple[CheckoutPlan, float]  # plan, epoch 过期时刻


class PlanCache:
    """带绝对到期时刻的方案缓存。"""

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        timer: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._timer = timer
        self._lock = threading.Lock()
        self._items: dict[Any, _CacheEntry] = {}

    def get(self, key: Any) -> CheckoutPlan | None:
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            plan, expires_at = entry
            if self._timer() >= expires_at:
                self._items.pop(key, None)
                return None
            return plan

    def put(self, key: Any, plan: CheckoutPlan, boundary: datetime | None) -> None:
        expires_after = self._ttl
        if boundary is not None:
            delta = (boundary - plan.generated_at).total_seconds()
            # 区间边界是硬上限：到点旧方案必须失效
            expires_after = min(expires_after, max(0.0, delta))
        with self._lock:
            self._items[key] = (plan, self._timer() + expires_after)

    def invalidate_all(self) -> None:
        with self._lock:
            self._items.clear()


class CheckoutOrchestrator:
    """按政策版本编排收银台方案。"""

    def __init__(
        self,
        policies: PolicyStore,
        catalog: Catalog,
        policy_id: str,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], datetime] | None = None,
        timer: Callable[[], float] | None = None,
    ) -> None:
        self._policies = policies
        self._catalog = catalog
        self._policy_id = policy_id
        self._clock = clock
        self._cache = PlanCache(
            ttl_seconds, timer if timer is not None else time.monotonic
        )
        self._seq_lock = threading.Lock()
        self._seq = 0
        # 任何配置/状态变更都使缓存失效（发布、上下线、冻结）
        self._catalog.add_listener(self._cache.invalidate_all)

    # ---- 供管理端在发布/提升后调用 -------------------------------

    def invalidate_cache(self) -> None:
        self._cache.invalidate_all()

    # ---- 方案生成 ---------------------------------------------------

    def build_plan(
        self,
        *,
        region: str,
        mcc: str,
        moment: datetime | None = None,
        canary_unit: str | None = None,
        use_cache: bool = True,
    ) -> CheckoutPlan:
        moment = moment or self._now()
        key = (region, mcc, canary_unit)
        if use_cache:
            cached = self._cache.get(key)
            # 缓存命中仍需校验方案未越过区间边界（put 时已截断，双保险）
            if cached is not None and cached.moment <= moment:
                policy = self._policies.get(cached.policy_id, cached.policy_version)
                if policy is not None and policy.window.contains(moment):
                    return cached

        resolution = self._policies.resolve(
            self._policy_id, region, moment, canary_unit
        )
        if resolution is None:
            raise ComplianceError(
                "no-effective-policy",
                f"地区 {region} 在 {iso(moment)} 没有生效的收银政策版本",
            )

        groups = self._build_groups(resolution, region, mcc, moment)
        source_ids = tuple(
            o.source.source_id for g in groups for o in g.options
        )

        with self._seq_lock:
            self._seq += 1
            seq = self._seq
        plan = CheckoutPlan(
            plan_id=f"plan-{moment.strftime('%Y%m%d%H%M%S')}-{seq:06d}",
            policy_id=resolution.policy.policy_id,
            policy_version=resolution.policy.version,
            policy_mode=resolution.mode,
            policy_window=resolution.policy.window.to_json(),
            region=region,
            mcc=mcc,
            moment=moment,
            generated_at=self._now(),
            groups=groups,
            global_disclosures=self._global_disclosures(resolution),
            source_ids=source_ids,
        )
        # 缓存到期时刻不得晚于方案中任何一个生效区间的边界
        boundary = self._earliest_boundary(resolution, groups, moment)
        self._cache.put(key, plan, boundary)
        return plan

    def _build_groups(
        self,
        resolution: Resolution,
        region: str,
        mcc: str,
        moment: datetime,
    ) -> tuple[CheckoutGroup, ...]:
        policy = resolution.policy
        result: list[CheckoutGroup] = []
        for spec in policy.group_specs:
            options: list[CheckoutOption] = []
            for category in spec.categories:
                rule = policy.rule_for(category)
                if rule is None or not rule.checkout_visible:
                    continue
                for source in self._catalog.list_sources():
                    if source.category != category:
                        continue
                    if not region_covers(source.regions, region):
                        continue
                    if source.merchant_mccs and mcc not in source.merchant_mccs:
                        continue
                    # 不在自身生效区间的资金源完全不出现（而非置灰）
                    if not source.window.contains(moment):
                        continue
                    availability = self._catalog.check_availability(
                        source.source_id, region=region, mcc=mcc, moment=moment
                    )
                    if not rule.payment_allowed:
                        # 政策禁止该类别付款：展示为不可选并给出政策原因
                        availability = Availability(
                            False,
                            "payment-not-allowed-by-policy",
                            f"现行政策版本 v{policy.version} 不允许 "
                            f"{rule.label} 作为付款方式",
                        )
                    options.append(
                        CheckoutOption(source=source, availability=availability)
                    )
            if not options:
                continue
            # 组内顺序：政策类别 order，其次登记序（list_sources 保持插入序）
            options.sort(
                key=lambda o: (
                    (policy.rule_for(o.source.category).order),
                    o.source.source_id,
                )
            )
            disclosures = self._group_disclosures(policy, spec.categories)
            result.append(
                CheckoutGroup(
                    key=spec.key,
                    title=spec.title,
                    order=spec.order,
                    risk_disclosures=disclosures,
                    options=tuple(options),
                )
            )
        return tuple(result)

    @staticmethod
    def _group_disclosures(policy, categories: tuple[Category, ...]) -> tuple[str, ...]:
        seen: list[str] = []
        for category in categories:
            rule = policy.rule_for(category)
            if rule is None:
                continue
            for text in rule.required_risk_disclosure:
                if text not in seen:
                    seen.append(text)
        return tuple(seen)

    @staticmethod
    def _global_disclosures(resolution: Resolution) -> tuple[str, ...]:
        if resolution.mode == "canary":
            return ("本方案为灰度版本，如展示异常可退出后重试",)
        return ()

    @staticmethod
    def _earliest_boundary(
        resolution: Resolution,
        groups: tuple[CheckoutGroup, ...],
        moment: datetime,
    ) -> datetime | None:
        """方案中政策窗口与所有资金源窗口的最近结束时刻。"""
        candidates: list[datetime] = []
        end = resolution.policy.window.end
        if end is not None and end > moment:
            candidates.append(end)
        for g in groups:
            for o in g.options:
                wend = o.source.window.end
                if wend is not None and wend > moment:
                    candidates.append(wend)
        return min(candidates) if candidates else None

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(tz=CST)
