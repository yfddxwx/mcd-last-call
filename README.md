# 麦门末班车 · mcd-last-call

**你不需要知道吃什么。你需要知道现在点还赶不赶得上。**

给午休被会议切碎的上班族：输入"我还有 40 分钟"，它告诉你这顿麦当劳**来不来得及**、
**最晚几点必须下单**、以及在这个时间窗口里**哪些餐真的吃得到**。

`Python` · `0 第三方依赖` · 离线可跑 · 基于麦当劳中国官方 MCP

> **[→ 打开在线试玩页](https://yfddxwx.github.io/mcd-last-call/)**（零安装、不联网、不读你的订单）
> 打不开说明 Pages 还没开启：Settings → Pages → Source 选 `main` + **`/docs`** 即可。
> **[→ 30 秒本地跑通](#30-秒跑起来不用-token)**（需要 Python 3.10+，其他什么都不用）

---

## 作为一个 Skill 安装

**整个仓库本身就是一个可安装的 Skill**（`SKILL.md` + `scripts/` + `references/`）：

```bash
git clone https://github.com/yfddxwx/mcd-last-call.git ~/.workbuddy/skills/mcd-last-call
```

装完在 WorkBuddy 里直接说人话：

> 我 12 点下班、1 点开会，楼下麦当劳走过去 8 分钟，现在 12:20 了，还来得及吃个巨无霸吗？

Agent 会按 `SKILL.md` 的流程走：先调 `now-time-info` / `query-nearby-stores` / `query-meals` /
`list-nutrition-foods` / `query-meal-detail` / `calculate-price` 取真实数据，
再把数据喂给 `scripts/solve.py` 做**确定性求解**（不让模型心算），
最后输出「最晚下单时刻」。

```
SKILL.md                  技能清单与工作流（触发条件、工具调用表、输出规范、禁止事项）
scripts/plan.py           完整流水线，任意工作目录可用
scripts/solve.py          纯计算器：JSON 进，结论出，不联网、不需要 Token
references/timing-model.md 时间/热量公式与边界
references/mcp-fields.md   每个工具的字段、单位、实测陷阱
```

`scripts/plan.py` 与根目录的 `mcd_last_call.py` 等价——多这一层是为了让脚本在被安装到
`~/.workbuddy/skills/` 后仍能从任意当前目录运行。

---

## 问题：不是没得选，是来不及选

现在的点餐工具都在帮你回答"吃什么"——推套餐、算热量、凑满减。
但真实的午休场景里，**第一个被卡住的永远是时间**：

- 12:00 下班，13:00 开会。楼下走过去 8 分钟，排队 6 分钟，吃完 10 分钟，走回来 8 分钟。
- 你算不清这笔账，于是打开 App 翻了 4 分钟菜单，然后发现已经来不及了。

`mcd-last-call` 把这个问题反过来解：**先判定能不能吃上，再在能吃上的里面挑**。
时间是一票否决的硬约束，热量和预算是软目标。

## 核心：一个数字，「最晚下单时刻」

别人的模型是把时间**加起来**；这里的关键是**取较大值**：

```
等待 = max(步行, 出餐)      ← 你在路上走的时候，后厨已经在做了
总耗时 = 等待 + 吃 + 返程 + 缓冲
最晚下单 = 截止时刻 − 总耗时   ← 晚于这个点，最早的一单也赶不上
```

看起来是细节，但它决定结论对不对：一个 8 分钟步行 + 12 分钟出餐的单，
按加法算是 20 分钟等待，按取大算是 12 分钟——**差了 8 分钟，足够让你从"来得及"变成"来不及"**。

---

## 三个场景，以及它真实的输出

### 场景 1 ·「我 12 点下班、1 点开会，走过去 8 分钟，现在 12:20 了，还来得及吃个巨无霸吗？」

```bash
python mcd_last_call.py --demo --scenario 1 --deadline 13:00 --walk 7
```

```
 门店    麦当劳深圳同方信息港餐厅
         科技园北区朗山路11号同方信息港E栋G层5号商铺
 距离    7 分钟（403m × 绕行 1.25 ÷ 75m/min）
 现在    12:20      截止 13:00      可用 40 分钟
──────────────────────────────────────────────────────────────────────
 判定    ✓ 来得及      最快的一单 30 分钟，余 10 分钟
 最晚下单时刻  12:30    ← 晚于这个点，最早的一单也赶不上
──────────────────────────────────────────────────────────────────────
 推荐（按稳妥程度）
 1. “辣”么快乐套餐           ¥24.00    394kcal       到手约 12:31
    max(步行7, 出餐11) + 吃7 + 返程7 + 缓冲5 = 30 分钟
 2. 牛气满满套餐             ¥35.00    495kcal       到手约 12:31
    max(步行7, 出餐11) + 吃8 + 返程7 + 缓冲5 = 31 分钟
 3. 麦乐鸡经典中套餐         ¥25.00    649kcal       到手约 12:31
    max(步行7, 出餐11) + 吃11 + 返程7 + 缓冲5 = 34 分钟
 调用    14 次 → now-time-info → query-nearby-stores → query-meals
       → list-nutrition-foods → query-meal-detail×9 → calculate-price
```

**注意那个 12:30。** 用户真正需要的不是这份菜单，而是"你现在只剩 10 分钟可以做决定"。
顺带一提：前三个都是 30–31 分钟就能吃上的，因为它们来自官方的「500大卡套餐」分类——
这是联调时从真实菜单里挖出来的，不是我编的档位。

### 场景 2 ·「我只有 20 分钟，别推要等 15 分钟的」

```bash
python mcd_last_call.py --demo --scenario 2 --minutes 20
```

```
 判定    ✗ 来不及      最快的一单也要 25 分钟，还差 5 分钟
 最晚下单时刻  已过    想赶上，需要再多 5 分钟，或换更近的门店
──────────────────────────────────────────────────────────────────────
 推荐（按到手速度）
 1. 薄皮焦香V翅              ¥16.00    192kcal       到手约 12:27  赶不上
 2. 麦乐鸡经典中套餐         ¥25.00    649kcal       到手约 12:31  赶不上
```

这个场景的本事是**敢说不行**。它不会硬凑一个"看起来还行"的推荐，
而是告诉你最快的选择是哪个、还差几分钟——你可以据此决定换门店还是改吃便利店。

### 场景 3 ·「40 分钟内吃完、600 大卡以内、30 块以内，直接告诉我点哪个」

```bash
python mcd_last_call.py --demo --scenario 3 --minutes 40 --kcal 600
```

```
 判定    ✓ 来得及      最快的一单 30 分钟，余 10 分钟
 最晚下单时刻  12:30
──────────────────────────────────────────────────────────────────────
 推荐（按价格）
 1. “辣”么快乐套餐           ¥24.00    394kcal       到手约 12:31
 2. 牛气满满套餐             ¥35.00    495kcal       到手约 12:31
```

普通套餐动辄 650–1140 kcal，600 kcal 线本来是过不去的。
是官方菜单里的「500大卡套餐」分类救了这一档——实测构成 394 / 412 / 476 / 495 kcal。

**没有解的时候，它告诉你差在哪、差多少**（把线压到 400 kcal、预算 20 元）：

```bash
python mcd_last_call.py --demo --scenario 3 --minutes 40 --kcal 400 --budget 20
#  判定    · 无可行选项
#         · 最接近的一单是「“辣”么快乐套餐」：预算放到 ¥24.00 就能吃上
```

---

## 30 秒跑起来（不用 Token）

只要装了 Python 3.10+：

```bash
git clone https://github.com/yfddxwx/mcd-last-call.git && cd mcd-last-call
python mcd_last_call.py --demo --scenario 1 --deadline 13:00
```

不需要 Token、不需要联网、不需要装任何包。`--demo` 回放的是**真实 MCP 响应的原样快照**
（`data/demo/*.json`），走的求解代码与在线模式**完全相同**——不是另写一套假数据演示。

跑测试：

```bash
python -m unittest discover -s tests -v     # 18 个用例，全绿
```

## 接入真实门店

在 [open.mcd.cn](https://open.mcd.cn) 登录 → 右上角「控制台」→「激活」→ 复制 Token，然后：

```bash
export MCD_MCP_TOKEN="你的 Token"          # bash
$env:MCD_MCP_TOKEN="你的 Token"            # PowerShell
```

```bash
python probe_mcp.py                                              # 先验握手 + 拉真实 schema
python mcd_last_call.py --city 深圳 --keyword 科技园              # 真实门店
python mcd_last_call.py --city 上海 --keyword 人民广场 --minutes 25
python mcd_last_call.py --demo --scenario 1 --json                # 机器可读
```

Token **只从环境变量读**，不写进代码、配置或快照。想重新录制离线快照：

```bash
python mcd_last_call.py --city 深圳 --keyword 科技园 --record
```

## 和手动点单的差别

| | 手动翻 App | mcd-last-call |
| --- | --- | --- |
| 判断"来不来得及" | 靠感觉，经常算错 | 给出具体分钟数 + 最晚下单时刻 |
| 等待时间 | 以为步行和出餐是相加的 | `max(步行, 出餐)`，因为后厨和你的腿是并行的 |
| 在 20 分钟窗口里挑 | 凭运气 | 先排除所有赶不上的，再排序 |
| 热量 | 自己搜、自己加 | 官方营养表逐项匹配，匹配不到就明说"未知" |
| 价格 | 结算时才知道 | 只认 `calculate-price` 的应付总额，券已计入 |
| 没有解的时候 | 继续翻，翻到超时 | 直接告诉你差几分钟 / 差多少卡，该放宽哪个 |

---

## 数据口径（不掺假的部分）

| 量 | 来源 | 算法 | 边界 |
| --- | --- | --- | --- |
| 现在 | `now-time-info` | 服务端时间，不用本机时钟 | 截止已过 → 直接判"来不及" |
| 步行 | `query-nearby-stores.distance` | `ceil(米 × 1.25 ÷ 75)`，向上取整 | 距离缺失 → 按 400m 估并标"置信度低" |
| 出餐 | 无接口，结构估算 | `品类基值 × 繁忙系数 × (1+0.15×(单品数-1))` | 午高峰 ×1.4、晚高峰 ×1.3，**不是实时排队** |
| 用餐 | 官方热量 | `clamp(kcal÷60, 6, 20)` 分钟 | 热量未知 → 不推荐该项，不猜 |
| 热量 | `list-nutrition-foods` | 套餐 = Σ 各单品；名称匹配 | 匹配不到给区间 + 覆盖率，一项未命中则排除 |
| 价格 | `calculate-price` | 直接取应付总额（单位：分） | 绝不用"菜单价×数量"自算 |

参数不是写死的，全在 [`data/model/prep_time.json`](data/model/prep_time.json)，改完立刻生效。
完整推导与边界处理见 [`docs/data-model.md`](docs/data-model.md)。

## 项目结构

```
SKILL.md            技能清单：触发条件 / 工具调用表 / 输出规范 / 禁止事项
scripts/            plan.py（完整流水线）、solve.py（纯计算器，JSON 进结论出）
references/         timing-model.md、mcp-fields.md
mcd_last_call.py    唯一入口：MCP 客户端 / 双数据源 / 求解器 / 终端卡片
mcp_client.py       Streamable HTTP 传输层（标准库 urllib，含 SSE 解析与退避）
probe_mcp.py        握手探针：验证连通性 + 落盘真实 tools/list schema
data/demo/          离线快照（真实响应原样录制）
data/model/         可调的时长与热量参数
docs/index.html     零配置试玩页（GitHub Pages 的发布目录就是 docs/）
docs/               数据口径 + MCP 工具 schema 快照
tests/              18 个行为锁用例（无需 Token）
```

MCP 调用细节、失败兜底、业务价值见 [`MCP_INTEGRATION.md`](MCP_INTEGRATION.md)；
每个真实返回字段的含义与用法见 [`docs/field-mapping.md`](docs/field-mapping.md)。

## 它做不到什么

诚实写在这里，免得你踩坑：

- **没有实时排队数据**。出餐时长是按品类结构估的，午高峰可能偏乐观。所有输出都标了这是估算。
- **没有路径规划**。步行时间是直线距离 × 绕行系数，不是真实路线。
- **热量覆盖不全**。官方营养表 160 条，长尾新品匹配不到——这时它说"未知"，不会编数字。
- **不能代付**。`create-order` 只建单返回支付链接，付款这一步永远是人做的。
- **只做到店自取**。外送需要配送时长，官方接口给不出可靠粒度，所以没做，也不假装能算。
- **团餐 / 主题活动未接入**。需要企业资质与场次信息，个人 Token 调不稳，不进主链路。

---

## 如果这个项目帮你少迟到一次

去 [**https://github.com/yfddxwx/mcd-last-call**](https://github.com/yfddxwx/mcd-last-call) 右上角点个 **Star** ⭐ —— 这是这个比赛唯一的计分方式，也是它继续更新的理由。

如果它算错了，开个 Issue 告诉我当时的门店、时间和实际用时，我把参数调准。
