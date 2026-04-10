"""Top-level doctor: check environment, dependencies, and config."""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from boxagent.paths import default_local_dir
from boxagent.utils import safe_print as _safe_print


# ---------------------------------------------------------------------------
# Dependency checks + auto-fix
# ---------------------------------------------------------------------------

def _find_uv() -> str | None:
    """Find uv binary, checking PATH and common install locations."""
    found = shutil.which("uv") or shutil.which("uv.exe")
    if found:
        return found
    candidates = [
        Path.home() / ".local" / "bin" / "uv",
        Path.home() / ".local" / "bin" / "uv.exe",
        Path.home() / ".cargo" / "bin" / "uv",
    ]
    if sys.platform == "win32":
        userprofile = Path(os.environ.get("USERPROFILE", Path.home()))
        candidates.extend([
            userprofile / ".local" / "bin" / "uv.exe",
            userprofile / "AppData" / "Roaming" / "uv" / "uv.exe",
        ])
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def _check_uv() -> tuple[str | None, str]:
    path = _find_uv()
    return path, "uv"


def _check_node() -> tuple[str | None, str]:
    path = shutil.which("node") or shutil.which("node.exe")
    return path, "Node.js"


def _check_npx() -> tuple[str | None, str]:
    path = shutil.which("npx") or shutil.which("npx.exe")
    return path, "npx"


def _check_claude_cli() -> tuple[str | None, str]:
    path = shutil.which("claude") or shutil.which("claude.exe")
    return path, "Claude CLI"


def _check_codex_cli() -> tuple[str | None, str]:
    path = shutil.which("codex") or shutil.which("codex.exe")
    return path, "Codex CLI"


def _check_codex_acp() -> tuple[str | None, str]:
    path = shutil.which("codex-acp") or shutil.which("codex-acp.exe")
    return path, "Codex ACP"


def _check_xc_copilot_api() -> tuple[str | None, str]:
    path = shutil.which("xc-copilot-api") or shutil.which("xc-copilot-api.exe")
    return path, "xc-copilot-api"


def _install_uv() -> bool:
    _safe_print("  Installing uv...")
    if sys.platform == "win32":
        result = subprocess.run(
            ["powershell", "-ExecutionPolicy", "ByPass", "-c",
             "irm https://astral.sh/uv/install.ps1 | iex"],
            timeout=60,
        )
    else:
        result = subprocess.run(
            ["sh", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"],
            timeout=60,
        )
    return result.returncode == 0


def _install_claude_cli() -> bool:
    _safe_print("  Installing Claude CLI...")
    if sys.platform == "win32":
        npm = shutil.which("npm") or shutil.which("npm.exe")
        if not npm:
            _safe_print("  Cannot install Claude CLI: npm not found")
            return False
        result = subprocess.run(
            [npm, "install", "-g", "@anthropic-ai/claude-code"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            _safe_print(f"  Install failed: {result.stderr.strip()}")
        return result.returncode == 0
    else:
        result = subprocess.run(
            ["sh", "-c", "curl -fsSL https://claude.ai/install.sh | bash"],
            timeout=120,
        )
    return result.returncode == 0


def _install_npm_package(package: str, name: str) -> bool:
    npm = shutil.which("npm") or shutil.which("npm.exe")
    if not npm:
        _safe_print(f"  Cannot install {name}: npm not found")
        return False
    _safe_print(f"  Installing {name} ({package})...")
    result = subprocess.run(
        [npm, "install", "-g", package],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        _safe_print(f"  Install failed: {result.stderr.strip()}")
    return result.returncode == 0


def _update_npm_package(package: str, name: str) -> bool:
    npm = shutil.which("npm") or shutil.which("npm.exe")
    if not npm:
        return False
    _safe_print(f"  Updating {name} ({package})...")
    result = subprocess.run(
        [npm, "install", "-g", f"{package}@latest"],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        _safe_print(f"  Update failed: {result.stderr.strip()}")
    else:
        _safe_print(f"  {name} updated.")
    return result.returncode == 0


# (check_fn, install_fn, is_required, npm_package)
_DEPENDENCY_CHECKS = [
    (_check_uv, _install_uv, True, None),
    (_check_node, None, True, None),
    (_check_npx, None, False, None),
    (_check_claude_cli, _install_claude_cli, False, "@anthropic-ai/claude-code"),
    (_check_codex_cli, lambda: _install_npm_package("@openai/codex", "Codex CLI"), False, "@openai/codex"),
    (_check_codex_acp, lambda: _install_npm_package("@zed-industries/codex-acp", "Codex ACP"), False, "@zed-industries/codex-acp"),
    (_check_xc_copilot_api, lambda: _install_npm_package("xc-copilot-api", "xc-copilot-api"), False, "xc-copilot-api"),
]


def _refresh_path_win32() -> None:
    """Reload PATH from Windows registry so newly installed tools are visible."""
    try:
        import winreg
        parts: list[str] = []
        for root, sub in [
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            (winreg.HKEY_CURRENT_USER, r"Environment"),
        ]:
            try:
                with winreg.OpenKey(root, sub) as key:
                    val, _ = winreg.QueryValueEx(key, "Path")
                    parts.append(val)
            except OSError:
                pass
        if parts:
            os.environ["PATH"] = ";".join(parts)
    except Exception:
        pass


def check_dependencies(fix: bool = False) -> tuple[list[str], list[str]]:
    """Check all dependencies. Returns (ok_list, issues_list)."""
    ok: list[str] = []
    issues: list[str] = []

    for check_fn, install_fn, required, npm_package in _DEPENDENCY_CHECKS:
        path, name = check_fn()
        if path:
            if fix and npm_package:
                _update_npm_package(npm_package, name)
            ok.append(f"✅ {name}: {path}")
        else:
            level = "❌" if required else "⚠️ "
            if fix and install_fn:
                try:
                    success = install_fn()
                except (PermissionError, OSError) as exc:
                    success = False
                    _safe_print(f"  Install failed: {exc}")
                if sys.platform == "win32":
                    _refresh_path_win32()
                path, _ = check_fn()
                if path:
                    ok.append(f"✅ {name}: {path} (just installed)")
                    continue
                else:
                    issues.append(f"{level} {name}: install failed")
            elif fix and not install_fn and required:
                if name == "Node.js":
                    issues.append(f"{level} {name}: install manually from https://nodejs.org")
                else:
                    issues.append(f"{level} {name}: no auto-installer available")
            else:
                issues.append(f"{level} {name} not found")

    return ok, issues


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def _validate_config(ba_dir: Path, local_dir: Path) -> tuple[list[str], list[str]]:
    ok: list[str] = []
    issues: list[str] = []

    config_file = ba_dir / "config.yaml"
    if not config_file.is_file():
        return ok, issues

    try:
        from boxagent.config import load_config, ConfigError
        config = load_config(ba_dir, box_agent_dir=ba_dir, local_dir=local_dir)
        ok.append(f"✅ Config valid: {len(config.bots)} bot(s) defined")

        if config.node_id and config.bots:
            from boxagent.config import node_matches
            active_bots = [
                name for name, bot in config.bots.items()
                if node_matches(bot.enabled_on_nodes, config.node_id)
            ]
            if active_bots:
                ok.append(f"✅ Active bots for node '{config.node_id}': {', '.join(active_bots)}")
            else:
                issues.append(
                    f"⚠️  No bots enabled for node '{config.node_id}' "
                    f"(check enabled_on_nodes in config.yaml)"
                )
        elif config.node_id and not config.bots:
            ok.append(f"✅ Empty config for node '{config.node_id}' (no bots yet)")
    except Exception as e:
        issues.append(f"❌ Config error: {e}")

    return ok, issues


def _validate_schedules(ba_dir: Path) -> tuple[list[str], list[str]]:
    ok: list[str] = []
    issues: list[str] = []

    schedules_file = ba_dir / "schedules.yaml"
    if not schedules_file.is_file():
        return ok, issues

    try:
        from boxagent.scheduler import load_schedules
        schedules = load_schedules(schedules_file)
        if schedules:
            ok.append(f"✅ Schedules: {len(schedules)} task(s) valid")
    except Exception as e:
        issues.append(f"⚠️  Schedules error: {e}")

    return ok, issues


def _validate_skill_dirs(ba_dir: Path, local_dir: Path) -> tuple[list[str], list[str]]:
    ok: list[str] = []
    issues: list[str] = []

    config_file = ba_dir / "config.yaml"
    if not config_file.is_file():
        return ok, issues

    try:
        import yaml
        with open(config_file, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        all_dirs: set[str] = set()
        for bot_raw in (raw.get("bots") or {}).values():
            for d in bot_raw.get("extra_skill_dirs", []):
                p = Path(d).expanduser()
                if not p.is_absolute():
                    p = ba_dir / p
                all_dirs.add(str(p))

        missing = [d for d in all_dirs if not Path(d).is_dir()]
        if missing:
            for d in missing:
                issues.append(f"⚠️  extra_skill_dirs not found: {d}")
        elif all_dirs:
            ok.append(f"✅ Skill dirs: {len(all_dirs)} path(s) valid")
    except Exception:
        pass

    return ok, issues


# ---------------------------------------------------------------------------
# Top-level doctor
# ---------------------------------------------------------------------------

def run_doctor(ba_dir: Path, fix: bool = False) -> None:
    """Check environment: dependencies, config, workspace."""
    local_dir = default_local_dir(ba_dir)
    all_ok: list[str] = []
    all_issues: list[str] = []

    # --- Files ---
    config_file = ba_dir / "config.yaml"
    if config_file.is_file():
        all_ok.append(f"✅ Config: {config_file}")
    else:
        all_issues.append(f"❌ Config not found: {config_file}")

    bots_file = ba_dir / "telegram_bots.yaml"
    if bots_file.is_file():
        all_ok.append(f"✅ Telegram bots: {bots_file}")
    else:
        all_issues.append(f"❌ Telegram bots not found: {bots_file}")

    # --- local.yaml ---
    local_yaml = local_dir / "local.yaml"
    if local_yaml.is_file():
        import yaml
        with open(local_yaml, encoding="utf-8") as f:
            local_data = yaml.safe_load(f) or {}
        node_id = local_data.get("node_id", "")
        if node_id:
            all_ok.append(f"✅ Node ID: {node_id}")
        else:
            all_issues.append("⚠️  node_id not set in local.yaml")
    else:
        if fix:
            local_dir.mkdir(parents=True, exist_ok=True)
            node_id = platform.node().lower()
            local_yaml.write_text(f"node_id: {node_id}\n", encoding="utf-8")
            all_ok.append(f"✅ Node ID: {node_id} (just created)")
        else:
            all_issues.append(f"❌ local.yaml not found: {local_yaml}")

    # --- Config validation ---
    cfg_ok, cfg_issues = _validate_config(ba_dir, local_dir)
    all_ok.extend(cfg_ok)
    all_issues.extend(cfg_issues)

    # --- Schedules validation ---
    sched_ok, sched_issues = _validate_schedules(ba_dir)
    all_ok.extend(sched_ok)
    all_issues.extend(sched_issues)

    # --- Skill dirs ---
    skill_ok, skill_issues = _validate_skill_dirs(ba_dir, local_dir)
    all_ok.extend(skill_ok)
    all_issues.extend(skill_issues)

    # --- Dependencies ---
    dep_ok, dep_issues = check_dependencies(fix=fix)
    all_ok.extend(dep_ok)
    all_issues.extend(dep_issues)

    # --- Copilot token ---
    from boxagent.copilot_api import find_token_path
    token = find_token_path()
    if token:
        all_ok.append(f"✅ Copilot token: {token}")
    else:
        all_issues.append("⚠️  Copilot token not found (run 'xc-copilot-api auth')")

    # --- Workspace ---
    workspace = ba_dir / "workspace"
    if workspace.is_dir():
        all_ok.append(f"✅ Workspace: {workspace}")
    else:
        if fix:
            workspace.mkdir(parents=True, exist_ok=True)
            all_ok.append(f"✅ Workspace: {workspace} (just created)")
        else:
            all_issues.append(f"⚠️  Workspace not found: {workspace}")

    # --- Print ---
    _safe_print(f"BoxAgent Doctor ({ba_dir})")
    if fix:
        _safe_print("  (running with --fix)")
    _safe_print("=" * 60)
    for item in all_ok:
        _safe_print(item)
    for item in all_issues:
        _safe_print(item)
    _safe_print("")
    if all_issues:
        error_count = sum(1 for i in all_issues if i.startswith("❌"))
        warn_count = sum(1 for i in all_issues if i.startswith("⚠️"))
        _safe_print(f"Found {error_count} error(s), {warn_count} warning(s)")
        if not fix:
            _safe_print("Run with --fix to auto-install missing dependencies.")
    else:
        _safe_print("All checks passed! 🎉")
