# Hermes Rocket.Chat Plugin v0.2 更新计划

> **日期:** 2026-07-23
> **基线:** 插件 v0.1.0（HEAD `36acf90`），Hermes Agent v0.19.0（upstream `8fc27820`），测试 120 passed
> **目标:** 对齐 Hermes 近期平台层更新（流式编辑、MEDIA 投递、能力标志、状态文本、内存安全、致命错误处理），把 Rocket.Chat 插件从"可用"提升到"体验与可靠性接近 Slack/Telegram 一等公民"。

---

## 1. Hermes 近期更新 vs 插件现状：差距清单

| # | Hermes 近期提交 / 能力 | 含义 | Rocket.Chat 插件现状 | 差距 |
|---|---|---|---|---|
| 1 | `4ab4894f4` fix(gateway): post-stream media delivery explicit-only | 流式回复后的媒体通过 `send_multiple_images` / `send_document` / `send_voice` / `send_animation` / `send_image_file` 分发给 adapter | 插件未覆写这些方法 → 走 base 兜底，只发一条 "⚠️ Couldn't deliver the file attachment" | **出站媒体实际不可用** |
| 2 | `run.py:19965/20021` 流式消费者 `adapter.edit_message(...)` | 支持 `edit_message` 的 adapter 获得"回复实时增长"的流式预览 | 未覆写 `edit_message`（网关探测到后用非流式路径），仅有单次"💭 Thinking…"占位 + 最终编辑 | 无流式回复预览 |
| 3 | `run.py:19517/19644` `supports_status_text` + `set_status_text()` | 支持者可在打字指示器上渲染实时工具状态（"is running pytest…"） | 未设置该标志；thinking 占位符固定文案 | 无实时工具进度展示 |
| 4 | `base.py` 能力标志体系 | `supports_code_blocks` / `splits_long_messages` / `typed_command_prefix` / `interactive_resume` 等 | 全部走默认值：代码块被当纯文本平台、长消息被硬截断 | 标志未对齐 |
| 5 | `533e54123` / `d42b29579` / `91693f9d4` Slack 缓存有界化（oldest-first 淘汰） | 防止每消息/每用户跟踪结构无限增长 | `_room_info`、`_typing_placeholders` 两个 dict 无上限 | 长期运行内存增长 |
| 6 | `2ab153218` fatal-error 交接、`54a0f0710` 未配置平台标记为不可重试 | adapter 通过 `_set_fatal_error(...)` 上报致命错误，网关退出而非空转重连 | 认证失败仅 `return False`，未上报 fatal/不可重试语义 | 401 后可能进入无意义重连循环 |
| 7 | `b41690753` feat(slack): `require_mention_channels` 按频道强制提及覆盖 | 按房间覆盖 mention 门控 | 只有全局 `ROCKETCHAT_MENTION_NAMES` | 无按房间覆盖 |
| 8 | `7f9cab15d` feat(slack): `ignore_other_user_mentions` | 大范围 @ 中提到 bot 时是否仍响应 | 当前只要被提及就响应 | 无同类开关 |
| 9 | `fc0009b9b` / `c8089dabc` 回复上下文回填（`reply_to_text` / `reply_to_author_*` / `reply_to_is_own_message`） | 线程回复带父消息上下文注入 | 插件只填 `reply_to_message_id` | 线程回复无上下文 |
| 10 | `c7b9dfa96` fix(slack): standalone cron delivery 解析 user→DM | cron/主动投递支持把用户解析成私聊房间 | `standalone_send` 只接受房间 ID | 无法向用户投递 |
| 11 | `f54e8706f` fix(platforms): 阻止图片上传重定向到私有 URL | 上传前的 SSRF/重定向守卫 | 出站 URL 图片路径未实现 | 随 #1 一并补齐 |
| 12 | `507d479c8` / `95aad9229` clarify 流规范化 + Slack Block Kit 按钮 | `send_clarify` 语义统一 | 走 base 默认编号文本回退（可用） | 按钮化可选增强 |

---

## 2. 版本目标

`plugin.yaml` version `0.1.0 → 0.2.0`。不改 Hermes 核心，全部在插件内完成。

### P0 — 核心体验（用户可见，优先）

#### P0.1 原生出站媒体投递（#1 + #11）

- 覆写 `send_image_file` / `send_document` / `send_video` / `send_voice` / `send_animation` / `send_multiple_images`，统一走 `rooms.media → rooms.mediaConfirm → chat.postMessage(file ref)`（复用现有 `_client_upload_attachment`，抽成共享 helper `_send_media(...)`）。
- 从 `standalone_send` 中抽出同一 helper，消除重复。
- `send_image(url)`：经受守卫的下载（复用 base `cache_image_from_url` / `_ssrf_redirect_guard` 语义，对齐 `f54e8706f`）再上传；拒绝重定向到私有 URL。
- 线程元数据（`tmid`）沿用到媒体消息。
- 测试：`tests/test_media_outbound.py` —— 每种 send_* 走 upload flow、线程参数传递、base 兜底不再触发、重定向守卫。

#### P0.2 流式回复预览（#2）

- 覆写 `edit_message(chat_id, message_id, content, *, finalize=False)` → `chat.update`（客户端已有 `update_message`）。
- 与现有 thinking 占位符整合：首条消息即预览消息，流式消费者后续以 `edit_message` 增长文本；保留占位符逻辑作为非流式回合的"thinking"状态。
- 中间编辑内容限制在 `ROCKETCHAT_MAX_MESSAGE_LENGTH` 内（chat.update 同样有长度限制）；`finalize` 语义按默认（无操作）即可，`REQUIRES_EDIT_FINALIZE` 保持 False。
- 测试：`tests/test_streaming.py` —— edit 调用链、占位符→预览的过渡、长度限制、finalize 幂等。

#### P0.3 实时工具进度状态文本（#3）

- 设置 `supports_status_text = True`。
- `send_typing` 创建占位符时读取 `self._status_text`（base 已维护），有则渲染为 "💭 正在运行 pytest…"；`set_status_text` 的刷新由网关驱动，adapter 侧在每次 typing refresh 时编辑占位符文本（防抖，避免刷屏 chat.update）。
- `stop_typing` 保持现状（不清除占位符，交给最终 send 编辑）。
- 测试：占位符文本随 status 更新、防抖、None 清除。

#### P0.4 能力标志对齐（#4）

- `supports_code_blocks = True`（Rocket.Chat 渲染 markdown 围栏代码块 → 工具进度可渲染 fenced block）。
- `splits_long_messages = True` + `send()` 原生分块：按段落边界把超长内容拆成多条消息（同房间同线程），替换现在的 4000 硬截断。对齐 `delivery.py _deliver_to_platform` 的 chunking-adapter 行为（完整输出直达 adapter，审计保存仍生效）。
- 分块首条走占位符编辑路径，后续块为新消息。
- 测试：`tests/test_capabilities.py` —— 标志值、分块边界、块内线程锚定、首块占位符消费。

### P1 — 可靠性与内存安全

#### P1.1 有界缓存（#5）

- `_room_info`、`_typing_placeholders` 改为 `OrderedDict` + 容量上限（如 200 / 500），oldest-first 淘汰，对齐 Slack 淘汰策略。
- 测试：超限淘汰、淘汰后重建正确。

#### P1.2 致命错误上报（#6）

- 认证失败（token 校验 401、重连后 re-auth 持续失败）→ `self._set_fatal_error("AUTH_FAILED", msg, retryable=False)`，`connect()` 返回 False。
- 缺失配置保持 `return False`（网关已按未配置平台不可重试处理）。
- 测试：`tests/test_fatal.py` —— 401 触发 fatal 语义、非重试标志、日志。

### P2 — 体验增强

#### P2.1 按房间提及覆盖（#7 + #8）

- `ROCKETCHAT_ALWAYS_RESPOND_ROOMS`：逗号分隔房间 ID，这些房间跳过提及门控（对齐 Slack `require_mention_channels` 的 force 语义，方向相反）。
- `ROCKETCHAT_IGNORE_OTHER_USER_MENTIONS`：多人 @ 中仅提到 bot 时（非直接 @）不响应。
- 测试：`tests/test_mentions.py` 扩展。

#### P2.2 回复上下文回填（#9）

- `event.tmid` 存在时经 `chat.getMessage` 拉父消息，填充 `reply_to_text` / `reply_to_author_id` / `reply_to_author_name` / `reply_to_is_own_message`（父消息作者为 bot 时置 True）。
- 按线程缓存父消息文本（有界，防刷 API）。
- 测试：`tests/test_reply_context.py` —— 回填字段、bot 自身消息识别、缓存命中。

#### P2.3 standalone 投递支持 user→DM（#10）

- `standalone_send`：当 `chat_id` 不是房间 ID 时，尝试按用户名/用户 ID 经 `dm.create`（或 `im.create`）解析/复用私聊房间再投递，对齐 Slack `c7b9dfa96`。
- 测试：`tests/test_standalone_sender.py` 扩展。

### 拉伸目标（不进 v0.2 主范围）

- **Rocket.Chat 原生打字指示器**：WebSocket 传输下经 `stream-notify-room` publish `typing` 事件（`ROCKETCHAT_NATIVE_TYPING=true` 可选）；polling 保持占位符方案。
- **clarify 按钮化**：Rocket.Chat UI blocks（messageKit）`actions` 按钮，版本相关（6.x+），须优雅降级到编号文本回退。

---

## 3. 里程碑

| 里程碑 | 内容 | 验收 |
|---|---|---|
| M1（P0） | P0.1–P0.4：媒体投递、流式编辑、状态文本、能力标志 | `pytest` 全绿；在真实 Rocket.Chat 上验证：MEDIA 指令传文件、长回复实时增长、工具状态可见、代码块正常渲染 |
| M2（P1） | P1.1–P1.2：有界缓存、致命错误 | 长跑内存稳定；token 失效后网关明确报错退出而非循环 |
| M3（P2） | P2.1–P2.3：按房间提及、回复上下文、user→DM | 行为测试覆盖；README/plugin.yaml 文档同步 |

每个里程碑结束更新 `README.md`（新配置项、Troubleshooting 表）与 `plugin.yaml`（新 `optional_env`），最终 `version: 0.2.0`。

---

## 4. 实现约束

- **TDD**：每个行为先写失败测试，沿用现有 `tests/conftest.py` 的假客户端/事件构造风格；不 mock Hermes 网关内部，用基类真实方法 + 注入假 `RocketChatClient`。
- 保持 Hermes 网关滚动兼容：所有新能力通过基类标志/方法表达，不依赖未公开内部。
- 不破坏现有 120 个测试（`pytest -q` 全绿为每次提交门槛）。
- 保持单 bot 身份范围；多账号留作未来版本。

---

## 5. 参考

- Hermes 安装路径：`~/.hermes/hermes-agent`（v0.19.0，`8fc27820`）
- `gateway/platforms/base.py`：`BasePlatformAdapter`（`edit_message` / `send_clarify` / `send_*` / `supports_status_text` / `_set_fatal_error`）
- `gateway/run.py`：`_deliver_media_from_response`（14941）、流式消费者 edit 路径（19965/20021）、`set_status_text` 调用（19517/19644）
- `gateway/delivery.py`：`_deliver_to_platform` chunking-adapter 分支
- 本仓库 v0.1 设计/实现文档：`docs/plans/2026-06-29-*.md`
