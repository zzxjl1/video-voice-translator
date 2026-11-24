# 计费（用户侧）

用户真的付钱，所以这里有两套数：**成本**（我们付给供应商的，单位元）和**积分**
（用户看到的，单位"分"）。成本只在内部可见，**任何面向用户的接口都不返回它**。

定价公式（产品口径）：

```text
积分 = 成本（元） × 倍率 × 100
```

所以 1 积分 = 0.01 元（在用户侧），倍率已经折进积分数量里。成本 1 元、倍率 2 →
用户看到 200 积分（= 付 2 元）。

## 两套账本

| | 存哪 | 谁看 | 内容 |
| --- | --- | --- | --- |
| 成本台账 | `backend/data/<project>/usage.json` | 运营 / 排障 | 每次调用的成本（元）、原始 usage、缺价标记 |
| 用户账本 | `backend/data/billing.sqlite3` | 用户 / 计费 | 每次调用的积分，从余额扣 |

两份都由 `services/usage_service.record()` 写：它先落成本，再调 `billing.service.charge()`
落用户价。**成本价格表在两处各有一份**（`usage_service._PRICE_TABLE` 与
`billing.pricing.COST_TABLE`），`test_billing.py` 断言两边完全相等——不一致会让"成本"
和"对外定价"漂开。

## 口径

- 币种只有 **CNY**（北京站），不支持国际站。
- **不考虑 DeepSeek 峰谷价差**：统一按峰时价一条线，账本里不记时段。
- **没有免费额度**：所有用量都计价。缺价时记 `no_price`（0 元、不扣钱），但要能被运营看见
  （`project_summary()["unpriced_calls"]` 只出现在内部视角）。
- 两种整数：成本用 **micro 元**（1e-6 元），积分用 **micro 积分**（1e-6 积分）。
  单句常常不到 1 积分（¥0.0006 → 0.12 积分），用整数存才不会在累加时丢零头。
  接口同时给 `*_micro` 和 `*_points`（已换算好的显示值）。
- 本地配音（Kokoro）、人声分离、ffmpeg 导出**不消耗积分**，账本里记 0 + `cost_source=free`，
  这样用户看到"这个工程没消耗"时能对上号。

## 倍率

`config.BILLING_MARKUP`：按步骤配（`tts` / `asr` / `omni` / `llm` / `clone`），
默认 2.0，可用环境变量覆盖：

```dotenv
BILLING_MARKUP_TTS=2.0
BILLING_MARKUP_ASR=2.0
BILLING_MARKUP_OMNI=2.0
BILLING_MARKUP_LLM=2.0
BILLING_MARKUP_CLONE=2.0
```

每笔账都存下当时的 `markup` 与金额快照，**改倍率不改历史**。倍率只影响之后的价格。

## 余额与拦截

- 预付费积分，管理员/线下充值到账后调用 `POST /api/billing/adjust`（入参 `amount_points`）。
- 会花钱的命令（`redub` / `retranslate` / `clone_voice` / `generate_voice_candidates` /
  `run_initialize`）在动手前检查余额，不够就拒绝并说清差多少。
  门槛是 `BILLING_MIN_START_POINTS_MICRO`（默认 100 积分，约 ¥1 的量级）——按用量计费没法精确预估，
  这个数只是"别让余额见底的人开跑十分钟的活"。
- **扣积分发生在每次调用之后**（按实际用量）。所以一个任务跑完可能把余额扣成负数：
  活已经干完，不能事后拒付；下一次任务会被拦住。
- 充值/扣减写 `adjustment` 表，和账本一起构成"余额为什么是这个数"的完整解释。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/billing/summary` | 余额、本月已用、按步骤 |
| GET | `/api/billing/entries?limit=&project=` | 明细（不含成本） |
| GET | `/api/billing/prices` | 用户可见单价（已含倍率；目前只有克隆） |
| GET | `/api/billing/projects/{id}` | 单个工程累计（自己的工程，别人的 404） |
| POST | `/api/billing/adjust` | 管理员调整余额（`BILLING_ADMIN_EMAILS`） |

命令层（UI 和 AI 共用）：

- `get_cost`：这个工程花了多少（用户价）。
- `get_billing`：账户视角（余额 / 本月 / 按步骤），可附最近明细。
- `get_price`：某项服务的单价（目前只有 `clone`）。

## 前端

- **账户菜单**：积分余额 + 「费用明细」→ 面板（余额、本月已用、这个工程累计、按步骤、最近分录）。
- **编辑页顶栏**：`xxx 积分` 胶囊显示本工程累计，点开同一个面板。
- **确认卡**：能算准的给预估（克隆 2 积分 × 次数），算不准的明说"按实际用量计费"。
- **音色面板**：单价从 `/api/billing/prices` 取（单位积分），不再写死在代码里。

## 已知边界

- 账本是**估算级**：供应商控制台的优惠、免额、峰谷价都不在账本里。真实账单以阿里云为准。
  对账（拉账单 API 比对）还没做。
- 多标签页/多设备同时用一个账号：余额扣减是 SQLite 事务，不会丢账，但"余额刚够"时
  两个任务可能都通过预检、最终把余额花成负数——和单任务透支同一个处理方式。
- 历史工程（计费上线前的花费）**不迁移**进用户账本：那时没有积分口径，补记等于凭空造欠账。
- 旧的"元计价"账本结构（`price_micro`/`balance_micro` 列）在启动时会被识别并**重建**：
  计费还没上线，没有需要保留的换算依据。
