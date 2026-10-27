# Pixiv 涩图 (astrbot_plugin_pixiv_setu)

一个轻量的 AstrBot 插件：检测到消息中含有 **「来份涩图」** 时，从 **Pixiv**（通过 [Lolicon API](https://api.lolicon.app) 图库索引）随机发一张涩图，并支持 **tag 匹配**。

## 功能

- 触发词子串匹配（默认「来份涩图」，可改）
- 从触发词后面的文字解析标签，例如「来份涩图 猫娘」→ 按 `cat_girl` 标签筛选
- 中文标签别名映射（如 `猫娘=cat_girl`）
- 随机抽取：一次拉取一小批候选再随机挑一张
- R18 默认关闭（非 R18），可选仅 R18 / 混合
- 私聊 R18 独立开关：群聊与私聊的 R18 等级分开配置，私聊可单独放开或锁死
- 排除 AI 生成图、可选图片尺寸、可选图片代理
- 回复末尾附带 Pixiv PID 与原作链接（可关）
- 按用户速率限制，防刷屏

## 使用示例

```
用户：来份涩图
机器人：[图片] 给你涩图~
        PID: 12345678  https://www.pixiv.net/artworks/12345678

用户：来份涩图 猫娘
机器人：[图片] 给你涩图~

用户：来份涩图 白丝 猫耳
机器人：[图片] 给你涩图~
```

- 多个标签默认为「全部命中」（AND）语义：`来份涩图 猫娘 白丝` 会返回同时命中 `猫娘` 和 `白丝` 的图；配置 `tag_match` 可改回 `or`（命中任一）。
- 标签没命中时，会自动兜底随机一张并提示。
- 标签别名在 WebUI 插件配置页配置，例如 `猫娘=cat_girl,白丝=white_pantyhose`。

## 安装

将本目录放到 AstrBot 的 `plugins` 目录下（即 `data/plugins/astrbot_plugin_pixiv_setu`），重启 AstrBot 即可。依赖 `aiohttp`、`aiofiles`（见 `requirements.txt`）。

## 配置

所有参数都在 **WebUI → 插件管理 → Pixiv 涩图** 里可视化配置，无需改代码：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `trigger_word` | string | `来份涩图` | 触发词（子串匹配），其后文字按标签解析 |
| `reply_text` | string | `给你涩图~` | 发图附带的一句话 |
| `r18` | int | `0` | 群聊 R18 等级：0=非 R18 / 1=仅 R18 / 2=混合 |
| `private_r18_enable` | bool | `false` | 私聊是否允许 R18（总开关） |
| `private_r18` | int | `0` | 私聊 R18 等级，仅在总开关开启时生效 |
| `exclude_ai` | bool | `true` | 排除 AI 生成图 |
| `tag_alias` | text | `""` | 中文→英文标签映射，逗号/换行分隔，格式 `猫娘=cat_girl` |
| `tag_match` | string | `and` | 多标签匹配：`and`=全部命中 / `or`=命中任一 |
| `size` | string | `original` | original/regular/small/thumb/mini |
| `random_pool` | int | `5` | 随机候选池大小（1-20） |
| `proxy` | string | `""` | 图片代理域名，如 `i.sukon.cc` |
| `show_pid` | bool | `true` | 回复末尾附带 PID 与 Pixiv 链接 |

## 说明与注意事项

- **图源**：Pixiv 没有公开的官方开放 API，本插件通过 Lolicon API（一个 Pixiv 图片索引服务）获取，返回的 `pid` 即 Pixiv 作品 ID，图片地址为 Pixiv 图床。
- **随机性**：Lolicon API 本身按投稿时间返回，故插件一次拉 `random_pool` 张候选再 `random.choice` 达到随机效果。
- **R18 默认关闭**：请遵守相关法律法规与所在平台规范，禁止传播非法内容。开启 R18 前请确认使用场景合规。
- **私聊 R18 独立控制**：群聊按 `r18`、私聊按 `private_r18_enable` + `private_r18` 分别出图。关闭 `private_r18_enable` 时，私聊**强制**只发非 R18，即使群聊设了 `r18=2` 也不会影响私聊——这是有意设计，避免"关掉私聊 R18"被群聊配置意外绕过。若私聊想跟群聊保持一致，把总开关打开、并把 `private_r18` 填成与 `r18` 相同即可。
- **图片直连**：Pixiv 原图地址在国内网络可能较慢或不通，遇到下载失败可配置 `proxy`（如 `i.sukon.cc`）走代理。
- 发送后的临时图片会立即删除，不会堆积。
