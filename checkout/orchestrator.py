"""收银台方案编排。

合规红线：

- 按请求时刻解析**唯一有效政策版本**，依据其分组与顺序渲染；
- 金融产品与支付工具分组隔离，金融产品必须附风险信息；
- 任何资金源都不由服务端预选：每个条目 ``selected=False``，
  方案里 ``preselected_source`` 恒为 null，也不输出"推荐/默认"标记；
- 方案缓存有明确 ``expires_at``：政策版本窗口、资金源生效区间、
  资质有效期三者最早到期点；旧方案到期即失效，登记处修订后亦失效。
"""

import hashlib
import threading
from dataclasses import dataclass

from . import timeutil
from .models import Category, PAYMENT_TOOL_CATEGORIES

# 各类金融产品必须呈现的风险提示及其政策依据（条款标识随营销规则集）。
CATEGORY_RISK_NOTICES = {
    Category.CREDIT: {
        "text": "本渠道为消费信贷（借款），非支付账户余额；借贷有成本，请按需借款、按时还款。",
        "clause": "financial-marketing-rules/credit-risk-notice",
    },
    Category.ASSET_MANAGEMENT: {
        "text": "本渠道为资管产品（含货币基金），不保本、不保收益，申购赎回存在规则限制。",
        "clause": "financial-marketing-rules/no-principal-guarantee",
    },
    Category.INSTALLMENT: {
        "text": "本渠道为分期付款，含分期费用（以总费用为准，不仅限首期），请确认期数与总成本。",
        "clause": "financial-marketing-rules/installment-full-cost",
    },
}

_MAX_CACHE_TTL_SECONDS = 300  # 即使窗口很长，缓存也最多存活 5 分钟（应对新增资金源）


class PlanCacheExpired(RuntimeError):
    pass


@dataclass(frozen=True)
class CheckoutRequest:
    merchant_id: str
    region: str
    subject_key: str  # 用户/设备稳定标识，用于灰度分桶
    amount: str
    at: object = None  # datetime 或 ISO 字符串；缺省取服务端时钟
    checkout_id: str | None = None  # 客户端提供的结账会话标识


class CheckoutOrchestrator:
    def __init__(self, registry, policy_chain, *, clock=timeutil.now):
        self.registry = registry
        self.policies = policy_chain
        self._clock = clock
        self._cache: dict[str, tuple[object, dict]] = {}
        self._plans: dict[str, dict] = {}  # checkout_id -> 最新方案快照（供选择/支付回查）
        self._lock = threading.RLock()

    def build_plan(self, req: CheckoutRequest, *, use_cache: bool = True) -> dict:
        at = timeutil.parse(req.at) if req.at is not None else self._clock()
        policy = self.policies.effective_policy(at)
        cache_key = self._cache_key(req, policy, at)
        if use_cache:
            cached = self._get_cached(cache_key, at)
            if cached is not None:
                cached = dict(cached, from_cache=True)
                if req.checkout_id:
                    self._remember(req.checkout_id, cached)
                return cached
        plan = self._render(req, policy, at)
        self._put_cache(cache_key, plan, at)
        if req.checkout_id:
            self._remember(req.checkout_id, plan)
        return plan

    def rendered_plan(self, checkout_id: str) -> dict:
        """取回某结账会话最近一次呈现的方案。"""
        with self._lock:
            try:
                return self._plans[checkout_id]
            except KeyError:
                raise LookupError(checkout_id) from None

    def _remember(self, checkout_id: str, plan: dict) -> None:
        with self._lock:
            self._plans[checkout_id] = plan

    def _render(self, req: CheckoutRequest, policy, at) -> dict:
        scan = self.registry.usable_sources(req.merchant_id, req.region, at)
        groups = []
        excluded = []
        for group_id, cats in policy.group_order:
            items = []
            # 顺序确定：先按政策规定的类别顺序，同类内按 source_id 稳定排序
            for cat in cats:
                cat_sources = sorted(
                    ((sid, triple) for sid, triple in scan.items()
                     if triple[0].legal_category == cat),
                    key=lambda kv: kv[0],
                )
                for source_id, (source, ok, reason) in cat_sources:
                    if not ok:
                        excluded.append({
                            "source_id": source_id,
                            "category": source.legal_category.value,
                            "reason": reason,
                            "presented": False,
                        })
                        continue
                    items.append(self._render_item(source, policy, req, at))
            groups.append({"group_id": group_id, "items": items})

        self._assert_no_cross_group_leak(groups, policy)

        active_grays = tuple(
            gray.feature
            for gray in policy.grays
            if gray.hits(req.region, at, req.subject_key)
        )
        expires_at = self._compute_expiry(policy, [s for s, ok, _ in scan.values() if ok], at)
        plan = {
            "plan_id": self._plan_id(req, policy, groups),
            "generated_at": timeutil.iso(at),
            "as_of": timeutil.iso(at),
            "merchant_id": req.merchant_id,
            "region": req.region,
            "amount": req.amount,
            "policy": {
                "chain_id": policy.chain_id,
                "version": policy.version,
                "legal_source": policy.legal_source,
            },
            "groups": groups,
            "excluded": excluded,
            "gray_features": list(active_grays),
            # 合规红线：没有任何服务端预选
            "preselected_source": None,
            "selection_required": True,
            "selection_mode": "user-explicit-only",
            "expires_at": timeutil.iso(expires_at),
            "from_cache": False,
        }
        return plan

    def _render_item(self, source, policy, req: CheckoutRequest, at) -> dict:
        item = {
            "source_id": source.source_id,
            "display_name": source.display_name,
            "category": source.legal_category.value,
            "group_kind": (
                "financial-product" if source.is_financial_product else "payment-tool"
            ),
            "provider": {
                "id": source.provider_id,
                "name": source.provider_name,
                "role": source.provider_role.value,
                "license": source.license.code,
            },
            "selectable": True,
            "selected": False,  # 恒为 False：禁止默认勾选/预选
            "default": False,
            "recommended": False,
            "marketing_allowed": source.marketing_allowed,
        }
        if source.is_financial_product and policy.require_risk_notice:
            required = CATEGORY_RISK_NOTICES[source.legal_category]
            item["required_risk_notice"] = {
                "text": source.risk_notice or required["text"],
                "clause": required["clause"],
                "ruleset": policy.marketing_ruleset,
            }
        return item

    def _assert_no_cross_group_leak(self, groups, policy) -> None:
        """纵深防御：支付工具与金融产品不得出现在同一展示分组。"""
        if not policy.require_separate_financial_group:
            return
        seen_groups: dict[Category, str] = {}
        for group in groups:
            for item in group["items"]:
                cat = Category(item["category"])
                kind = item["group_kind"]
                is_fin = cat in FIN_CATS
                if is_fin != (kind == "financial-product"):
                    raise RuntimeError(f"资金源 {item['source_id']} 分组归类错误")
                if cat in seen_groups and seen_groups[cat] != group["group_id"]:
                    raise RuntimeError(f"类别 {cat.value} 跨越多个分组")
                seen_groups[cat] = group["group_id"]
        ids = [g["group_id"] for g in groups]
        if len(set(ids)) != len(ids):
            raise RuntimeError("分组标识重复")

    def _compute_expiry(self, policy, sources, at):
        candidates = []
        from datetime import timedelta
        if policy.effective_until is not None:
            candidates.append(timeutil.parse(policy.effective_until))
        for source in sources:
            if source.effective_until is not None:
                candidates.append(timeutil.parse(source.effective_until))
            if source.license.valid_until is not None:
                candidates.append(timeutil.parse(source.license.valid_until))
        ttl_cap = at + timedelta(seconds=_MAX_CACHE_TTL_SECONDS)
        if candidates:
            return min(min(candidates), ttl_cap)
        return ttl_cap

    def _cache_key(self, req: CheckoutRequest, policy, at) -> str:
        raw = "|".join([
            req.merchant_id, req.region, req.amount,
            policy.chain_id, policy.version, str(self.registry.revision),
        ])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _plan_id(self, req: CheckoutRequest, policy, groups) -> str:
        raw = [req.merchant_id, req.region, req.amount,
               policy.version, str(self.registry.revision)]
        for group in groups:
            raw.append(group["group_id"])
            raw.extend(i["source_id"] for i in group["items"])
        return hashlib.sha256("|".join(raw).encode("utf-8")).hexdigest()[:16]

    def _get_cached(self, key: str, at):
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            expires_at, plan = entry
            if at >= expires_at:
                # 旧方案到期即失效
                del self._cache[key]
                return None
            return plan

    def _put_cache(self, key: str, plan: dict, at) -> None:
        expires_at = timeutil.parse(plan["expires_at"])
        with self._lock:
            self._cache[key] = (expires_at, plan)

    def invalidate_cache(self) -> None:
        with self._lock:
            self._cache.clear()


FIN_CATS = frozenset(
    {Category.CREDIT, Category.ASSET_MANAGEMENT, Category.INSTALLMENT}
)
assert FIN_CATS.isdisjoint(PAYMENT_TOOL_CATEGORIES)
