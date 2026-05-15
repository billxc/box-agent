# Codex Setup

先让 Codex CLI 自己在命令行里 work，再接 BoxAgent。

## Backend

BoxAgent 当前只支持一种 Codex 后端：`codex-cli`（每 turn spawn `codex exec --json`，靠 `thread_id` 保持 session 连续）。

> 历史上还有 `codex-acp`（ACP 长连接），已于 2026-04-23 删除（commit `01d2558`）。旧 config 写 `ai_backend: codex-acp` 会被 `config.py` 拒绝并报 ConfigError，提示改为 `codex-cli`。

## Install

```bash
npm install -g @openai/codex
codex --version
```

## Verify

```bash
codex exec "reply with exactly OK"
```

## Config

### Option A: xc-copilot-api (推荐)

走 GitHub Copilot 额度：

```bash
# 一次性认证
npx xc-copilot-api@latest auth

# 推荐：用 easy-service 注册为后台服务
easy-service install copilot-api -- npx xc-copilot-api@latest start
easy-service start copilot-api

# 或手动运行
npx xc-copilot-api@latest start
```

`~/.codex/config.toml`:

```toml
model = "gpt-5.4"
model_provider = "xc-copilot-api"

[model_providers.xc-copilot-api]
name = "xc-copilot-api"
base_url = "http://localhost:4141/v1"
wire_api = "responses"
```

### Option B: LiteLLM proxy

```toml
model = "gpt-5.4"
model_provider = "litellm"

[model_providers.litellm]
name = "LiteLLM"
base_url = "http://localhost:4000/v1"
wire_api = "responses"
```

### Option C: OpenAI 官方 API

```bash
codex login --with-api-key
```

直接用 OpenAI API key，不需要额外 proxy 配置。

## BoxAgent Config

在 `~/.boxagent/config.yaml` 里选 backend：

```yaml
bots:
  my-bot:
    ai_backend: codex-cli
    model: gpt-5.4
```

## Notes

- `codex-cli` 后端每 turn 启动一个 `codex exec` 进程，通过 `thread_id` 维持 session 连续性
- BoxAgent 只是调用已经配好的 Codex CLI
