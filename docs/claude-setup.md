# Claude Setup

先让 Claude CLI 自己在命令行里 work，再接 BoxAgent。

## Verify

```bash
claude -p "reply with exactly OK"
```

## Config

### Option A: xc-copilot-api (推荐)

走 GitHub Copilot 额度，免费：

```bash
# 一次性认证
npx xc-copilot-api@latest auth

# 推荐：用 easy-service 注册为后台服务
easy-service install copilot-api -- npx xc-copilot-api@latest start
easy-service start copilot-api

# 或手动运行
npx xc-copilot-api@latest start
```

> **Note:** BoxAgent 的 `global.copilot_api: true` 配置项已废弃，会覆盖 Claude CLI 的用户配置导致问题。请按上面方式独立运行 xc-copilot-api，然后在下面配置 Claude CLI 直接连接。

`~/.claude/settings.json`:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:4141",
    "ANTHROPIC_AUTH_TOKEN": "dummy",
    "ANTHROPIC_MODEL": "claude-opus-4.6"
  }
}
```

### Option B: LiteLLM proxy

走 LiteLLM 转发到第三方 key：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:4000",
    "ANTHROPIC_AUTH_TOKEN": "dummy",
    "ANTHROPIC_MODEL": "claude-opus-4.6"
  }
}
```

### Option C: Anthropic 官方 API

直接用 Anthropic API key，`claude login` 登录即可，不需要额外配置。

## Notes

- BoxAgent 只是调用已经配好的 Claude CLI backend
- `ANTHROPIC_AUTH_TOKEN` 设 `"dummy"` 是因为走 proxy 不需要真 key
- 可用模型取决于你的 proxy 配置
