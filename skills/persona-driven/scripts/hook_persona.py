#!/usr/bin/env python3
"""L3 硬约束钩子：档位注入、安全底线拦截、行为埋点。

由 DSH hooks（Claude Code 桥接 @deepseek-ai/dsh-hooks-claude-code）调用，
通过 stdin 接收事件 JSON，通过 stdout 返回决策 JSON。
设计约束：
1. 任何异常都不得影响主流程，失败时静默放行。
2. 不联网，不读取凭据，不执行外部命令。
3. 默认只记录派生标记，不落盘原始命令内容。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# 钩子会被高频调用，禁止写入 .pyc，避免在技能目录里堆积 __pycache__。
sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parent))

INJECT_EVENTS = {"sessionstart", "prompt"}
LOG_COMMANDS_ENV = "PERSONA_LOG_COMMANDS"
PROBE_TAG_ENV = "PERSONA_PROBE_TAG"
METRICS_TAG_ENV = "PERSONA_METRICS_TAG"
# DSH 注入 DSH_SESSION_ID；后两个键只作兼容回退（换平台时会话标识改名，
# 但埋点不该因此整体归到 unknown）。
SESSION_ENV = "DSH_SESSION_ID"
SESSION_ENV_FALLBACKS = (SESSION_ENV, "CODEBUDDY_SESSION_ID", "CLAUDE_SESSION_ID")

# 触发词扫描（v1.5.31）：对 UserPromptSubmit 的 prompt 文本只做**内存内模式匹配**，
# 只落「命中与否 + 命中类别」，绝不落原文。模式表刻意保守（宁漏勿误报），
# 命中率是粗信号，不代表全部调教话术。
TRIGGER_PATTERNS = {
    "persona_code": re.compile(r"\b[EI][NS][TF][JP]\b"),
    "persona_kw": re.compile(r"人格|档位|旋钮"),
    "phrase": re.compile(r"雷厉风行|果断点|果断一点|直接点|啰嗦|简洁点|慢下来"),
}

# 工具名 → 统计类别的**规范词表**。
#
# 必须与 lib/persona-core.js 的 TOOL_ALIASES 逐项一致（一致性由
# tests/tool-vocab-sync.mjs 比对）。看板 persona_dashboard.py 与 persona.py report
# 都按这套类别名读取，**单方面改名会让统计静默归零**——移植时 JS 侧曾把 shell 改成
# bash、把 grep+glob 合并成 search，结果探查画像/终端调用/验证回收全部恒为 0。
#
# DSH 的工具名是**小写**（read / write / edit / grep / glob / pwsh），而上游词表停留在
# Claude Code 的 Read / Edit / Grep / Glob / Bash，两边都已补齐小写名。
TOOL_ALIASES = {
    "read": {"read_file", "Read", "get_cell_ranges", "read_table", "read"},
    "write": {"write_to_file", "Write", "set_cell_range", "append_file", "write"},
    "edit": {"replace_in_file", "Edit", "multi_edit", "MultiEdit", "apply_patch",
             "edit_file", "edit"},
    "shell": {"execute_command", "Bash", "run_command", "shell", "bash", "pwsh", "PowerShell"},
    "grep": {"search_content", "Grep", "grep"},
    "glob": {"list_dir", "Glob", "glob"},
    "task": {"task", "Task", "subagent", "subagent_fork", "workflow"},
    "web": {"web_search", "web_fetch", "WebSearch", "WebFetch"},
}

# 前缀兜底：精确名匹配不到时按前缀归类，**顺序敏感，先匹配者胜**。
# 存在的理由：DSH 的工具名由插件贡献（mcp__*、blender_rt_*、内置的 skill /
# todo_write / job_* …），枚举永远追不上。退化为 other 时埋点会写 raw_tool，
# 所以「不认识」是可诊断的，不是静默丢失。
TOOL_PREFIX_RULES = (
    ("read", "read"),
    ("write_to", "write"),
    ("replace_in_file", "edit"),
    ("apply_patch", "edit"),
    ("edit", "edit"),
    ("grep", "grep"),
    ("search_content", "grep"),
    ("glob", "glob"),
    ("list_dir", "glob"),
    ("bash", "shell"),
    ("pwsh", "shell"),
    ("shell", "shell"),
    ("powershell", "shell"),
    ("execute_command", "shell"),
    ("run_command", "shell"),
    ("web_search", "web"),
    ("web_fetch", "web"),
    ("task", "task"),
    ("subagent", "task"),
    ("workflow", "task"),
)


def read_stdin() -> dict:
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def emit(payload: dict) -> None:
    try:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    except Exception:
        try:
            sys.stdout.write('{"continue": true}')
        except Exception:
            pass


def normalize_tool(name: str) -> str:
    """统计类别。精确名优先，未命中走前缀兜底，仍未知为 other。

    与 lib/persona-core.js 的 normalizeTool 行为一致（tests/tool-vocab-sync.mjs 比对）。
    """
    for cls, members in TOOL_ALIASES.items():
        if name in members:
            return cls
    lower = str(name or "").lower()
    for prefix, cls in TOOL_PREFIX_RULES:
        if lower.startswith(prefix):
            return cls
    return "other"


def metrics_bucket() -> str:
    """实验流量分桶标记：serve 进程环境变量 PERSONA_METRICS_TAG（如 exp-p7full）。

    钩子继承 serve 环境 → 每条记录带 bucket 字段；看板按前缀 exp- 精确剔除
    实验流量，取代「会话事件数 ≥ 阈值」的启发式。生产环境不设该变量，字段缺省。
    """
    import os

    raw = (os.environ.get(METRICS_TAG_ENV) or "").strip()
    cleaned = "".join(ch for ch in raw if ch.isascii() and (ch.isalnum() or ch in "-_"))[:32]
    return cleaned or ""


def path_hash(tool_input: dict) -> str:
    """被操作文件的 sha256 前 12 位（不落路径原文，解锁返工率与重复读统计）。"""
    import persona as p

    raw = ""
    for key in p.GUARD_PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            raw = value
            break
    if not raw:
        return ""
    return hashlib.sha256(raw.strip().encode("utf-8", "replace")).hexdigest()[:12]


def proj_key(data: dict) -> str:
    """项目归属键：事件 cwd 的 sha256 前 12 位（不落路径原文）。

    v1.5.33 起「会话 / 项目 / 全局」三层在数据侧彻底可分：session 字段归会话，
    proj 字段归项目，两者皆空（钩子外调用）的记录视为全局/未知层。
    """
    cwd = str(data.get("cwd") or "").strip()
    if not cwd:
        return ""
    return hashlib.sha256(cwd.encode("utf-8", "replace")).hexdigest()[:12]


def event_input(data: dict) -> dict:
    value = data.get("tool_input")
    return value if isinstance(value, dict) else {}


def log_injection(state: dict, block: str, data: dict, event: str) -> None:
    """记录一次注入内容已提交，用于判断每轮注入是否发生。

    只记录人格、级别、事件名与档位块长度，不记录用户的提问原文。
    任何失败都不得影响注入本身。

    口径（别把这条记录读过头）：本函数在内容**提交给平台之后**调用，记录的是
    「已向平台提交 N 字」，**不是「平台已采纳」**。两个注入事件是否真被送进模型，
    要用 `PERSONA_PROBE_TAG` 探针在**真实请求体**里核对——本记录只能证明「已提交」；
    判断「已采纳」请看探针结论（`references/limits.md`），
    不要拿本记录当该事件生效的证据。
    """
    try:
        import persona as p

        record = {
            "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"),
            "session": str(session_of(data) or "unknown")[:24],
            "proj": proj_key(data),
            "layer": state.get("layer"),
            "event": event,
            "persona": state.get("persona"),
            "level": state.get("level"),
            "chars": len(block),
        }
        path = p.metrics_dir() / f"injections-{datetime.now():%Y-%m-%d}.jsonl"
        p.append_jsonl(path, record)
    except Exception:
        pass


def session_of(data: dict) -> str | None:
    """取会话标识：事件 JSON 的 session_id 优先；缺失时回退平台注入的环境变量。

    实测 `DSH_SESSION_ID` 与钩子事件里的 session_id 同值；更早的平台用
    `CODEBUDDY_SESSION_ID`，故按 DSH → CODEBUDDY → CLAUDE 依次回退。
    部分平台事件不带 session_id（历史实测约 22%），回退可把这部分埋点归回真实会话，
    缩小 experiment 多会话检测与 report 会话汇总的 unknown 盲区。
    两者都没有时返回 None（调用方落 unknown，与旧行为一致）。
    """
    value = data.get("session_id")
    if not value:
        import os

        for key in SESSION_ENV_FALLBACKS:
            value = os.environ.get(key) or ""
            if value:
                break
    return str(value) if value else None


def probe_tag() -> str:
    """读取并清洗注入采纳探针的标签；未设置时返回空串（生产行为完全不变）。

    用途（详见 references/limits.md 的探针步骤）：`SessionStart` 的 additionalContext
    是否真被模型看到，靠「注入内容因事件而异」来区分——设了本变量后，注入块末尾
    会追加一行 `[persona-probe:<标签>:<事件名>]`。新会话里让模型原样列出这些行，
    即可判读各事件的注入是否被采纳。

    标签只保留 ASCII 字母数字与 -_（≤32 字符），杜绝把任意文本写进注入块。
    """
    import os

    raw = (os.environ.get(PROBE_TAG_ENV) or "").strip()
    if not raw:
        return ""
    cleaned = "".join(ch for ch in raw if ch.isascii() and (ch.isalnum() or ch in "-_"))[:32]
    return cleaned or "default"


def handle_inject(event: str, data: dict) -> None:
    import persona as p

    if event == "prompt":
        # 触发词扫描：只记命中类别，不落 prompt 原文（v1.5.31）。
        try:
            text = data.get("prompt")
            kinds = sorted(name for name, pat in TRIGGER_PATTERNS.items()
                           if isinstance(text, str) and pat.search(text))
            record = {
                "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"),
                "session": str(session_of(data) or "unknown")[:24],
                "proj": proj_key(data),
                "ev": "trigger_scan",
                "hit": bool(kinds),
                "kinds": kinds,
                "scan": isinstance(text, str),
            }
            bucket = metrics_bucket()
            if bucket:
                record["bucket"] = bucket
            p.append_jsonl(
                p.metrics_dir() / f"{datetime.now():%Y-%m-%d}.jsonl", record)
        except Exception:
            pass

    cwd = data.get("cwd")
    state = p.read_state("auto", Path(cwd) if cwd else None, session_of(data))
    # l3 注入完整块；l2 注入**三条底线**（v1.5.19 起：l2 不再零注入——P2 复测的 l2 组
    # 收尾自报率仅 7%，根因就是自报要求只随 l3 的完整块下发）；off 不注入。
    if not state or state.get("level") not in ("l2", "l3"):
        emit({"continue": True})
        return
    block = p.render_block(state)
    if not block:
        emit({"continue": True})
        return
    hook_event_name = {
        "sessionstart": "SessionStart",
        "prompt": "UserPromptSubmit",
    }[event]
    tag = probe_tag()
    if tag:
        block = block + f"\n[persona-probe:{tag}:{hook_event_name}]"
    emit(
        {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": hook_event_name,
                "additionalContext": block,
            },
        }
    )
    # 埋点放在 emit 之后：先把内容交付给平台，再记录「已提交」。
    # 反过来会在提交失败时留下一条「已注入」的假记录。
    log_injection(state, block, data, hook_event_name)


def handle_guard(data: dict) -> None:
    import persona as p

    tool_name = str(data.get("tool_name") or "")
    hit = p.guard_check(tool_name, event_input(data))
    if not hit:
        emit({"continue": True})
        return
    category, pattern = hit
    reason = (
        f"[安全底线] 命中「{category}」（规则 {pattern}）。"
        "这是不可逆或高影响操作，任何人格档位都不豁免："
        "请先向用户说明影响面与回滚方式，取得明确确认后再执行；"
        "本次执行不得按快档直接放行。"
    )
    emit(
        {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": reason,
            },
        }
    )
    # 危险拦截计数（v1.5.31）：只记命中的规则类别与会话，不记命令原文；失败静默。
    try:
        import persona as p

        record = {
            "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"),
            "session": str(session_of(data) or "unknown")[:24],
            "proj": proj_key(data),
            "ev": "guard",
            "category": str(category)[:40],
            "tool": normalize_tool(tool_name),
        }
        bucket = metrics_bucket()
        if bucket:
            record["bucket"] = bucket
        p.append_jsonl(
            p.metrics_dir() / f"{datetime.now():%Y-%m-%d}.jsonl", record)
    except Exception:
        pass


def handle_metrics(data: dict) -> None:
    import persona as p

    tool_name = str(data.get("tool_name") or "")
    cls = normalize_tool(tool_name)
    tool_input = event_input(data)
    command = str(tool_input.get("command") or tool_input.get("cmd") or "")

    record = {
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"),
        "session": str(session_of(data) or "unknown")[:24],
        "proj": proj_key(data),
        "persona": None,
        "tool": cls,
        "test": bool(p.TEST_PATTERN.search(command)),
        "static": bool(p.STATIC_PATTERN.search(command)),
        "write": cls in ("write", "edit"),
        "read": cls == "read",
    }
    bucket = metrics_bucket()
    if bucket:
        record["bucket"] = bucket
    if cls in ("edit", "write", "read"):
        ph = path_hash(tool_input)
        if ph:
            record["ph"] = ph

    # 未识别的工具名与最终仍归不到会话的记录是统计失效的前兆（平台改名会让埋点
    # 静默归零），因此对这类记录留下原始工具名与事件名，便于事后诊断。不记录任何命令原文。
    if cls == "other" or record["session"] == "unknown":
        record["raw_tool"] = tool_name[:60]
        record["ev"] = str(data.get("hook_event_name") or "")[:24]

    cwd = data.get("cwd")
    state = p.read_state("auto", Path(cwd) if cwd else None, session_of(data))
    if state:
        record["persona"] = state.get("persona")
        record["layer"] = state.get("layer")

    import os

    if os.environ.get(LOG_COMMANDS_ENV) == "1" and command:
        record["command"] = command[:200]

    directory = p.metrics_dir()
    path = directory / f"{datetime.now():%Y-%m-%d}.jsonl"
    p.append_jsonl(path, record)
    emit({"continue": True})


def main(argv: list[str]) -> int:
    event = "prompt"
    for index, token in enumerate(argv):
        if token == "--event" and index + 1 < len(argv):
            event = argv[index + 1].lower()
    data = read_stdin()
    try:
        if event in INJECT_EVENTS:
            handle_inject(event, data)
        elif event == "pretool":
            handle_guard(data)
        elif event == "posttool":
            handle_metrics(data)
        else:
            emit({"continue": True})
    except Exception:
        emit({"continue": True})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
