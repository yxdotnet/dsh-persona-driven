#!/usr/bin/env python3
"""启动前环境检查。

不修改配置、不输出凭据、不联网。返回 ready / partial / needs_setup / unavailable。

检查两件事：① DSH 插件包（`@dsh-external/persona-driven`）是否已挂载——它是 L3 的
主投递路径，装了就每轮注入并强制安全底线；② 可选的 Claude Code 桥接配置
（`$DSH_HOME/persona/hooks.json`）里的命令能否真的被调起。

唯一的对外动作是钩子自检：把 hooks.json 里注册的那条命令执行一次，喂入固定伪事件，
用来证明钩子真的能被调起，而不只是写进了配置。伪事件只参与钩子的规则匹配与决策输出，
既不会被执行，也不会写入任何文件。自检走 PreToolUse 分支，该分支不落盘，因此不污染埋点。
桥接配置缺失不算故障（DSH 插件已覆盖主路径），只标注「未配置（可选）」。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 不写 .pyc：doctor 通过 persona.py 导入本模块，避免在技能目录里生成 __pycache__。
sys.dont_write_bytecode = True


def dsh_home() -> Path:
    """DSH 主目录：$DSH_HOME 优先，缺省 ~/.dsh。"""
    override = (os.environ.get("DSH_HOME") or "").strip()
    return Path(override).expanduser() if override else Path.home() / ".dsh"


SKILL_DIR = Path(__file__).resolve().parent.parent
DATA_FILE = SKILL_DIR / "assets" / "personas.json"
REFERENCES_DIR = SKILL_DIR / "references"
HOOK_SCRIPT = SKILL_DIR / "scripts" / "hook_persona.py"
HOME_ROOT = dsh_home() / "persona"
# 桥接钩子配置文件（Claude Code 事件表形态，由 dsh-hooks-claude-code 读取）。
DEFAULT_HOOKS_FILE = HOME_ROOT / "hooks.json"

# L3 主投递：DSH 插件包名与认领标记（cordis.patch.yml 里也是这个标记）。
DSH_PLUGIN_NAME = "@dsh-external/persona-driven"
DSH_PLUGIN_MARKER = "persona-driven"

REQUIRED_PYTHON = (3, 10)
HOOK_MARKER = "hook_persona.py"
SELFTEST_TIMEOUT = 15

# 与 persona.GUARD_SCOPE_ENV 同名。此处只读取展示，不重复实现规则解析
# （两个脚本互相导入会形成循环）。
GUARD_SCOPE_ENV = "PERSONA_GUARD_SCOPE"

SELFTEST_PASSTHROUGH = {
    "hook_event_name": "PreToolUse",
    "session_id": "doctor-selftest",
    "tool_name": "Bash",
    "tool_input": {"command": "echo persona-selftest"},
}
SELFTEST_GUARD = {
    "hook_event_name": "PreToolUse",
    "session_id": "doctor-selftest",
    "tool_name": "Bash",
    "tool_input": {"command": "git reset --hard HEAD"},
}

TOKEN_PATTERN = re.compile(r'"([^"]*)"|\'([^\']*)\'|([^\s"\']+)')


def domain_file(name: str) -> Path:
    """领域映射文件路径。

    平台只接受「根目录/二级目录/文件」两级结构，因此领域映射统一放在 references/
    下、以 domains-<name>.md 命名。源码与发布包布局一致，无需双布局兼容。
    """
    return REFERENCES_DIR / f"domains-{name}.md"


def split_command(command: str) -> list[str]:
    """按 Shell 引号规则切分命令，兼容 Windows 反斜杠路径。"""
    tokens = []
    for match in TOKEN_PATTERN.finditer(command):
        token = next((g for g in match.groups() if g is not None), "")
        if token:
            tokens.append(token)
    return tokens


def registered_hook_command(hooks_path: Path | None = None) -> str:
    """取出钩子配置里注册的命令，PreToolUse 优先。

    配置文件是 Claude Code 事件表形态：裸事件映射（DSH 桥接偏好）或
    `{"hooks": {...}}` 外壳，两者都认。
    """
    hooks_path = hooks_path or DEFAULT_HOOKS_FILE
    if not hooks_path.exists():
        return ""
    try:
        with hooks_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = data
    fallback = ""
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for hook in entry.get("hooks") or []:
                if not isinstance(hook, dict):
                    continue
                command = str(hook.get("command", ""))
                if HOOK_MARKER not in command:
                    continue
                if event == "PreToolUse":
                    return command
                fallback = fallback or command
    return fallback


def hooks_installed(hooks_path: Path | None = None) -> bool:
    return bool(registered_hook_command(hooks_path))


def dsh_plugin_installed() -> bool:
    """L3 主投递是否已挂载：DSH 插件包 @dsh-external/persona-driven。

    两条判据，任一命中即算已装：
    ① `$DSH_HOME/profiles/*/package.json` 的 `dsh.profile.bundles` 数组里出现该包名
       （或任何含 `persona-driven` 的条目）；
    ② `$DSH_HOME/cordis.patch.yml` 或 `$DSH_HOME/profiles/*/cordis.patch.yml` 里
       提到 `persona-driven`（纯文本扫描，插件行可能以别的形态挂载）。
    """
    home = dsh_home()
    profiles = home / "profiles"
    for pkg in sorted(profiles.glob("*/package.json")):
        try:
            # utf-8-sig：Windows 上被其他工具写出的 package.json 可能带 BOM。
            data = json.loads(pkg.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        bundles = ((data.get("dsh") or {}).get("profile") or {}).get("bundles")
        if not isinstance(bundles, list):
            continue
        for item in bundles:
            if isinstance(item, str):
                if item == DSH_PLUGIN_NAME or DSH_PLUGIN_MARKER in item:
                    return True
            elif isinstance(item, dict):
                if DSH_PLUGIN_MARKER in json.dumps(item, ensure_ascii=False):
                    return True
    patches = [home / "cordis.patch.yml"]
    patches += sorted(profiles.glob("*/cordis.patch.yml"))
    for patch in patches:
        try:
            if DSH_PLUGIN_MARKER in patch.read_text(encoding="utf-8"):
                return True
        except Exception:
            continue
    return False


def run_hook_probe(command: str, payload: dict) -> tuple[bool, str, dict]:
    """执行注册的钩子命令并喂入伪事件，返回 (是否成功, 说明, 解析后的输出)。"""
    argv = split_command(command)
    if not argv:
        return False, "注册的命令无法解析", {}
    try:
        proc = subprocess.run(
            argv,
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SELFTEST_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"超过 {SELFTEST_TIMEOUT} 秒未返回", {}
    except FileNotFoundError:
        return False, f"命令中的解释器不存在：{argv[0]}", {}
    except OSError as exc:
        return False, f"无法启动钩子（{exc.__class__.__name__}）", {}
    except Exception as exc:
        return False, f"自检异常（{exc.__class__.__name__}）", {}

    raw = (proc.stdout or "").strip()
    try:
        parsed = json.loads(raw) if raw else {}
    except Exception:
        return False, f"钩子未返回合法 JSON（退出码 {proc.returncode}）", {}
    if proc.returncode != 0:
        return False, f"钩子退出码 {proc.returncode}", parsed
    if not isinstance(parsed, dict) or parsed.get("continue") is not True:
        return False, "钩子返回值不符合平台约定（缺少 continue: true）", parsed
    return True, "已执行并返回合法决策", parsed


def hook_selftest(hooks_path: Path | None = None) -> dict:
    """证明钩子真的能被调起，而不只是写进了配置。

    两个探针：一是普通调用能否返回合法决策，二是安全底线规则能否真的触发。
    只往钩子喂固定输入，不改配置、不写文件、不执行任何被扫描的命令。

    本自检针对的是**可选的 Claude Code 桥接**（hooks.json）。桥接未配置不算故障——
    DSH 的主投递是插件包，没有 hooks.json 时调用方标注「未配置（可选）」即可。
    这里不需要 shell：桥接用 DSH 的 ctx.shell 跑命令，原生插件完全不经 shell。
    """
    command = registered_hook_command(hooks_path)
    if not command:
        return {
            "installed": False,
            "healthy": False,
            "detail": "未在配置中找到本技能的钩子（未配置，可选）",
        }
    if not HOOK_SCRIPT.exists():
        return {
            "installed": True,
            "healthy": False,
            "detail": f"钩子脚本不在技能目录内：{HOOK_SCRIPT}",
        }

    ok, detail, _ = run_hook_probe(command, SELFTEST_PASSTHROUGH)
    if not ok:
        return {"installed": True, "healthy": False, "detail": detail}

    ok, detail, parsed = run_hook_probe(command, SELFTEST_GUARD)
    if not ok:
        return {"installed": True, "healthy": False, "detail": detail}
    decision = (parsed.get("hookSpecificOutput") or {}).get("permissionDecision")
    if decision != "ask":
        return {
            "installed": True,
            "healthy": False,
            "detail": f"底线规则未触发（返回 {decision or '无决策'}）",
        }
    return {"installed": True, "healthy": True, "detail": "已执行，且底线规则可正常触发"}


def check() -> dict:
    core = {}
    optional = {}
    faults = []

    version_ok = sys.version_info[:2] >= REQUIRED_PYTHON
    core["Python 版本"] = (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        if version_ok
        else None
    )

    data_ok = False
    type_count = 0
    if DATA_FILE.exists():
        try:
            with DATA_FILE.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
            type_count = len(payload.get("types", {}))
            data_ok = type_count > 0 and len(payload.get("dials", {})) == 8
        except Exception as exc:
            faults.append(f"档位数据解析失败：{exc}")
    if data_ok:
        core["档位数据"] = f"{type_count} 型 + 8 旋钮"

    missing_domains = [
        name
        for name in (
            "coding",
            "ui-design",
            "visual-image",
            "finance",
            "writing",
            "planning",
            "collaboration",
            "decision",
        )
        if not domain_file(name).exists()
    ]
    present = 8 - len(missing_domains)
    if present:
        core["领域映射"] = f"{present}/8"
    if missing_domains:
        optional["缺失领域映射"] = "、".join(missing_domains)

    # 全程只读：不创建、不写入、不删除任何文件，用权限位判断可写性。
    state_dir = HOME_ROOT if HOME_ROOT.exists() else HOME_ROOT.parent
    writable = state_dir.exists() and os.access(state_dir, os.W_OK)
    if not writable:
        faults.append(f"状态目录不可写或父目录不存在：{state_dir}")
    else:
        core["状态目录"] = str(HOME_ROOT)

    # 只读展示：本进程看到的值。钩子进程另行启动，须设成系统 / 用户级变量才一致。
    guard_scope_value = (os.environ.get(GUARD_SCOPE_ENV) or "").strip().lower()
    optional["守卫扫描范围"] = (
        f"{guard_scope_value}（须为系统/用户级变量才对钩子生效）"
        if guard_scope_value in ("all", "exec")
        else "all（默认，未设置或取值非法）"
    )

    # L3 主投递：DSH 插件包（装了就在每轮注入 + 强制安全底线确认）。
    plugin_installed = dsh_plugin_installed()
    if plugin_installed:
        core["DSH 插件"] = "已安装（L3 级：每轮注入 + 安全底线强制确认）"
    else:
        optional["DSH 插件"] = (
            "未安装（当前为 L1/L2 级：档位仍生效，但无每轮注入、安全底线不强制）"
        )

    # 兼容投递：可选的 Claude Code 桥接配置。缺失不算故障，自检失败只如实标注。
    hooks = hook_selftest()
    if not hooks["installed"]:
        optional["hook 桥接（可选）"] = "未配置（可选）"
    elif hooks["healthy"]:
        optional["hook 桥接（可选）"] = "已配置且自检通过（重启 DSH 后生效）"
    else:
        optional["hook 桥接（可选）"] = f"已配置但自检失败（{hooks['detail']}）"

    if not version_ok:
        status = "unavailable"
    elif not data_ok or not writable:
        status = "needs_setup"
    elif plugin_installed:
        status = "ready"
    else:
        status = "partial"

    return {
        "status": status,
        "core": core,
        "optional": optional,
        "faults": faults,
        "hooks": hooks,
        "plugin": plugin_installed,
        "hooks_file": str(DEFAULT_HOOKS_FILE),
    }


def render(result: dict) -> str:
    lines = ["## 环境检查", f"- 总体状态：{result['status']}"]
    lines.append("- 已通过：")
    for key, value in result["core"].items():
        lines.append(f"    - {key}：{value}")
    missing = result.get("optional") or {}
    if missing:
        lines.append("- 可选项状态：")
        for key, value in missing.items():
            lines.append(f"    - {key}：{value}")
    if result["faults"]:
        lines.append("- 故障项：")
        for item in result["faults"]:
            lines.append(f"    - {item}")
    else:
        lines.append("- 故障项：无")

    status = result["status"]
    hooks = result.get("hooks") or {}
    broken = bool(hooks.get("installed")) and not hooks.get("healthy")
    plugin = bool(result.get("plugin"))

    if plugin:
        lines.append("- 硬约束：**已由 DSH 插件提供**（每轮注入 + 安全底线强制确认）。")
    if broken:
        lines.append(
            "- hook 桥接（可选）：已配置但自检失败；DSH 插件不受影响，"
            "只是这份桥接配置不会生效。"
        )
    if status == "ready":
        lines.append("- 下一步：直接进入正常流程")
    elif status == "partial":
        lines.append(
            "- 下一步：可正常使用 L1/L2 影响；需要 L3 硬约束时安装 DSH 插件包 "
            "`@dsh-external/persona-driven`（主路径），或运行 "
            "`python scripts/persona.py install-hooks --apply --write-confirmed` "
            "生成桥接配置后重启 DSH"
        )
    elif status == "needs_setup":
        lines.append("- 下一步：按上面的故障项补齐后重新检查")
    else:
        lines.append(
            f"- 下一步：停止。需要 Python {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]} 及以上，"
            "且状态目录可写"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    payload = check()
    print(render(payload))
    sys.exit(0 if payload["status"] in ("ready", "partial") else 2)
