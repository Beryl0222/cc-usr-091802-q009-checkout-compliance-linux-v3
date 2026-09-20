"""领域模型：法律类别、提供方资质、资金来源登记项与更正记录。

分类法是新规则的核心：银行卡等支付工具与消费信贷、货币基金等资管
产品必须分区呈现，因此类别不可由提供方自行声明，只能由登记处的
``legal_category`` 决定，且任何更正都会留下版本与原因。
"""

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Optional

from . import timeutil


class Category(str, Enum):
    """法律类别，同时决定收银台分组。"""

    BANK_CARD = "bank-card"            # 银行卡（支付工具）
    PAYMENT_BALANCE = "payment-tool"   # 支付账户余额（支付工具）
    CREDIT = "credit"                  # 消费信贷：可付款，但不得无感借贷
    ASSET_MANAGEMENT = "asset-management"  # 货币基金等资管产品
    INSTALLMENT = "installment"        # 分期付款


# 属于"金融产品"而非"支付工具"的类别：可用于付款，但展示必须与
# 银行卡/余额分组隔离，且必须携带风险信息、不得被预选。
FINANCIAL_PRODUCT_CATEGORIES = frozenset(
    {Category.CREDIT, Category.ASSET_MANAGEMENT, Category.INSTALLMENT}
)
PAYMENT_TOOL_CATEGORIES = frozenset(
    {Category.BANK_CARD, Category.PAYMENT_BALANCE}
)


class MarketRole(str, Enum):
    """提供方在金融业务中的法律角色。支付机构不得为金融产品违规导流。"""

    BANK = "bank"
    PAYMENT_INSTITUTION = "payment-institution"  # 非银支付机构（支付牌照）
    CONSUMER_FINANCE = "consumer-finance"        # 持牌消费金融公司
    FUND_MANAGER = "fund-manager"                # 公募基金管理人
    MERCHANT_CREDIT = "merchant-credit"          # 商户自有分期（非金融机构）


@dataclass(frozen=True)
class License:
    """提供方资质。许可有编号、有效期，到期即视为不可用。"""

    code: str
    name: str
    authority: str
    valid_from: str
    valid_until: Optional[str] = None  # None 表示长期有效

    def valid_at(self, at) -> bool:
        at = timeutil.parse(at)
        start = timeutil.parse(self.valid_from)
        if at < start:
            return False
        if self.valid_until is not None and at >= timeutil.parse(self.valid_until):
            return False
        return True


@dataclass(frozen=True)
class CategoryCorrection:
    """错误分类的更正记录：错误回执可以存在，但必须被追溯纠正。"""

    source_id: str
    claimed: Category
    corrected: Category
    reason: str
    at: str
    by: str = "compliance"


@dataclass
class FundingSource:
    """资金来源登记项。

    ``legal_category`` 是权威分类；``merchant_scope`` 为 None 表示全商户，
    否则为允许的商户号集合；``region_scope`` 为允许的地区集合。
    生效区间左闭右开，支持同一资金源随政策切换换发。
    """

    source_id: str
    display_name: str
    legal_category: Category
    provider_id: str
    provider_name: str
    provider_role: MarketRole
    license: License
    effective_from: str
    effective_until: Optional[str] = None
    merchant_scope: Optional[frozenset] = None
    region_scope: Optional[frozenset] = None
    marketing_allowed: bool = False  # "可付款"不等于"可营销"
    risk_notice: Optional[str] = None
    corrections: tuple = field(default_factory=tuple)

    def effective_at(self, at) -> bool:
        at = timeutil.parse(at)
        return timeutil.between(
            timeutil.parse(self.effective_from), at,
            None if self.effective_until is None else timeutil.parse(self.effective_until),
        )

    def usable_for(self, merchant_id: str, region: str, at) -> tuple[bool, str]:
        """综合判断在给定时刻/商户/地区能否用于付款，并返回原因码。"""
        at = timeutil.parse(at)
        if not self.effective_at(at):
            return False, "outside-effective-window"
        if not self.license.valid_at(at):
            return False, "license-invalid-or-expired"
        if self.merchant_scope is not None and merchant_id not in self.merchant_scope:
            return False, "merchant-out-of-scope"
        if self.region_scope is not None and region not in self.region_scope:
            return False, "region-out-of-scope"
        return True, "ok"

    def with_category_correction(self, correction: CategoryCorrection) -> "FundingSource":
        """应用分类更正：旧值进入 corrections 留痕，类别改为更正值。"""
        if correction.source_id != self.source_id:
            raise ValueError("更正记录与资金源不匹配")
        return replace(
            self,
            legal_category=correction.corrected,
            corrections=self.corrections + (correction,),
        )

    @property
    def is_financial_product(self) -> bool:
        return self.legal_category in FINANCIAL_PRODUCT_CATEGORIES
