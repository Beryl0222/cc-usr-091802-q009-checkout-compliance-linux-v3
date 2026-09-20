"""收银渠道合规编排后端。

模块划分：

- models：领域类型（资金源法律类别、提供方资质、时间工具）
- policy：政策版本存储（生效区间、地区、灰度、并发发布）
- catalog：资金来源登记与可用性核验
- orchestrator：收银台方案编排与短期缓存
- marketing：营销素材审核链
- payments：显式选源的扣款与重试
- ledger：只追加回执台账与审计复原
- app：把以上部件装配成一个应用
- api：基于标准库的 HTTP 路由
"""

from .app import CheckoutApp, load_app

__all__ = ["CheckoutApp", "load_app"]
