# Codex Setup

先让 Codex CLI 自己在命令行里 work，再接 BoxAgent。

## Backend Options

BoxAgent supports two Codex backends:

| Backend | `ai_backend` | How it works | Dependencies |
|---------|-------------|--------------|-------------|
| **Codex CLI** | `codex-cli` | Spawns `codex exec --json` per turn | Only `codex` CLI |
| **Codex ACP** | `codex-acp` | Long-running ACP connection | `codex-acp` binary |

**codex-cli** is simpler — just install the Codex CLI and go. **codex-acp** gives richer tool call lifecycle events and native session persistence, but requires the `codex-acp` bridge.

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
    ai_backend: codex-cli    # 或 codex-acp
    model: gpt-5.4
```

## Notes

- `codex-cli` 后端每 turn 启动一个 `codex exec` 进程，通过 `thread_id` 维持 session 连续性
- `codex-acp` 后端维持长连接，tool call 事件更丰富
- BoxAgent 只是调用已经配好的 Codex CLI/ACP backend
