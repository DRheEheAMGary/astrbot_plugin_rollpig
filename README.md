<div align="center">
  <img src="./PigLogo.jpeg" width="260" alt="PigLogo">
  <h2>🐖 astrbot_plugin_rollpig 🐖</h2>
  <p>今天是什么小猪 — AstrBot 移植版</p>
</div>

移植自 [SonderXiaoming/huannai_plugin_rollpig](https://github.com/SonderXiaoming/huannai_plugin_rollpig)（原项目：[Bearlele/nonebot-plugin-rollpig](https://github.com/Bearlele/nonebot-plugin-rollpig)）。

## 功能指令

| 指令 | 说明 |
| --- | --- |
| `今日小猪` | 抽取今天属于你的小猪类型（每日 0 点重置，当天重复抽取结果不变） |
| `随机小猪` | 从 PigHub 随机获取一张猪猪图 |
| `找猪 <关键词>` | 从 PigHub 中搜索关键词相关的猪猪图片并发送（默认最多 8 张） |
| `刷新小猪` | 手动从 PigHub 刷新图库缓存 |
| `今日小猪帮助` | 查看指令帮助 |

> 若 AstrBot 配置了指令前缀（如 `/`），请按你的前缀触发，例如 `/今日小猪`、`/找猪 喜报`。

## 安装方式

1. 将本仓库放入 AstrBot 的 `data/plugins/astrbot_plugin_rollpig` 目录，或在 AstrBot 插件管理页面通过仓库地址安装。
2. 确保依赖 `httpx` 已安装（AstrBot 环境通常已内置）。
3. 重启或重载插件即可使用。

## 配置项

可在 AstrBot WebUI 插件配置页调整以下选项：

- `timezone`：时区，默认 `Asia/Shanghai`，用于每日小猪重置与 0 点自动刷新。
- `max_search_results`：`找猪` 最多返回图片数量（1–20，默认 8）。
- `auto_refresh_enabled`：是否每天 0 点自动从 PigHub 刷新图库（默认开启）。
- `refresh_admin_only`：是否仅允许管理员执行 `刷新小猪`（默认关闭）。
- `pighub_api_url`：PigHub 图库 API 地址（默认 `https://pighub.top/api/all-images`）。
- `pighub_image_base_url`：PigHub 图片基础 URL（默认 `https://pighub.top/data/`）。
- `request_timeout`：网络请求超时时间（5–120 秒，默认 30）。

## 新增自定义小猪

资源目录位于 `resource/`：

- `resource/pig.json`：小猪定义列表，每项包含 `id`、`name`、`description`、`analysis`。
- `resource/image/`：对应小猪图片，文件名需与 `id` 一致，支持 `png` / `jpg` / `jpeg` / `webp` / `gif`。

## 致谢与许可

- 上游 HoshinoBot 移植版：[huannai_plugin_rollpig](https://github.com/SonderXiaoming/huannai_plugin_rollpig)
- 原版 NoneBot2 插件：[nonebot-plugin-rollpig](https://github.com/Bearlele/nonebot-plugin-rollpig)
- 本项目沿用 [LICENSE](./LICENSE)（MIT License）。
