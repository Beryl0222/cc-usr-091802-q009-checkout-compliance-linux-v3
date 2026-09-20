"""资金源登记处：登记、错误分类更正、按时刻查询可用资金源。

登记项的权威分类只在这里维护；客户端/商户回执里自称的分类一律
不可信——历史上出现过把消费信贷回报成"支付工具"的错误分类，
更正时必须携带原因并保留原回执。
"""

import threading
from dataclasses import dataclass, field

from . import timeutil
from .models import CategoryCorrection, FundingSource


@dataclass
class _Record:
    source: FundingSource
    version: int
    superseded_by: str | None = None  # 换发后的新登记项 id
    supersedes: str | None = None


@dataclass
class RegistrationResult:
    source_id: str
    version: int
    warnings: list = field(default_factory=list)


class DuplicateRegistration(ValueError):
    pass


class UnknownSource(KeyError):
    pass


class Registry:
    def __init__(self):
        self._lock = threading.RLock()
        self._records: dict[str, _Record] = {}
        self._revision = 0

    @property
    def revision(self) -> int:
        """登记处修订号：登记、换发、分类更正都会自增，用于缓存失效。"""
        with self._lock:
            return self._revision

    def register(self, source: FundingSource, *, supersedes: str | None = None) -> RegistrationResult:
        """登记（或换发）资金源。同一 id 不能重复登记；换发使用新 id。"""
        with self._lock:
            if source.source_id in self._records:
                raise DuplicateRegistration(source.source_id)
            version = 1
            if supersedes is not None:
                old = self._records.get(supersedes)
                if old is None:
                    raise UnknownSource(supersedes)
                if old.superseded_by is not None:
                    raise DuplicateRegistration(f"{supersedes} 已被换发")
                old.superseded_by = source.source_id
                version = old.version + 1
            warnings = []
            if source.is_financial_product:
                if not source.risk_notice:
                    warnings.append("financial-product-without-risk-notice")
                if source.marketing_allowed:
                    # 登记阶段不禁止，但提示需要营销审核链放行
                    warnings.append("marketing-requires-review-chain")
            record = _Record(source=source, version=version, supersedes=supersedes)
            self._records[source.source_id] = record
            self._revision += 1
            return RegistrationResult(source.source_id, version, warnings)

    def correct_category(self, correction: CategoryCorrection) -> None:
        """纠正错误分类。原 claimed 分类保留在更正链上。"""
        with self._lock:
            record = self._records.get(correction.source_id)
            if record is None:
                raise UnknownSource(correction.source_id)
            record.source = record.source.with_category_correction(correction)
            self._revision += 1

    def get(self, source_id: str) -> FundingSource:
        with self._lock:
            try:
                return self._records[source_id].source
            except KeyError:
                raise UnknownSource(source_id) from None

    def version_of(self, source_id: str) -> int:
        with self._lock:
            return self._records[source_id].version

    def all_sources(self) -> list[FundingSource]:
        with self._lock:
            return [r.source for r in self._records.values()]

    def effective_sources(self, at) -> list[FundingSource]:
        """某一时刻处于生效区间内、且未被换发的登记项。"""
        at = timeutil.parse(at) if not hasattr(at, "tzinfo") else at
        with self._lock:
            out = []
            for record in self._records.values():
                if record.superseded_by is not None:
                    continue
                if record.source.effective_at(at):
                    out.append(record.source)
            return out

    def usable_sources(self, merchant_id: str, region: str, at) -> dict:
        """返回 {source_id: (source, ok, reason)}，便于编排与审计。"""
        result = {}
        for source in self.effective_sources(at):
            ok, reason = source.usable_for(merchant_id, region, at)
            result[source.source_id] = (source, ok, reason)
        return result

    def resolve_at(self, source_id: str, at) -> FundingSource:
        """按 id 取当前视图的资金源（含已更正分类）。"""
        return self.get(source_id)
