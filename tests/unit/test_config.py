"""Unit tests for config loading and validation."""

import os
from textwrap import dedent
from unittest.mock import patch

import pytest

from boxagent.config import load_config, ConfigError, node_matches


@pytest.fixture
def config_dir(tmp_path):
    """Create a temp config dir with a valid config.yaml."""
    config = dedent("""\
        global:
          log_level: info

        bots:
          test-bot:
            ai_backend: claude-cli
            workspace: /tmp/test
            channels:
              telegram:
                token: "123:ABC"
                allowed_users: [111222]
            display:
              tool_calls: summary
              streaming: true
    """)
    (tmp_path / "config.yaml").write_text(config)
    return tmp_path


class TestLoadConfig:
    def test_valid_config_parses(self, config_dir):
        config = load_config(config_dir)
        assert config.node_id == ""
        assert config.log_level == "info"
        assert "test-bot" in config.bots
        bot = config.bots["test-bot"]
        assert bot.workspace == "/tmp/test"
        assert bot.telegram_token == "123:ABC"
        assert bot.allowed_users == [111222]
        assert bot.display_tool_calls == "summary"

    def test_telegram_block_without_token_loads_web_only(self, tmp_path):
        """A telegram block without token/bot_id is now valid — bot runs on web only."""
        config = dedent("""\
            global: {}
            bots:
              web-only-bot:
                ai_backend: claude-cli
                workspace: /tmp
                channels:
                  telegram:
                    allowed_users: [111]
        """)
        (tmp_path / "config.yaml").write_text(config)
        config = load_config(tmp_path)
        bot = config.bots["web-only-bot"]
        assert bot.telegram_token == ""
        assert bot.web_enabled is True

    def test_env_override_workspace(self, config_dir):
        with patch.dict(
            os.environ, {"BOXAGENT_TEST_BOT_workspace": "/override"}
        ):
            config = load_config(config_dir)
        assert config.bots["test-bot"].workspace == "/override"

    def test_env_override_log_level(self, config_dir):
        with patch.dict(
            os.environ, {"BOXAGENT_GLOBAL_LOG_LEVEL": "debug"}
        ):
            config = load_config(config_dir)
        assert config.log_level == "debug"

    def test_unknown_env_ignored(self, config_dir):
        with patch.dict(
            os.environ, {"BOXAGENT_UNKNOWN_THING": "whatever"}
        ):
            config = load_config(config_dir)
        assert config.node_id == ""

    def test_display_defaults(self, tmp_path):
        config = dedent("""\
            global: {}
            bots:
              simple-bot:
                ai_backend: claude-cli
                workspace: /tmp
                channels:
                  telegram:
                    token: "123:ABC"
                    allowed_users: [111]
        """)
        (tmp_path / "config.yaml").write_text(config)
        config = load_config(tmp_path)
        assert config.bots["simple-bot"].display_tool_calls == "summary"

    def test_box_agent_dir_changes_default_workspace(self, tmp_path):
        config = dedent("""\
            global: {}
            bots:
              simple-bot:
                ai_backend: claude-cli
                channels:
                  telegram:
                    token: "123:ABC"
                    allowed_users: [111]
        """)
        (tmp_path / "config.yaml").write_text(config)

        with patch.dict(
            os.environ, {"BOX_AGENT_DIR": "/tmp/ba-test-dir"}
        ):
            config = load_config(tmp_path)

        assert config.bots["simple-bot"].workspace == "/tmp/ba-test-dir/workspace"

    def test_loads_telegram_bots_mapping_into_app_config(self, tmp_path):
        (tmp_path / "telegram_bots.yaml").write_text("""\
            bots:
              - id: my_test_bot
                token: "123:ABC"
        """)
        (tmp_path / "config.yaml").write_text(dedent("""\
            global: {}
            bots:
              demo:
                ai_backend: claude-cli
                channels:
                  telegram:
                    bot_id: my_test_bot
                    allowed_users: [111]
        """))

        config = load_config(tmp_path)

        assert config.telegram_bots["my_test_bot"] == "123:ABC"
        assert config.telegram_bots["123"] == "123:ABC"

    def test_relative_extra_skill_dirs_resolve_from_config_dir(self, tmp_path):
        config = dedent("""\
            global: {}
            bots:
              skill-bot:
                ai_backend: claude-cli
                channels:
                  telegram:
                    token: "123:ABC"
                    allowed_users: [111]
                extra_skill_dirs:
                  - ./my-notes/myproject/skills
                  - ./my-notes/myproject/extra-skills
        """)
        (tmp_path / "config.yaml").write_text(config)

        config = load_config(tmp_path)

        assert config.bots["skill-bot"].extra_skill_dirs == [
            str(tmp_path / "my-notes" / "myproject" / "skills"),
            str(tmp_path / "my-notes" / "myproject" / "extra-skills"),
        ]

    def test_explicit_box_agent_dir_changes_default_workspace(self, tmp_path):
        config = dedent("""\
            global: {}
            bots:
              simple-bot:
                ai_backend: claude-cli
                channels:
                  telegram:
                    token: "123:ABC"
                    allowed_users: [111]
        """)
        (tmp_path / "config.yaml").write_text(config)

        config = load_config(tmp_path, box_agent_dir="/tmp/ba-cli-dir")

        assert config.bots["simple-bot"].workspace == "/tmp/ba-cli-dir/workspace"

    def test_api_port_defaults_to_zero(self, config_dir):
        config = load_config(config_dir)
        assert config.api_port == 0

    def test_api_port_from_config(self, tmp_path):
        config = dedent("""\
            global:
              node_id: test
              api_port: 9800
            bots:
              bot1:
                ai_backend: claude-cli
                workspace: /tmp
                channels:
                  telegram:
                    token: "123:ABC"
                    allowed_users: [111]
        """)
        (tmp_path / "config.yaml").write_text(config)
        config = load_config(tmp_path)
        assert config.api_port == 9800

    def test_codex_acp_backend_is_rejected(self, tmp_path):
        from boxagent.config import ConfigError
        config = dedent("""\
            global: {}
            bots:
              acp-bot:
                ai_backend: codex-acp
                workspace: /tmp/acp
                channels:
                  telegram:
                    token: "123:ABC"
                    allowed_users: [111]
        """)
        (tmp_path / "config.yaml").write_text(config)
        with pytest.raises(ConfigError, match="codex-acp"):
            load_config(tmp_path)

    def test_codex_mcp_backend_is_rejected_with_deprecation_message(self, tmp_path):
        config = dedent("""\
            global: {}
            bots:
              old-bot:
                ai_backend: codex-mcp
                workspace: /tmp/codex
                channels:
                  telegram:
                    token: "123:ABC"
                    allowed_users: [111]
        """)
        (tmp_path / "config.yaml").write_text(config)
        with pytest.raises(ConfigError, match="codex-mcp"):
            load_config(tmp_path)


class TestTelegramBotsYaml:
    """Tests for telegram_bots.yaml bot_id resolution."""

    def test_bot_id_resolves_from_telegram_bots_yaml(self, tmp_path):
        config = dedent("""\
            global: {}
            bots:
              my-bot:
                ai_backend: claude-cli
                workspace: /tmp
                channels:
                  telegram:
                    bot_id: "123"
                    allowed_users: [111]
        """)
        bots_yaml = dedent("""\
            "123": "123:ABC_TOKEN"
            "456": "456:DEF_TOKEN"
        """)
        (tmp_path / "config.yaml").write_text(config)
        (tmp_path / "telegram_bots.yaml").write_text(bots_yaml)
        config = load_config(tmp_path)
        assert config.bots["my-bot"].telegram_token == "123:ABC_TOKEN"

    def test_token_takes_priority_over_bot_id(self, tmp_path):
        config = dedent("""\
            global: {}
            bots:
              my-bot:
                ai_backend: claude-cli
                workspace: /tmp
                channels:
                  telegram:
                    token: "999:DIRECT_TOKEN"
                    bot_id: "123"
                    allowed_users: [111]
        """)
        bots_yaml = dedent("""\
            "123": "123:FROM_YAML"
        """)
        (tmp_path / "config.yaml").write_text(config)
        (tmp_path / "telegram_bots.yaml").write_text(bots_yaml)
        config = load_config(tmp_path)
        assert config.bots["my-bot"].telegram_token == "999:DIRECT_TOKEN"

    def test_bot_id_not_found_raises_error(self, tmp_path):
        config = dedent("""\
            global: {}
            bots:
              my-bot:
                ai_backend: claude-cli
                workspace: /tmp
                channels:
                  telegram:
                    bot_id: "999"
                    allowed_users: [111]
        """)
        bots_yaml = dedent("""\
            "123": "123:ABC"
        """)
        (tmp_path / "config.yaml").write_text(config)
        (tmp_path / "telegram_bots.yaml").write_text(bots_yaml)
        with pytest.raises(ConfigError, match="not found in telegram_bots.yaml"):
            load_config(tmp_path)

    def test_bot_id_without_yaml_file_raises_error(self, tmp_path):
        config = dedent("""\
            global: {}
            bots:
              my-bot:
                ai_backend: claude-cli
                workspace: /tmp
                channels:
                  telegram:
                    bot_id: "123"
                    allowed_users: [111]
        """)
        (tmp_path / "config.yaml").write_text(config)
        # No telegram_bots.yaml
        with pytest.raises(ConfigError, match="telegram_bots.yaml not found"):
            load_config(tmp_path)

    def test_no_telegram_bots_yaml_works_with_token(self, tmp_path):
        config = dedent("""\
            global: {}
            bots:
              my-bot:
                ai_backend: claude-cli
                workspace: /tmp
                channels:
                  telegram:
                    token: "123:DIRECT"
                    allowed_users: [111]
        """)
        (tmp_path / "config.yaml").write_text(config)
        # No telegram_bots.yaml — should still work
        config = load_config(tmp_path)
        assert config.bots["my-bot"].telegram_token == "123:DIRECT"

    def test_missing_both_token_and_bot_id_loads_web_only(self, tmp_path):
        """telegram block with neither token nor bot_id is not an error;
        the bot just runs on the web channel."""
        config = dedent("""\
            global: {}
            bots:
              my-bot:
                ai_backend: claude-cli
                workspace: /tmp
                channels:
                  telegram:
                    allowed_users: [111]
        """)
        (tmp_path / "config.yaml").write_text(config)
        config = load_config(tmp_path)
        assert config.bots["my-bot"].telegram_token == ""
        assert config.bots["my-bot"].web_enabled is True

    def test_numeric_bot_id_also_works(self, tmp_path):
        """bot_id as int in YAML should still resolve."""
        config = dedent("""\
            global: {}
            bots:
              my-bot:
                ai_backend: claude-cli
                workspace: /tmp
                channels:
                  telegram:
                    bot_id: 123
                    allowed_users: [111]
        """)
        bots_yaml = dedent("""\
            123: "123:NUM_TOKEN"
        """)
        (tmp_path / "config.yaml").write_text(config)
        (tmp_path / "telegram_bots.yaml").write_text(bots_yaml)
        config = load_config(tmp_path)
        assert config.bots["my-bot"].telegram_token == "123:NUM_TOKEN"


class TestNodeMatches:
    """Tests for node_matches helper."""

    def test_empty_matches_everything(self):
        assert node_matches("", "any-node") is True

    def test_empty_list_matches_everything(self):
        assert node_matches([], "any-node") is True

    def test_string_match(self):
        assert node_matches("cloud-pc", "cloud-pc") is True

    def test_string_no_match(self):
        assert node_matches("cloud-pc", "home-server") is False

    def test_list_match(self):
        assert node_matches(["cloud-pc", "home-server"], "cloud-pc") is True

    def test_list_no_match(self):
        assert node_matches(["cloud-pc", "home-server"], "office") is False

    def test_empty_node_id_no_match_when_filter_set(self):
        assert node_matches("cloud-pc", "") is False

    def test_empty_node_id_matches_when_no_filter(self):
        assert node_matches("", "") is True


class TestNodeId:
    """Tests for node_id loading from local.yaml."""

    def test_node_id_from_local_yaml(self, tmp_path):
        config = dedent("""\
            global: {}
            bots:
              my-bot:
                ai_backend: claude-cli
                workspace: /tmp
                channels:
                  telegram:
                    token: "123:ABC"
                    allowed_users: [111]
        """)
        (tmp_path / "config.yaml").write_text(config)
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        (local_dir / "local.yaml").write_text("node_id: cloud-pc\n")
        config = load_config(tmp_path, local_dir=local_dir)
        assert config.node_id == "cloud-pc"

    def test_node_id_auto_generated_when_local_yaml_missing(self, tmp_path):
        """When local_dir is provided but local.yaml has no node_id, generate one
        and persist it so subsequent loads return the same id."""
        import yaml as _yaml

        config = dedent("""\
            global: {}
            bots:
              my-bot:
                ai_backend: claude-cli
                workspace: /tmp
                channels:
                  telegram:
                    token: "123:ABC"
                    allowed_users: [111]
        """)
        (tmp_path / "config.yaml").write_text(config)
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        loaded = load_config(tmp_path, local_dir=local_dir)
        assert loaded.node_id  # non-empty
        # Persisted to local.yaml
        local_file = local_dir / "local.yaml"
        assert local_file.is_file()
        persisted = _yaml.safe_load(local_file.read_text())
        assert persisted["node_id"] == loaded.node_id
        # Stable across reloads
        reloaded = load_config(tmp_path, local_dir=local_dir)
        assert reloaded.node_id == loaded.node_id

    def test_node_id_auto_generation_preserves_existing_local_keys(self, tmp_path):
        """Auto-generation must merge into existing local.yaml, not overwrite it."""
        import yaml as _yaml

        config = dedent("""\
            global: {}
            bots:
              my-bot:
                ai_backend: claude-cli
                workspace: /tmp
                channels:
                  telegram:
                    token: "123:ABC"
                    allowed_users: [111]
        """)
        (tmp_path / "config.yaml").write_text(config)
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        (local_dir / "local.yaml").write_text(
            "global:\n  log_level: debug\n"
        )
        loaded = load_config(tmp_path, local_dir=local_dir)
        assert loaded.node_id
        persisted = _yaml.safe_load((local_dir / "local.yaml").read_text())
        assert persisted["node_id"] == loaded.node_id
        assert persisted["global"]["log_level"] == "debug"

    def test_node_id_empty_when_no_local_dir(self, tmp_path):
        config = dedent("""\
            global: {}
            bots:
              my-bot:
                ai_backend: claude-cli
                workspace: /tmp
                channels:
                  telegram:
                    token: "123:ABC"
                    allowed_users: [111]
        """)
        (tmp_path / "config.yaml").write_text(config)
        config = load_config(tmp_path)
        assert config.node_id == ""

    def test_node_id_fallback_from_global_config(self, tmp_path):
        """Compat: global.node_id in config.yaml is used when local.yaml has none."""
        config = dedent("""\
            global:
              node_id: legacy-node
            bots:
              my-bot:
                ai_backend: claude-cli
                workspace: /tmp
                channels:
                  telegram:
                    token: "123:ABC"
                    allowed_users: [111]
        """)
        (tmp_path / "config.yaml").write_text(config)
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        config = load_config(tmp_path, local_dir=local_dir)
        assert config.node_id == "legacy-node"

    def test_local_yaml_takes_priority_over_global(self, tmp_path):
        """local.yaml node_id wins over deprecated global.node_id."""
        config = dedent("""\
            global:
              node_id: legacy-node
            bots:
              my-bot:
                ai_backend: claude-cli
                workspace: /tmp
                channels:
                  telegram:
                    token: "123:ABC"
                    allowed_users: [111]
        """)
        (tmp_path / "config.yaml").write_text(config)
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        (local_dir / "local.yaml").write_text("node_id: new-node\n")
        config = load_config(tmp_path, local_dir=local_dir)
        assert config.node_id == "new-node"

    def test_node_id_auto_generated_when_local_yaml_empty(self, tmp_path):
        config = dedent("""\
            global: {}
            bots:
              my-bot:
                ai_backend: claude-cli
                workspace: /tmp
                channels:
                  telegram:
                    token: "123:ABC"
                    allowed_users: [111]
        """)
        (tmp_path / "config.yaml").write_text(config)
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        (local_dir / "local.yaml").write_text("")
        config = load_config(tmp_path, local_dir=local_dir)
        assert config.node_id

    def test_local_yaml_overrides_global_log_level(self, tmp_path):
        """local.yaml global section overrides config.yaml global."""
        config = dedent("""\
            global:
              log_level: info
            bots:
              my-bot:
                ai_backend: claude-cli
                workspace: /tmp
                channels:
                  telegram:
                    token: "123:ABC"
                    allowed_users: [111]
        """)
        (tmp_path / "config.yaml").write_text(config)
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        (local_dir / "local.yaml").write_text(
            "node_id: test-node\nglobal:\n  log_level: debug\n"
        )
        config = load_config(tmp_path, local_dir=local_dir)
        assert config.node_id == "test-node"
        assert config.log_level == "debug"

    def test_local_yaml_partial_global_override(self, tmp_path):
        """local.yaml can override only some global fields."""
        config = dedent("""\
            global:
              log_level: info
              api_port: 8080
            bots:
              my-bot:
                ai_backend: claude-cli
                workspace: /tmp
                channels:
                  telegram:
                    token: "123:ABC"
                    allowed_users: [111]
        """)
        (tmp_path / "config.yaml").write_text(config)
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        (local_dir / "local.yaml").write_text(
            "node_id: test-node\nglobal:\n  api_port: 9090\n"
        )
        config = load_config(tmp_path, local_dir=local_dir)
        assert config.log_level == "info"  # not overridden
        assert config.api_port == 9090     # overridden


class TestEnabledOnNode:
    """Tests for BotConfig.enabled_on_nodes parsing."""

    def test_string_value(self, tmp_path):
        config = dedent("""\
            global: {}
            bots:
              my-bot:
                ai_backend: claude-cli
                workspace: /tmp
                enabled_on_nodes: "cloud-pc"
                channels:
                  telegram:
                    token: "123:ABC"
                    allowed_users: [111]
        """)
        (tmp_path / "config.yaml").write_text(config)
        config = load_config(tmp_path)
        assert config.bots["my-bot"].enabled_on_nodes == "cloud-pc"

    def test_list_value(self, tmp_path):
        config = dedent("""\
            global: {}
            bots:
              my-bot:
                ai_backend: claude-cli
                workspace: /tmp
                enabled_on_nodes:
                  - cloud-pc
                  - home-server
                channels:
                  telegram:
                    token: "123:ABC"
                    allowed_users: [111]
        """)
        (tmp_path / "config.yaml").write_text(config)
        config = load_config(tmp_path)
        assert config.bots["my-bot"].enabled_on_nodes == ["cloud-pc", "home-server"]

    def test_default_empty(self, config_dir):
        config = load_config(config_dir)
        assert config.bots["test-bot"].enabled_on_nodes == ""


class TestNodeOverrides:
    """Tests for node-specific overrides from config.yaml."""

    def test_node_override_applies_global_and_bot_fields(self, tmp_path):
        config = dedent("""\
            global:
              log_level: info
              api_port: 9800
            node_overrides:
              cloud-pc:
                global:
                  log_level: debug
                bots:
                  my-bot:
                    workspace: /tmp/override
                    model: gpt-5
            bots:
              my-bot:
                ai_backend: claude-cli
                workspace: /tmp/base
                channels:
                  telegram:
                    token: "123:ABC"
                    allowed_users: [111]
        """)
        (tmp_path / "config.yaml").write_text(config)

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        (local_dir / "local.yaml").write_text("node_id: cloud-pc\n")

        config = load_config(tmp_path, local_dir=local_dir)

        assert config.log_level == "debug"
        assert config.api_port == 9800
        assert config.bots["my-bot"].workspace == "/tmp/override"
        assert config.bots["my-bot"].model == "gpt-5"

    def test_node_override_not_applied_when_node_unmatched(self, tmp_path):
        config = dedent("""\
            global:
              log_level: info
            node_overrides:
              cloud-pc:
                global:
                  log_level: debug
                bots:
                  my-bot:
                    workspace: /tmp/override
            bots:
              my-bot:
                ai_backend: claude-cli
                workspace: /tmp/base
                channels:
                  telegram:
                    token: "123:ABC"
                    allowed_users: [111]
        """)
        (tmp_path / "config.yaml").write_text(config)

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        (local_dir / "local.yaml").write_text("node_id: home-server\n")

        config = load_config(tmp_path, local_dir=local_dir)

        assert config.log_level == "info"
        assert config.bots["my-bot"].workspace == "/tmp/base"

    def test_invalid_node_overrides_shape_raises(self, tmp_path):
        config = dedent("""\
            global: {}
            node_overrides:
              - cloud-pc
            bots:
              my-bot:
                ai_backend: claude-cli
                workspace: /tmp/base
                channels:
                  telegram:
                    token: "123:ABC"
                    allowed_users: [111]
        """)
        (tmp_path / "config.yaml").write_text(config)

        with pytest.raises(ConfigError, match="node_overrides must be a mapping"):
            load_config(tmp_path)
