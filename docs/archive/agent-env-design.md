# AgentEnv + ChannelInfo 设计文档

## 问题

Agent 运行环境信息散落在 6+ 个地方：BotConfig, base_cli, IncomingMessage, Router, ChannelCallback, claude_process。导致变量重复传递、判断逻辑分散、新增 channel 类型时到处改。

## 设计

两个 frozen dataclass，per-message 生成：

### ChannelInfo — 消息来源的完整描述

```python
@dataclass(frozen=True)
class ChannelInfo:
    """Where this message came from."""
    
    platform: str                  # "telegram" / "discord" / "web"
    
    # Discord
    discord_channel_type: str = "" # "guild_text" / "guild_thread" / "dm"
    discord_guild_id: int = 0
    discord_category_id: int = 0
    discord_channel_id: int = 0
    discord_thread_id: int = 0
    
    # Telegram
    telegram_chat_type: str = ""   # "private" / "group" / "supergroup" / "channel"
    
    # Derived capabilities
    @property
    def is_dm(self) -> bool: ...
    @property
    def is_thread(self) -> bool: ...
    @property
    def is_group(self) -> bool: ...
    @property
    def supports_webhooks(self) -> bool: ...
    @property
    def supports_topic(self) -> bool: ...
    @property
    def supports_threads(self) -> bool: ...
    @property
    def supports_media_upload(self) -> bool: ...
    @property
    def reply_channel_id(self) -> str: ...
```

### AgentEnv — 完整的 agent 运行环境

```python
@dataclass(frozen=True)
class AgentEnv:
    """Per-message agent environment. Generated on each incoming message."""

    channel: ChannelInfo
    chat_id: str
    user_id: str
    via_workgroup: bool = False

    bot_name: str = ""
    display_name: str = ""
    node_id: str = ""
    workspace: str = ""

    telegram_token: str = ""

    workgroup_role: str = ""           # "" / "admin" / "specialist"
    workgroup_agents: tuple[str, ...] = ()
    running_tasks: tuple = ()

    ai_backend: str = "claude-cli"
    model: str = ""
    yolo: bool = False

    def mcp_server_names(self) -> list[str]: ...
    def callback_webhook_name(self) -> str: ...
    def heartbeat_display_mode(self) -> str: ...
    def build_context_prompt(self) -> str: ...
```

## 生成时机

**每条消息到达时生成**，不是 bot 启动时。这样 workgroup_agents、running_tasks 等动态数据自动是最新的。

### Discord
```python
# DiscordChannel._handle_incoming
channel_info = self._build_channel_info(message)
# 传入 IncomingMessage，或直接传给 Router
```

### Telegram
```python
# TelegramChannel handler
channel_info = ChannelInfo(platform="telegram", telegram_chat_type=message.chat.type)
```

### Router
```python
def _build_env(self, msg: IncomingMessage, channel_info: ChannelInfo) -> AgentEnv:
    return AgentEnv(
        channel=channel_info,
        chat_id=msg.chat_id,
        user_id=msg.user_id,
        via_workgroup=msg.via_workgroup,
        bot_name=self.bot_name,
        display_name=self.display_name,
        node_id=self.node_id,
        workspace=self.pool.get_workspace(msg.chat_id) if self.pool else self.workspace,
        telegram_token=getattr(self.cli_process, "bot_token", ""),
        workgroup_role="admin" if getattr(self.cli_process, "is_workgroup_admin", False) else "",
        workgroup_agents=tuple(self.workgroup_agents),
        running_tasks=tuple(self.get_running_tasks() if callable(self.get_running_tasks) else []),
        model=self.pool.get_model(msg.chat_id) if self.pool else getattr(self.cli_process, "model", ""),
        yolo=getattr(self.cli_process, "yolo", False),
    )
```

## 消费点

### 1. Context injection (context.py)
```python
# Before: 10+ individual params
build_session_context(bot_name=..., display_name=..., node_id=..., ...)

# After: single env
build_session_context(env)
```

### 2. MCP server selection (claude_process.py)
```python
# Before: if self.bot_token ... if self.is_workgroup_admin ...
# After:
for name in env.mcp_server_names():
    mcp_servers[name] = self._build_mcp_config(name, env)
```

### 3. Callback behavior (router_callback.py)
```python
# Before: webhook_name=self.bot_name if msg.via_workgroup else ""
# After:
callback = ChannelCallback(channel=ch, chat_id=env.chat_id,
                          webhook_name=env.callback_webhook_name())
```

### 4. Heartbeat display (heartbeat.py)
```python
# Before: if self.discord_channel and self.discord_chat_id ... try webhook ... fallback
# After:
mode = env.heartbeat_display_mode()  # "webhook" / "text" / "none"
```

## Migration path (分步实施)

### Phase 1: 定义 ChannelInfo + AgentEnv（新文件，不改现有代码）
- 创建 `src/boxagent/agent_env.py`
- 定义两个 dataclass + 所有 properties
- 写测试

### Phase 2: IncomingMessage 携带 ChannelInfo
- Discord/Telegram handler 生成 ChannelInfo 附在 IncomingMessage 上
- `IncomingMessage.channel_info: ChannelInfo | None = None`
- 向后兼容：`channel_info is None` 时行为不变

### Phase 3: Router 生成 AgentEnv
- Router._dispatch_one 开头生成 AgentEnv
- 先只在 context injection 消费（替换 build_session_context 的参数）
- 其他地方暂不动

### Phase 4: Process 消费 AgentEnv
- proc.send 接受 env 参数
- claude_process._build_args 用 env.mcp_server_names()
- 删除 base_cli 上的 bot_token, is_workgroup_admin

### Phase 5: Callback 消费 AgentEnv
- ChannelCallback 从 env 推导 webhook_name
- Heartbeat 从 env 推导 display mode

### Phase 6: 清理
- 删除散落的变量
- 更新文档和测试

## 不变的

- IncomingMessage 仍然存在（per-message 数据）
- SessionPool（管进程复用，不感知 env）
- Channel 对象（TelegramChannel, DiscordChannel — 通信层）
- BotConfig / WorkgroupConfig（配置层，AgentEnv 从它们生成但不替代它们）
