"""查询麦麦的 token 消耗和花费。

数据来自宿主的 `statistics.local.models` 能力，底层读 `statistics_model_hourly`
聚合表（按小时预聚合，不需要扫 `llm_usage` 明细）。

## 为什么默认是「最近 24 小时」而不是「今天 0 点」

宿主的统计窗口按 `now - N 天` 计算（`data.py:926`），没有自然日边界，
所以 `days=1` 得到的是滚动 24 小时。凌晨查的时候会带上昨天的一部分，
但白天基本等于今天。要精确到自然日只能自己扫 `llm_usage`，代价大得多，
这里不值得。
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from maibot_sdk import (
    Command,
    Field,
    MaiBotPlugin,
    PluginConfigBase,
    Tool,
)
from maibot_sdk.types import ToolParameterInfo, ToolParamType

#: 宿主硬性要求：config.toml 必须有 [plugin].config_version，缺失即加载失败。
SUPPORTED_CONFIG_VERSION = "1.0.0"


class PluginSectionConfig(PluginConfigBase):
    """`[plugin]` 配置节。config_version 由插件维护，勿手改。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用",
        json_schema_extra={"label": "启用", "order": 0},
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置结构版本，由插件维护",
        json_schema_extra={"hidden": True, "disabled": True, "order": 99},
    )


class ReportConfig(PluginConfigBase):
    __ui_label__ = "统计"
    __ui_icon__ = "bar_chart"
    __ui_order__ = 1

    enabled: bool = Field(
        default=True,
        description="是否允许查询开销",
        json_schema_extra={"label": "启用查询", "order": 0},
    )

    currency: str = Field(
        default="元",
        description="金额单位",
        json_schema_extra={"label": "货币单位", "order": 1},
    )

    top_models: int = Field(
        default=8,
        ge=1,
        le=20,
        description="按花费列出前几个模型",
        json_schema_extra={
            "label": "展示模型数",
            "hint": "按花费从高到低取前 N 个，避免一次刷屏。",
            "step": 1,
            "order": 2,
        },
    )

    max_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="允许查询的最大天数",
        json_schema_extra={
            "label": "最大查询天数",
            "hint": "防止有人发 `/开销 9999` 把聚合表扫一遍。",
            "step": 1,
            "order": 3,
        },
    )

    show_cache: bool = Field(
        default=True,
        description="是否显示缓存命中率",
        json_schema_extra={
            "label": "显示缓存命中率",
            "hint": (
                "开启后额外读一次 `llm_usage` 明细表算命中率（聚合表里没有缓存字段）。"
                "命中率直接影响实际花费：命中部分按缓存价计费，通常只有正常价的百分之几。"
                "关掉可以省一次查询。"
            ),
            "order": 4,
        },
    )

    output_mode: str = Field(
        default="image",
        description="输出形式：text / image / both",
        json_schema_extra={
            "label": "输出形式",
            "hint": (
                "image 用 Playwright 把统计渲染成一张卡片图，比纯文本好看但要多花 1~2 秒；"
                "text 直接发文本；both 先发图再补一份文字（方便复制数字）。"
                "渲染失败会自动退回文本。"
            ),
            "order": 5,
        },
    )

    title: str = Field(
        default="麦麦开销",
        description="统计图卡片标题",
        json_schema_extra={
            "label": "卡片标题",
            "hint": (
                "统计图左上角的大标题，后面会自动接上时间范围（如「最近 24 小时」）。"
                "留空则只显示时间范围。"
            ),
            "placeholder": "麦麦开销",
            "order": 6,
        },
    )

    tool_enabled: bool = Field(
        default=True,
        description="是否给 LLM 提供查询开销的工具",
        json_schema_extra={
            "label": "允许 LLM 自查开销",
            "hint": (
                "开启后注册一个工具，用户问「今天花了多少钱」「哪个模型最贵」时"
                "LLM 会自己调它拿数字，再用自己的话回答。关掉则只有 `/开销` 命令能用。"
            ),
            "order": 7,
        },
    )

    tool_send_image: bool = Field(
        default=False,
        description="工具调用时是否同时发统计图",
        json_schema_extra={
            "label": "工具调用也发图",
            "hint": (
                "默认关闭：工具只把数字交给 LLM，由它转述，回复更自然。"
                "开启后每次调用会额外发一张卡片图，但 LLM 通常还会再说一遍，容易重复。"
            ),
            "depends_on": "tool_enabled",
            "order": 8,
        },
    )


class RootConfig(PluginConfigBase):
    """配置根模型。⚠ `plugin` 节必须显式声明，否则加载失败。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)


class CostReportPlugin(MaiBotPlugin):
    config_model = RootConfig

    async def on_load(self) -> None:
        self.ctx.logger.info("开销统计插件已加载")

    async def on_unload(self) -> None:
        self.ctx.logger.info("开销统计插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope == "self":
            self.ctx.logger.info("开销统计配置已热更新 version=%s", version)

    # ---------------------------------------------------------------- 数据

    def _clamp_days(self, days: int) -> int:
        """把天数夹到 [1, max_days]。命令和工具都走这里，避免各写一份。"""
        if days < 1:
            return 1
        return min(days, self.config.report.max_days)

    async def _gather(
        self, days: int
    ) -> tuple[list[dict[str, Any]] | None, dict[str, dict[str, int]]]:
        """取聚合统计 + 缓存命中。

        返回 `None` 表示查询本身失败（区别于「查询成功但没数据」的空列表），
        调用方据此决定是报错还是说「还没有记录」。
        """
        try:
            models = await self.ctx.statistics.local.models(days=days, limit=20)
        except Exception as exc:
            self.ctx.logger.error("查询模型统计失败: %s", exc)
            return None, {}

        cache_stats: dict[str, dict[str, int]] = {}
        if self.config.report.show_cache:
            cache_stats = await asyncio.to_thread(self._query_cache_stats, days)
        return models, cache_stats

    # ---------------------------------------------------------------- 工具

    @Tool(
        "check_cost",
        brief_description=(
            "查麦麦自己烧了多少钱：有人问花了多少、成本多少、token 用了多少、"
            "哪个模型最贵时调用"
        ),
        detailed_description=(
            "查的是麦麦运行时真实的模型开销，数据来自本机统计表，不是估算。"
            "返回总花费、token 数、调用次数、缓存命中率，以及花钱最多的几个模型。"
            "用户问「今天花了多少钱」「这个月开销多少」「哪个模型最贵」时调用。"
            "拿到数字后用你自己的话一两句说完，不要照着念，也不要加表格。"
            "只有对方真的在问开销时才调，别主动提这个话题。"
        ),
        parameters=[
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="当前聊天流 ID",
                required=True,
            ),
            ToolParameterInfo(
                name="days",
                param_type=ToolParamType.INTEGER,
                description="统计最近几天，1 就是最近 24 小时。不填默认 1",
                required=False,
            ),
        ],
    )
    async def handle_check_cost(self, stream_id: str = "", days: int = 0, **kwargs):
        cfg = self.config.report
        if not self.config.plugin.enabled or not cfg.enabled or not cfg.tool_enabled:
            return {"success": False, "content": "开销统计没开，查不了。"}

        # 模型可能把天数传成字符串或 0，别直接 int() 崩掉。
        try:
            want = int(days or 0)
        except (TypeError, ValueError):
            want = 1
        span = self._clamp_days(want if want >= 1 else 1)
        # 留个痕迹，方便回头数 LLM 到底多爱查账。
        self.ctx.logger.debug("LLM 调用 check_cost days=%s", span)

        models, cache_stats = await self._gather(span)
        if models is None:
            return {"success": False, "content": "统计表查不到，可能是数据库被占用或者路径不对。"}

        content = self._summarize(models, span, cache_stats)

        if cfg.tool_send_image and stream_id and models:
            await self._send_image(models, span, cache_stats, stream_id)

        return {"success": True, "content": content}

    # ---------------------------------------------------------------- 命令

    @Command("cost", pattern=r"^/(开销|成本|花费|cost)(?:\s+(?P<days>\d{1,3}))?$")
    async def handle_cost(self, **kwargs):
        cfg = self.config.report
        if not self.config.plugin.enabled or not cfg.enabled:
            return False, "开销统计已关闭。", 1

        matched = kwargs.get("matched_groups") or {}
        raw_days = str(matched.get("days") or "").strip()
        days = self._clamp_days(int(raw_days) if raw_days else 1)

        models, cache_stats = await self._gather(days)
        if models is None:
            return False, "查询失败，看看日志。", 1

        text = self._format(models, days, cache_stats)
        stream_id = str(kwargs.get("stream_id") or "")
        mode = str(cfg.output_mode or "image").strip().lower()

        # ⚠ 命令的返回值不会自动发到聊天流（bot.py:665 只存库记日志），必须自己发。
        sent_image = False
        if mode in ("image", "both") and stream_id:
            sent_image = await self._send_image(models, days, cache_stats, stream_id)

        if stream_id:
            if mode == "both" or not sent_image:
                try:
                    await self.ctx.send.text(text, stream_id)
                except Exception as exc:
                    self.ctx.logger.error("发送开销统计失败: %s", exc)

        return True, text, 1

    # ---------------------------------------------------------------- 图片输出

    async def _send_image(
        self,
        models: list[dict[str, Any]],
        days: int,
        cache_stats: dict[str, dict[str, int]],
        stream_id: str,
    ) -> bool:
        """把统计渲染成卡片图发出去。失败返回 False，由调用方退回文本。"""
        html = self._build_html(models, days, cache_stats)
        try:
            # 只截 .card 本体：用 body + full_page 会把整页视口都截进去，
            # 底部留一大片空白（视口 720 高、内容只有 600）。
            result = await self.ctx.render.html2png(
                html,
                selector=".card",
                viewport={"width": 760, "height": 400},
                device_scale_factor=2.0,
                full_page=False,
                allow_network=False,
            )
        except Exception as exc:
            self.ctx.logger.warning("统计图渲染失败: %s", exc)
            return False

        b64 = ""
        if isinstance(result, dict):
            b64 = str(result.get("image_base64") or "")
        if not b64:
            self.ctx.logger.warning("统计图渲染没有返回 image_base64")
            return False

        try:
            await self.ctx.send.image(b64, stream_id)
            return True
        except Exception as exc:
            self.ctx.logger.warning("发送统计图失败: %s", exc)
            return False

    def _build_html(
        self,
        models: list[dict[str, Any]],
        days: int,
        cache_stats: dict[str, dict[str, int]],
    ) -> str:
        cfg = self.config.report
        unit = cfg.currency
        label = "最近 24 小时" if days == 1 else f"最近 {days} 天"
        # 标题可自定义；留空就只显示时间范围，不留一个孤零零的「·」。
        custom_title = str(cfg.title or "").strip()
        heading = f"{custom_title} · {label}" if custom_title else label

        total_cost = sum(float(m.get("total_cost") or 0) for m in models)
        total_tokens = sum(int(m.get("total_tokens") or 0) for m in models)
        total_req = sum(int(m.get("request_count") or 0) for m in models)
        avg_per_k = (total_cost / total_req * 1000) if total_req else 0.0

        hit_sum = sum(v["hit"] for v in cache_stats.values())
        miss_sum = sum(v["miss"] for v in cache_stats.values())
        denom = hit_sum + miss_sum
        hit_rate = (hit_sum / denom * 100) if denom else 0.0

        def esc(value: Any) -> str:
            return (
                str(value)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )

        ranked = sorted(models, key=lambda m: -float(m.get("total_cost") or 0))[: cfg.top_models]
        rows: list[str] = []
        max_cost = float(ranked[0].get("total_cost") or 0) if ranked else 0.0
        for m in ranked:
            name = esc(str(m.get("model_name") or "Unknown"))
            cost = float(m.get("total_cost") or 0)
            tokens = int(m.get("total_tokens") or 0)
            req = int(m.get("request_count") or 0)
            share = (cost / total_cost * 100) if total_cost else 0.0
            bar = (cost / max_cost * 100) if max_cost else 0.0

            cache_cell = "<span class='muted'>—</span>"
            stats = cache_stats.get(str(m.get("model_name") or ""))
            if stats:
                d = stats["hit"] + stats["miss"]
                if d > 0:
                    rate = stats["hit"] / d * 100
                    tone = "#16a34a" if rate >= 60 else "#f59e0b" if rate >= 30 else "#dc2626"
                    cache_cell = f"<span style='color:{tone};font-weight:600'>{rate:.0f}%</span>"

            rows.append(
                f"""
                <tr>
                  <td class="name" title="{name}">{name}</td>
                  <td class="num">{cost:.2f}{esc(unit)}</td>
                  <td class="num">{tokens:,}</td>
                  <td class="num">{req:,}</td>
                  <td class="num">{cache_cell}</td>
                  <td class="bar-cell"><div class="bar"><i style="width:{bar:.1f}%"></i></div></td>
                  <td class="num muted">{share:.1f}%</td>
                </tr>
                """
            )

        cache_block = ""
        if denom > 0:
            cache_block = f"""
            <div class="stat">
              <div class="k">缓存命中</div>
              <div class="v">{hit_rate:.1f}<small>%</small></div>
              <div class="d">{hit_sum:,} 命中 / {miss_sum:,} 未命中</div>
            </div>
            """

        return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 24px;
    font-family: "Microsoft YaHei", "PingFang SC", system-ui, sans-serif;
    background: linear-gradient(160deg, #f8fafc 0%, #eef2f7 100%);
    color: #0f172a;
  }}
  .card {{
    background: #fff; border-radius: 18px; padding: 26px 28px;
    box-shadow: 0 10px 30px rgba(15,23,42,.08), 0 2px 6px rgba(15,23,42,.04);
  }}
  h1 {{ margin: 0 0 4px; font-size: 20px; font-weight: 700; letter-spacing: .5px; }}
  .sub {{ margin: 0 0 20px; font-size: 12px; color: #64748b; }}
  .stats {{ display: flex; gap: 14px; margin-bottom: 22px; }}
  .stat {{
    flex: 1; background: #f8fafc; border: 1px solid #e2e8f0;
    border-radius: 12px; padding: 14px 16px;
  }}
  .stat .k {{ font-size: 11px; color: #64748b; margin-bottom: 6px; letter-spacing: .5px; }}
  .stat .v {{ font-size: 24px; font-weight: 700; color: #0f172a; line-height: 1.1; }}
  .stat .v small {{ font-size: 13px; font-weight: 500; margin-left: 2px; color: #64748b; }}
  .stat .d {{ margin-top: 5px; font-size: 11px; color: #94a3b8; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12.5px; }}
  th {{
    text-align: left; font-weight: 600; font-size: 11px; color: #64748b;
    padding: 0 8px 8px; border-bottom: 1px solid #e2e8f0; white-space: nowrap;
  }}
  th.num, td.num {{ text-align: right; }}
  td {{ padding: 9px 8px; border-bottom: 1px solid #f1f5f9; }}
  tr:last-child td {{ border-bottom: none; }}
  td.name {{
    max-width: 210px; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; font-weight: 500;
  }}
  td.num {{ font-variant-numeric: tabular-nums; }}
  .muted {{ color: #94a3b8; }}
  .bar-cell {{ width: 110px; }}
  .bar {{ height: 6px; background: #f1f5f9; border-radius: 999px; overflow: hidden; }}
  .bar i {{ display: block; height: 100%; background: linear-gradient(90deg,#60a5fa,#3b82f6); border-radius: 999px; }}
  .foot {{ margin-top: 16px; font-size: 11px; color: #94a3b8; }}
</style></head>
<body>
  <div class="card">
    <h1>{esc(heading)}</h1>
    <p class="sub">共 {total_req:,} 次模型调用</p>

    <div class="stats">
      <div class="stat">
        <div class="k">花费</div>
        <div class="v">{total_cost:.2f}<small>{esc(unit)}</small></div>
        <div class="d">平均每千次 {avg_per_k:.2f}{esc(unit)}</div>
      </div>
      <div class="stat">
        <div class="k">Token</div>
        <div class="v">{total_tokens:,}</div>
        <div class="d">prompt + completion</div>
      </div>
      {cache_block}
    </div>

    <table>
      <thead><tr>
        <th>模型</th><th class="num">花费</th><th class="num">Token</th>
        <th class="num">调用</th><th class="num">缓存</th><th>占比</th><th class="num"></th>
      </tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    <div class="foot">数据来自 statistics_model_hourly 聚合表{cache_stats and '，缓存命中率取自 llm_usage 明细' or ''}</div>
  </div>
</body></html>"""

    # ---------------------------------------------------------------- 缓存统计

    def _db_path(self) -> Path | None:
        """插件数据目录在 `<root>/data/plugins/<id>`，往上两级就是数据库所在目录。"""
        try:
            return Path(self.ctx.paths.data_dir).parent.parent / "MaiBot.db"
        except Exception:
            return None

    def _query_cache_stats(self, days: int) -> dict[str, dict[str, int]]:
        """从 `llm_usage` 明细表算缓存命中。聚合表没有缓存字段，只能查明细。"""
        db_path = self._db_path()
        if db_path is None or not db_path.is_file():
            self.ctx.logger.debug("数据库文件不存在，跳过缓存统计: %s", db_path)
            return {}

        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        # 用 model_assign_name 分组：它才是配置里的模型名（`wb-proxy/deepseek-v4.1-flash`），
        # 和 statistics_model_hourly.model_name 对得上。llm_usage.model_name 存的是
        # model_identifier（`deepseek-v4.1-flash`），直接拿它对不上。
        sql = """
            SELECT COALESCE(NULLIF(model_assign_name, ''), NULLIF(model_name, ''), 'Unknown') AS model,
                   SUM(prompt_cache_hit_tokens) AS hit,
                   SUM(prompt_cache_miss_tokens) AS miss
            FROM llm_usage
            WHERE timestamp >= ?
            GROUP BY model
        """
        try:
            # 只读连接 + busy_timeout，避免和正在跑的 MaiBot 抢锁
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
            try:
                rows = con.execute(sql, (since,)).fetchall()
            finally:
                con.close()
        except Exception as exc:
            self.ctx.logger.warning("读取缓存统计失败: %s", exc)
            return {}

        stats: dict[str, dict[str, int]] = {}
        for model, hit, miss in rows:
            stats[str(model)] = {
                "hit": int(hit or 0),
                "miss": int(miss or 0),
            }
        return stats

    # ---------------------------------------------------------------- 格式化

    def _summarize(
        self,
        models: list[dict[str, Any]],
        days: int,
        cache_stats: dict[str, dict[str, int]],
    ) -> str:
        """给 LLM 读的紧凑摘要：两行、没有列对齐、没有缩进，方便它直接转述。"""
        cfg = self.config.report
        unit = cfg.currency
        label = "最近 24 小时" if days == 1 else f"最近 {days} 天"

        if not models:
            return f"{label}还没有模型调用记录。"

        total_cost = sum(float(m.get("total_cost") or 0) for m in models)
        total_tokens = sum(int(m.get("total_tokens") or 0) for m in models)
        total_req = sum(int(m.get("request_count") or 0) for m in models)

        head = (
            f"{label}：花费 {total_cost:.2f}{unit}，{total_tokens:,} token，"
            f"{total_req} 次调用"
        )
        hit = sum(v["hit"] for v in cache_stats.values())
        miss = sum(v["miss"] for v in cache_stats.values())
        if hit + miss:
            head += f"，缓存命中 {hit / (hit + miss) * 100:.1f}%"

        ranked = sorted(models, key=lambda m: -float(m.get("total_cost") or 0))[:3]
        items = [
            f"{str(m.get('model_name') or 'Unknown')} "
            f"{float(m.get('total_cost') or 0):.2f}{unit}"
            for m in ranked
        ]
        return f"{head}。\n花钱最多：{'、'.join(items)}。"

    def _format(
        self,
        models: list[dict[str, Any]],
        days: int,
        cache_stats: dict[str, dict[str, int]] | None = None,
    ) -> str:
        cfg = self.config.report
        unit = cfg.currency
        cache_stats = cache_stats or {}

        if not models:
            label = "最近 24 小时" if days == 1 else f"最近 {days} 天"
            return f"{label}还没有模型调用记录。"

        total_cost = sum(float(m.get("total_cost") or 0) for m in models)
        total_tokens = sum(int(m.get("total_tokens") or 0) for m in models)
        total_req = sum(int(m.get("request_count") or 0) for m in models)
        avg_cost = total_cost / total_req if total_req else 0.0

        label = "最近 24 小时" if days == 1 else f"最近 {days} 天"
        lines = [
            f"{label}",
            f"  花费 {total_cost:.2f}{unit}",
            f"  token {total_tokens:,}",
            f"  请求 {total_req} 次，平均每千次 {avg_cost * 1000:.2f}{unit}",
        ]

        # 缓存命中：命中部分按缓存价计费，直接影响实际花费
        if cache_stats:
            hit_sum = sum(v["hit"] for v in cache_stats.values())
            miss_sum = sum(v["miss"] for v in cache_stats.values())
            denom = hit_sum + miss_sum
            if denom > 0:
                rate = hit_sum / denom * 100
                lines.append(
                    f"  缓存命中 {rate:.1f}%（{hit_sum:,} 命中 / {miss_sum:,} 未命中）"
                )

        lines.extend(["", "按模型（花费降序）："])

        ranked = sorted(models, key=lambda m: -float(m.get("total_cost") or 0))
        for m in ranked[: cfg.top_models]:
            name = str(m.get("model_name") or "Unknown")
            cost = float(m.get("total_cost") or 0)
            tokens = int(m.get("total_tokens") or 0)
            req = int(m.get("request_count") or 0)
            share = (cost / total_cost * 100) if total_cost else 0.0
            row = f"  {name[:24]:<26} {cost:>7.2f}{unit} {tokens:>11,} tok {req:>5}次 {share:>5.1f}%"
            stats = cache_stats.get(name)
            if stats:
                denom = stats["hit"] + stats["miss"]
                if denom > 0:
                    row += f" 缓存{stats['hit'] / denom * 100:.0f}%"
            lines.append(row)

        if len(ranked) > cfg.top_models:
            rest = ranked[cfg.top_models :]
            rest_cost = sum(float(m.get("total_cost") or 0) for m in rest)
            lines.append(f"  另外 {len(rest)} 个模型合计 {rest_cost:.2f}{unit}")

        return "\n".join(lines)


def create_plugin() -> CostReportPlugin:
    return CostReportPlugin()
