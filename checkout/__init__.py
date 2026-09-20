"""收银渠道合规编排后端。

模块划分：

- models       资金来源、法律类别、资质等领域模型
- registry     资金源登记、更正留痕、适用核验
- policy       政策版本、发布并发控制、灰度配置
- orchestrator 收银台方案编排与到期缓存
- marketing    营销素材审核链
- payments     显式选择、扣款与重试重新核验
- audit        只追加事件链与结账现场复原
- demo         横跨 2026-09-30 的演示场景
- app          服务装配与 HTTP 接线
"""

SERVICE_ID = "checkout-compliance"
SERVICE_NAME = "收银渠道合规编排"
