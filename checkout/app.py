"""应用装配：从 fixtures 目录加载政策、提供方、资金源、历史回执并接线。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .catalog import (
    Catalog,
    Provider,
    funding_source_from_dict,
    provider_from_dict,
)
from .ledger import Auditor, EventLedger
from .marketing import MarketingReviewer, ReviewLedger
from .orchestrator import CheckoutOrchestrator
from .payments import PaymentService, ScriptedGateway
from .policy import (
    PolicyStore,
    PolicyVersion,
    policy_from_dict,
)

DEFAULT_POLICY_ID = "financial-marketing"


class CheckoutApp:
    """所有部件的聚合根，HTTP 层只与本类对话。"""

    def __init__(
        self,
        policy_id: str = DEFAULT_POLICY_ID,
        *,
        gateway: ScriptedGateway | None = None,
        ttl_seconds: float = 30.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.policy_id = policy_id
        self.policies = PolicyStore()
        self.catalog = Catalog()
        self.ledger = EventLedger()
        self.reviews = ReviewLedger()
        self.orchestrator = CheckoutOrchestrator(
            self.policies,
            self.catalog,
            policy_id,
            ttl_seconds=ttl_seconds,
            clock=clock,
        )
        self.reviewer = MarketingReviewer(
            self.policies, policy_id, ledger=self.reviews, clock=clock
        )
        self.payments = PaymentService(
            self.catalog, self.ledger, gateway or ScriptedGateway(), clock=clock
        )
        self.auditor = Auditor(self.ledger, self.policies, policy_id)

    # ---- 加载夹具 ---------------------------------------------------

    def load_fixtures(self, fixtures_dir: str | Path) -> None:
        base = Path(fixtures_dir)
        providers: dict[str, Provider] = {}
        provider_path = base / "providers.json"
        if provider_path.exists():
            for item in json.loads(provider_path.read_text(encoding="utf-8")):
                provider = provider_from_dict(item)
                self.catalog.register_provider(provider)
                providers[provider.provider_id] = provider
        for item in _load_json_list(base / "funding_sources.json"):
            self.catalog.register_source(
                funding_source_from_dict(item, providers)
            )
        for item in _load_json_list(base / "policies.json"):
            self.policies.register(policy_from_dict(item))
        receipts_path = base / "receipts.jsonl"
        if receipts_path.exists():
            self.ledger.load_jsonl(receipts_path)

    # ---- 管理端：发布（发布/提升即让缓存失效） --------------------

    def publish_policy(
        self, policy: PolicyVersion, expected_version: int | None
    ) -> PolicyVersion:
        published = self.policies.publish(policy, expected_version)
        self.orchestrator.invalidate_cache()
        return published

    def promote_policy(self, version: int, expected_canary_percent: int | None = None):
        promoted = self.policies.promote(
            self.policy_id,
            version,
            expected_canary_percent,
        )
        self.orchestrator.invalidate_cache()
        return promoted

    def head_version(self) -> int | None:
        return self.policies.head(self.policy_id)

    def self_check(self) -> dict[str, Any]:
        """``--check`` 使用的配置自检。"""
        versions = self.policies.list_versions(self.policy_id)
        issues: list[str] = []
        if not versions:
            issues.append("未加载任何政策版本")
        if not self.catalog.list_sources():
            issues.append("未登记任何资金源")
        # 存储层不变量：每个地区、每个放量阶段至多一个有效版本
        for region in {r for p in versions for r in p.regions}:
            full = [
                p
                for p in versions
                if region in p.regions and p.canary_percent >= 100
            ]
            for i, a in enumerate(full):
                for b in full[i + 1 :]:
                    if a.window.overlaps(b.window):
                        issues.append(
                            f"地区 {region} 存在重叠全量版本 v{a.version}/v{b.version}"
                        )
        return {
            "policy_id": self.policy_id,
            "versions": [p.version for p in versions],
            "head": self.head_version(),
            "sources": len(self.catalog.list_sources()),
            "receipts": len(self.ledger.all()),
            "ok": not issues,
            "issues": issues,
        }


def _load_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else [data]


def load_app(
    fixtures_dir: str | Path = "fixtures",
    *,
    gateway: ScriptedGateway | None = None,
    ttl_seconds: float = 30.0,
    clock: Callable[[], datetime] | None = None,
) -> CheckoutApp:
    app = CheckoutApp(gateway=gateway, ttl_seconds=ttl_seconds, clock=clock)
    app.load_fixtures(fixtures_dir)
    return app
