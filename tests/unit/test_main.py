"""Tests for CLI entry point path resolution."""

import sys
from unittest.mock import AsyncMock, MagicMock, patch


class TestMain:
    def test_ba_dir_sets_default_config_and_local_dirs(
        self, monkeypatch, tmp_path
    ):
        from boxagent import main as main_mod

        custom_box_agent_dir = tmp_path / "ba-dir"
        config = MagicMock()
        config.log_level = "info"
        monkeypatch.setattr(
            sys,
            "argv",
            ["boxagent", "--ba-dir", str(custom_box_agent_dir)],
        )

        with patch("boxagent.main.load_config", return_value=config) as load, \
             patch("boxagent.main._run", new_callable=AsyncMock) as run:
            main_mod.main()

        load.assert_called_once_with(
            custom_box_agent_dir,
            box_agent_dir=custom_box_agent_dir,
            local_dir=custom_box_agent_dir / "local",
        )
        run.assert_awaited_once_with(
            config,
            custom_box_agent_dir,
            custom_box_agent_dir / "local",
        )

    def test_config_overrides_ba_dir_default_config_dir(
        self, monkeypatch, tmp_path
    ):
        from boxagent import main as main_mod

        custom_box_agent_dir = tmp_path / "ba-dir"
        custom_config = tmp_path / "custom-config"
        config = MagicMock()
        config.log_level = "info"
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "boxagent",
                "--box-agent-dir", str(custom_box_agent_dir),
                "--config", str(custom_config),
            ],
        )

        with patch("boxagent.main.load_config", return_value=config) as load, \
             patch("boxagent.main._run", new_callable=AsyncMock) as run:
            main_mod.main()

        load.assert_called_once_with(
            custom_config,
            box_agent_dir=custom_box_agent_dir,
            local_dir=custom_box_agent_dir / "local",
        )
        run.assert_awaited_once_with(
            config,
            custom_config,
            custom_box_agent_dir / "local",
        )
