# 字段对照表

本表格全部来自**真实调用**，不是文档抄写。生成方式：

```bash
export MCD_MCP_TOKEN="..."     # 只从环境变量读
python dev/calibrate.py        # 落盘 dev/raw/*.json（已脱敏，且在 .gitignore 里）
```

采集快照：2026-10-23 22:19，门店 `1420762`（麦当劳深圳同方信息港餐厅），
`orderType=1` / `beType=1`（到店自取）。协议 `2025-06-18`，服务端 `mcd-mcp 1.0.0`。

---

## 1. `now-time-info`

| 官方返回字段 | 类型 | 我们代码中的用法 | 备注 |
| --- | --- | --- | --- |
| `data.datetime` | string `2026-10-23T22:19:24.333` | `datetime.fromisoformat()` 后**去掉时区**，作为 `Plan.now` | 整个项目的基准时钟。不用本机 `datetime.now()`，避免时区/漂移把结论算反 |
| `data.timestamp` | int（毫秒） | 未使用 | 有 `datetime` 就不需要 |
| `data.formatted` | string | 仅作降级兜底 | `datetime` 缺失时才用 |
| `data.timezone` / `offset` | string | 未使用 | 服务端固定 `GMT+08:00`；解析时丢弃 tzinfo 是因为其余字段也都是本地时间 |
| `data.date` / `year` / `month` / `day` / `dayOfWeek` / `utc` | mixed | 未使用 | — |

## 2. `query-nearby-stores`

入参（**两个都必须传**，见备注）：

| 参数 | 我们的取值 | 备注 |
| --- | --- | --- |
| `beType` | `1` | 到店自取。`5` 是得来速，会返回 `beCode` |
| `searchType` | `2` | 按位置搜索 |
| `city` | CLI `--city`，默认 `深圳` | — |
| `keyword` | CLI `--keyword`，默认 `科技园` | **schema 只把 city 标为必填，但只传 city 必报 600058**。所以代码里两者总是同传 |

返回 `data` 是**数组**：

| 官方返回字段 | 类型 | 我们代码中的用法 | 备注 |
| --- | --- | --- | --- |
| `data[].storeCode` | string | `Store.code`，后续所有调用的门店键 | — |
| `data[].storeName` | string | `Store.name`，终端卡片首行 | — |
| `data[].address` | string | `Store.address` | — |
| `data[].distance` | **int，单位米** | `Store.distance_m` → `walk_minutes()` | 实测 403 / 810 / 1233。为 0 或缺失时降级按 400m 估并标"置信度低" |
| `data[].businessStartTime` / `businessEndTime` | string `HH:MM` | `Store.open_at` / `close_at` | 用于打烊判断；`end < start` 视为跨零点 |
| `data[].businessStatus` | bool | `Store.business_status` | 当前是否营业 |
| `data[].reservation` / `reservationTimeOptions[]` | bool / array | **未使用** | 里面是早餐/午餐/下午茶/夜市的分段时段，是"现在能点到什么"的潜力信号，但当前版本没用它做过滤 |
| `data[].longitude` / `latitude` | float | **未使用** | 实测恒为 `0.0`，没有坐标可用，所以步行时间只能走"直线距离 × 绕行系数" |

## 3. `query-meals` —— 结构最容易被误读的一个

返回 `data` 是**两部分拼起来的**，只看其中一部分会得出错误结论：

```
data.categories[] = { name, meals: [ { code, tags } ] }        ← 只有顺序和标签
data.meals{code}  = { name, image, currentPrice,
                      originalPrice, canWithOrder }           ← 名称和价格在这里
```

| 官方返回字段 | 类型 | 我们代码中的用法 | 备注 |
| --- | --- | --- | --- |
| `data.categories[].name` | string（含 `\n`，如 `"500\n大卡套餐"`） | `MenuItem.category`，**先去换行**再判断 | 分类名是重要的业务信号：`500大卡套餐`、`精选单人餐`、`小食拼盘/多人餐` |
| `data.categories[].meals[].code` | string | `MenuItem.code` | 唯一键 |
| `data.categories[].meals[].tags` | string[]（可为 `null`） | `MenuItem.tags` → 相关度打分 | 实测取值：`套餐` / `单品` / `爆款` / `人气经典` / `精选` / `随心配` / `麦金卡` / `粉丝之选` / `立省10.5元起` |
| `data.meals{code}.name` | string | `MenuItem.name` | **名称只能从这里拿**，categories 里没有 |
| `data.meals{code}.currentPrice` | **string**，如 `"33.5"` | `int(round(float(x) * 100))` → `price_cents` | 是字符串不是数字，直接做算术会 `TypeError`。这是菜单现价（已含优惠），但**不是最终结算价** |
| `data.meals{code}.originalPrice` | **string** | 未使用 | 划线价，只用于展示"省了多少"，与判据无关 |
| `data.meals{code}.canWithOrder` | bool | **刻意忽略** | 实测 14 个套餐**全部 false**、91/110 为 false。它不是"可售"标记，按它过滤会把所有套餐筛没 |
| `data.meals{code}.image` | string (URL) | 未使用 | 终端卡片不显示图片 |

实测规模：110 个 SKU、15 个分类，两个结构完全对齐（无孤儿 code）。

## 4. `query-meal-detail`

| 官方返回字段 | 类型 | 我们代码中的用法 | 备注 |
| --- | --- | --- | --- |
| `data.code` / `data.name` | string | `Candidate.code` / `Candidate.name` | 名称优先用详情里的（比菜单更规范） |
| `data.rounds[]` | array | `join_items()` 还原"这一单包含哪几样" | 套餐有几轮就有几件（主食/小食/饮料 → 3 件） |
| `data.rounds[].name` | string | 未直接使用 | 如 `"选择套餐内小食"` |
| `data.rounds[].choices[].name` | string | 逐项去营养表查热量 | — |
| `data.rounds[].choices[].quantity` | int | **只取 `quantity > 0` 的项** | 未选中的选项 `quantity = 0`，混进来会把热量算高 |
| `data.rounds[].choices[].diffPrice` | string `"+ ¥2.5"` / `"- ¥0.5"` | 未使用 | 换品差价；我们用默认组合，所以是 0 |
| `data.rounds[].choices[].supportModify` / `modification` | bool / object | 未使用 | 口味特制（去冰、去酱）。当前版本不做特制 |
| `data.supportModify` | bool | 未使用 | 套餐级是否支持换品 |
| `data.image` | string | 未使用 | — |

**只在需要时调用**：只有 `套餐` 标签或名称含"套餐"的 SKU 才拉详情；
单品直接用菜单名查营养表。10 个候选里通常 6–8 个需要详情。

## 5. `list-nutrition-foods`

返回 `data` 是**字符串**，不是 JSON 数组：

```
[160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
  巨无霸,null,2146,513,27,26,42,961,171
  中薯条,null,1210,289,4,12,38,165,18
```

| 列 / 字段 | 我们的用法 | 备注 |
| --- | --- | --- |
| 第 1 列 `productName` | `parse_nutrition_table()` 的键；热量匹配的主键 | **只有名称，没有 productCode**，所以只能按名字匹配 |
| 第 4 列 `energyKcal` | `match_kcal()` 的返回值，单位 kcal | 第 3 列 `energyKj` 是千焦，别拿错 |
| 其余列（蛋白质/脂肪/碳水/钠/钙） | 未使用 | 当前版本只做热量，不越界给营养建议 |
| 总条数 | 160 | 覆盖不了长尾新品，匹配不到就报"未知"，不填 0 |

匹配顺序：精确 → 去括号后缀 → **归一化后精确**（去空格/连字符/引号，让 `麦辣鸡翅2块` 对上 `麦辣鸡翅-2块`）→ 归一化后包含匹配。

## 6. `calculate-price`

| 官方返回字段 | 类型 | 我们代码中的用法 | 备注 |
| --- | --- | --- | --- |
| `data.price` | int，**单位分** | 校验用；`/100` 后展示 | `3750` = ¥37.50 |
| `data.productList[].productCode` | string | 与候选 `code` 对齐的键 | — |
| `data.productList[].subtotal` | int，单位分 | **覆盖候选价格**（最终以此为准） | 与菜单 `currentPrice` 不一致时打 note 说明"以结算价为准" |
| `data.productList[].originalSubtotal` | int | 未使用 | 优惠前小计 |
| `data.discount` / `originalPrice` / `productPrice` / `productOriginalPrice` | int | 未使用 | `price` 已含优惠，不需要自己减 |
| `data.takeWayList[].code` | string | `pick_take_way()` → `create-order` 的 `takeWayCode` | 实测取值 `eat-in` / `locker-out`。**早前硬编码的 `locker-in` 并不存在**，会导致建单失败 |
| `data.takeWayList[].title` / `subtitle` | string | 未使用 | `堂食` / `外带` |

## 7. `create-order`（仅 `--order` 时调用，本轮**未执行真实建单**）

真实建单会产生订单并占用门店产能，且必须由人付款，因此本轮只核对 schema、不落单。
参数结构以 `docs/mcp-tools.schema.json` 为准，其中 `takeWayCode` 已改为从
`calculate-price.takeWayList` 动态取（见上）。

## 8. 刻意忽略的字段与原因

| 字段 | 为什么不用 |
| --- | --- |
| `canWithOrder` | 14 个套餐全为 false，语义不是"可售"；用了会误杀 |
| `longitude` / `latitude` | 实测恒为 0，等于没有坐标 |
| `image` | 终端卡片不渲染图片 |
| `reservationTimeOptions` | 有价值（早/午/下午茶/夜市分段），但当前版本未做时段过滤，留作后续 |
| `traceId` / 顶层 `datetime` | 可追溯的请求标识，落盘前用 `_scrub()` 删掉 |
| `originalPrice` / 划线价 | 与"来不来得及"无关 |

## 9. 错误码（实测）

| 场景 | `code` | `message` | 我们的处理 |
| --- | --- | --- | --- |
| 门店搜索查无结果 | `600014` | 没有查询到该地址，请检查配送地址是否正确 | `success=false` → 抛 `McpError`，CLI 提示"附近没有查到门店"并退出码 4 |
| 门店搜索缺 keyword | `600058` | 城市名或者关键词不能为空 | 固化为 city+keyword 同传，不再触发 |
| 缺 `storeCode` | `400` | storeCode门店编码不能为空 | 参数校验失败，前置避免 |
| 门店不存在 | `60002` | 未查询到门店信息, 检查storeCode和beCode是否配对 | 抛 `McpError`，不静默降级 |
| 餐品编码不存在 | `60001` | 未匹配到商品，请重新拉取全量商品列表 | 跳过该候选并记 note |
| `items` 为空 | `600010` | （参数错误） | 前置校验，不发起空请求 |
| Token 无效 | HTTP `401` | — | `AuthError`，**不重试**，提示检查 `MCD_MCP_TOKEN` |
| 限流 | HTTP `429` | — | 指数退避 3 次（1s/2s/4s），仍失败则抛错 |

## 10. 本轮按真实字段改掉的代码

| 位置 | 改前（推测） | 改后（实测） |
| --- | --- | --- |
| `parse_menu()` | 只读 `categories[].meals[].code/tags`，认为"菜单没有名称和价格" | 新增：从 `data.meals{code}` 读 `name` / `currentPrice` |
| 价格换算 | 假设 `currentPrice` 是数字 | 实测是字符串，`float()` 后再 `×100` 取分 |
| `needs_detail()` | 只看 `"套餐" in tags` | 加名称判断：`500大卡套餐` 类 SKU 的 tags 是"粉丝之选"或空，只看 tags 会漏 |
| `canWithOrder` | 曾计划用作可售过滤 | **明确弃用**，并在 docstring 与测试里锁死 |
| `submit_order()` | 硬编码 `takeWayCode="locker-in"` | 从 `calculate-price.takeWayList` 动态取（`locker-out` / `eat-in`） |
| `match_kcal()` | 精确 + 去括号 + 包含 | 增加归一化（去空格/连字符/引号），修复 `麦辣鸡翅2块` ↔ `麦辣鸡翅-2块` |
| 候选池 | 全部 SKU 参与打分 | 先剔除「学生专享 / 亲子 / 周边」等需资格或非餐品的 SKU |
| 候选上限 | 6 → 8 | 10：同类目 2 个名额曾把最快的一单挤掉 |
| `call_tool()` 重试 | `initialize()` 在重试循环外，握手阶段 429 一次都不退避 | `initialize()` 进入重试循环（异常矩阵实测发现） |
