# Workgroup 对 Discord 的依赖分析

## 现状

workgroup.py 和 config.py 中有大量 Discord 特定逻辑，与核心的 agent 编排逻辑深度耦合。

## 依赖清单

### config.py (WorkgroupConfig)

| 字段 | Discord 特有 | 用途 |
|------|-------------|------|
| `discord_bot_id` | 是 | 引用 discord_bots.yaml 的 bot 身份 |
| `discord_token` | 是 | 解析后的 bot token |
| `admin_discord_category` | 是 | admin 监听的 Discord category ID |

### config.py (SpecialistConfig)

| 字段 | Discord 特有 | 用途 |
|------|-------------|------|
| `discord_channel` | 是 | specialist 的 Discord 频道 ID（可选，用于可见性） |

### workgroup.py (WorkgroupManager)

| 字段/参数 | Discord 特有 | 用途 |
|-----------|-------------|------|
| `discord_channels` | 是 | bot_id → DiscordChannel 的映射 |

### workgroup.py 中的 Discord 操作

| 位置 | 操作 | 说明 |
|------|------|------|
| `start_workgroup` L163-165 | 查找 dc_channel | 按 discord_bot_id 查找 DiscordChannel |
| `start_workgroup` L227-236 | register_route | 将 admin 注册到 Discord category |
| `start_workgroup` L232 | _channels["discord"] | 设置 router 的 Discord 渠道 |
| `_create_specialist_agent` L142 | channel=dc_channel | 将 dc_channel 传给 Router |
| `_create_specialist_agent` L156-157 | _channels["discord"] | 设置 specialist router 的 Discord 渠道 |
| `send_to_specialist` L293-301 | 查找 sp_discord_channel | 判断是否有 Discord 可见性 |
| `send_to_specialist` L312-316 | send_via_webhook | 通过 webhook 在 specialist channel 发布任务 |
| `send_to_specialist` L337-347 | ensure_allowed_webhook + send | 在 admin channel 发短通知 |
| `create_specialist` L406-417 | create_text_channel | 动态创建 Discord 频道 |
| `delete_specialist` L471-481 | delete_text_channel | 删除 Discord 频道 |

## 耦合分析

### 核心逻辑（与 Discord 无关）
- admin/specialist 的 process 创建、pool 管理
- Router 创建和 dispatch_sync
- 任务分发（_run 里的 dispatch_sync）
- 结果回调给 admin router（IncomingMessage）
- 持久化（save/load/delete specialist yaml）
- heartbeat

### Discord 特有逻辑
- admin 注册到 Discord category（消息入口）
- specialist channel 的 webhook 任务发布（可见性）
- specialist channel 的 streaming 输出（可见性）
- 短通知 webhook（admin channel 通知）
- 动态创建/删除 Discord 频道
- dc_channel 传递（贯穿所有方法）

## 问题

1. **dc_channel 到处传** — 从 start_workgroup 到 _create_specialist_agent 到 send_to_specialist，每个方法都要处理"有没有 Discord"的分支
2. **可见性逻辑嵌在业务逻辑里** — send_to_specialist 既做"发任务"又做"Discord 可见性"又做"Discord 通知"
3. **无法单独替换渠道** — 要加 Telegram 或纯本地模式需要到处加 if/else
4. **IncomingMessage.channel 硬编码 "discord"** — callback 消息写死了 channel="discord"

## 建议重构方向

### 方案：抽出 ChannelAdapter 接口

```python
class WorkgroupChannelAdapter(Protocol):
    """可选的渠道适配器，提供消息入口和可见性。"""

    async def register_admin(self, router, config) -> None:
        """注册 admin 的消息入口（如 Discord category）"""

    async def setup_specialist(self, sp_name, router) -> None:
        """为 specialist 设置渠道（如创建 Discord channel）"""

    async def post_task(self, sp_name, text, admin_display) -> None:
        """在 specialist 频道发布任务（可见性）"""

    async def notify_admin(self, chat_id, text) -> None:
        """给 admin 发短通知"""

    async def cleanup_specialist(self, sp_name) -> None:
        """清理 specialist 渠道（如删除 Discord channel）"""

    def get_specialist_chat_id(self, sp_name) -> str:
        """返回 specialist 的 chat_id（用于 streaming）"""
```

然后实现：
- `DiscordWorkgroupAdapter` — 现有的 Discord 逻辑
- `LocalWorkgroupAdapter` — 纯本地模式，任务日志写 JSONL
- `TelegramWorkgroupAdapter` — 未来

WorkgroupManager 只持有一个 `adapter: WorkgroupChannelAdapter | None`，所有渠道操作通过 adapter 调用。没有 adapter 就是纯内部模式。

### 改动估计

| 文件 | 改动 |
|------|------|
| 新建 `workgroup_channel.py` | ~150 行（Protocol + DiscordAdapter） |
| `workgroup.py` | ~-80 行（删除散落的 Discord 代码，换成 adapter 调用） |
| `config.py` | 不变（Discord 字段留着，由 adapter 读取） |
| `gateway.py` | ~10 行（创建 adapter 并传入 WorkgroupManager） |

总计 ~150 行新增，~80 行删除，净增 ~70 行。不大。

### 改动后的 workgroup.py

```python
# 改动前
if sp_discord_channel and dc_channel:
    try:
        await dc_channel.send_via_webhook(sp_discord_channel, wg_display, text)
    except Exception as e:
        logger.warning(...)

# 改动后
if self.adapter:
    await self.adapter.post_task(target, text, wg_display)
```

所有 Discord 特定代码集中到 `DiscordWorkgroupAdapter`，workgroup.py 变成纯粹的编排逻辑。
