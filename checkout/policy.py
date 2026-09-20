"""政策版本存储。

一份 ``PolicyVersion`` 同时决定：

- 哪些资金源法律类别在收银台中可作为支付手段（``payment_allowed``）；
- 各类别在收银台中的分组、组内顺序与必须展示的风险提示；
- 营销侧的禁用词、保本暗示模式、首期费用与导流规则；
- 适用地区与生效区间（灰度只在生效区间之内、按地区放量，**不能**
  绕开地区与生效日）。

存储保证：对同一地区、同一放量阶段、任意时刻，始终只有一个生效版本。
新版本发布走 CAS（compare-and-swap，乐观并发控制），两个人并发发布时
后提交者会收到 ``PolicyConflict``，必须基于最新版本重试，杜绝双有效
版本与更新丢失。
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Iterable

from .models import Category, ComplianceError, TimeWindow, iso, parse_dt

# 旧规与新规的稳定标识
POLICY_V1 = "financial-marketing-2017"
POLICY_V2 = "financial-marketing-2026"


class PolicyConflict(ComplianceError):
    """并发发布冲突或同一地区同一时刻存在多个生效版本。"""


@dataclass(frozen=True, slots=True)
class GroupSpec:
    """收银台分组定义。"""

    key: str
    title: str
    categories: tuple[Category, ...]
    order: int

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "categories": [c.value for c in self.categories],
            "order": self.order,
        }


@dataclass(frozen=True, slots=True)
class CategoryRule:
    """单个法律类别在某版政策下的待遇。"""

    category: Category
    label: str
    # 可用于付款（信贷与货基新规下“仍可用于付款”）
    payment_allowed: bool
    # 是否允许出现在收银台（可付款但必须独立分组，不得与银行卡混排）
    checkout_visible: bool
    # 必须随选项呈现的风险信息（金融产品上收银台必须有）
    required_risk_disclosure: tuple[str, ...]
    group_key: str
    order: int
    # 该类别是否允许被营销（可付款 ≠ 可营销）
    marketable: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "label": self.label,
            "payment_allowed": self.payment_allowed,
            "checkout_visible": self.checkout_visible,
            "required_risk_disclosure": list(self.required_risk_disclosure),
            "group_key": self.group_key,
            "order": self.order,
            "marketable": self.marketable,
        }


@dataclass(frozen=True, slots=True)
class MarketingRule:
    """营销侧规则内容，在 PolicyVersion 上做不可变快照。"""

    prohibited_phrases: frozenset[str]
    guaranteed_return_patterns: frozenset[str]
    first_installment_only_patterns: frozenset[str]
    # 不具金融产品销售资质的页面（还款页、支付成功页等）出现借贷/理财
    # 引导，即支付机构为金融产品导流，一律阻断
    forbidden_traffic_surfaces: frozenset[str]
    financial_keywords: frozenset[str]
    # 该版本实际启用的审核规则 id；空集合表示全部启用（旧规可只开部分）
    enabled_rules: frozenset[str]
    # 引用的具体条款（命中时原样返回，构成“具体命中依据”）
    clause_refs: dict[str, str]

    def to_json(self) -> dict[str, Any]:
        return {
            "prohibited_phrases": sorted(self.prohibited_phrases),
            "guaranteed_return_patterns": sorted(self.guaranteed_return_patterns),
            "first_installment_only_patterns": sorted(
                self.first_installment_only_patterns
            ),
            "forbidden_traffic_surfaces": sorted(self.forbidden_traffic_surfaces),
            "financial_keywords": sorted(self.financial_keywords),
            "enabled_rules": sorted(self.enabled_rules),
            "clause_refs": dict(self.clause_refs),
        }


@dataclass(frozen=True, slots=True)
class PolicyVersion:
    """政策的一个不可变版本。"""

    policy_id: str
    version: int
    regions: frozenset[str]
    window: TimeWindow
    group_specs: tuple[GroupSpec, ...]
    category_rules: tuple[CategoryRule, ...]
    marketing: MarketingRule
    published_at: datetime
    # 灰度放量比例（0~100），100 为全量；仅在版本已生效且地区命中时有意义
    canary_percent: int = 0
    note: str = ""
    parent_version: int | None = None
    # 是否由灰度版本经 promote 显式交接而来：仅此路径允许与旧全量窗口
    # 重叠（新版本在其地区确定性遮蔽旧版本，解析结果仍唯一）
    promoted: bool = False

    def applies_to(self, region: str, moment: datetime) -> bool:
        return (region in self.regions or "*" in self.regions) and self.window.contains(
            moment
        )

    def canary_hit(self, unit: str) -> bool:
        """稳定哈希分桶；100 表示全量。灰度不得绕开生效日与地区。"""
        if self.canary_percent <= 0:
            return False
        if self.canary_percent >= 100:
            return True
        digest = hashlib.md5(
            f"{self.policy_id}:{self.version}:{unit}".encode("utf-8")
        ).hexdigest()
        bucket = int(digest[:8], 16) % 100
        return bucket < self.canary_percent

    def rule_for(self, category: Category) -> CategoryRule | None:
        for rule in self.category_rules:
            if rule.category == category:
                return rule
        return None

    def group(self, key: str) -> GroupSpec | None:
        for g in self.group_specs:
            if g.key == key:
                return g
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "regions": sorted(self.regions),
            "window": self.window.to_json(),
            "canary_percent": self.canary_percent,
            "published_at": iso(self.published_at),
            "note": self.note,
            "parent_version": self.parent_version,
            "groups": [g.to_json() for g in self.group_specs],
            "categories": [r.to_json() for r in self.category_rules],
            "marketing": self.marketing.to_json(),
        }


@dataclass(frozen=True, slots=True)
class Resolution:
    """版本解析结果；``mode`` 记入回执，供审计复原。

    - full：窗口内全量版本
    - canary：窗口内灰度版本且本请求命中灰度分桶
    - fallback：处于灰度期但未命中分桶，回退到刚到期的最近全量版本
    """

    policy: PolicyVersion
    mode: str


def _groups_from_data(groups: Iterable[dict[str, Any]]) -> tuple[GroupSpec, ...]:
    result = [
        GroupSpec(
            key=g["key"],
            title=g["title"],
            categories=tuple(Category(c) for c in g["categories"]),
            order=g["order"],
        )
        for g in groups
    ]
    return tuple(sorted(result, key=lambda g: g.order))


def _rules_from_data(rules: Iterable[dict[str, Any]]) -> tuple[CategoryRule, ...]:
    return tuple(
        CategoryRule(
            category=Category(r["category"]),
            label=r["label"],
            payment_allowed=r["payment_allowed"],
            checkout_visible=r.get("checkout_visible", True),
            required_risk_disclosure=tuple(r.get("required_risk_disclosure", [])),
            group_key=r["group_key"],
            order=r["order"],
            marketable=r.get("marketable", False),
        )
        for r in rules
    )


def _marketing_from_data(data: dict[str, Any]) -> MarketingRule:
    # 缺省启用全部规则；旧版本通过 enabled_rules 显式收窄
    enabled = data.get("enabled_rules")
    enabled_rules = (
        frozenset(enabled)
        if enabled is not None
        else frozenset(
            {
                "prohibited-phrase",
                "guaranteed-return",
                "borrowing-inducement",
                "first-installment-only",
                "payment-traffic-diversion",
                "non-marketable-category",
            }
        )
    )
    return MarketingRule(
        prohibited_phrases=frozenset(data.get("prohibited_phrases", [])),
        guaranteed_return_patterns=frozenset(
            data.get("guaranteed_return_patterns", [])
        ),
        first_installment_only_patterns=frozenset(
            data.get("first_installment_only_patterns", [])
        ),
        forbidden_traffic_surfaces=frozenset(
            data.get("forbidden_traffic_surfaces", [])
        ),
        financial_keywords=frozenset(data.get("financial_keywords", [])),
        enabled_rules=enabled_rules,
        clause_refs=dict(data.get("clause_refs", {})),
    )


def policy_from_dict(data: dict[str, Any]) -> PolicyVersion:
    """从夹具/配置字典构造不可变政策版本。"""
    regions_raw = data["regions"]
    regions = frozenset(regions_raw if isinstance(regions_raw, list) else [regions_raw])
    window = TimeWindow(
        parse_dt(data["effective_from"]),
        parse_dt(data["effective_to"]) if data.get("effective_to") else None,
    )
    return PolicyVersion(
        policy_id=data["policy_id"],
        version=data["version"],
        regions=regions,
        window=window,
        group_specs=_groups_from_data(data["groups"]),
        category_rules=_rules_from_data(data["categories"]),
        marketing=_marketing_from_data(data["marketing"]),
        published_at=parse_dt(data.get("published_at", window.start)),
        canary_percent=int(data.get("canary_percent", 100)),
        note=data.get("note", ""),
        parent_version=data.get("parent_version"),
    )


def _regions_overlap(a: frozenset[str], b: frozenset[str]) -> bool:
    return "*" in a or "*" in b or bool(a & b)


def _is_subset(a: frozenset[str], b: frozenset[str]) -> bool:
    """地区集合 a 是否为 b 的子集（b 含 * 时覆盖所有省级地区）。"""
    return "*" in b or ("*" not in a and a <= b)


def _shared_regions(a: frozenset[str], b: frozenset[str]) -> list[str]:
    if "*" in a or "*" in b:
        return ["*"]
    return sorted(a & b)


class PolicyStore:
    """线程安全的政策版本存储，保证地区+时刻唯一有效版本。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # policy_id -> version -> PolicyVersion
        self._versions: dict[str, dict[int, PolicyVersion]] = {}
        # policy_id -> 当前已发布最大版本（CAS 期望值）
        self._head: dict[str, int] = {}

    # ---- 注册与发布 -------------------------------------------------

    def register(self, policy: PolicyVersion) -> None:
        """登记（导入）一个已存在的版本，做一致性校验并推进 head。"""
        with self._lock:
            self._validate_locked(policy)
            self._versions.setdefault(policy.policy_id, {})[policy.version] = policy
            head = self._head.get(policy.policy_id)
            if head is None or policy.version > head:
                self._head[policy.policy_id] = policy.version

    def publish(
        self, policy: PolicyVersion, expected_version: int | None
    ) -> PolicyVersion:
        """发布新版本（CAS 乐观并发）。

        ``expected_version`` 必须等于当前 head（首次发布传 None）。
        两个运营人员并发发布时，后提交者收到 :class:`PolicyConflict`，
        必须拉取最新 head 后重试——杜绝更新丢失与双有效版本。
        """
        with self._lock:
            current = self._head.get(policy.policy_id)
            if expected_version != current:
                raise PolicyConflict(
                    "policy-conflict",
                    f"政策 {policy.policy_id} 当前 head="
                    f"{'v' + str(current) if current is not None else '无'}，"
                    f"基于 v{expected_version} 的发布已过期，请重试",
                )
            if current is not None and policy.version <= current:
                raise PolicyConflict(
                    "policy-conflict",
                    f"新版本号 {policy.version} 必须大于当前 head v{current}",
                )
            self._validate_locked(policy)
            self._versions.setdefault(policy.policy_id, {})[policy.version] = policy
            self._head[policy.policy_id] = policy.version
            return policy

    def promote(
        self,
        policy_id: str,
        version: int,
        expected_canary_percent: int | None = None,
    ) -> PolicyVersion:
        """把灰度版本原子提升为全量（CAS），发布事件会让旧缓存失效。

        ``expected_canary_percent`` 为调用方读到的当前灰度比例；两个人
        同时操作时第二个提交者会收到冲突，避免覆盖彼此的放量调整。
        """
        with self._lock:
            original = self._versions[policy_id][version]
            if (
                expected_canary_percent is not None
                and expected_canary_percent != original.canary_percent
            ):
                raise PolicyConflict(
                    "policy-conflict",
                    f"v{version} 灰度比例已变为 {original.canary_percent}，"
                    f"基于 {expected_canary_percent} 的提升被拒绝",
                )
            if original.canary_percent >= 100:
                raise PolicyConflict(
                    "already-full", f"v{version} 已是全量版本，无需提升"
                )
            promoted = replace(original, canary_percent=100, promoted=True)
            self._validate_locked(promoted)
            self._versions[policy_id][version] = promoted
            return promoted

    def _validate_locked(self, policy: PolicyVersion) -> None:
        if not policy.regions:
            raise ComplianceError("empty-regions", "政策版本必须至少适用一个地区")
        if not 0 <= policy.canary_percent <= 100:
            raise ComplianceError("bad-canary", "灰度比例必须在 0~100 之间")
        group_keys = {g.key for g in policy.group_specs}
        for rule in policy.category_rules:
            if rule.group_key not in group_keys:
                raise ComplianceError(
                    "bad-group-ref",
                    f"类别 {rule.category.value} 引用了不存在的分组 {rule.group_key}",
                )
            if (
                rule.category.is_financial_product
                and rule.checkout_visible
                and not rule.required_risk_disclosure
            ):
                raise ComplianceError(
                    "missing-disclosure",
                    f"金融产品 {rule.category.value} 上收银台必须配置风险提示",
                )
        # 生效区间唯一性：同地区、同放量阶段的两个版本窗口不得重叠。
        # 全量与灰度允许重叠（灰度对命中用户优先，未命中走旧全量）；
        # 两个全量或两个灰度并存直接拒绝——这就是“并发发布配置时
        # 始终只有一个有效版本”的存储层保证。
        others = [
            p
            for p in self._versions.get(policy.policy_id, {}).values()
            if p.version != policy.version
        ]
        for other in others:
            if not _regions_overlap(policy.regions, other.regions):
                continue
            if not policy.window.overlaps(other.window):
                continue
            policy_full = policy.canary_percent >= 100
            other_full = other.canary_percent >= 100
            # 唯一的合法重叠交接：由灰度提升而来的新全量（CAS）遮蔽灰度期
            # 内共存的父版本全量。提升只可能收窄地区（如 SH 先转全量、
            # 全国仍用旧版），解析时高版本在其地区确定性胜出，有效版本唯一。
            handoff = (
                policy_full
                and policy.promoted
                and other_full
                and other.version == policy.parent_version
                and _regions_overlap(policy.regions, other.regions)
                and _is_subset(policy.regions, other.regions)
            )
            if handoff:
                continue
            both_full = policy_full and other_full
            both_canary = not policy_full and not other_full
            if both_full or both_canary:
                kind = "全量" if both_full else "灰度"
                raise PolicyConflict(
                    "overlapping-window",
                    f"v{policy.version} 与 v{other.version}（均为{kind}）在地区 "
                    f"{sorted(_shared_regions(policy.regions, other.regions))} "
                    "的生效区间重叠",
                )

    # ---- 查询 -------------------------------------------------------

    def head(self, policy_id: str) -> int | None:
        with self._lock:
            return self._head.get(policy_id)

    def get(self, policy_id: str, version: int) -> PolicyVersion | None:
        with self._lock:
            return self._versions.get(policy_id, {}).get(version)

    def list_versions(self, policy_id: str) -> list[PolicyVersion]:
        with self._lock:
            return [
                self._versions[policy_id][v]
                for v in sorted(self._versions.get(policy_id, {}))
            ]

    def effective(
        self, policy_id: str, region: str, moment: datetime
    ) -> PolicyVersion | None:
        """法律基线：该地区、该时刻的全量生效版本（不受灰度分桶影响）。

        营销审核未显式指定版本时使用该结果。正常情况下至多一个，
        若 invariant 被破坏则抛 :class:`PolicyConflict`。
        """
        with self._lock:
            candidates = sorted(
                (
                    p
                    for p in self._versions.get(policy_id, {}).values()
                    if p.canary_percent >= 100 and p.applies_to(region, moment)
                ),
                key=lambda p: p.version,
                reverse=True,
            )
        if not candidates:
            return None
        winner = candidates[0]
        # 允许的唯一重叠形态：高版本由灰度提升而来、在其地区遮蔽父版本。
        if len(candidates) > 1:
            for loser in candidates[1:]:
                valid_handoff = (
                    winner.promoted
                    and loser.version == winner.parent_version
                    and _is_subset(winner.regions, loser.regions)
                )
                if not valid_handoff:
                    raise PolicyConflict(
                        "multiple-effective",
                        f"{policy_id} 在 {region} @ {iso(moment)} 同时存在 "
                        f"{len(candidates)} 个无法定序的全量生效版本",
                    )
        return winner

    def resolve(
        self,
        policy_id: str,
        region: str,
        moment: datetime,
        canary_unit: str | None = None,
    ) -> Resolution | None:
        """收银台请求的唯一版本解析入口。

        灰度判定严格排在地区与生效日校验之后，因此灰度不可能绕开
        地区与生效日：

        1. 窗口内、地区命中的灰度版本，且本请求命中分桶 → canary；
        2. 否则取窗口内全量版本 → full；
        3. 灰度期内未命中分桶且窗口内无全量版本 → 回退到刚到期的
           最近全量版本（fallback），即“灰度没中继续用旧方案”。
        """
        with self._lock:
            versions = sorted(
                self._versions.get(policy_id, {}).values(),
                key=lambda p: p.version,
                reverse=True,
            )

        active_canary = [
            p
            for p in versions
            if p.canary_percent < 100 and p.applies_to(region, moment)
        ]
        if canary_unit:
            for p in active_canary:
                if p.canary_hit(canary_unit):
                    return Resolution(p, "canary")

        full_in_window = [
            p
            for p in versions
            if p.canary_percent >= 100 and p.applies_to(region, moment)
        ]
        if full_in_window:
            return Resolution(full_in_window[0], "full")

        if active_canary:
            predecessors = [
                p
                for p in versions
                if p.canary_percent >= 100
                and (region in p.regions or "*" in p.regions)
                and p.window.start <= moment
            ]
            if predecessors:
                return Resolution(predecessors[0], "fallback")
        return None
