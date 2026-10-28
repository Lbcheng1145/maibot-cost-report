# 开销统计

麦麦（MaiBot）插件。查麦麦自己烧了多少 token、花了多少钱、缓存命中率怎么样。

## 功能

| 功能 | 触发方式 |
| --- | --- |
| 查开销 | `/开销 [天数]` 命令，渲染成一张卡片图 |
| LLM 自查 | `check_cost` 工具，用户问「今天花了多少钱」时由模型自己调 |

数据来自宿主 `statistics.local.models` 能力，底层是 `statistics_model_hourly` 聚合表。

## 安装

1. 把仓库内容放进 `MaiBot/plugins/maibot-cost-report/`，目录里必须有 `_manifest.json` 和 `plugin.py`
2. 在 WebUI 插件页启用，或重启麦麦
3. 首次加载会自动生成 `config.toml`

## 配置

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `report.enabled` | `true` | 是否允许查询 |
| `report.currency` | `元` | 金额单位 |
| `report.top_models` | `8` | 按花费列出前几个模型 |
| `report.max_days` | `30` | 允许查询的最大天数，防止 `/开销 9999` 扫表 |
| `report.show_cache` | `true` | 是否显示缓存命中率 |
| `report.output_mode` | `image` | `text` / `image` / `both`，渲染失败会自动退回文本 |
| `report.title` | `麦麦开销` | 卡片图标题，留空则只显示时间范围 |
| `report.tool_enabled` | `true` | 是否给 LLM 提供查询工具 |
| `report.tool_send_image` | `false` | 工具调用时是否同时发图 |

## 命令

| 命令 | 说明 |
| --- | --- |
| `/开销` | 最近 24 小时 |
| `/开销 7` | 最近 7 天，上限由 `max_days` 控制 |
| `/成本` `/花费` `/cost` | 别名 |

## 关于「最近 24 小时」

宿主的统计窗口按 `now - N 天` 计算，没有自然日边界，所以 `days=1` 得到的是**滚动 24 小时**而不是今天 0 点至今。凌晨查会带上昨天的一部分，白天基本等于今天。

要精确到自然日只能自己扫 `llm_usage` 明细，代价大得多，这里没做。

## 关于缓存命中率

聚合表里没有缓存字段，命中率只能从 `llm_usage` 明细算。插件会用**只读模式**（`mode=ro`）打开宿主数据库，只执行一条 `SELECT ... GROUP BY`，不写入、不改结构。

如果不想让插件碰数据库，把 `report.show_cache` 关掉即可，此时只走官方能力，不读文件。

为什么不用 `database.query` 能力：它按模型名做等值查询，做不了 `GROUP BY` + `SUM`，拉全表到内存再聚合在 24 小时几万条的规模下不可行。

## 常见问题

**图里的数字和模型配置页对不上？**
模型名取的是 `llm_usage.model_assign_name`（配置里的模型名），不是 `model_identifier`。两者不同名时以配置名为准。

**缓存命中率显示「—」？**
该模型在统计窗口内没有缓存字段数据，或者数据库读不到。检查 `show_cache` 是否开启、数据库是否被其他进程长时间锁住。

**渲染失败？**
会自动退回纯文本，日志里会有一条「统计图渲染失败」。常见原因是 Playwright 浏览器没装好。

**能查某个具体模型吗？**
目前不支持，只按花费降序列出前 N 个。

## 许可

MIT
