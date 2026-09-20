"""政策版本与发布并发控制。

关键不变量：

1. 同一政策链上的版本生效区间互不重叠（左闭右开），因此任意时刻
   ``effective_policy`` 的结果唯一——并发发布也只能有一个有效版本。
2. 发布串行化并带版本令牌（CAS）：两个发布者竞争同一后继位置时，
   后者得到 ``ConcurrentPublish``，必须重新拉取再提交。
3. 灰度配置只能调节**当前有效版本内部**的展示特性（分桶比例、地区、
   生效日缺一不可），不能选择另一个政策版本，也不能延长或绕过版本
   生效区间——所以灰度不可能造成"两个有效版本"。
"""

import hashlib
import itertools
import threading
from dataclasses import dataclass, field
from typing import Optional

from . import timeutil
from .models import Category


class OverlappingPolicyWindow(ValueError):
    pass


class ConcurrentPublish(RuntimeError):
    pass


class InvalidGrayConfig(ValueError):
    pass


@dataclass(frozen=True)
class GrayConfig:
    """版本内灰度：必须同时具备地区、生效日、分桶。"""

    feature: str
    regions: frozenset
    effective_from: str
    bucket_percent: int  # 0..100，命中前 N% 桶
    salt: str = "gray"

    def __post_init__(self):
        if not self.regions:
            raise InvalidGrayConfig("灰度必须限定地区，不得全网绕行")
        if not self.effective_from:
            raise InvalidGrayConfig("灰度必须带生效日")
        if not 0 <= self.bucket_percent <= 100:
            raise InvalidGrayConfig("分桶比例必须在 0..100")

    def hits(self, region: str, at, subject_key: str) -> bool:
        at = timeutil.parse(at) if not hasattr(at, "tzinfo") else at
        if region not in self.regions:
            return False
        if at < timeutil.parse(self.effective_from):
            return False
        if self.bucket_percent == 0:
            return False
        if self.bucket_percent == 100:
            return True
        digest = hashlib.sha256(f"{self.salt}:{subject_key}".encode("utf-8")).hexdigest()
        bucket = int(digest[:8], 16) % 100
        return bucket < self.bucket_percent


@dataclass(frozen=True)
class PolicyVersion:
    chain_id: str
    version: str
    effective_from: str
    effective_until: Optional[str]
    legal_source: str                      # 政策来源（文件+条款），供审计
    marketing_ruleset: str
    group_order: tuple = (
        ("payment-tools", (Category.BANK_CARD, Category.PAYMENT_BALANCE)),
        ("financial-products", (Category.CREDIT, Category.ASSET_MANAGEMENT, Category.INSTALLMENT)),
    )
    require_separate_financial_group: bool = True
    require_risk_notice: bool = True
    allow_server_preselection: bool = False  # 合规红线，任何版本都为 False
    grays: tuple = field(default_factory=tuple)

    def window_contains(self, at) -> bool:
        at = timeutil.parse(at) if not hasattr(at, "tzinfo") else at
        start = timeutil.parse(self.effective_from)
        end = None if self.effective_until is None else timeutil.parse(self.effective_until)
        return timeutil.between(start, at, end)

    def feature_on(self, feature: str, region: str, at, subject_key: str) -> bool:
        """灰度命中检查；任何命中都不可能越过本版本的生效区间。"""
        at = timeutil.parse(at) if not hasattr(at, "tzinfo") else at
        if not self.window_contains(at):
            return False
        for gray in self.grays:
            if gray.feature == feature and gray.hits(region, at, subject_key):
                return True
        return False


@dataclass
class PublishToken:
    """发布令牌：记录当前链尾版本，提交时做比较交换。"""

    chain_id: str
    head_version: Optional[str]
    seq: int


class PolicyChain:
    def __init__(self, chain_id: str):
        self.chain_id = chain_id
        self._lock = threading.RLock()
        self._versions: list[PolicyVersion] = []
        self._seq = itertools.count(1)

    def head_token(self) -> PublishToken:
        with self._lock:
            head = self._versions[-1].version if self._versions else None
            return PublishToken(self.chain_id, head, next(self._seq))

    def publish(self, policy: PolicyVersion, token: Optional[PublishToken] = None) -> PublishToken:
        """发布新版本。

        - 区间与任何已发布版本重叠 → OverlappingPolicyWindow；
        - 令牌与当前链尾不一致 → ConcurrentPublish（并发竞争失败）。
        """
        if policy.chain_id != self.chain_id:
            raise ValueError("政策链不匹配")
        if policy.allow_server_preselection:
            raise ValueError("服务端预选在任何版本都被禁止")
        new_start = timeutil.parse(policy.effective_from)
        new_end = None if policy.effective_until is None else timeutil.parse(policy.effective_until)
        with self._lock:
            if token is not None:
                head = self._versions[-1].version if self._versions else None
                if token.head_version != head:
                    raise ConcurrentPublish(
                        f"链尾已变为 {head}，当前提交基于 {token.head_version}"
                    )
            for existing in self._versions:
                ex_start = timeutil.parse(existing.effective_from)
                ex_end = (
                    None if existing.effective_until is None
                    else timeutil.parse(existing.effective_until)
                )
                if self._windows_overlap(new_start, new_end, ex_start, ex_end):
                    raise OverlappingPolicyWindow(
                        f"{policy.version} 与 {existing.version} 的生效区间重叠"
                    )
            for gray in policy.grays:
                if timeutil.parse(gray.effective_from) < new_start:
                    raise InvalidGrayConfig("灰度生效日不得早于政策版本生效日")
                if new_end is not None and timeutil.parse(gray.effective_from) >= new_end:
                    raise InvalidGrayConfig("灰度生效日必须落在版本生效区间内")
            self._versions.append(policy)
            self._versions.sort(key=lambda p: timeutil.parse(p.effective_from))
            return PublishToken(self.chain_id, policy.version, next(self._seq))

    @staticmethod
    def _windows_overlap(a_start, a_end, b_start, b_end) -> bool:
        """左闭右开区间是否重叠。None 端表示 +∞。"""
        if a_end is not None and b_start >= a_end:
            return False
        if b_end is not None and a_start >= b_end:
            return False
        return True

    def effective_policy(self, at) -> PolicyVersion:
        """返回某时刻唯一有效版本；无生效版本或同时命中多个都属致命错误。"""
        at = timeutil.parse(at) if not hasattr(at, "tzinfo") else at
        with self._lock:
            hits = [p for p in self._versions if p.window_contains(at)]
        if len(hits) > 1:  # 发布时已拦截，这里是纵深防御
            raise RuntimeError(f"存在多个有效政策版本: {[p.version for p in hits]}")
        if not hits:
            raise LookupError(f"{at.isoformat()} 没有已生效的政策版本")
        return hits[0]

    def versions(self) -> list[PolicyVersion]:
        with self._lock:
            return list(self._versions)
