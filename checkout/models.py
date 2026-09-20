"""领域基础类型。

政策与资金源均围绕四个维度约束：法律类别、提供方资质、适用商户（MCC）、
生效区间与适用地区。时间一律使用带时区的 ``datetime``，避免跨 9 月 30 日
边界时出现歧义。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

# 北京时区是所有生效日的法定基准
CST = timezone(timedelta(hours=8), name="CST")


class Category(str, Enum):
    """资金来源的法律类别。

    支付工具与消费信贷、资管、分期必须可区分：可用于付款不代表可以
    混同展示，更不代表可以被营销。
    """

    BANK_CARD = "bank-card"              # 银行卡（支付工具）
    PAYMENT_TOOL = "payment-tool"        # 余额等支付账户工具
    CREDIT = "credit"                    # 消费信贷
    ASSET_MANAGEMENT = "asset-management"  # 货币基金等资管产品
    INSTALLMENT = "installment"          # 分期付款

    @property
    def is_payment_instrument(self) -> bool:
        """银行卡、余额等支付工具，可与信贷/资管分组展示。"""
        return self in (Category.BANK_CARD, Category.PAYMENT_TOOL)

    @property
    def is_financial_product(self) -> bool:
        """信贷、资管、分期属于金融产品，受营销规则约束。"""
        return self in (
            Category.CREDIT,
            Category.ASSET_MANAGEMENT,
            Category.INSTALLMENT,
        )


class Qualification(str, Enum):
    """提供方资质。目录登记时必须携带，且必须与法律类别匹配。"""

    BANK = "bank"                              # 商业银行
    PAYMENT_INSTITUTION = "payment-institution"  # 持牌支付机构
    CONSUMER_FINANCE = "consumer-finance"      # 持牌消费金融公司
    FUND_MANAGER = "fund-manager"              # 公募基金管理人
    TRUST = "trust"                            # 信托公司


# 类别允许的提供方资质：信贷只能由银行/消金提供，支付机构不得为金融产品
# 导流（营销规则），资管必须由基金管理人/信托提供。
_ALLOWED_QUALIFICATIONS: dict[Category, frozenset[Qualification]] = {
    Category.BANK_CARD: frozenset({Qualification.BANK}),
    Category.PAYMENT_TOOL: frozenset({Qualification.PAYMENT_INSTITUTION}),
    Category.CREDIT: frozenset({Qualification.BANK, Qualification.CONSUMER_FINANCE}),
    Category.ASSET_MANAGEMENT: frozenset({
        Qualification.FUND_MANAGER,
        Qualification.TRUST,
    }),
    Category.INSTALLMENT: frozenset({
        Qualification.BANK,
        Qualification.CONSUMER_FINANCE,
    }),
}


class ComplianceError(ValueError):
    """所有登记/校验失败的基类，携带机器可读错误码。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def parse_dt(value: str | datetime) -> datetime:
    """把 ISO-8601 字符串解析为带时区时间；裸时间按 +08:00 解释。"""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt


def cst(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
) -> datetime:
    """构造北京时间。"""
    return datetime(year, month, day, hour, minute, second, tzinfo=CST)


def now_cst() -> datetime:
    return datetime.now(tz=CST)


@dataclass(frozen=True, slots=True)
class TimeWindow:
    """左闭右开的生效区间 ``[start, end)``；end 为 None 表示持续有效。"""

    start: datetime
    end: datetime | None = None

    @classmethod
    def from_strings(cls, start: str, end: str | None = None) -> "TimeWindow":
        return cls(parse_dt(start), parse_dt(end) if end else None)

    def contains(self, moment: datetime) -> bool:
        if moment < self.start:
            return False
        return self.end is None or moment < self.end

    def overlaps(self, other: "TimeWindow") -> bool:
        if self.end is None or other.end is None:
            return self.start < (other.end or datetime.max.replace(tzinfo=timezone.utc)) \
                and other.start < (self.end or datetime.max.replace(tzinfo=timezone.utc))
        return self.start < other.end and other.start < self.end

    def to_json(self) -> dict[str, Any]:
        return {
            "start": iso(self.start),
            "end": iso(self.end) if self.end else None,
        }


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(CST).isoformat(timespec="seconds")


def qualification_allowed(category: Category, qualification: Qualification) -> bool:
    return qualification in _ALLOWED_QUALIFICATIONS[category]


# 国家级通配地区："*" 与 "CN" 覆盖所有省级地区
_NATIONAL_REGIONS = frozenset({"*", "CN"})


def region_covers(covered, region: str) -> bool:
    """登记地区是否覆盖请求地区（支持国家级 CN/* 与精确省级匹配）。"""
    covered = set(covered)
    return bool(covered & _NATIONAL_REGIONS) or region in covered


def to_category(value: str | Category) -> Category:
    try:
        return value if isinstance(value, Category) else Category(value)
    except ValueError as exc:
        raise ComplianceError("unknown-category", f"未知资金源类别: {value}") from exc


def to_qualification(value: str | Qualification) -> Qualification:
    try:
        return value if isinstance(value, Qualification) else Qualification(value)
    except ValueError as exc:
        raise ComplianceError(
            "unknown-qualification", f"未知提供方资质: {value}"
        ) from exc
