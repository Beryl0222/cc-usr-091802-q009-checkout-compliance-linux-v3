"""营销素材审核链。

营销素材不与资金源可用性混为一谈：资金源“可用于付款”不代表“可营销”。
每条素材依次经过若干条规则，任何一条命中即阻断，且必须在结论中留下
**具体命中依据**：规则编号、政策条款、命中片段及其位置、所在投放位/
字段、缺失要素等。

阻断范围（对应新规）：

1. 禁用表述（``prohibited_phrases``）；
2. 保本暗示（保本、零风险、稳赚不赔等）；
3. 只突出首期费用：用“首期 0 元/月供低至”吸引，却不披露总成本；
4. 优惠诱导借款（借钱立减、开通额度领红包）；
5. 支付机构为金融产品导流，以及在还款页等禁投位投放金融引导；
6. 为生效政策下不可营销的法律类别投放素材。

审核按**审核时**该地区生效的全量政策版本判定；也可显式指定版本复核。
每次审核产生只追加的审核记录。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable

from .models import CST, Category, ComplianceError, iso, parse_dt
from .policy import PolicyStore, PolicyVersion


class ReviewRejected(ComplianceError):
    """素材结构不合法（无法审核），区别于审核不通过。"""


@dataclass(frozen=True, slots=True)
class MaterialField:
    name: str
    text: str


@dataclass(frozen=True, slots=True)
class MarketingMaterial:
    material_id: str
    # 投放位：checkout / repayment（还款页）/ payment-result / home-banner ...
    surface: str
    fields: tuple[MaterialField, ...]
    # 投放主体类型：payment-institution / bank / consumer-finance / fund-manager
    publisher_type: str = ""
    # 推广的资金/产品类别（可空，纯品牌素材）
    promoted_category: Category | None = None
    # 是否已在素材中醒目披露总成本（分期/信贷必须为 true 才能提首期）
    total_cost_disclosed: bool = False
    # 已披露的费用要素：apr / total_fees / principal ...
    fee_elements: frozenset[str] = frozenset()
    submitted_at: datetime | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "material_id": self.material_id,
            "surface": self.surface,
            "publisher_type": self.publisher_type,
            "promoted_category": self.promoted_category.value
            if self.promoted_category
            else None,
            "total_cost_disclosed": self.total_cost_disclosed,
            "fee_elements": sorted(self.fee_elements),
            "fields": [{"name": f.name, "text": f.text} for f in self.fields],
        }


def material_from_dict(data: dict[str, Any]) -> MarketingMaterial:
    fields_raw = data.get("fields")
    if fields_raw is None:
        # 兼容单文案素材
        if not data.get("text"):
            raise ReviewRejected("bad-material", "素材缺少 fields/text")
        fields_raw = [{"name": "body", "text": data["text"]}]
    fields = tuple(MaterialField(f["name"], f["text"]) for f in fields_raw)
    category = data.get("promoted_category")
    return MarketingMaterial(
        material_id=data["material_id"],
        surface=data["surface"],
        fields=fields,
        publisher_type=data.get("publisher_type", ""),
        promoted_category=Category(category) if category else None,
        total_cost_disclosed=bool(data.get("total_cost_disclosed", False)),
        fee_elements=frozenset(data.get("fee_elements", [])),
        submitted_at=parse_dt(data["submitted_at"]) if data.get("submitted_at") else None,
    )


@dataclass(frozen=True, slots=True)
class Violation:
    """一条具体命中依据。"""

    rule_id: str
    clause: str
    message: str
    field: str | None = None
    matched: str | None = None
    start: int | None = None
    end: int | None = None
    surface: str | None = None
    extra: tuple[tuple[str, str], ...] = ()

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "rule_id": self.rule_id,
            "clause": self.clause,
            "message": self.message,
            "field": self.field,
            "matched": self.matched,
            "position": None
            if self.start is None
            else {"start": self.start, "end": self.end},
            "surface": self.surface,
        }
        if self.extra:
            payload["evidence"] = dict(self.extra)
        return payload


@dataclass(frozen=True, slots=True)
class ReviewResult:
    material_id: str
    decision: str  # approved | blocked
    policy_id: str
    policy_version: int
    reviewed_at: datetime
    violations: tuple[Violation, ...]
    material_snapshot: dict[str, Any]

    @property
    def approved(self) -> bool:
        return self.decision == "approved"

    def to_json(self) -> dict[str, Any]:
        return {
            "material_id": self.material_id,
            "decision": self.decision,
            "policy_source": {
                "policy_id": self.policy_id,
                "version": self.policy_version,
            },
            "reviewed_at": iso(self.reviewed_at),
            "violations": [v.to_json() for v in self.violations],
        }


# ---- 规则 ---------------------------------------------------------------

def _dedupe_overlapping(violations: list[Violation]) -> list[Violation]:
    """同一规则、同一字段内位置重叠的命中只保留最长匹配。

    例如「开通额度」已命中时，不再把其子串「额度」重复报告；
    无位置信息的命中（如类别不可营销）按 (rule_id, matched) 去重。
    """
    kept: list[Violation] = []
    # 同字段、位置重叠时长匹配优先（-(len) 升序使更长者先出现）
    ordered = sorted(
        violations,
        key=lambda x: (
            x.rule_id,
            x.field or "",
            x.start if x.start is not None else -1,
            -(len(x.matched or "")),
        ),
    )
    for v in ordered:
        duplicate = False
        for k in kept:
            if k.rule_id != v.rule_id or k.field != v.field:
                continue
            if v.start is None or k.start is None:
                duplicate = v.matched == k.matched
            else:
                duplicate = v.start < k.end and k.start < v.end
            if duplicate:
                break
        if not duplicate:
            kept.append(v)
    return kept


class Rule:
    rule_id = ""
    description = ""

    def check(
        self, material: MarketingMaterial, policy: PolicyVersion
    ) -> list[Violation]:
        raise NotImplementedError


def _iter_hits(patterns: Iterable[str], material: MarketingMaterial):
    """对每个字段做不区分大小写的子串匹配，产出 (field, pattern, start, end)。"""
    for f in material.fields:
        haystack = f.text.lower()
        for pattern in patterns:
            needle = pattern.lower()
            start = haystack.find(needle)
            while start != -1:
                yield f, pattern, start, start + len(needle)
                start = haystack.find(needle, start + 1)


class ProhibitedPhraseRule(Rule):
    rule_id = "prohibited-phrase"
    description = "禁用表述"

    def check(self, material, policy):
        clause = policy.marketing.clause_refs.get(
            self.rule_id, policy.policy_id
        )
        return [
            Violation(
                rule_id=self.rule_id,
                clause=clause,
                message=f"命中禁用表述“{pattern}”",
                field=f.name,
                matched=pattern,
                start=start,
                end=end,
                surface=material.surface,
            )
            for f, pattern, start, end in _iter_hits(
                policy.marketing.prohibited_phrases, material
            )
        ]


class GuaranteedReturnRule(Rule):
    rule_id = "guaranteed-return"
    description = "保本/无风险暗示"

    def check(self, material, policy):
        clause = policy.marketing.clause_refs.get(self.rule_id, policy.policy_id)
        return [
            Violation(
                rule_id=self.rule_id,
                clause=clause,
                message=f"命中保本/无风险暗示“{pattern}”，资管产品不得承诺保本保收益",
                field=f.name,
                matched=pattern,
                start=start,
                end=end,
                surface=material.surface,
            )
            for f, pattern, start, end in _iter_hits(
                policy.marketing.guaranteed_return_patterns, material
            )
        ]


class BorrowingInducementRule(Rule):
    rule_id = "borrowing-inducement"
    description = "优惠诱导借款"

    def check(self, material, policy):
        clause = policy.marketing.clause_refs.get(self.rule_id, policy.policy_id)
        # 借贷相关词（额度/借款/借钱…）与优惠词（立减/返现/红包…）在同一
        # 字段共现，构成“以优惠诱导借贷”；金融关键词来自政策快照
        inducement_words = sorted(policy.marketing.financial_keywords)
        benefit_patterns = ("立减", "返现", "红包", "免费拿", "0元购", "白拿", "领券")
        violations: list[Violation] = []
        for f in material.fields:
            low = f.text.lower()
            benefits = [b for b in benefit_patterns if b in low]
            if not benefits:
                continue
            hits = []
            for word in inducement_words:
                pos = low.find(word.lower())
                if pos != -1:
                    hits.append((word, pos))
            for word, pos in hits:
                benefit = next(
                    (b for b in benefits if b in low), benefits[0]
                )
                violations.append(
                    Violation(
                        rule_id=self.rule_id,
                        clause=clause,
                        message=(
                            f"以优惠“{benefit}”诱导使用{word}，"
                            "不得以优惠诱导消费者产生借贷"
                        ),
                        field=f.name,
                        matched=word,
                        start=pos,
                        end=pos + len(word),
                        surface=material.surface,
                        extra=(("co_occurring_benefit", benefit),),
                    )
                )
        return violations


class FirstInstallmentOnlyRule(Rule):
    rule_id = "first-installment-only"
    description = "只突出首期费用、不披露总成本"

    def check(self, material, policy):
        clause = policy.marketing.clause_refs.get(self.rule_id, policy.policy_id)
        violations: list[Violation] = []
        for f, pattern, start, end in _iter_hits(
            policy.marketing.first_installment_only_patterns, material
        ):
            missing: list[str] = []
            if not material.total_cost_disclosed:
                missing.append("total_cost_disclosed")
            if "total_fees" not in material.fee_elements:
                missing.append("total_fees")
            if "apr" not in material.fee_elements:
                missing.append("apr")
            if not missing:
                # 已完整披露总成本与年化：仅使用首期价格不违规
                continue
            violations.append(
                Violation(
                    rule_id=self.rule_id,
                    message=(
                        f"突出“{pattern}”等首期/低月供表述，"
                        f"但未披露 {', '.join(missing)}，涉嫌只突出首期费用"
                    ),
                    clause=clause,
                    field=f.name,
                    matched=pattern,
                    start=start,
                    end=end,
                    surface=material.surface,
                    extra=(("missing_elements", ",".join(missing)),),
                )
            )
        return violations


class TrafficDiversionRule(Rule):
    rule_id = "payment-traffic-diversion"
    description = "支付机构导流金融产品 / 禁投页面金融广告"

    def check(self, material, policy):
        m = policy.marketing
        clause = m.clause_refs.get(self.rule_id, policy.policy_id)
        violations: list[Violation] = []

        # 情形一：还款页、支付结果页等禁投位出现金融引导关键词
        if material.surface in m.forbidden_traffic_surfaces:
            for f, pattern, start, end in _iter_hits(m.financial_keywords, material):
                violations.append(
                    Violation(
                        rule_id=self.rule_id,
                        message=(
                            f"禁投页面“{material.surface}”出现金融引导"
                            f"“{pattern}”，禁止在还款/支付链路为借贷理财导流"
                        ),
                        clause=clause,
                        field=f.name,
                        matched=pattern,
                        start=start,
                        end=end,
                        surface=material.surface,
                        extra=(("surface_banned", "true"),),
                    )
                )

        # 情形二：支付机构作为投放主体为金融产品导流（任何位置均禁止）
        if (
            material.publisher_type == "payment-institution"
            and material.promoted_category is not None
            and material.promoted_category.is_financial_product
        ):
            violations.append(
                Violation(
                    rule_id=self.rule_id,
                    message=(
                        "支付机构不得为金融产品导流："
                        f"推广类别 {material.promoted_category.value}"
                    ),
                    clause=clause,
                    field=None,
                    matched=material.promoted_category.value,
                    surface=material.surface,
                    extra=(
                        ("publisher_type", "payment-institution"),
                        ("promoted_category", material.promoted_category.value),
                    ),
                )
            )
        return violations


class NonMarketableCategoryRule(Rule):
    rule_id = "non-marketable-category"
    description = "生效政策下该类别不可营销（可用 ≠ 可营销）"

    def check(self, material, policy):
        if material.promoted_category is None:
            return []
        rule = policy.rule_for(material.promoted_category)
        if rule is None or rule.marketable:
            return []
        clause = policy.marketing.clause_refs.get(self.rule_id, policy.policy_id)
        return [
            Violation(
                rule_id=self.rule_id,
                message=(
                    f"{rule.label} 在政策 v{policy.version} 下可用于付款，"
                    "但不得进行营销推广"
                ),
                clause=clause,
                field=None,
                matched=material.promoted_category.value,
                surface=material.surface,
                extra=(
                    ("payment_allowed", str(rule.payment_allowed)),
                    ("marketable", str(rule.marketable)),
                ),
            )
        ]


DEFAULT_CHAIN: tuple[Rule, ...] = (
    ProhibitedPhraseRule(),
    GuaranteedReturnRule(),
    BorrowingInducementRule(),
    FirstInstallmentOnlyRule(),
    TrafficDiversionRule(),
    NonMarketableCategoryRule(),
)


class ReviewLedger:
    """只追加的审核记录台账。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: list[ReviewResult] = []

    def append(self, result: ReviewResult) -> None:
        with self._lock:
            self._records.append(result)

    def all(self) -> list[ReviewResult]:
        with self._lock:
            return list(self._records)

    def for_material(self, material_id: str) -> list[ReviewResult]:
        with self._lock:
            return [r for r in self._records if r.material_id == material_id]


class MarketingReviewer:
    """审核链执行器：选择生效政策版本，逐条规则执行并记录。"""

    def __init__(
        self,
        policies: PolicyStore,
        policy_id: str,
        *,
        chain: tuple[Rule, ...] = DEFAULT_CHAIN,
        ledger: ReviewLedger | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._policies = policies
        self._policy_id = policy_id
        self._chain = chain
        self.ledger = ledger or ReviewLedger()
        self._clock = clock

    def review(
        self,
        material: MarketingMaterial,
        *,
        region: str,
        moment: datetime | None = None,
        policy_version: int | None = None,
        persist: bool = True,
    ) -> ReviewResult:
        moment = moment or self._now()
        if policy_version is not None:
            policy = self._policies.get(self._policy_id, policy_version)
            if policy is None:
                raise ReviewRejected(
                    "unknown-policy-version",
                    f"政策 {self._policy_id} v{policy_version} 不存在",
                )
        else:
            # 审核按审核时刻的全量生效版本；灰度中的新规则不提前适用
            policy = self._policies.effective(self._policy_id, region, moment)
            if policy is None:
                raise ReviewRejected(
                    "no-effective-policy",
                    f"地区 {region} 在 {iso(moment)} 没有生效的营销政策版本",
                )

        violations: list[Violation] = []
        for rule in self._chain:
            # 规则随政策版本启用：旧规没有的规则（导流、优惠诱导、首期费用
            # 等）不得提前适用
            if rule.rule_id not in policy.marketing.enabled_rules:
                continue
            violations.extend(rule.check(material, policy))

        violations = _dedupe_overlapping(violations)

        result = ReviewResult(
            material_id=material.material_id,
            decision="blocked" if violations else "approved",
            policy_id=policy.policy_id,
            policy_version=policy.version,
            reviewed_at=moment,
            violations=tuple(violations),
            material_snapshot=material.to_json(),
        )
        if persist:
            self.ledger.append(result)
        return result

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(tz=CST)
