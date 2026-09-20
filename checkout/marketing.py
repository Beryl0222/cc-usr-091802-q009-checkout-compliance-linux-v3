"""营销素材审核链（与收银台编排相互独立）。

资金源"可付款"不意味着"可营销"：任何营销素材在投放前都必须通过本
审核链，命中任一阻断规则即 BLOCK，并留下具体命中依据（规则号、
条款、命中原文字段、字符偏移、适用规则集版本）。

阻断规则：

- R100 禁用表述：规则集明确列举的禁用词（低风险、保证收益、零费用…）
- R200 保本暗示：保本/刚性兑付/类存款等暗示，即使不是逐字禁用词
- R300 只突出首期费用：出现首期 0 元/零首付等却未同时披露总费用
- R400 支付机构导流：非银支付机构的支付场景为金融产品引流
- R500 默认勾选：以默认选中/默认开通方式让用户"无感借贷"
- R600 还款页广告：还款页投放信贷/分期借贷广告
- R700 优惠诱导借款：以立减返现等优惠作为开通/借款的唯一钩子，
  且未给出风险确认
"""

import hashlib
import threading
from dataclasses import dataclass, field
from enum import Enum

from . import timeutil
from .models import Category
from .models import FINANCIAL_PRODUCT_CATEGORIES


class Decision(str, Enum):
    APPROVED = "approved"
    BLOCKED = "blocked"


# ---- 规则集：随政策版本切换，审核结论记录所用规则集，供审计 ---------

@dataclass(frozen=True)
class Ruleset:
    ruleset_id: str
    prohibited_terms: dict  # {规则词: [别名...]}，匹配大小写不敏感
    principal_guarantee_terms: tuple
    first_installment_terms: tuple
    full_cost_disclosure_terms: tuple
    borrowing_incentive_terms: tuple


RULESET_V1 = Ruleset(
    ruleset_id="ruleset-2025-v1",
    prohibited_terms={
        "low-risk": ["low-risk", "低风险"],
        "guaranteed-return": ["guaranteed-return", "保证收益"],
        "no-cost": ["no-cost", "零费用"],
    },
    principal_guarantee_terms=(
        "保本", "保本保息", "刚性兑付", "100%兑付", "等同于存款", "和存款一样", "闭眼买", "零风险",
    ),
    first_installment_terms=(
        "首期0元", "首期0息", "首月0元", "首月仅需", "零首付", "0首付", "first installment only",
    ),
    full_cost_disclosure_terms=(
        "总费用", "总成本", "综合年化", "年化资金成本", "含全部费用", "费用合计",
    ),
    borrowing_incentive_terms=(
        "借款立减", "借钱返现", "开通额度送", "借款送", "分期立减", "领券借钱",
    ),
)

# 2026-09-30 生效的新规则：禁用词扩充，新增对"类存款"暗示的明确表述。
RULESET_V2 = Ruleset(
    ruleset_id="ruleset-2026-v2",
    prohibited_terms={
        "low-risk": ["low-risk", "低风险", "几乎无风险", "风险极低"],
        "guaranteed-return": [
            "guaranteed-return", "保证收益", "稳赚不赔", "收益有保障", "固定收益承诺",
        ],
        "no-cost": ["no-cost", "零费用", "0费率", "全程0收费", "分文不取"],
    },
    principal_guarantee_terms=(
        "保本", "保本保息", "刚性兑付", "100%兑付", "等同于存款", "和存款一样",
        "相当于存款", "存款级安全", "闭眼买", "零风险", "本金无忧",
    ),
    first_installment_terms=(
        "首期0元", "首期0息", "首月0元", "首月仅需", "零首付", "0首付",
        "首期仅", "first installment only",
    ),
    full_cost_disclosure_terms=(
        "总费用", "总成本", "综合年化", "年化资金成本", "含全部费用", "费用合计", "总还款额",
    ),
    borrowing_incentive_terms=(
        "借款立减", "借钱返现", "开通额度送", "借款送", "分期立减", "领券借钱",
        "借钱消费享折扣", "授信领红包",
    ),
)

RULESETS = {r.ruleset_id: r for r in (RULESET_V1, RULESET_V2)}

CLAUSE_BOOK = "金融产品网络营销管理规则"


@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    title: str
    clause: str
    field: str            # 命中的素材字段/路径
    snippet: str          # 命中原文字段（片段）
    start: int | None     # 字符偏移；上下文类规则为 None
    end: int | None
    detail: str

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "title": self.title,
            "clause": self.clause,
            "field": self.field,
            "snippet": self.snippet,
            "start": self.start,
            "end": self.end,
            "detail": self.detail,
        }


@dataclass
class MarketingMaterial:
    material_id: str
    text: str                       # 文案正文
    target_category: Category       # 推广的金融产品类别
    publisher_role: str             # 投放方角色（见 MarketRole.value）
    surface: str                    # 投放位置：checkout/repayment-page/...
    default_checked: bool = False   # 是否默认勾选/默认开通
    incentive: str = ""             # 优惠诱导文案，如"借款立减20"
    risk_confirmed: bool = False    # 是否经独立风险确认（非与勾选捆绑）


@dataclass
class ReviewDecision:
    material_id: str
    decision: Decision
    ruleset_id: str
    hits: list
    reviewed_at: str
    material_hash: str

    def to_dict(self) -> dict:
        return {
            "material_id": self.material_id,
            "decision": self.decision.value,
            "ruleset_id": self.ruleset_id,
            "hits": [h.to_dict() for h in self.hits],
            "reviewed_at": self.reviewed_at,
            "material_hash": self.material_hash,
        }


def _find_offsets(text: str, needle: str) -> tuple[int, int]:
    idx = text.casefold().find(needle.casefold())
    if idx < 0:
        return (-1, -1)
    return idx, idx + len(needle)


def _phrase_matches(text: str, phrase: str, *, negatable: bool = False) -> list[tuple[int, int]]:
    """找出短语在文案中的全部出现位置。

    negatable=True 时，紧邻否定前缀"不/未/非"的出现视为合规否定表述
    （如"不保本""不保证收益"），不计入命中。
    """
    folded = text.casefold()
    target = phrase.casefold()
    out = []
    start = 0
    while True:
        idx = folded.find(target, start)
        if idx < 0:
            break
        end = idx + len(target)
        if negatable and idx > 0 and text[idx - 1] in "不未非":
            start = end
            continue
        out.append((idx, end))
        start = end
    return out


def _dedupe_overlaps(candidates: list[tuple]) -> list[tuple]:
    """跨短语去重重叠命中：同一文案区间保留最长的短语。"""
    # candidates: (start, end, phrase)
    ordered = sorted(candidates, key=lambda c: (c[0], -(c[1] - c[0])))
    kept = []
    for cand in ordered:
        start, end, _ = cand
        if kept and start < kept[-1][1]:
            continue  # 与已保留的更长短语重叠
        kept.append(cand)
    return kept


def review_material(material: MarketingMaterial, ruleset: Ruleset, at) -> ReviewDecision:
    """按审核链顺序执行全部规则：阻断类规则全部要跑，命中全量留痕。"""
    at = timeutil.parse(at) if not hasattr(at, "tzinfo") else at
    text = material.text
    hits: list[RuleHit] = []

    # R100 禁用表述（识别"不保证收益"这类否定语境，并去重重叠词）
    term_candidates = []
    alias_of = {}
    for canonical, aliases in ruleset.prohibited_terms.items():
        for alias in aliases:
            for start, end in _phrase_matches(text, alias, negatable=True):
                term_candidates.append((start, end, alias))
                alias_of[(start, end)] = (canonical, alias)
    for start, end, alias in _dedupe_overlaps(term_candidates):
        canonical, alias = alias_of[(start, end)]
        hits.append(RuleHit(
            rule_id="R100", title="禁用表述",
            clause=f"{CLAUSE_BOOK}/{ruleset.ruleset_id}/prohibited:{canonical}",
            field="text", snippet=text[start:end], start=start, end=end,
            detail=f"命中规则集禁用词 {canonical!r}（别名 {alias!r}）",
        ))

    # R200 保本暗示（同样排除"不保本"等否定，并对重叠表述去重）
    guarantee_candidates = []
    for phrase in ruleset.principal_guarantee_terms:
        for start, end in _phrase_matches(text, phrase, negatable=True):
            guarantee_candidates.append((start, end, phrase))
    for start, end, phrase in _dedupe_overlaps(guarantee_candidates):
        hits.append(RuleHit(
            rule_id="R200", title="保本暗示",
            clause=f"{CLAUSE_BOOK}/{ruleset.ruleset_id}/no-principal-guarantee",
            field="text", snippet=text[start:end], start=start, end=end,
            detail="资管/信贷营销不得暗示保本保收益或类存款安全性",
        ))

    # R300 只突出首期费用（有首期表述但无总费用披露即命中）
    first_hit = None
    for phrase in ruleset.first_installment_terms:
        start, end = _find_offsets(text, phrase)
        if start >= 0:
            first_hit = (phrase, start, end)
            break
    if first_hit is not None:
        phrase, start, end = first_hit
        disclosed = any(_find_offsets(text, term)[0] >= 0
                        for term in ruleset.full_cost_disclosure_terms)
        if not disclosed:
            hits.append(RuleHit(
                rule_id="R300", title="只突出首期费用",
                clause=f"{CLAUSE_BOOK}/{ruleset.ruleset_id}/installment-full-cost",
                field="text", snippet=text[start:end], start=start, end=end,
                detail=("突出首期/首付费用却未同时披露总费用、总成本或综合年化成本；"
                        f"应出现披露词之一：{list(ruleset.full_cost_disclosure_terms)}"),
            ))

    # R400 支付机构为金融产品导流
    if (material.publisher_role == "payment-institution"
            and material.target_category in FINANCIAL_PRODUCT_CATEGORIES
            and material.surface in {
                "checkout", "repayment-page", "payment-success", "balance-page",
            }):
        hits.append(RuleHit(
            rule_id="R400", title="支付机构为金融产品导流",
            clause=f"{CLAUSE_BOOK}/{ruleset.ruleset_id}/no-payment-traffic-diversion",
            field="placement",
            snippet=f"publisher={material.publisher_role};surface={material.surface};"
                    f"target={material.target_category.value}",
            start=None, end=None,
            detail="非银行支付机构不得在支付链路页面为贷款、资管、分期等金融产品导流",
        ))

    # R500 默认勾选/默认开通
    if material.default_checked and material.target_category in (
        Category.CREDIT, Category.INSTALLMENT
    ):
        hits.append(RuleHit(
            rule_id="R500", title="默认勾选借贷",
            clause=f"{CLAUSE_BOOK}/{ruleset.ruleset_id}/no-default-borrowing",
            field="default_checked", snippet="default_checked=true",
            start=None, end=None,
            detail="不得通过默认勾选、默认开通让消费者无感产生借贷",
        ))

    # R600 还款页广告
    if material.surface == "repayment-page" and material.target_category in (
        Category.CREDIT, Category.INSTALLMENT
    ):
        hits.append(RuleHit(
            rule_id="R600", title="还款页借贷广告",
            clause=f"{CLAUSE_BOOK}/{ruleset.ruleset_id}/no-repayment-page-loan-ad",
            field="placement.surface", snippet="surface=repayment-page",
            start=None, end=None,
            detail="还款页面不得投放引导新增借款/分期的营销内容",
        ))

    # R700 优惠诱导借款（无独立风险确认）
    incentive_text = material.incentive or ""
    for phrase in ruleset.borrowing_incentive_terms:
        start, end = _find_offsets(incentive_text, phrase)
        if start >= 0:
            if material.target_category in (Category.CREDIT, Category.INSTALLMENT) \
                    and not material.risk_confirmed:
                hits.append(RuleHit(
                    rule_id="R700", title="优惠诱导借款",
                    clause=f"{CLAUSE_BOOK}/{ruleset.ruleset_id}/no-incentive-only-borrowing",
                    field="incentive", snippet=incentive_text[start:end],
                    start=start, end=end,
                    detail="不得仅以立减/返现/红包诱导开通额度或借款，且缺少独立风险确认",
                ))
            break

    material_hash = hashlib.sha256(
        "|".join([material.material_id, text, material.target_category.value,
                  material.publisher_role, material.surface,
                  str(material.default_checked), incentive_text]).encode("utf-8")
    ).hexdigest()
    decision = Decision.BLOCKED if hits else Decision.APPROVED
    return ReviewDecision(
        material_id=material.material_id,
        decision=decision,
        ruleset_id=ruleset.ruleset_id,
        hits=hits,
        reviewed_at=timeutil.iso(at),
        material_hash=material_hash,
    )


class MarketingReviewChain:
    """解析当前有效政策对应的规则集并审核，审核结论只追加保存。"""

    def __init__(self, policy_chain):
        self.policies = policy_chain
        self._lock = threading.RLock()
        self._records: list[ReviewDecision] = []

    def ruleset_at(self, at) -> Ruleset:
        policy = self.policies.effective_policy(at)
        ruleset = RULESETS.get(policy.marketing_ruleset)
        if ruleset is None:
            raise LookupError(f"政策 {policy.version} 引用了未知规则集 {policy.marketing_ruleset}")
        return ruleset

    def review(self, material: MarketingMaterial, at) -> ReviewDecision:
        at = timeutil.parse(at) if not hasattr(at, "tzinfo") else at
        result = review_material(material, self.ruleset_at(at), at)
        with self._lock:
            self._records.append(result)
        return result

    def records(self) -> list[ReviewDecision]:
        with self._lock:
            return list(self._records)
