# 收银渠道合规编排

支付平台收银渠道编排后端：统一登记银行卡、余额、消费信贷、货币基金（资管）与
分期等资金来源的**法律类别、提供方资质、适用商户/地区与生效区间**；收银台按
**当前有效政策版本**返回分组、顺序、必要风险信息和可选状态；营销素材另走审核
链；支付、重试与渠道回执全程留痕，可按结账会话复原现场。仅用 Python 标准库。

## 合规红线如何落地

- **"可付款" ≠ "可营销/可混展"**：`credit`/`asset-management`/`installment`
  与 `bank-card`/`payment-tool` 分属独立分组，任何方案中金融产品必带风险提示。
- **服务端绝不预选**：每个条目 `selected=false/default=false/recommended=false`，
  方案 `preselected_source=null`；扣款只接受用户显式选择后颁发的选择令牌。
- **营销审核链**阻断七类行为并给出规则号、条款、原文片段与字符偏移：
  R100 禁用表述、R200 保本暗示、R300 只突出首期费用、R400 支付机构导流、
  R500 默认勾选、R600 还款页借贷广告、R700 优惠诱导借款。
  "不保本/不保证收益"等否定式合规披露不会被误判。
- **版本与灰度**：政策版本生效区间左闭右开、互不重叠；并发发布以令牌 CAS
  串行化，竞争方收到 `ConcurrentPublish`；任意时刻只有一个有效版本。灰度必须
  同时带地区、生效日与分桶，且只能在版本窗口内调节，无法绕开地区与生效日。
- **缓存到期即失效**：方案 `expires_at` 取政策窗口、资金源生效区间、资质
  有效期的最早到期点（并设 5 分钟上限）；登记处修订号变化也使旧缓存失效。
- **重试**：沿用用户明确选择的同一渠道，但每次按当前时刻重新核验资质、生效
  区间、商户与地区；不可用即拒绝并记录 `substituted_source=null`，绝不换渠道。
- **错误分类可更正、可追溯**：登记处分类更正保留 claimed→corrected 链；渠道
  回执里的错误分类与迟到确认原样收下，审计复原时与权威分类交叉核对。

## 模块

| 文件 | 职责 |
|---|---|
| `checkout/models.py` | 法律类别、提供方角色、资质（含有效期）、资金源与更正记录 |
| `checkout/registry.py` | 登记/换发、分类更正留痕、按时刻与商户/地区核验可用性 |
| `checkout/policy.py` | 政策版本链、区间不重叠、发布 CAS、版本内灰度 |
| `checkout/orchestrator.py` | 方案编排、分组隔离、风险信息、绝不预选、到期缓存 |
| `checkout/marketing.py` | 规则集 v1/v2 与七类阻断审核，命中依据完整 |
| `checkout/payments.py` | 显式选择令牌、扣款、重试重新核验、回执接入 |
| `checkout/audit.py` | 只追加事件链与结账现场复原（布局/选择/政策/扣款/回执） |
| `checkout/demo.py` | 横跨 2026-09-30 的端到端场景装配 |
| `checkout/app.py` | HTTP 接线 |

## 运行

```bash
python3 service.py --check           # 校验 fixture 与全部合规不变量
python3 service.py --demo            # 输出跨 9·30 的审计复原 JSON 报告
python3 service.py --port 8000       # HTTP 服务
python3 -m pytest -q                 # 53 个测试（或 python3 -m unittest discover -s tests）
```

HTTP 接口：`POST /checkout/plan`、`POST /checkout/select`、
`POST /payments/charge`（`is_retry=true` 即重试）、`POST /payments/receipt`、
`POST /marketing/review`、`GET /audit/checkouts[/<id>]`、`GET /audit/policies`、
`GET /health`。

## 审计场景（2026-09-30 生效日前后）

- 09-29 22:15 结账 C1（旧规）：用户显式选择消费信贷 → 首次渠道超时 →
  22:25 沿用同一渠道重试成功；
- 09-29 23:40 `biz-paylater` 错误分类（支付工具→消费信贷）被纠正并留痕；
- 09-30 00:20 结账 C2（新规）：该渠道已移入金融产品分组；
- 09-30 09:12 收到 C1 的迟到成功确认；10-01 又收到将其误报为
  `payment-tool` 的回执——复原报告标注 `late_confirmation` 与
  `category_mismatch`，并给出权威分类 `credit`。

`fixtures/sample.json` 记录基线分类与禁用表述；`fixtures/receipts.json` 是
场景重放的回执数据。仓库不保存真实账户、余额或交易凭据。
