# Auth & API Keys

## TL;DR

Just make Claude / Codex run in your shell first.

### Claude

```bash
claude -p "reply with exactly OK"
```

### Codex

```bash
codex exec "reply with exactly OK"
```

If they already work in your shell, BoxAgent is much easier to set up.

## Provider Options

Both Claude CLI and Codex CLI need a backend API. Three common options:

### 1. Official API (direct)

- Claude: `ANTHROPIC_API_KEY` in environment
- Codex: OpenAI API key via `codex login --with-api-key`

### 2. xc-copilot-api (GitHub Copilot proxy)

Use GitHub Copilot credits via [xc-copilot-api](https://github.com/billxc/xc-copilot-api):

```bash
# Start the proxy
npx xc-copilot-api@latest start
# → Listening on http://localhost:4141/
```

Claude config (`~/.claude/settings.json`):

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:4141",
    "ANTHROPIC_AUTH_TOKEN": "dummy",
    "ANTHROPIC_MODEL": "claude-opus-4.6"
  }
}
```

Codex config (`~/.codex/config.toml`):

```toml
model = "gpt-5.4"
model_provider = "xc-copilot-api"

[model_providers.xc-copilot-api]
name = "xc-copilot-api"
base_url = "http://localhost:4141/v1"
wire_api = "responses"
```

### 3. LiteLLM (multi-provider proxy)

Route through LiteLLM to any provider:

Claude config (`~/.claude/settings.json`):

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:4000",
    "ANTHROPIC_AUTH_TOKEN": "dummy",
    "ANTHROPIC_MODEL": "claude-opus-4.6"
  }
}
```

Codex config (`~/.codex/config.toml`):

```toml
model = "gpt-5.4"
model_provider = "litellm"

[model_providers.litellm]
name = "LiteLLM"
base_url = "http://localhost:4000/v1"
wire_api = "responses"
```

## Docs

- Claude setup: [`docs/claude-setup.md`](./claude-setup.md)
- Codex setup: [`docs/codex-setup.md`](./codex-setup.md)
