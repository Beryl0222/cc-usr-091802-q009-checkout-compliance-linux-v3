"""资金来源登记目录。

每条登记记录资金源的：法律类别、提供方及其资质、适用商户（MCC 白名单，
空表示全商户）、适用地区、生效区间，以及运行时状态（上下线、风控冻结）。

登记即校验：资质与法律类别不匹配、风险机构、时间窗倒置等都会被拒绝。
目录不保存真实账号、余额或交易凭据。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Iterable

from .models import (
    Category,
    ComplianceError,
    Qualification,
    TimeWindow,
    iso,
    parse_dt,
    qualification_allowed,
    region_covers,
    to_category,
    to_qualification,
)


@dataclass(frozen=True, slots=True)
class Provider:
    """资金提供方。"""

    provider_id: str
    name: str
    qualification: Qualification
    # 资质证照编号（脱敏登记，仅存后四位在回执中）
    license_no: str
    # 资质自身的有效期；过期提供方的资金源不可用
    valid_until: datetime | None = None

    def license_hint(self) -> str:
        return self.license_no[-4:].rjust(len(self.license_no), "*")

    def to_json(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "name": self.name,
            "qualification": self.qualification.value,
            "license_hint": self.license_hint(),
            "valid_until": iso(self.valid_until),
        }


@dataclass(frozen=True, slots=True)
class FundingSource:
    """一个可登记的资金来源。"""

    source_id: str
    display_name: str
    category: Category
    provider: Provider
    window: TimeWindow
    regions: frozenset[str]
    # 适用商户 MCC；空集合表示全商户
    merchant_mccs: frozenset[str] = frozenset()
    # 金融产品必须展示的自身风险要素（叠加政策要求的统一披露）
    risk_notes: tuple[str, ...] = ()
    enabled: bool = True
    # 风控冻结（如提供方被监管约谈、系统故障），优先级高于 enabled
    frozen: bool = False
    freeze_reason: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "display_name": self.display_name,
            "category": self.category.value,
            "provider": self.provider.to_json(),
            "window": self.window.to_json(),
            "regions": sorted(self.regions),
            "merchant_mccs": sorted(self.merchant_mccs),
            "risk_notes": list(self.risk_notes),
            "enabled": self.enabled,
            "frozen": self.frozen,
        }


def funding_source_from_dict(
    data: dict[str, Any], providers: dict[str, Provider]
) -> FundingSource:
    provider_id = data["provider_id"]
    if provider_id not in providers:
        raise ComplianceError(
            "unknown-provider", f"资金源 {data.get('source_id')} 引用未登记提供方 {provider_id}"
        )
    category = to_category(data["category"])
    window = TimeWindow.from_strings(data["effective_from"], data.get("effective_to"))
    regions_raw = data.get("regions", ["*"])
    regions = frozenset(regions_raw if isinstance(regions_raw, list) else [regions_raw])
    return FundingSource(
        source_id=data["source_id"],
        display_name=data["display_name"],
        category=category,
        provider=providers[provider_id],
        window=window,
        regions=regions,
        merchant_mccs=frozenset(data.get("merchant_mccs", [])),
        risk_notes=tuple(data.get("risk_notes", [])),
        enabled=data.get("enabled", True),
        frozen=data.get("frozen", False),
        freeze_reason=data.get("freeze_reason", ""),
    )


def provider_from_dict(data: dict[str, Any]) -> Provider:
    return Provider(
        provider_id=data["provider_id"],
        name=data["name"],
        qualification=to_qualification(data["qualification"]),
        license_no=data["license_no"],
        valid_until=parse_dt(data["valid_until"]) if data.get("valid_until") else None,
    )


@dataclass(frozen=True, slots=True)
class Availability:
    """资金源在某次请求时刻的可用性核验结论。"""

    available: bool
    reason_code: str = ""
    reason: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason_code": self.reason_code,
            "reason": self.reason,
        }


class Catalog:
    """线程安全的资金源目录，负责登记与运行时可用性核验。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._providers: dict[str, Provider] = {}
        self._sources: dict[str, FundingSource] = {}
        # 状态变更监听器（如编排器缓存失效）
        self._listeners: list[Callable[[], None]] = []

    def add_listener(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    # ---- 登记 -------------------------------------------------------

    def register_provider(self, provider: Provider) -> None:
        with self._lock:
            if provider.provider_id in self._providers:
                raise ComplianceError(
                    "duplicate-provider", f"提供方已登记: {provider.provider_id}"
                )
            self._providers[provider.provider_id] = provider

    def register_source(self, source: FundingSource) -> None:
        with self._lock:
            if source.source_id in self._sources:
                raise ComplianceError(
                    "duplicate-source", f"资金源已登记: {source.source_id}"
                )
            if source.provider.provider_id not in self._providers:
                raise ComplianceError(
                    "unknown-provider",
                    f"资金源 {source.source_id} 的提供方 "
                    f"{source.provider.provider_id} 未登记资质",
                )
            # 支付机构不得发行金融产品（更具体的规则，优先于资质匹配）
            if (
                source.category.is_financial_product
                and source.provider.qualification == Qualification.PAYMENT_INSTITUTION
            ):
                raise ComplianceError(
                    "payment-institution-issuer",
                    f"资金源 {source.source_id}: 支付机构不得作为金融产品的发行方",
                )
            if not qualification_allowed(source.category, source.provider.qualification):
                raise ComplianceError(
                    "qualification-mismatch",
                    f"资金源 {source.source_id}（{source.category.value}）的提供方资质 "
                    f"{source.provider.qualification.value} 与法律类别不匹配",
                )
            if source.window.end is not None and source.window.end <= source.window.start:
                raise ComplianceError(
                    "bad-window", f"资金源 {source.source_id} 生效区间倒置"
                )
            self._sources[source.source_id] = source
            self._notify()

    def load_many(
        self,
        providers: Iterable[Provider] = (),
        sources: Iterable[FundingSource] = (),
    ) -> None:
        for p in providers:
            self.register_provider(p)
        for s in sources:
            self.register_source(s)

    # ---- 状态变更 ---------------------------------------------------

    def set_enabled(self, source_id: str, enabled: bool) -> None:
        with self._lock:
            source = self._require(source_id)
            self._sources[source_id] = replace(source, enabled=enabled)
        self._notify()

    def freeze(self, source_id: str, reason: str) -> None:
        with self._lock:
            source = self._require(source_id)
            self._sources[source_id] = replace(
                source, frozen=True, freeze_reason=reason
            )
        self._notify()

    def unfreeze(self, source_id: str) -> None:
        with self._lock:
            source = self._require(source_id)
            self._sources[source_id] = replace(source, frozen=False, freeze_reason="")
        self._notify()

    def _require(self, source_id: str) -> FundingSource:
        try:
            return self._sources[source_id]
        except KeyError:
            raise ComplianceError("unknown-source", f"资金源未登记: {source_id}") from None

    # ---- 查询 -------------------------------------------------------

    def get(self, source_id: str) -> FundingSource:
        with self._lock:
            return self._require(source_id)

    def find(self, source_id: str) -> FundingSource | None:
        with self._lock:
            return self._sources.get(source_id)

    def list_sources(self) -> list[FundingSource]:
        with self._lock:
            return list(self._sources.values())

    def check_availability(
        self,
        source_id: str,
        *,
        region: str,
        mcc: str,
        moment: datetime,
    ) -> Availability:
        """重新核验资金源此刻是否可用。

        支付/重试每次都要调用：即使收银台展示时可用，扣款时也可能已被
        冻结或过了生效区间。核验顺序固定，原因码可直接用于审计。
        """
        source = self.find(source_id)
        if source is None:
            return Availability(False, "unknown-source", f"资金源未登记: {source_id}")
        if source.frozen:
            return Availability(
                False, "frozen", f"资金源被风控冻结: {source.freeze_reason}"
            )
        if not source.enabled:
            return Availability(False, "disabled", "资金源已下线")
        if source.provider.valid_until and source.provider.valid_until < moment:
            return Availability(
                False,
                "provider-license-expired",
                f"提供方资质已于 {iso(source.provider.valid_until)} 到期",
            )
        if not region_covers(source.regions, region):
            return Availability(
                False, "region-unsupported", f"该资金源不适用于地区 {region}"
            )
        if not source.window.contains(moment):
            return Availability(
                False, "out-of-window", "资金源不在生效区间内"
            )
        if source.merchant_mccs and mcc not in source.merchant_mccs:
            return Availability(
                False, "merchant-unsupported", f"该资金源不适用于商户类别 {mcc}"
            )
        return Availability(True)
