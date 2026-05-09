"""Top-level doctor: check environment, dependencies, and config."""

import os
import platform
import shutil
import subprocess
import sys
from functools import partial
from pathlib import Path

from boxagent.utils import default_local_dir
from boxagent.utils import safe_print as _safe_print


# ---------------------------------------------------------------------------
# Dependency checks + auto-fix
# ---------------------------------------------------------------------------

def _which(cmd: str) -> str | None:
    return shutil.which(cmd) or shutil.which(f"{cmd}.exe")


def _uv_extra_paths() -> list[Path]:
    paths = [
        Path.home() / ".local" / "bin" / "uv",
        Path.home() / ".local" / "bin" / "uv.exe",
        Path.home() / ".cargo" / "bin" / "uv",
    ]
    if sys.platform == "win32":
        userprofile = Path(os.environ.get("USERPROFILE", Path.home()))
        paths.extend([
            userprofile / ".local" / "bin" / "uv.exe",
            userprofile / "AppData" / "Roaming" / "uv" / "uv.exe",
        ])
    return paths


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
        return _npm_install("@anthropic-ai/claude-code", "Claude CLI", update=False)
    result = subprocess.run(
        ["sh", "-c", "curl -fsSL https://claude.ai/install.sh | bash"],
        timeout=120,
    )
    return result.returncode == 0


def _npm_install(package: str, name: str, *, update: bool) -> bool:
    """Install or update an npm-distributed CLI globally.

    update=True installs ``<package>@latest``; update=False installs ``<package>``.
    """
    npm = _which("npm")
    if not npm:
        if not update:
            _safe_print(f"  Cannot install {name}: npm not found")
        return False
    verb = "Updating" if update else "Installing"
    spec = f"{package}@latest" if update else package
    _safe_print(f"  {verb} {name} ({package})...")
    result = subprocess.run(
        [npm, "install", "-g", spec],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        _safe_print(f"  {'Update' if update else 'Install'} failed: {result.stderr.strip()}")
    elif update:
        _safe_print(f"  {name} updated.")
    return result.returncode == 0


# Each row: (cmd, display_name, install_fn_or_None, required, npm_package_or_None, extra_paths_fn)
_DEPENDENCY_CHECKS = [
    ("uv",              "uv",              _install_uv,         True,  None,                          _uv_extra_paths),
    ("node",            "Node.js",         None,                True,  None,                          None),
    ("npx",             "npx",             None,                False, None,                          None),
    ("claude",          "Claude CLI",      _install_claude_cli, False, "@anthropic-ai/claude-code",   None),
    ("codex",           "Codex CLI",       None,                False, "@openai/codex",               None),
    ("xc-copilot-api",  "xc-copilot-api",  None,                False, "xc-copilot-api",              None),
]


def _resolve(cmd: str, extra_paths_fn) -> str | None:
    found = _which(cmd)
    if found:
        return found
    if extra_paths_fn is None:
        return None
    for c in extra_paths_fn():
        if c.is_file():
            return str(c)
    return None


def _refresh_path_win32() -> None:
    """Reload PATH from Windows registry so newly installed tools are visible."""
    try:
        import winreg  # type: ignore[import-not-found]  # Windows-only stdlib
        parts: list[str] = []
        for root, sub in [
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),  # type: ignore[attr-defined]
            (winreg.HKEY_CURRENT_USER, r"Environment"),  # type: ignore[attr-defined]
        ]:
            try:
                with winreg.OpenKey(root, sub) as key:  # type: ignore[attr-defined]
                    val, _ = winreg.QueryValueEx(key, "Path")  # type: ignore[attr-defined]
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

    for cmd, name, install_fn, required, npm_package, extra_paths_fn in _DEPENDENCY_CHECKS:
        # Built-in installer (custom) wins; otherwise fall back to npm install.
        effective_install = install_fn
        if effective_install is None and npm_package:
            effective_install = partial(_npm_install, npm_package, name, update=False)

        path = _resolve(cmd, extra_paths_fn)
        if path:
            if fix and npm_package:
                _npm_install(npm_package, name, update=True)
            ok.append(f"✅ {name}: {path}")
            continue

        level = "❌" if required else "⚠️ "
        if fix and effective_install:
            try:
                effective_install()
            except (PermissionError, OSError) as exc:
                _safe_print(f"  Install failed: {exc}")
            if sys.platform == "win32":
                _refresh_path_win32()
            path = _resolve(cmd, extra_paths_fn)
            if path:
                ok.append(f"✅ {name}: {path} (just installed)")
            else:
                issues.append(f"{level} {name}: install failed")
        elif fix and required:
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
        from boxagent.config import load_config
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
    # xc-copilot-api stores its token at ~/.local/share/copilot-api/github_token
    token_path = Path.home() / ".local" / "share" / "copilot-api" / "github_token"
    if token_path.is_file():
        all_ok.append(f"✅ Copilot token: {token_path}")
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
