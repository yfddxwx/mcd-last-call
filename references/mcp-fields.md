# MCP 返回字段与陷阱

接入地址 `https://mcp.mcd.cn`，Streamable HTTP，认证头 `Authorization: Bearer $MCD_MCP_TOKEN`。
协议 `2025-06-18`，服务端 `mcd-mcp 1.0.0`，实测 35 个工具，限流 600 次/分钟。

`tools/call` 的返回不是裸 JSON，而是 markdown（`Response Structure` + `## Original Response`
+ 可能追加的展示提示）。解析要取 `## Original Response` 之后的第一个完整 JSON。

## now-time-info

| 字段 | 类型 | 用途 |
| --- | --- | --- |
| `data.datetime` | string ISO | 基准时钟，去掉时区后作为 `now` |
| `data.timestamp` / `formatted` / `timezone` | — | 备用 |

## query-nearby-stores

入参：`beType=1`（到店自取，`5` 是得来速）、`searchType=2`、`city`、`keyword`。

> **`city` 和 `keyword` 必须同时传。** schema 只把 `city` 标为必填，
> 但 `searchType=2` 时只传 `city` 会返回 `600058`。

| 字段 | 类型 | 用途 |
| --- | --- | --- |
| `data[].storeCode` | string | 后续所有调用的门店键 |
| `data[].storeName` / `address` | string | 展示 |
| `data[].distance` | int，**米** | 步行时间 |
| `data[].businessStartTime` / `businessEndTime` | `HH:MM` | 打烊判断，`end < start` 视为跨零点 |
| `data[].businessStatus` | bool | 当前是否营业 |
| `data[].longitude` / `latitude` | float | 实测恒为 `0.0`，**不可用** |
| `data[].reservationTimeOptions` | array | 早餐/午餐/下午茶/夜市分段，可判断"现在能点什么" |

## query-meals

返回 `data` 是**两部分拼的**，只读一部分会误判：

```
data.categories[] = { name, meals: [{ code, tags }] }        ← 只有顺序和标签
data.meals{code}  = { name, currentPrice, originalPrice,
                      canWithOrder, image }                  ← 名称和价格在这里
```

| 字段 | 类型 | 用途 |
| --- | --- | --- |
| `categories[].name` | string，含 `\n` | 先去换行。`500大卡套餐`、`精选单人餐`、`小食拼盘/多人餐` 是业务信号 |
| `categories[].meals[].code` | string | 唯一键 |
| `categories[].meals[].tags` | string[]，可为 `null` | `套餐` / `单品` / `爆款` / `人气经典` / `精选` / `随心配` / `麦金卡` |
| `meals{code}.name` | string | 名称**只能**从这里拿 |
| `meals{code}.currentPrice` | **string** `"33.5"` | 菜单现价，`float()` 后 ×100 转成分 |
| `meals{code}.canWithOrder` | bool | **不要用**。实测 14 个套餐全为 `false`，按它过滤套餐会全没 |
| `meals{code}.image` | URL | 不需要 |

实测规模：110 个 SKU / 15 个分类，`categories` 与 `meals` 完全对齐。

## query-meal-detail

| 字段 | 用途 |
| --- | --- |
| `data.name` | 规范名称（优先于菜单名） |
| `data.rounds[]` | 套餐分组（主食/小食/饮料），**有几轮就是几件** |
| `rounds[].choices[].name` / `quantity` | **只取 `quantity > 0`** 的项，混入 0 会把热量算高 |
| `rounds[].choices[].diffPrice` | `"+ ¥2.5"` 换品差价，用默认组合时为 0 |
| `rounds[].choices[].supportModify` / `modification` | 口味特制（去冰/去酱），当前不处理 |

只对**套餐**调用。单品直接用菜单名查营养表，能省一半调用。

## list-nutrition-foods

`data` 是**字符串**，不是数组：

```
[160]{productName,nutritionDescription,energyKj,energyKcal,...}:
  巨无霸,null,2146,513,27,26,42,961,171
```

- 第 1 列 `productName`：唯一主键，**没有 productCode**，只能按名称匹配。
- 第 4 列 `energyKcal`：热量（注意第 3 列是千焦）。
- 共 160 条，覆盖不了长尾新品。匹配不到 → 标"未知"，不填 0。

## calculate-price

| 字段 | 类型 | 用途 |
| --- | --- | --- |
| `data.price` | int，**分** | 应付总额（已含优惠），最终以此为准 |
| `data.productList[].productCode` / `subtotal` | string / int | 按 SKU 拆分的价格，用来覆盖菜单现价 |
| `data.productList[].originalSubtotal` | int | 优惠前，仅供参考 |
| `data.discount` | int | 优惠额 |
| `data.takeWayList[].code` | string | 实测 `eat-in` / `locker-out`，`create-order` 的 `takeWayCode` 必须从这里取 |

## create-order

只在用户明确要求时调用。`takeWayCode` 从 `calculate-price.takeWayList` 取，
**不要硬编码**（曾用 `locker-in`，该值不存在）。返回订单号与支付链接，
**付款由人完成**，任何情况下都不要声称已支付。

## 错误码

| code | 含义 | 处理 |
| --- | --- | --- |
| `400` | 缺 `storeCode` 等必填参数 | 前置校验，不要发空参数 |
| `60001` | 未匹配到商品 | 丢弃该候选，继续其余 |
| `60002` | 未查询到门店信息 | 重新搜索门店 |
| `600010` | `items` 为空 | 先确认候选非空 |
| `600014` | 没有查询到该地址 | 让用户换地标/商圈，不要编门店 |
| `600058` | 城市名或关键词不能为空 | `city` + `keyword` 同传 |
| HTTP `401` | Token 无效/过期/缺失 | 提示检查 `MCD_MCP_TOKEN`，**不重试** |
| HTTP `429` | 超过 600 次/分钟 | 退避 1/2/4 秒重试 3 次，仍失败则改用离线快照 |

> 实现细节：`initialize` 握手同样可能被 429 打回，重试循环必须把它包在里面，
> 否则限流时一次都不退避就失败。
