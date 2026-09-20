"""横跨 2026-09-30 的端到端场景装配与审计报告。

时间线（均为北京时间 +08:00）：

- 旧版政策 financial-marketing-2025：2025-09-30 ~ 2026-09-30 00:00
- 新版政策 financial-marketing-2026：2026-09-30 00:00 起
- 09-28  biz-paylater 被错误登记为支付工具
- 09-29 22:15 结账 C1（旧规）：用户选消费信贷；22:20 渠道超时，
           22:25 沿用同一渠道重试成功
- 09-29 23:40 合规纠正 biz-paylater 分类（payment-tool → credit）
- 09-30 00:20 结账 C2（新规）：biz-paylater 已进入金融产品分组
- 09-30 09:12 收到 C1 扣款的迟到确认
- 10-01 10:00 又收到 C1 的错误分类回执（自称 payment-tool）
"""

import json
from dataclasses import dataclass
from pathlib import Path

from . import timeutil
from .audit import AuditLog
from .marketing import (
    MarketingMaterial, MarketingReviewChain, RULESET_V1, RULESET_V2,
)
from .models import (
    Category, FundingSource, License, MarketRole, CategoryCorrection,
)
from .orchestrator import CheckoutOrchestrator, CheckoutRequest
from .payments import PaymentError, PaymentService
from .policy import GrayConfig, PolicyChain, PolicyVersion
from .registry import Registry

T_OLD = "2026-09-29T22:15:00+08:00"
T_BOUNDARY = "2026-09-30T00:00:00+08:00"
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _licenses():
    return {
        "bank": License("BK-ICBC-001", "金融许可证", "国家金融监督管理总局",
                        "2020-01-01T00:00:00+08:00"),
        "payment": License("PI-ZF-2026-08", "支付业务许可证", "中国人民银行",
                           "2021-05-01T00:00:00+08:00",
                           "2027-01-01T00:00:00+08:00"),
        "consumer": License("CF-XFJ-2024-03", "消费金融牌照", "国家金融监督管理总局",
                            "2024-03-01T00:00:00+08:00"),
        "fund": License("FM-MF-2019-17", "公募基金管理资格", "中国证监会",
                        "2019-06-01T00:00:00+08:00"),
    }


def build_world(*, corrected: bool = False):
    """构建登记处、政策链、编排器、审核链、支付服务与审计日志。

    corrected=True 时直接施加分类更正（用于在线服务的合规当前态）；
    场景复原则用 False，以便在时间线上重放更正动作。
    """
    lic = _licenses()
    registry = Registry()

    registry.register(FundingSource(
        source_id="icbc-debit", display_name="工商银行储蓄卡(1027)",
        legal_category=Category.BANK_CARD,
        provider_id="icbc", provider_name="中国工商银行", provider_role=MarketRole.BANK,
        license=lic["bank"], effective_from="2025-01-01T00:00:00+08:00",
    ))
    registry.register(FundingSource(
        source_id="pay-balance", display_name="支付余额",
        legal_category=Category.PAYMENT_BALANCE,
        provider_id="payco", provider_name="支付通", provider_role=MarketRole.PAYMENT_INSTITUTION,
        license=lic["payment"], effective_from="2025-01-01T00:00:00+08:00",
    ))
    registry.register(FundingSource(
        source_id="cf-credit", display_name="消金信用付",
        legal_category=Category.CREDIT,
        provider_id="cfco", provider_name="普惠消费金融", provider_role=MarketRole.CONSUMER_FINANCE,
        license=lic["consumer"], effective_from="2025-01-01T00:00:00+08:00",
        risk_notice="信用付为消费信贷，借款有利息成本，逾期影响信用记录。",
        marketing_allowed=False,
    ))
    registry.register(FundingSource(
        source_id="mmf-fund", display_name="零钱宝货币基金",
        legal_category=Category.ASSET_MANAGEMENT,
        provider_id="fundco", provider_name="稳健基金管理有限公司", provider_role=MarketRole.FUND_MANAGER,
        license=lic["fund"], effective_from="2025-01-01T00:00:00+08:00",
        region_scope=frozenset({"CN-GD", "CN-SH"}),
        risk_notice="货币基金不保本、不保收益，不等同于银行存款。",
        marketing_allowed=True,  # 可营销仍须逐素材过审核链
    ))
    registry.register(FundingSource(
        source_id="merchant-installment", display_name="商户分期",
        legal_category=Category.INSTALLMENT,
        provider_id="m1001", provider_name="示例商户", provider_role=MarketRole.MERCHANT_CREDIT,
        license=License("MC-REG-2026", "商事登记", "市场监管部门",
                        "2026-01-01T00:00:00+08:00"),
        effective_from="2026-01-01T00:00:00+08:00",
        merchant_scope=frozenset({"m1001"}),
        risk_notice="分期产生总费用，请按期数确认总成本，不仅限首期。",
    ))
    # 错误登记：先用支付工具类别进入登记处，之后更正（留痕）。
    registry.register(FundingSource(
        source_id="biz-paylater", display_name="先用后付",
        legal_category=Category.PAYMENT_BALANCE,  # 错误分类
        provider_id="cfco", provider_name="普惠消费金融", provider_role=MarketRole.CONSUMER_FINANCE,
        license=lic["consumer"], effective_from="2026-09-28T00:00:00+08:00",
        risk_notice="先用后付为消费信贷，逾期将产生费用并影响信用。",
    ))

    gray = GrayConfig(
        feature="new-risk-card-copy",
        regions=frozenset({"CN-SH"}),
        effective_from=T_BOUNDARY,
        bucket_percent=100,  # 演示场景固定上海全量；生产可按 subject_key 分桶
        salt="risk-card-2026",
    )
    chain = PolicyChain("financial-product-marketing")
    old_policy = PolicyVersion(
        chain_id="financial-product-marketing",
        version="financial-marketing-2025",
        effective_from="2025-09-30T00:00:00+08:00",
        effective_until=T_BOUNDARY,
        legal_source="金融产品网络营销管理规则（2025年版）",
        marketing_ruleset=RULESET_V1.ruleset_id,
    )
    new_policy = PolicyVersion(
        chain_id="financial-product-marketing",
        version="financial-marketing-2026",
        effective_from=T_BOUNDARY,
        effective_until=None,
        legal_source="金融产品网络营销管理规则（2026年修订，2026-09-30施行）",
        marketing_ruleset=RULESET_V2.ruleset_id,
        grays=(gray,),
    )
    token = chain.head_token()
    chain.publish(old_policy, token)
    chain.publish(new_policy, chain.head_token())

    audit = AuditLog()
    for policy in (old_policy, new_policy):
        audit.append("policy.published", {
            "chain_id": policy.chain_id,
            "version": policy.version,
            "effective_from": policy.effective_from,
            "effective_until": policy.effective_until,
            "legal_source": policy.legal_source,
            "marketing_ruleset": policy.marketing_ruleset,
        }, timeutil.now())

    def gateway(source, attempt_no):
        """模拟渠道：cf-credit 首次超时，重试成功；其余直接成功。"""
        if source.source_id == "cf-credit" and attempt_no == 1:
            raise PaymentError("channel-timeout", "渠道响应超时")

    payments = PaymentService(registry, audit, gateway=gateway)
    orchestrator = CheckoutOrchestrator(registry, chain)
    reviews = MarketingReviewChain(chain)

    if corrected:
        registry.correct_category(CategoryCorrection(
            source_id="biz-paylater",
            claimed=Category.PAYMENT_BALANCE,
            corrected=Category.CREDIT,
            reason="先用后付由持牌消金提供资金、形成贷款债权，法律类别为消费信贷，"
                   "不得归入支付工具分组",
            at="2026-09-29T23:40:00+08:00",
        ))

    return types(registry, chain, audit, payments, orchestrator, reviews)


@dataclass
class types:
    registry: Registry
    policies: PolicyChain
    audit: AuditLog
    payments: PaymentService
    orchestrator: CheckoutOrchestrator
    reviews: MarketingReviewChain


def run_scenario(world: types | None = None) -> dict:
    w = world or build_world()

    # ---- 结账 C1：09-29 22:15，旧规；信用付首次超时，重试成功 --------
    c1_req = CheckoutRequest(
        merchant_id="m1001", region="CN-GD", subject_key="user-9001",
        amount="368.00", at="2026-09-29T22:15:00+08:00",
    )
    c1_plan = w.orchestrator.build_plan(c1_req)
    w.audit.append("plan.rendered", {"checkout_id": "C1", "plan": c1_plan}, c1_req.at)

    sel1 = w.payments.record_selection(
        checkout_id="C1", source_id="cf-credit",
        merchant_id="m1001", region="CN-GD", plan=c1_plan,
        explicit=True, at="2026-09-29T22:16:30+08:00",
    )
    try:
        w.payments.charge(sel1.token, "368.00", "2026-09-29T22:20:00+08:00",
                          channel_ref="CH-7001")
    except PaymentError as exc:
        first_failure = exc.code
    else:
        first_failure = None
    retry_result = w.payments.retry(sel1.token, "368.00",
                                    "2026-09-29T22:25:00+08:00", channel_ref="CH-7002")

    # ---- 09-29 23:40：纠正 biz-paylater 的错误分类 -------------------
    correction = CategoryCorrection(
        source_id="biz-paylater",
        claimed=Category.PAYMENT_BALANCE,
        corrected=Category.CREDIT,
        reason="先用后付由持牌消金提供资金、形成贷款债权，法律类别为消费信贷，"
               "不得归入支付工具分组",
        at="2026-09-29T23:40:00+08:00",
    )
    w.registry.correct_category(correction)
    w.audit.append("source.category-corrected", {
        "source_id": "biz-paylater",
        "claimed_category": correction.claimed.value,
        "corrected_category": correction.corrected.value,
        "reason": correction.reason,
    }, correction.at)

    # ---- 结账 C2：09-30 00:20，新规 -----------------------------------
    c2_req = CheckoutRequest(
        merchant_id="m1001", region="CN-SH", subject_key="user-9002",
        amount="1299.00", at="2026-09-30T00:20:00+08:00",
    )
    c2_plan = w.orchestrator.build_plan(c2_req)
    w.audit.append("plan.rendered", {"checkout_id": "C2", "plan": c2_plan}, c2_req.at)
    sel2 = w.payments.record_selection(
        checkout_id="C2", source_id="icbc-debit",
        merchant_id="m1001", region="CN-SH", plan=c2_plan,
        explicit=True, at="2026-09-30T00:21:00+08:00",
    )
    w.payments.charge(sel2.token, "1299.00", "2026-09-30T00:21:10+08:00",
                      channel_ref="CH-8001")

    # ---- 渠道回执：迟到确认 + 错误分类（来自 fixtures/receipts.json）--
    fixtures = json.loads((FIXTURES_DIR / "receipts.json").read_text(encoding="utf-8"))
    for receipt in fixtures["receipts"]:
        w.payments.ingest_receipt(
            checkout_id=receipt["checkout_id"],
            source_id=receipt["source_id"],
            status=receipt["status"],
            claimed_category=receipt.get("claimed_category"),
            channel_ref=receipt["channel_ref"],
            attempt_at=receipt["attempt_at"],
            received_at=receipt["received_at"],
        )

    # ---- 营销审核：旧规下通过、新规下同表述被禁；七类阻断各一 ---------
    marketing_cases = _marketing_cases()
    review_results = []
    for case in marketing_cases:
        result = w.reviews.review(case, case_at[case.material_id])
        review_results.append(result.to_dict())

    report = {
        "c1_before_boundary": w.audit.reconstruct_checkout("C1", w.registry),
        "c2_after_boundary": w.audit.reconstruct_checkout("C2", w.registry),
        "first_charge_failure": first_failure,
        "retry_result": retry_result,
        "marketing_reviews": review_results,
        "policy_timeline": w.audit.effective_version_timeline(),
        "effective_version_at": {
            "2026-09-29T22:15:00+08:00": w.policies.effective_policy(
                "2026-09-29T22:15:00+08:00").version,
            T_BOUNDARY: w.policies.effective_policy(T_BOUNDARY).version,
            "2026-09-30T00:20:00+08:00": w.policies.effective_policy(
                "2026-09-30T00:20:00+08:00").version,
        },
    }
    return report


def _marketing_cases():
    return [
        # 旧规下：措辞合规，通过
        MarketingMaterial(
            material_id="AD-OLD-OK",
            text="零钱宝货币基金：产品不保本，过往业绩不预示未来表现，投资需谨慎。",
            target_category=Category.ASSET_MANAGEMENT,
            publisher_role="fund-manager", surface="fund-detail",
        ),
        # 同一"几乎无风险"措辞：旧规词表没有 → 过；新规扩充词表 → 禁（R100）
        MarketingMaterial(
            material_id="AD-OLD-LOW-RISK",
            text="零钱宝：几乎无风险的现金管理选择。",
            target_category=Category.ASSET_MANAGEMENT,
            publisher_role="fund-manager", surface="fund-detail",
        ),
        MarketingMaterial(
            material_id="AD-NEW-LOW-RISK",
            text="零钱宝：几乎无风险的现金管理选择。",
            target_category=Category.ASSET_MANAGEMENT,
            publisher_role="fund-manager", surface="fund-detail",
        ),
        # 保本暗示（新规）
        MarketingMaterial(
            material_id="AD-NEW-GUARANTEE",
            text="零钱宝：保本保息，和存款一样安心。",
            target_category=Category.ASSET_MANAGEMENT,
            publisher_role="fund-manager", surface="fund-detail",
        ),
        # 只突出首期费用（新规）
        MarketingMaterial(
            material_id="AD-NEW-FIRST-ONLY",
            text="商户分期：首期0元，轻松带走心仪商品。",
            target_category=Category.INSTALLMENT,
            publisher_role="merchant-credit", surface="checkout",
        ),
        # 支付机构为金融产品导流（新规）
        MarketingMaterial(
            material_id="AD-NEW-DIVERSION",
            text="付款时开通信用付，额度最高5万元。",
            target_category=Category.CREDIT,
            publisher_role="payment-institution", surface="checkout",
        ),
        # 默认勾选借贷（新规）
        MarketingMaterial(
            material_id="AD-NEW-DEFAULT",
            text="开通信用付完成付款",
            target_category=Category.CREDIT,
            publisher_role="payment-institution", surface="checkout",
            default_checked=True,
        ),
        # 还款页借贷广告（新规）
        MarketingMaterial(
            material_id="AD-NEW-REPAYMENT",
            text="还款有压力？再借一笔分期轻松还。",
            target_category=Category.INSTALLMENT,
            publisher_role="consumer-finance", surface="repayment-page",
        ),
        # 优惠诱导借款、无独立风险确认（新规）
        MarketingMaterial(
            material_id="AD-NEW-INCENTIVE",
            text="开通信用付付款",
            target_category=Category.CREDIT,
            publisher_role="consumer-finance", surface="checkout",
            incentive="借款立减20元",
        ),
    ]


case_at = {
    "AD-OLD-OK": "2026-09-29T10:00:00+08:00",
    "AD-OLD-LOW-RISK": "2026-09-29T10:05:00+08:00",
    "AD-NEW-LOW-RISK": "2026-09-30T08:00:00+08:00",
    "AD-NEW-GUARANTEE": "2026-09-30T08:05:00+08:00",
    "AD-NEW-FIRST-ONLY": "2026-09-30T08:10:00+08:00",
    "AD-NEW-DIVERSION": "2026-09-30T08:15:00+08:00",
    "AD-NEW-DEFAULT": "2026-09-30T08:20:00+08:00",
    "AD-NEW-REPAYMENT": "2026-09-30T08:25:00+08:00",
    "AD-NEW-INCENTIVE": "2026-09-30T08:30:00+08:00",
}


def main():
    report = run_scenario()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
