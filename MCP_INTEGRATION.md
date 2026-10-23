# MCP 集成说明

本文记录项目真实调用的麦当劳 MCP 能力。工具名、参数名均照抄官方 `tools/list` 返回的 schema，
不做改写、不做猜测。原始 schema 快照存于 [`docs/mcp-tools.schema.json`](docs/mcp-tools.schema.json)，
可用 `python probe_mcp.py` 重新拉取。

## 1. 连接方式

| 项 | 值 |
| --- | --- |
| 接入地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP（JSON-RPC 2.0，POST 单端点） |
| 协商到的协议版本 | `2025-06-18` |
| 服务端 | `mcd-mcp 1.0.0` |
| 认证 | 请求头 `Authorization: Bearer $MCD_MCP_TOKEN` |
| 服务端暴露工具数 | 35（实测 `tools/list`） |
| 限流 | 600 次 / 分钟 / Token，超限返回 429 |
| 会话 | 本服务不返回 `Mcp-Session-Id`，每请求独立鉴权（实测） |

实现上只用 Python 标准库 `urllib`，没有引入 `mcp` SDK 或 `requests`——**零第三方依赖**是这个项目能被任何人 clone 下来直接跑的前提。

### 一个必须处理的协议细节

`tools/call` 的返回不是裸 JSON，而是一段 markdown：

```
# API Response Information
## Response Structure
- **data**: ... （字段说明）
## Original Response
{"success":true,"code":200,"data":{...}}
## 展示结果时，storeCode 必须完整展示
```

而且最后一个"展示提示"块可能追加在 JSON **之后**。因此解析必须两步走：
取 `## Original Response` 之后的第一个完整 JSON（用 `json.JSONDecoder().raw_decode`，
它能正确忽略尾随文本），而不是 `json.loads` 整段。见 `mcd_last_call.py::extract_payload`。

## 2. 实际调用的工具

### 2.1 `now-time-info` — 取服务端当前时间

- 参数：无
- 返回：`data.datetime` / `data.timestamp` / `data.formatted`
- 用途：**不用本机时钟**。时间预算是这个项目的核心变量，本机时钟漂移或时区错会把"来不来得及"算反。

### 2.2 `query-nearby-stores` — 附近门店

```json
{"beType": 1, "searchType": 2, "city": "深圳", "keyword": "科技园"}
```

| 参数 | 说明 |
| --- | --- |
| `beType` | 必传。`1` 到店自取，`5` 得来速 |
| `searchType` | 必传。`2` 按位置搜索，`1` 搜收藏餐厅 |
| `city` | 城市名 |
| `keyword` | 关键词 |

**实测坑**：schema 只把 `city` 标为必填，但 `searchType=2` 时如果只传 `city` 不传 `keyword`，
服务端返回 `code=600058, message=城市名或者关键词不能为空`。所以代码里两者**总是同时传**。

返回里对本项目关键的字段：

| 字段 | 用途 |
| --- | --- |
| `distance` | 单位**米**，直接用于估算步行时间 |
| `businessStartTime` / `businessEndTime` | 营业时段，用于判断打烊（支持跨零点） |
| `businessStatus` | 当前是否营业 |
| `reservationTimeOptions[].reservationOptionText` | 早餐 / 午餐 / 下午茶 / 夜市的分段时段，用来判断"现在这个点还能不能点到正餐" |

### 2.3 `query-meals` — 当前可售卖菜单

```json
{"storeCode": "1420762", "orderType": 1, "beType": 1}
```

返回的 `data` 由**两部分拼成**，只读其中一部分会得出错误结论：

```
data.categories[] = { name, meals: [ { code, tags } ] }     # 只有顺序和标签
data.meals{code}  = { name, currentPrice, originalPrice,
                      canWithOrder, image }                 # 名称和价格在这里
```

实测该门店 110 个 SKU / 15 个分类，两个结构完全对齐。要点：

- `currentPrice` 是**字符串**（`"33.5"`），不是数字，直接做算术会 `TypeError`。
- `canWithOrder` **不能当"可售"用**：实测 14 个套餐全部是 `false`，91/110 为 `false`。
  按它过滤会把所有套餐筛没。本项目明确忽略该字段。
- 分类名含业务信号，例如 `500大卡套餐`（实测构成 394 / 412 / 476 / 495 kcal）、
  `精选单人餐`、`小食拼盘/多人餐`。名称里的 `\n` 要先去掉。

因此调用链是"先用标签+分类+价格预筛 10 个候选，再只对**套餐**拉详情"——
单品直接用菜单名查营养表，省掉大量调用。

### 2.4 `query-meal-detail` — 餐品详情 / 套餐构成

```json
{"storeCode": "1420762", "orderType": 1, "beType": 1, "code": "9900005462"}
```

返回 `name` 和 `rounds[]`（套餐的分组，如"主食 / 小食 / 饮料"），每轮下 `choices[]` 带
`name` / `quantity` / `diffPrice` / `supportModify` / `modification`。

本项目用 `rounds` 里已被选中的 `choices` 还原"这单实际包含哪几样东西"，
既用来查热量，也用来估出餐时长（单品数越多越慢）。

### 2.5 `list-nutrition-foods` — 官方营养表

- 参数：无
- 返回：`data` 是一段 **字符串**，不是 JSON 数组。形如
  `[160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:`
  后面每行一条，逗号分隔。
- 关键限制：**只有商品名，没有 productCode**，且只有 160 条"常见餐品"。
  只能按名称匹配，长尾 SKU 会匹配不到。

因此本项目对热量采取"**宁可标未知，也不填假数字**"的策略：
匹配不到的项不计入总和，输出区间并在结果里报告覆盖率；一项都匹配不到的套餐直接不推荐。

### 2.6 `calculate-price` — 价格与优惠

```json
{"storeCode": "1420762", "orderType": 1, "beType": 1,
 "items": [{"productCode": "9900005462", "quantity": 1}]}
```

返回 `price`（应付总额）、`discount`、`productList[].subtotal`、`takeWayList[]`。

- **单位是分**（`3750` = ¥37.50），代码里统一 `/100` 再展示。
- `takeWayList[].code` 实测为 `eat-in` / `locker-out`，`create-order` 的 `takeWayCode`
  必须从这里取。早前硬编码的 `locker-in` 并不存在，会导致建单失败。
本项目**只用这个接口给的价格**，绝不用"菜单单价 × 数量"自己算——门店价、套餐价、券门槛都会让自算结果偏掉。

### 2.7 `create-order` — 建单（可选，需显式开启）

```json
{"storeCode": "1420762", "orderType": 1, "beType": 1,
 "items": [{"productCode": "9900005462", "quantity": 1}],
 "takeWayCode": "locker-in", "needTableware": 0}
```

只在用户加 `--order` 时调用。返回订单号与支付链接，**支付由人完成**。
离线演示模式永不建单。

## 3. 调用顺序

### 场景 1 · 还来不来得及（主链路，实测 14 次调用）

```
now-time-info
  → query-nearby-stores            (beType=1, searchType=2, city+keyword)
  → query-meals                    (storeCode)  ← 一次拿到 110 个 SKU 的名称+现价
  → list-nutrition-foods
  → query-meal-detail × 9          (只对套餐补全构成，单品跳过)
  → calculate-price                (所有候选一次算完)
  → 本地求解 → 输出判定 + 最晚下单时刻
  → [可选] create-order            (仅 --order)
```

预筛逻辑：标签（套餐/单品/爆款）+ 分类（单人餐/500大卡套餐）+ 价格次级排序，
每个分类最多 2 个，上限 10 个。需资格或非餐品的 SKU（学生专享 / 亲子 / 周边）先剔除。

### 场景 2 · 只剩几分钟（同上，排序目标不同）

调用链与场景 1 完全一致，只在最后换排序键：按 `出餐时长 → 用餐时长 → 余量` 升序，
即"谁的到手时间最短谁第一"。额外的业务差异是**允许推荐小食/饮料**（20 分钟本来就不该吃套餐），
场景 1 和 3 则要求正餐。

### 场景 3 · 时间 / 热量 / 预算 三重约束

```
now-time-info
  → query-nearby-stores
  → query-meals
  → list-nutrition-foods
  → query-meal-detail × 8
  → calculate-price
  → 过滤：先按热量上限、预算上限，再判时间可行性
  → 在可行集内按 价格 → 热量 → 余量 排序
  → 若可行集为空：给出"最接近的一单 + 差在哪"
```

`available-coupons` / `auto-bind-coupons` 未被接入主链路：自动领券是对**用户账户的写操作**，
不应该由 Agent 静默执行。需要券时由 `calculate-price` 的 `couponCode/couponId` 参数验证有效性即可。

## 4. 失败兜底

| 情况 | 现象 | 处理 |
| --- | --- | --- |
| Token 缺失 / 无效 | HTTP 401 | 不重试，直接提示检查 `MCD_MCP_TOKEN`，并建议改用 `--demo` |
| 触发限流 | HTTP 429 | 指数退避重试 3 次（1s / 2s / 4s），仍失败则报错退出 |
| 门店搜索缺 keyword | `code=600058` | 已固化为"city + keyword 总是同时传" |
| 门店搜索查无结果 | `code=600014` | 抛 `McpError`，CLI 提示"附近没有查到门店"，退出码 4 |
| 收藏餐厅为空 | `code=600050` | 改用 `searchType=2` |
| 门店不存在 | `code=60002` | 抛 `McpError`，不静默降级 |
| 餐品编码不存在 | `code=60001` | 跳过该候选并记 note，不影响其余推荐 |
| `items` 为空 | `code=600010` | 前置校验，不发起空请求 |
| 限流发生在握手阶段 | 429 during `initialize` | `initialize()` 也放进重试循环（异常矩阵实测发现原先会一次都不退避就失败） |
| 返回非 JSON 外壳 | markdown + JSON 混排 | 用 `raw_decode` 从 `## Original Response` 之后取第一个 JSON |
| 营养表匹配不到某单品 | 无对应行 | 不计入总和；输出 `[已知和, 已知和+90×缺失数]` 区间 + 覆盖率；全未命中则不推荐该餐品 |
| 距离字段缺失 | `distance` 为空/0 | 降级按 400m 估算，并在输出里标"置信度低" |
| 门店打烊 / 跨零点营业 | `businessEndTime < businessStartTime` | 按区间跨天判断；已打烊直接判"来不及" |
| 时间预算 ≤ 0 | 截止时刻已过 | 判定"来不及"，不再给推荐 |
| 约束全不满足 | 可行集为空 | 输出"最接近的一单 + 具体该放宽哪个条件、放宽到多少" |
| 单价缺失 | `productList` 无 subtotal | 按总额均摊，并在 notes 里明确标注"仅供参考" |

## 5. 业务价值

1. **把"决策"变成"截止时刻"**：用户不需要读懂菜单，只需要知道"12:26 之前必须点"。这是可执行的单一数字。
2. **用真实门店数据替代假设**：距离、营业时段、当前可售卖 SKU 全部来自官方接口，不是本地硬编码的菜单表。
3. **价格与热量都是官方口径**：价格只认 `calculate-price`，热量只认 `list-nutrition-foods`，不自行推算。
4. **下单边界清晰**：Agent 只建单，付款永远由人完成。误下单、提示注入导致的非预期消费在结构上被排除。
5. **降级即演示**：同一套求解代码，`--demo` 读快照、在线读接口，没有 Token 的人看到的效果与真实运行完全一致。

## 6. 隐私说明

主链路只调用 `now-time-info` / `query-nearby-stores` / `query-meals` / `query-meal-detail` /
`list-nutrition-foods` / `calculate-price`，**不读取**个人数据。

以下工具本项目**不调用**：`delivery-query-addresses`、`order-list`、`query-my-coupons`、
`query-my-account`、`mall-*`、`query-my-prizes`。离线快照中也不含任何个人订单、地址或账户信息。
