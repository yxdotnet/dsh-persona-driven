#!/usr/bin/env python3
"""人格档位状态机与命令行入口。

职责：
1. 读写当前人格档位状态（全局或项目级）。
2. 渲染供 hook 每轮注入的紧凑档位块。
3. 提供报告、规则查询、配置卡导出与 hooks 安装。
本脚本只使用 Python 标准库，不联网，不读取凭据。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# 不写 .pyc：doctor 与 install-hooks 会导入同级脚本，避免在技能目录里生成 __pycache__。
sys.dont_write_bytecode = True

SKILL_DIR = Path(__file__).resolve().parent.parent
DATA_FILE = SKILL_DIR / "assets" / "personas.json"
REFERENCES_DIR = SKILL_DIR / "references"
HOOK_SCRIPT = SKILL_DIR / "scripts" / "hook_persona.py"


def domain_file(name: str) -> Path:
    """领域映射文件路径。

    平台只接受「根目录/二级目录/文件」两级结构，因此领域映射统一放在 references/
    下、以 domains-<name>.md 命名。源码与发布包布局一致，无需双布局兼容。
    """
    return REFERENCES_DIR / f"domains-{name}.md"

def dsh_home() -> Path:
    """DSH 主目录：$DSH_HOME 优先，缺省 ~/.dsh。"""
    override = (os.environ.get("DSH_HOME") or "").strip()
    return Path(override).expanduser() if override else Path.home() / ".dsh"


HOME_ROOT = dsh_home() / "persona"
HOME_STATE = HOME_ROOT / "state.json"
HOME_METRICS = HOME_ROOT / "metrics"
HOME_SESSIONS = HOME_ROOT / "sessions"
# DSH 把会话标识注入命令行环境（DSH_SESSION_ID），钩子则从事件 JSON 拿到
# 同一个值——两者实测一致，因此可以按会话隔离档位。
# 后两个键只作兼容回退：换平台时会话标识改名，档位隔离不该因此失效。
SESSION_ENV_KEYS = ("DSH_SESSION_ID", "CODEBUDDY_SESSION_ID", "CLAUDE_SESSION_ID")

LAYER_LABEL = {
    "session": "仅本对话",
    "project": "本项目目录",
    "global": "全局默认（所有对话）",
}

# sessions --prune 的默认清理阈值（天）。会话档位文件约 0.7 KB，长期使用会累积。
DEFAULT_PRUNE_DAYS = 30

# compact-metrics 的默认合并阈值（秒）。只有超过这个时长未变动的溢出分片才会被折回
# 主文件——正在运行的写进程可能还在追加它（分片是按 pid 分的，短命钩子进程写一次就走）。
DEFAULT_COMPACT_MIN_AGE = 3600

DOMAINS = [
    "coding",
    "ui-design",
    "visual-image",
    "finance",
    "writing",
    "planning",
    "collaboration",
    "decision",
]

DIAL_ORDER = ["PACE", "VERIFY", "RISK", "STRUCT", "DETAIL", "EXPLORE", "STYLE", "WARMTH"]
DIAL_SHORT = {
    "PACE": "P",
    "VERIFY": "V",
    "RISK": "R",
    "STRUCT": "S",
    "DETAIL": "D",
    "EXPLORE": "E",
    "STYLE": "ST",
    "WARMTH": "W",
}

SAFETY_FLOOR_LINE = (
    "安全底线（任何档位都不得绕过）：命中 删除/覆盖、支付与资金、鉴权与权限、"
    "密钥与凭据、生产环境变更、数据库破坏性语句、不可逆操作 时，"
    "强制切回高验证并先确认，不得按快档直接执行。"
)

# 验证下限：与安全底线并列的第二条底线。
# 起因是首轮对照实验的负增益——快档下「漏关键验证」由 0 升到 2（一个条件分支的 VIP
# 路径、一个边界缺陷的回归测试）。根因不是档位数值太小，而是判据有缝：
# 低档成立的前提是「改动不改变被验对象的语义」，但这条前提此前没写进任何一处，
# 于是「单文件 ≤20 行」可以盖过「这次改的是条件分支」。这一行是那条缺失前提的落点。
# v1.5.17（P2）加固化条款：P1 复测（300 轮）显示「留可复现证据」仍被执行为
# 「跑一遍命令」——C 组 97% 跑了测试套件、却 95% 不补测试（缺陷修复类）。
# 根因：证据停留在 transient（跑一次就丢）。P2 把证据定义为**固化产出**
# （测试用例优先，不便写测试则留命令+期望输出）+ 收尾一行**自报**（可机检）。
# v1.5.19（P3）把「固化」升级为「有效」：P2 复测暴露口径是代理——「改没改 tests/」
# 只证明写了测试，不证明测试抓得住缺陷。新增一条可机检要求：**把本次改动退回后，
# 新增用例应当失败**（否则等于没验）。二级口径「有效测试率」据此可在收尾抽查。
VERIFY_FLOOR_LINE = (
    "验证下限（不随档位下降，且与全部旋钮解耦）：档位调的是「额外验证」的多少，不是「要不要验」。"
    "改什么至少验什么——本次交付对象本身必须被真实验证并给出可复核证据；"
    "改动一旦引入新的行为、事实主张、数据推导或交互状态，即不满足低档前提，按领域红线取最严档；"
    "修复缺陷必须留下能复现该缺陷的验证证据。"
    "证据要固化、可复跑，不能跑一次就丢：引入新行为或修复缺陷时，"
    "把验证固化为新增或修改一条可复跑的测试用例（不便写测试则留命令+期望输出）。"
    "而这条用例必须真的抓住这次改动——把本次改动退回后它应当失败；"
    "只跑一遍命令、或写一条无论如何都通过的用例，都不算证据。"
    "并在收尾最后一行自报「验证：<证据与复跑方式>」，纯机械改动自报「验证：不适用」。"
    "探索广度（EXPLORE）与推进节奏（PACE）只决定「给几个方案」「多久停一次」，"
    "不参与本下限的计算——不得因「只给一个方案」或「只求快」而少验。"
)

# l2 档的注入抬头。v1.5.19 起 l2 由「零注入」改为注入**三条底线**（不含档位数值与思考步骤）：
# P2 复测的 l2 组收尾自报率仅 7%，根因是自报要求此前只随 l3 的完整块每轮注入。
# 三条底线是「约束」而不是「花样」，应当随所有**已落盘**档位生效，与档位高低无关。
L2_FLOOR_HEAD = "[人格底线 · 不随档位下降 · 与全部旋钮解耦]"

# 一次到位：v1.5.15 新增的第三条底线，与安全底线、验证下限并列。
# 起因是 v3（300 轮）的层 2 归因：l3 组降幅中约 4~22pp 来自「少改 tests/」——即靠「少做事」
# 换降幅。标准化反事实后净效应区间 [16%, 33%]、点估计 25%，下沿低于 25% 闸门，增益不稳固；
# 且同批数据里漏验证 C 79/100 vs 对照 48/100（Fisher p<0.0001，一票否决级劣化）。
# 裁定（experiment/REPORT-P0 §7）：不能靠「少做事」换降幅。这一行把那笔降幅的**来源**换轨——
# 省下来的调用必须出自「一次改对」，不得出自「少验/少改」。它不新增验证要求，
# 而是把「返工」定义为验证投入不足的信号，堵住「跳过验证→出错→回头再改」这条伪省钱路径。
#
# v1.5.23（P4）追加「调用经济」段。机制假设 H1 与其证据来源：
#   P3 的 300 轮逐轮工具调用序列显示，l3 组已经把「探查」压下来了（读文件 5 → 2、
#   discovery 3 → 2），但**验证阶段的调用没有合并**——同一轮里「跑用例」「退回改动再跑」
#   「看 diff」被拆成 3–5 次独立调用，而它们完全可以是一次调用里的多条命令。
#   此前注入的三条底线里，没有任何一条约束**调用次数本身**：安全底线管不可逆操作，
#   验证下限管「验什么」，一次到位管「省下来的调用从哪来」，都不管「一次能做完的事
#   有没有被拆成多次」。这一版补的正是这个缺口。
#   与「不得靠少做事换降幅」不冲突：这里合并的是**调用次数**，不是验证内容——
#   验证下限要求的证据一件都不能少（原文已写死这一句，防止条款被读成放松验证）。
#
# v1.5.25（P5）在「调用经济」内追加三条**点名式**条款。机制假设 H2 与其证据来源
#   （experiment/REPORT-P5-侦察-可压缩空间量化-2026-10-14.md：首次使用 P4 每轮的原始
#   事件流 updates.jsonl，拿到了每个调用的真实参数，从而能对「浪费」做调用级归因）：
#   ① 最大单项浪费＝**开幕列目录**。任务已经点名了要改的文件，模型却先用
#      `ls -la <沙箱> && echo "---" && ls -la <沙箱>/tests` 看目录结构。C 组 inspect 类命令
#      1.09/轮，而 C 组自己的「高效轮」（≤8 次调用，占 40%）只有 0.86、低效轮（≥12 次）
#      达 1.97。任务提示已给出文件路径时，目录清单对完成没有贡献。
#   ② 第二项＝**同一套用例换框架重跑**。同一轮里 pytest 与 unittest 都出现的比例：
#      C 组 36%，A 组 12% —— 注入反而把验证冗余抬高三倍（为「证明验证到位」而重跑）。
#      它不增加验证内容，纯属调用浪费，因此拦它不触碰验证下限。
#   ③ **收尾不止**：改动完成后回看代码、复述改动，属无谓调用。
#   与 H1 的关键区别：H1 是**原则式**（「合并调用」，模型要自己判断什么算可合并），
#   实测对 C 组**零响应**（逐工具类型 3.94→3.92 / 2.85→2.84）；H2 改为**点名式**——
#   直接禁掉实测到的具体动作，不留给模型解释空间。
CALL_ECONOMY_LINE = (
    "调用经济（只减次数，不减内容）：探查够用即止——先定清改动面再动手，"
    "不读与本次改动无关的文件；同一个文件不要重复完整读取，改完看一次差异即可确认。"
    "一次调用能做完的事不要拆成多次：多条验证命令（跑用例、把改动退回后看是否失败、看差异）"
    "尽量写进同一次调用。合并的是调用次数，不是验证内容——验证下限要求的证据一件都不能少。"
    "多处同类修改（跨文件重命名、同一常量或文案的多处替换）按文件合并处理："
    "每个文件用一次批量编辑完成该文件内的全部改动，或用一条脚本一次改写完，"
    "逐处单独编辑同一段代码是调用浪费；批量改写后仍按验证下限核对全部证据。"
    "任务已点名要改的文件就直接读，不要先列目录找它；只在确实不知道位置时才搜索。"
    "同一套用例跑通一次就够，不要换个框架再跑一遍（如 pytest 跑过又用 unittest）。"
    "改动完成且验证通过即结束，不再回看代码、不再复述改动。"
)
# 注意：本常量由「一次到位」正文 + CALL_ECONOMY_LINE 拼成，**不得再抄一份**——
# 同一段文案存在两份副本正是本项目反复踩过的失效模式（r12 P2-4：改正则漏改副本）。
PASS_FIRST_LINE = (
    "一次到位（省调用的合法来源）：本档位省下的调用必须来自「一次改对」，不得来自「少做事」。"
    "动手前把改动面与验收判据一次定全，再一次改到位。"
    "若出现返工——同一处改了又改、或验证失败后回头再改——即视为先前的验证投入不足："
    "此时必须补齐验证，而不是继续跳过。"
    + CALL_ECONOMY_LINE
)

# 守卫扫描范围。PreToolUse 钩子命中即转人工确认，扫描字段范围由环境变量决定：
#   all （默认）命令 + 路径 + 文本内容字段。最保守，写文档时正文里提到危险命令
#              也会弹一次确认（宁可多问，不放过真操作）。
#   exec       仅命令 + 路径字段，跳过 content / new_string / query。代价是
#              「写入一个含危险命令的脚本文件」这类场景不再被拦——由用户明确选择。
# 路径字段在任何范围下都扫：路径本身携带的破坏性信息不会造成文档误弹。
# 取值非法时回落到 all，绝不因为配置写错而放大放行。
GUARD_SCOPE_ENV = "PERSONA_GUARD_SCOPE"
GUARD_SCOPE_DEFAULT = "all"
GUARD_FIELD_KEYS = {
    "all": ("command", "cmd", "script", "content", "new_string", "query"),
    "exec": ("command", "cmd", "script"),
}
GUARD_PATH_KEYS = ("file_path", "filePath", "path", "target_file", "notebook_path")


def guard_scope() -> str:
    """当前生效的守卫扫描范围。"""
    value = (os.environ.get(GUARD_SCOPE_ENV) or "").strip().lower()
    return value if value in GUARD_FIELD_KEYS else GUARD_SCOPE_DEFAULT


GUARD_RULES = {
    "破坏性文件操作": [
        r"rm\s+-[a-zA-Z]*[rf]",
        r"\brmdir\s+/s",
        r"\bdel\s+/[fq]",
        r"remove-item.*-recurse",
        r"shutil\.rmtree",
        r"\bmkfs\b",
        r"\bdd\s+if=",
        r"\bformat\s+[a-z]:",
        r">\s*/dev/sd[a-z]",
        r"find\s+.*-delete",
    ],
    "Git 破坏性操作": [
        r"git\s+push\s+.*--force",
        r"git\s+push\s+-f\b",
        r"git\s+reset\s+--hard",
        r"git\s+clean\s+-[a-z]*f",
        r"git\s+branch\s+-D\b",
        r"git\s+filter-branch",
        r"git\s+checkout\s+--\s+\.",
    ],
    "生产环境变更": [
        r"kubectl\s+delete",
        r"kubectl\s+apply",
        r"terraform\s+(apply|destroy)",
        r"docker\s+(system|volume|image)\s+(prune|rm)",
        r"aws\s+s3\s+rm",
        r"aws\s+.*\bterminate",
        r"npm\s+publish",
        r"twine\s+upload",
        r"vercel\s+.*--prod",
        r"netlify\s+deploy\s+.*--prod",
    ],
    "数据库破坏性语句": [
        r"drop\s+(table|database|schema)",
        r"truncate\s+table",
        r"delete\s+from\s+(?!.*\bwhere\b)",
    ],
    "支付与资金": [
        r"stripe\s+.*(charge|refund|transfer|payout)",
        r"\bpaypal\b",
        r"alipay|支付宝|微信支付",
        r"\btransfer\s+funds\b",
    ],
    "密钥与凭据": [
        r"\.ssh/",
        r"id_rsa",
        r"\.aws/credentials",
        r"\.npmrc",
        # 只认**真正的 dotenv 文件**，不认代码里对 env 属性的访问。与 lib/persona-core.js
        # 的同名条目逐字一致（两侧都会扫描编辑内容，所以同样写成 `[.]env` 而非反斜杠转义，
        # 否则这一行会被本条规则命中、无法被编辑）。
        r"(^|[^a-zA-Z0-9_])[.]env(\b|$)",
        r"keychain",
        r"\.netrc",
    ],
    "本机全局配置": [
        r"\.dsh[/\\](cordis\.patch\.yml|persona[/\\]hooks\.json)",
        r"\.claude[/\\]settings\.json",
        r"settings\.local\.json",
        r"\.git[/\\]config",
    ],
    "系统级变更": [
        r"chmod\s+777",
        r"chown\s+-R",
        r"\bcrontab\b",
        r"systemctl\s+(disable|stop)",
        r"reg\s+delete",
        r"schtasks\s+/delete",
        r"takeown\s+/f",
    ],
}

WRITE_TOOLS = {
    "write_to_file",
    "Write",
    "replace_in_file",
    "Edit",
    "multi_edit",
    "MultiEdit",
    "apply_patch",
    "edit_file",
}
SHELL_TOOLS = {"execute_command", "Bash", "run_command", "shell", "bash"}
READ_TOOLS = {"read_file", "Read", "get_cell_ranges"}
TEST_PATTERN = re.compile(
    r"\b(pytest|py\.test|unittest|jest|vitest|mocha|rspec|phpunit|"
    r"go\s+test|cargo\s+test|dotnet\s+test|mvn\s+test|gradle\s+test|"
    r"npm\s+(run\s+)?test|yarn\s+test|pnpm\s+test|rake\s+test|"
    r"test_runner|run_tests)\b",
    re.IGNORECASE,
)
STATIC_PATTERN = re.compile(
    r"\b(tsc|mypy|pyright|eslint|ruff|flake8|golangci-lint|"
    r"cargo\s+check|go\s+build|go\s+vet|dotnet\s+build|"
    r"npm\s+run\s+(lint|build|typecheck))\b",
    re.IGNORECASE,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_data() -> dict:
    with DATA_FILE.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def sanitize_session(sid: str) -> str:
    """会话标识只保留安全字符，避免拼出越界路径。"""
    return re.sub(r"[^0-9A-Za-z_-]", "", sid or "")[:64]


def current_session_id() -> str | None:
    """当前进程所属会话的标识；取不到时返回 None。"""
    for key in SESSION_ENV_KEYS:
        value = os.environ.get(key)
        if value and value.strip():
            return sanitize_session(value.strip())
    return None


def session_state_path(sid: str) -> Path:
    """某个会话的档位文件路径。"""
    return HOME_SESSIONS / f"state-{sanitize_session(sid)}.json"


# 作用范围支持中文直书（v1.5.33）：--scope 会话 / 项目 / 全局 / 自动。
SCOPE_ALIASES = {
    "会话": "session", "本对话": "session",
    "项目": "project",
    "全局": "global",
    "自动": "auto", "当前": "auto",
}
SCOPE_CHOICES = ("auto", "session", "project", "global")


def normalize_scope(value: str) -> str:
    """中文别名归一到内部 scope；未知值原样返回（由调用方校验报错）。"""
    v = str(value or "").strip()
    return SCOPE_ALIASES.get(v, v)


def scope_or_die(value: str) -> str:
    """归一 + 校验；未知 scope 直接报错退出，绝不静默当 global 处理。"""
    s = normalize_scope(value)
    if s not in SCOPE_CHOICES:
        print(f"未知作用范围：{value}（可用：会话 / 项目 / 全局 / 自动，"
              f"或 session / project / global / auto）", file=sys.stderr)
        raise SystemExit(2)
    return s


def proj_id(cwd) -> str:
    """项目短 ID：cwd 的 sha256 前 12 位。

    **必须与 `lib/persona-core.js` 的 `sha12(cwd)` 同算法、同输入。** 这个值写进工具事件的
    `proj` 字段，看板靠它把「档位流水」和「工具事件」归到同一个项目下。算法或输入差一点，
    两边就永远对不上，而且是静默的——所以改动前先用 `tests/floor-text-sync.mjs` 的思路对拍。
    """
    return hashlib.sha256(str(cwd).encode("utf-8", "replace")).hexdigest()[:12]


def find_project_root(start) -> Path:
    """项目根：最近的含 `.git` 的祖先，找不到就是起点本身。

    **必须与 `lib/persona-core.js` 的 `findProjectRoot` 逐条同义。** 用 `abspath` 而不是
    `resolve()`：前者只做规范化、不解析符号链接，与 JS 的 `path.resolve` 语义一致。

    修的是一个真实错位：这里原来直接用裸 `Path.cwd()`，而 JS 用 git 根。在子目录里执行
    `set --scope 项目`，Python 写进「子目录/.dsh/persona/state.json」，JS 却从
    「仓库根/.dsh/persona/state.json」读——**档位设了永远不生效，而且不报错**。

    注：上游 v1.5.44/45 是更彻底的重设计（项目身份=规范化路径 sha256、集中存到
    `persona/projects/`、加 `--cwd` 与「在技能目录里落笔」守卫）。本仓库暂不跟随那套
    存储位置变更，先保证两端解析到同一个目录；升级路径见 `docs/架构与移植说明.md`。
    """
    origin = Path(os.path.abspath(str(start)))
    current = origin
    while True:
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            return origin
        current = parent


def resolve_layer(scope: str = "auto", cwd: Path | None = None,
                  session: str | None = None) -> tuple[str, Path]:
    """决定哪一层的档位生效，返回 (层名, 路径)。

    三层的用途各不相同，高层存在即覆盖低层，互不污染：

    - **session**：一个对话一个人格。你在 A 对话说「用 ENTJ」，B 对话不受影响。
    - **project**：一个仓库一套风格，换目录即换档。
    - **global**：默认人格，兜底用。

    auto 的解析顺序为 会话 > 项目 > 全局。没有会话标识（例如在钩子之外手工调用）
    时自动跳过会话层，不会报错——这仅指**读**路径；**写**路径（set/off
    --scope session）在无会话标识时会被 session_write_guard 拒绝（rc=2），
    绝不静默落全局层。
    """
    sid = sanitize_session(session) if session else current_session_id()
    # 项目根与 JS 侧同义（git 根）。用裸 cwd 会让「在子目录里设的项目档」写进注入读不到的
    # 位置——见 find_project_root 的注释。
    project_path = find_project_root(cwd or Path.cwd()) / ".dsh" / "persona" / "state.json"
    if scope == "session":
        return ("session", session_state_path(sid)) if sid else ("global", HOME_STATE)
    if scope == "project":
        return "project", project_path
    if scope == "global":
        return "global", HOME_STATE
    if sid:
        path = session_state_path(sid)
        if path.exists():
            return "session", path
    if project_path.exists():
        return "project", project_path
    return "global", HOME_STATE


def session_write_guard(scope: str) -> str | None:
    """写路径守卫：set/off 落会话层（--scope session 或默认 auto）但没有会话标识时，拒绝落笔。

    两代坑各堵一面：旧行为一是 --scope session 无会话标识时静默回落全局层（用户说
    「只改本对话」，结果所有对话都被改）；二是默认 scope 为 global——用户在会话里
    说「set XX」，没带 --scope 就把全局层也改了。v1.5.33 起默认 auto：有会话标识写
    会话层，没有则拒绝；改全局必须显式 --scope global。读路径（get/export 的 auto
    解析）不受影响：读全局只是欠注入，不会把本会话的档位泄漏给别人。
    """
    if scope not in ("session", "auto"):
        return None
    if current_session_id():
        return None
    return (
        "拒绝写入：set/off 默认只写当前会话（auto→session），需要会话标识"
        "（DSH_SESSION_ID / CODEBUDDY_SESSION_ID / CLAUDE_SESSION_ID），当前环境三者皆空。"
        "请在本会话内执行；若确要修改全局档位，请显式使用 --scope global。"
        "（v1.5.32 及之前默认写全局层，会话内一句 set 会波及所有对话。）"
    )


def resolve_state_path(scope: str = "auto", cwd: Path | None = None,
                       session: str | None = None) -> Path:
    return resolve_layer(scope, cwd, session)[1]


def read_state(scope: str = "auto", cwd: Path | None = None,
               session: str | None = None) -> dict | None:
    path = resolve_state_path(scope, cwd, session)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def write_state(state: dict, scope: str = "global", session: str | None = None) -> Path:
    path = resolve_state_path(scope, None, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    tmp.replace(path)
    return path


def dials_str(dials: dict) -> str:
    return "/".join(f"{DIAL_SHORT[k]}{dials[k]}" for k in DIAL_ORDER if k in dials)


def short_level(text: str) -> str:
    return text.split("：", 1)[0].strip()


def dials_line(dials: dict, data: dict) -> str:
    parts = []
    for key in DIAL_ORDER:
        if key not in dials:
            continue
        meta = data["dials"][key]
        parts.append(f"{meta['label']}:{short_level(meta['levels'][str(dials[key])])}")
    return " ".join(parts)


def live_steps(state: dict, data: dict) -> list:
    """按档位名从数据源实时取思考步骤；查不到返回空列表。

    `steps` 在 set 时会被快照进 state，但数据源里的文案会随版本修订（例如给「不新增测试」
    补上「缺陷修复与新增行为除外」）。注入块里「验证强度原文」一直是实时读数据源的，
    这里用同一语义，让文案修订立即对已设档用户生效、不必重新 set；
    快照只在数据源查不到该档位时兜底（自定义 dials、档位被改名或删除）。
    """
    kind, name = state.get("kind"), state.get("persona")
    if kind == "type":
        table = data.get("types")
    elif kind == "preset":
        table = data.get("presets")
    else:
        table = None
    if isinstance(table, dict) and isinstance(name, str):
        entry = table.get(name)
        if isinstance(entry, dict):
            steps = entry.get("steps")
            if isinstance(steps, list) and steps:
                return steps
    return []


def render_block(state: dict, data: dict | None = None) -> str:
    """渲染每轮注入的紧凑档位块。

    l3 给完整块（档位 + 思考步骤 + 三条底线）；**l2 只给三条底线**（不给档位数值与
    思考步骤，v1.5.19 起 l2 不再零注入）；off 不注入。
    """
    if not state:
        return ""
    level = state.get("level", "l3")
    if level == "off":
        return ""
    data = data or load_data()

    if level == "l2":
        # v1.5.19：l2 由「档位已设但不注入」改为注入**三条底线**。起因：P2 复测（300 轮）
        # 的 l2 组收尾自报率仅 7%——自报要求此前只随 l3 的完整块每轮注入，l2 拿不到。
        # 三条底线是「约束」不是「花样」，与档位高低无关，应随所有已落盘档位生效；
        # 但 l2 仍不给档位数值与思考步骤（那是 l3 的完整块才有的定位）。
        return "\n".join(
            [L2_FLOOR_HEAD, SAFETY_FLOOR_LINE, VERIFY_FLOOR_LINE, PASS_FIRST_LINE]
        )

    dials = state.get("dials") or {}

    head = f"[人格档位 {dials_str(dials)} · {state.get('persona', '自定义')}]"
    lines = [head, f"定位：{state.get('gist', '')}", f"档位：{dials_line(dials, data)}"]

    verify_key = str(dials.get("VERIFY", ""))
    if verify_key in data["dials"]["VERIFY"]["levels"]:
        lines.append(f"验证强度原文：{data['dials']['VERIFY']['levels'][verify_key]}")

    if level == "l3":
        # 优先用数据源里的实时 steps，快照只作兜底——与上面「验证强度原文」同语义。
        steps = live_steps(state, data) or state.get("steps") or []
        if steps:
            marks = "①②③④⑤⑥"
            joined = " ".join(
                f"{marks[i]}{s}" for i, s in enumerate(steps[: len(marks)])
            )
            lines.append(f"思考步骤：{joined}")
        lines.append(SAFETY_FLOOR_LINE)
        lines.append(VERIFY_FLOOR_LINE)
        lines.append(PASS_FIRST_LINE)
    return "\n".join(lines)


def resolve_persona(name: str, data: dict) -> dict | None:
    if name in data["types"]:
        entry = data["types"][name]
        return {
            "persona": name,
            "kind": "type",
            "gist": entry["gist"],
            "dials": entry["dials"],
            "steps": entry["steps"],
        }
    if name in data["presets"]:
        entry = data["presets"][name]
        return {
            "persona": name,
            "kind": "preset",
            "gist": entry["gist"],
            "dials": entry["dials"],
            "steps": entry["steps"],
        }
    return None


def parse_custom(spec: str, data: dict) -> dict:
    """解析自定义档位，如 P5,V4,R5,S4,D5,E1,ST4,W2 或 P5V4R5。"""
    reverse = {v: k for k, v in DIAL_SHORT.items()}
    dials = {}
    tokens = re.findall(r"([A-Za-z]+)\s*[=:]?\s*([1-5])", spec)
    for name, val in tokens:
        key = reverse.get(name.upper())
        if not key:
            continue
        dials[key] = int(val)
    if not dials:
        raise ValueError(f"无法解析自定义档位：{spec}")
    full = {k: dials.get(k, 3) for k in DIAL_ORDER}
    return {
        "persona": "自定义",
        "kind": "custom",
        "gist": "手动指定档位",
        "dials": full,
        "steps": [],
    }


def log_dial_change(action: str, state: dict, layer: str) -> None:
    """档位变更历史：set/off 成功落盘后追加一条到 dials-<日期>.jsonl。

    只记动作、人格名、级别、作用范围与会话标识，不记任何命令原文；
    失败静默（遥测不干预主流程）。看板用它算档位存续时长与变更频率。
    """
    try:
        record = {
            "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"),
            "ev": "dial_change",
            "action": action,
            "persona": state.get("persona"),
            "kind": state.get("kind"),
            "level": state.get("level"),
            "scope": layer,
            # 项目层的档位流水带上 proj，看板才能把它归到对应项目下（与工具事件同算法）。
            "proj": proj_id(Path.cwd()) if layer == "project" else None,
            "session": str(current_session_id() or "unknown")[:24],
        }
        append_jsonl(metrics_dir() / f"dials-{datetime.now():%Y-%m-%d}.jsonl", record)
    except Exception:
        pass


def cmd_set(args: argparse.Namespace) -> int:
    args.scope = scope_or_die(getattr(args, "scope", "auto"))
    guard = session_write_guard(args.scope)
    if guard:
        print(guard, file=sys.stderr)
        return 2
    # 写入 scope 规范化：默认 auto = 会话层（守卫已确保有会话标识才走到这里）。
    # 全局层只能显式 --scope global 进来，杜绝「会话内一句 set 波及所有对话」。
    scope = "session" if args.scope == "auto" else args.scope
    data = load_data()
    state = None
    if args.persona and args.persona in ("off", "关闭"):
        args.scope = scope
        return cmd_off(args)
    if args.dials:
        state = parse_custom(args.dials, data)
    elif args.persona:
        state = resolve_persona(args.persona.upper(), data)
        if state is None:
            state = resolve_persona(args.persona, data)
        if state is None:
            print(f"未找到人格或预设：{args.persona}", file=sys.stderr)
            print("可用类型：" + "、".join(data["types"].keys()), file=sys.stderr)
            print("可用预设：" + "、".join(data["presets"].keys()), file=sys.stderr)
            return 2
    else:
        prev = read_state(scope)
        if not prev:
            print("用法：persona.py set <类型|预设> 或 --dials P5,V4,...", file=sys.stderr)
            return 2
        state = prev

    layer, _ = resolve_layer(scope)
    state["level"] = args.level
    state["domain"] = args.domain
    state["scope"] = scope
    state["layer"] = layer
    state["updated_at"] = now_iso()
    path = write_state(state, scope)
    log_dial_change("set", state, layer)
    print(f"已切换：{state['persona']}  档位 {dials_str(state['dials'])}  级别 {state['level']}")
    print(f"作用范围：{LAYER_LABEL[layer]}")
    print(f"状态文件：{path}")
    print()
    print(render_block(state, data))
    return 0


def cmd_off(args: argparse.Namespace) -> int:
    args.scope = scope_or_die(getattr(args, "scope", "auto"))
    guard = session_write_guard(getattr(args, "scope", "auto"))
    if guard:
        print(guard, file=sys.stderr)
        return 2
    scope = getattr(args, "scope", "auto")
    if scope == "auto":
        scope = "session"
    layer, path = resolve_layer(scope)
    prev = read_state(scope) or {"persona": None, "kind": None, "dials": {}}
    prev["level"] = "off"
    prev["scope"] = scope
    prev["layer"] = layer
    prev["updated_at"] = now_iso()
    # 即使原先没有状态文件也显式落一个 off，使「已关闭」成为可审计的状态，
    # 而不是依赖「文件不存在」这种隐式约定——对照实验需要它可被核对。
    write_state(prev, scope)
    log_dial_change("off", prev, layer)
    print(f"人格注入已关闭（作用范围：{LAYER_LABEL[layer]}；状态文件保留，可随时用 set 恢复）：{path}")
    if layer == "global":
        # 关全局的真实后果是「所有没单独设过档位的对话一起失效」，而命令输出用户通常看不到
        # （模型代跑）。这里把它显式喊出来，避免出现「用户以为只关了当前对话」的静默影响面。
        print(
            "注意：本次关的是**全局**档位，所有未单独设会话 / 项目档位的对话都会一起关闭。"
        )
        print("不带 --scope 的 off 只关当前对话；确认要关全局请重跑：persona.py off --scope global")
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    args.scope = scope_or_die(getattr(args, "scope", "auto"))
    layer, path = resolve_layer(args.scope)
    state = read_state(args.scope)
    if not state:
        print("当前未激活任何人设档位。")
        print("启用示例：python persona.py set ESTP")
        print(f"查询层级：{LAYER_LABEL.get(layer, layer)}")
        return 0
    data = load_data()
    print(f"人格：{state.get('persona')}（{state.get('kind')}）")
    print(f"级别：{state.get('level')}   领域偏好：{state.get('domain')}")
    print(f"档位：{dials_str(state.get('dials', {}))}")
    print(f"作用范围：{LAYER_LABEL.get(layer, layer)}")
    print(f"状态文件：{path}")
    print(f"更新：{state.get('updated_at')}")
    print()
    block = render_block(state, data)
    print("—— 每轮注入内容 ——")
    print(block if block else "(当前级别不注入)")
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    """列出各会话的专属档位，并标出当前会话。加 --prune 可清理陈旧档位。"""
    current = current_session_id()
    rows = []
    if HOME_SESSIONS.exists():
        for path in sorted(HOME_SESSIONS.glob("state-*.json")):
            state = None
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
            rows.append((path.stem[len("state-"):], state, path))
    if not rows:
        if args.prune is not None:
            print("没有会话专属档位，无需清理。")
            return 0
        print("还没有任何会话专属档位。")
        print("用法：persona.py set ENTJ   # 默认只影响当前对话；--scope 全局 改默认人格")
        return 0

    if args.prune is not None:
        return prune_sessions(rows, current, args)

    print(f"共 {len(rows)} 个会话专属档位：")
    for sid, state, _path in rows:
        mark = "  ← 当前会话" if sid == current else ""
        if not state:
            print(f"  {sid[:12]}…  (文件无法解析){mark}")
            continue
        print(f"  {sid[:12]}…  {state.get('persona')} / 级别 {state.get('level')} / "
              f"{dials_str(state.get('dials', {}))}  更新 {state.get('updated_at', '?')}{mark}")
    print()
    print(f"目录：{HOME_SESSIONS}")
    print("删掉对应文件即可清理；删除后该会话回落到「项目层 → 全局层」。")
    print(f"批量清理陈旧档位：persona.py sessions --prune {DEFAULT_PRUNE_DAYS}")
    return 0


def prune_sessions(rows, current: str | None, args: argparse.Namespace) -> int:
    """清理 N 天未更新的会话档位文件。

    两条安全设计：① 默认只预览，需 `--apply --write-confirmed` 才真删；
    ② **当前会话的档位一律保留**——否则用户会在一条命令里把自己正在用的档位清掉。
    """
    days = args.prune
    if days <= 0:
        print("--prune 的天数必须大于 0。", file=sys.stderr)
        return 2
    now_ts = datetime.now().timestamp()
    cutoff = now_ts - days * 86400
    directory = HOME_SESSIONS.resolve()
    targets, kept = [], []
    for sid, _state, path in rows:
        # 越界校验：只处理本技能自建的会话目录下的 state-*.json
        if not path.is_file() or path.resolve().parent != directory:
            kept.append((sid, path, "路径越界，已跳过"))
            continue
        if sid == current:
            kept.append((sid, path, "当前会话，保留"))
            continue
        try:
            age_days = (now_ts - path.stat().st_mtime) / 86400
        except Exception:
            kept.append((sid, path, "无法读取修改时间"))
            continue
        if path.stat().st_mtime < cutoff:
            targets.append((sid, path, age_days))
        else:
            kept.append((sid, path, f"{age_days:.1f} 天内活跃"))

    print(f"清理阈值：{days} 天未更新（当前会话保留）")
    print(f"可清理 {len(targets)} 个 / 保留 {len(kept)} 个")
    if targets:
        print("\n将被删除（预览）：")
        for sid, path, age in sorted(targets, key=lambda x: -x[2]):
            print(f"  {sid[:12]}…  静置 {age:.1f} 天  {path.name}")
    if kept:
        print("\n保留：")
        for sid, _path, reason in kept:
            print(f"  {sid[:12]}…  {reason}")
    if not targets:
        print("\n没有需要清理的文件。")
        return 0
    if not args.apply:
        print(f"\n共 {len(targets)} 个文件，均位于 {HOME_SESSIONS}")
        print("确认后加 --apply --write-confirmed 执行。")
        return 0
    if not args.write_confirmed:
        print("缺少 --write-confirmed，已停止。", file=sys.stderr)
        return 2
    for _sid, path, _age in targets:
        try:
            path.unlink()
        except Exception as exc:
            print(f"删除失败：{path}（{exc}）", file=sys.stderr)
            return 2
    print(f"\n已删除 {len(targets)} 个会话档位文件；这些会话回落到「项目层 → 全局层」。")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    data = load_data()
    print("预设：")
    for name, entry in data["presets"].items():
        print(f"  {name}  {dials_str(entry['dials'])}  {entry['gist']}")
    print()
    print("16 型：")
    for name, entry in data["types"].items():
        print(f"  {name}  {dials_str(entry['dials'])}  {entry['gist']}")
    return 0


def cmd_dials(args: argparse.Namespace) -> int:
    data = load_data()
    for key in DIAL_ORDER:
        meta = data["dials"][key]
        print(f"{DIAL_SHORT[key]} {meta['label']}（{meta['ask']}）")
        for value in ["5", "4", "3", "2", "1"]:
            print(f"    {value}  {meta['levels'][value]}")
    return 0


def cmd_domains(args: argparse.Namespace) -> int:
    for name in DOMAINS:
        path = domain_file(name)
        mark = "存在" if path.exists() else "缺失"
        print(f"  {name:<15} {mark}  {path}")
    print()
    print("auto：由当前任务自动判断领域，不写死。")
    return 0


def cmd_rules(args: argparse.Namespace) -> int:
    scope = guard_scope()
    detail = (
        "命令 + 路径 + 文本内容字段"
        if scope == "all"
        else "命令 + 路径字段（已跳过文本内容字段）"
    )
    print("安全底线规则（PreToolUse 命中即强制询问，任何档位都不豁免）：")
    print(f"当前扫描范围：{scope} —— {detail}")
    print(f"  切换：设环境变量 {GUARD_SCOPE_ENV}={GUARD_SCOPE_DEFAULT}|exec，")
    print("  且必须是系统级 / 用户级变量（只在某个终端里 export 不会传给钩子进程），")
    print("  改完需重启 DSH。")
    for category, patterns in GUARD_RULES.items():
        print(f"\n[{category}]")
        for pat in patterns:
            print(f"    {pat}")
    return 0


def guard_check(tool_name: str, tool_input: dict) -> tuple[str, str] | None:
    """命中安全底线时返回 (类别, 命中文本)。

    扫描哪些字段由 PERSONA_GUARD_SCOPE 决定，见 GUARD_FIELD_KEYS 的注释。
    路径字段在所有范围下都参与扫描。
    """
    haystacks = []
    if isinstance(tool_input, dict):
        keys = list(GUARD_FIELD_KEYS[guard_scope()]) + list(GUARD_PATH_KEYS)
        for key in keys:
            value = tool_input.get(key)
            if isinstance(value, str):
                haystacks.append(value)
    haystack = "\n".join(haystacks)
    if not haystack:
        return None
    lowered = haystack.lower()
    for category, patterns in GUARD_RULES.items():
        for pat in patterns:
            if re.search(pat, lowered):
                return category, pat
    return None


def metrics_dir() -> Path:
    """埋点目录：默认 `$DSH_HOME/persona/metrics`（即 `~/.dsh/persona/metrics`），
    可用 `PERSONA_METRICS_DIR` 旁路。

    旁路是**仪器专用缝**（v1.5.32，还「埋点全局共享」债）：doctor / hook_selftest
    必须在真实 HOME 下探活（读真实 hook 配置才算数），但探针会真实写埋点——
    回归套件据此把埋点重定向到临时目录，根除「跑回归即污染真实埋点」。
    生产环境不设该变量，行为与旧版逐字节一致；读路径（report/compact/reset）
    同样生效，测试夹具用它指向自建的隔离目录。
    """
    override = (os.environ.get("PERSONA_METRICS_DIR") or "").strip()
    path = Path(override) if override else HOME_METRICS
    path.mkdir(parents=True, exist_ok=True)
    return path


LOCK_TIMEOUT = 5.0


def _lock_fd(fd: int) -> None:
    """对已打开的文件描述符加排他锁；等不到就抛异常。

    Windows 上不用 msvcrt.LK_LOCK：它的内置重试是「10 次 × 1 秒」，而并发窗口只有
    毫秒级——粒度太粗，进程一多就会直接失败。这里改用 LK_NBLCK 自己控节奏：
    5ms 一探、最多等 LOCK_TIMEOUT 秒。POSIX 侧 flock(LOCK_EX) 由内核排队，直接阻塞等待即可。

    ⚠️ **调用方拿到异常后不得无锁写共享文件**——那正是 v1.5.1 之前丢行的根因。
    正确处置见 `append_jsonl` / `cmd_compact_metrics`：改写到本进程专属分片，或干脆放弃。
    """
    if os.name != "nt":
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)
        return

    import msvcrt

    deadline = time.monotonic() + LOCK_TIMEOUT
    while True:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.005)


def _unlock_fd(fd: int) -> None:
    """释放 _lock_fd 加的锁。"""
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _write_all(fd: int, data: bytes) -> None:
    """写完全部字节再返回。

    常规文件通常是「一次写完或直接报错」，但 `os.write` 允许部分写。只要发生一次
    部分写，落盘的就是半行——本技能对外声明「撕裂行恒为 0」，所以这里显式循环，
    把这个口子堵死。调用方需保证已持锁（锁覆盖整个循环）。
    """
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        if not n:
            raise OSError("write 返回 0，已中止")
        view = view[n:]


SHARD_SUFFIX = ".shard-"


def shard_path(path: Path, pid: int | None = None) -> Path:
    """本进程专属的溢出分片路径。

    分片只由**创建它的那一个进程**写，所以它不需要锁，也就不存在「拿不到锁」这一
    失败模式。这是全模块唯一允许的无锁写目标。
    """
    return path.with_name(f"{path.name}{SHARD_SUFFIX}{os.getpid() if pid is None else pid}")


def metrics_files(directory: Path) -> list[Path]:
    """列出埋点目录下的全部数据文件：主文件 + 溢出分片。

    分片名形如 `xxx.jsonl.shard-12345`，紧随其主文件排序。**读取端一律走本函数**，
    不要直接 glob `*.jsonl`——那会静默漏掉溢出期间的记录（看起来像"数据丢了"，
    其实只是没读）。
    """
    found = list(directory.glob("*.jsonl"))
    found += list(directory.glob(f"*.jsonl{SHARD_SUFFIX}*"))
    return sorted(found)


def append_jsonl(path: Path, record: dict) -> None:
    """并发安全地追加一行 JSONL。

    PostToolUse 钩子会被并行工具调用并发触发，多个短命进程同时往同一个
    埋点文件追加。实测（12 进程 × 25 行）在 Windows 上：

        open("a").write()            262 行，1 处撕裂
        O_APPEND + os.write          258 行，1 处撕裂
        O_APPEND + 文件锁（拿到锁时）  300 行，0 处撕裂

    Windows 上 O_APPEND 并不保证原子追加：`_write` 内部是「定位到 EOF → 写入」两步，
    两个进程交错就会互相覆盖。所以共享文件**必须持锁写**。

    ⚠️ **绝不能「拿不到锁就无锁写」**——这正是 v1.5.1 之前残留丢行的根因：锁等待超过
    `LOCK_TIMEOUT` 后 `_lock_fd` 抛异常，被 `except: pass` 静默吞掉，于是退化为无锁写，
    与持锁进程竞态。症状是**丢整行、撕裂恒为 0**（双方各自覆盖对方），与实测的
    「总数少 1–2、撕裂 0」完全一致。因为触发条件是锁等待异常变长（负载尖峰、杀毒扫描），
    它间歇出现、极难复现：同一份代码曾观测到「19 次里 8 次失败」与「连续 16 次全绿」。

    现在的兜底改为**写本进程专属分片**：分片无第二个写者，记录不可能被覆盖，
    丢行从「概率性」变成「结构上不可能」。分片由 `metrics_files()` 在读取端合并，
    由 `compact-metrics` 折回主文件。

    任何失败仍然静默吞掉：埋点属于旁路观测，绝不允许影响主流程。
    """
    try:
        data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    except Exception:
        return

    fd = None
    locked = False
    try:
        try:
            fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        except Exception:
            fd = None
        if fd is not None:
            try:
                _lock_fd(fd)
                locked = True
            except Exception:
                locked = False
            if locked:
                try:
                    _write_all(fd, data)
                    return
                except Exception:
                    pass  # 写失败同样走分片，绝不无锁重写共享文件
    finally:
        if fd is not None:
            if locked:
                try:
                    _unlock_fd(fd)
                except Exception:
                    pass
            try:
                os.close(fd)
            except Exception:
                pass

    # 兜底：写本进程专属分片。这里没有第二个写者，记录不会被任何进程覆盖。
    try:
        sfd = os.open(shard_path(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            _write_all(sfd, data)
        finally:
            os.close(sfd)
    except Exception:
        pass


def cmd_report(args: argparse.Namespace) -> int:
    files = metrics_files(metrics_dir())
    rows: dict[tuple[str, str], dict] = {}
    injections = 0
    by_proj: dict[str, int] = {}
    inj_by_layer: dict[str, int] = {}
    for path in files:
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if not rec.get("tool"):
                        # 注入日志（injections-*.jsonl）不是工具调用，单独计数。
                        if "chars" in rec:
                            injections += 1
                            lay = rec.get("layer") or "未知"
                            inj_by_layer[lay] = inj_by_layer.get(lay, 0) + 1
                        continue
                    if rec.get("proj") is not None:
                        pj = rec.get("proj") or "无cwd"
                        by_proj[pj] = by_proj.get(pj, 0) + 1
                    key = (rec.get("persona") or "-", rec.get("session") or "unknown")
                    bucket = rows.setdefault(
                        key,
                        {"calls": 0, "tests": 0, "static": 0, "writes": 0, "reads": 0},
                    )
                    bucket["calls"] += 1
                    for flag, field in (
                        ("test", "tests"),
                        ("static", "static"),
                        ("write", "writes"),
                        ("read", "reads"),
                    ):
                        if rec.get(flag):
                            bucket[field] += 1
        except Exception:
            continue

    if not rows:
        print("暂无埋点数据。启用 PostToolUse hook 后，每次工具调用都会记录一行指标。")
        print(f"指标目录：{metrics_dir()}")
        return 0

    if args.json:
        payload = [
            {"persona": k[0], "session": k[1], **v} for k, v in sorted(rows.items())
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print("按人格与会话汇总（用于对照实验）：")
    print(f"{'人格':<12}{'会话':<22}{'调用':>6}{'测试':>6}{'静态':>6}{'写':>6}{'读':>6}")
    for (persona, session), v in sorted(rows.items()):
        print(
            f"{persona:<12}{session[:20]:<22}{v['calls']:>6}{v['tests']:>6}"
            f"{v['static']:>6}{v['writes']:>6}{v['reads']:>6}"
        )
    print()
    by_persona: dict[str, dict] = {}
    for (persona, _), v in rows.items():
        acc = by_persona.setdefault(
            persona, {"calls": 0, "tests": 0, "static": 0, "writes": 0, "sessions": 0}
        )
        acc["sessions"] += 1
        for field in ("calls", "tests", "static", "writes"):
            acc[field] += v[field]
    print("按人格合并：")
    for persona, acc in sorted(by_persona.items()):
        ratio = acc["tests"] / acc["calls"] if acc["calls"] else 0
        print(
            f"  {persona:<12} 会话 {acc['sessions']:>2}  调用 {acc['calls']:>4}  "
            f"测试 {acc['tests']:>3}  静态 {acc['static']:>3}  写 {acc['writes']:>3}  "
            f"测试/调用 {ratio:.2f}"
        )
    print()
    if injections:
        print(f"每轮注入记录：{injections} 次（证明档位确实被逐轮注入到上下文）")
        if inj_by_layer:
            print("  注入按层：" + "  ".join(
                f"{k} {v}" for k, v in sorted(inj_by_layer.items(), key=lambda kv: -kv[1])))
    else:
        print("每轮注入记录：0 次（档位可能为 off，或钩子未生效）")
    # 三层归属（v1.5.33 起记录带 proj 字段；更早的记录没有该字段，如实计入「旧记录」）。
    total_proj = sum(by_proj.values())
    if total_proj:
        print(f"事件按项目归属：共 {total_proj} 条有项目键（旧记录无此字段不计入）；"
              + "  ".join(f"{k[:8]} {v}" for k, v in
                           sorted(by_proj.items(), key=lambda kv: -kv[1])[:5]))
    else:
        print("事件按项目归属：暂无（proj 字段自 v1.5.33 起记录）")
    return 0


def cmd_reset_metrics(args: argparse.Namespace) -> int:
    directory = metrics_dir()
    # 只清理本技能自建的埋点目录下的数据文件（含溢出分片），路径越界一律拒绝。
    targets = [
        path
        for path in metrics_files(directory)
        if path.is_file() and path.resolve().parent == directory.resolve()
    ]
    if not targets:
        print("没有需要清理的埋点文件。")
        return 0
    if not args.apply:
        print("以下文件将被删除（预览，未执行）：")
        for path in targets:
            print(f"  {path}")
        print(f"\n共 {len(targets)} 个文件，均位于 {directory}")
        print("确认后加 --apply --write-confirmed 执行。")
        return 0
    if not args.write_confirmed:
        print("缺少 --write-confirmed，已停止。", file=sys.stderr)
        return 2
    for path in targets:
        path.unlink()
    print(f"已删除 {len(targets)} 个埋点文件。")
    return 0


def cmd_compact_metrics(args: argparse.Namespace) -> int:
    """把溢出分片折回主文件，再删掉分片。

    只合并 `--min-age` 秒（默认 1 小时）内未变动的分片：正在运行的写进程可能还在
    追加它，提前搬走会与写入交错。超过这个时长的分片，写它的那个短命进程早已退出。

    合并时同样**必须持锁**——这是上一版丢行缺陷的教训：合并本身也是一次追加，
    拿不到锁就该放弃并保留分片，而不是无锁写。
    """
    directory = metrics_dir()
    now = time.time()
    pairs: list[tuple[Path, Path]] = []
    for shard in sorted(directory.glob(f"*.jsonl{SHARD_SUFFIX}*")):
        if not shard.is_file() or shard.resolve().parent != directory.resolve():
            continue
        try:
            if now - shard.stat().st_mtime < args.min_age:
                continue
        except OSError:
            continue
        main = shard.with_name(shard.name.split(SHARD_SUFFIX, 1)[0])
        pairs.append((shard, main))

    if not pairs:
        print(f"没有可合并的溢出分片（只合并 {args.min_age} 秒未变动的分片）。")
        return 0
    if not args.apply:
        print("以下分片将被折回主文件（预览，未执行）：")
        for shard, main in pairs:
            print(f"  {shard.name}  ->  {main.name}")
        print(f"\n共 {len(pairs)} 个分片，均位于 {directory}")
        print("确认后加 --apply --write-confirmed 执行。")
        return 0
    if not args.write_confirmed:
        print("缺少 --write-confirmed，已停止。", file=sys.stderr)
        return 2

    merged = 0
    for shard, main in pairs:
        try:
            payload = shard.read_bytes()
        except OSError as exc:
            print(f"读取失败，跳过 {shard.name}：{exc}", file=sys.stderr)
            continue
        fd = None
        locked = False
        ok = False
        try:
            fd = os.open(main, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
            try:
                _lock_fd(fd)
                locked = True
            except Exception:
                locked = False
            if locked:
                try:
                    _write_all(fd, payload)
                    ok = True
                except Exception as exc:
                    print(f"合并失败，跳过 {shard.name}：{exc}", file=sys.stderr)
        except Exception as exc:
            print(f"合并失败，跳过 {shard.name}：{exc}", file=sys.stderr)
        finally:
            if fd is not None:
                if locked:
                    try:
                        _unlock_fd(fd)
                    except Exception:
                        pass
                try:
                    os.close(fd)
                except Exception:
                    pass
        if not ok:
            if not locked:
                print(f"未取得锁，跳过 {shard.name}：记录仍在分片里，稍后重试即可。",
                      file=sys.stderr)
            continue
        try:
            shard.unlink()
            merged += 1
        except OSError as exc:
            # 合并已经成功，只是删不掉。把分片清空，保证重跑本命令不会把同一批记录
            # 再算一遍——否则「删除失败」会静默变成「读端重复计数」。
            try:
                shard.write_bytes(b"")
                merged += 1
                print(f"已合并 {shard.name}，但未能删除；已清空以免重跑时重复计入：{exc}",
                      file=sys.stderr)
            except OSError as exc2:
                print(f"**注意** {shard.name} 已合并、但既删不掉也清不掉，"
                      f"重跑本命令可能重复计入：{exc} / {exc2}", file=sys.stderr)
    print(f"已合并 {merged} / {len(pairs)} 个分片。")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    args.scope = scope_or_die(getattr(args, "scope", "auto"))
    state = read_state(args.scope)
    if not state:
        print("当前未激活人设档位，无法导出。", file=sys.stderr)
        return 2
    data = load_data()
    block = render_block(state, data) or "(当前级别不注入)"
    card = "\n".join(
        [
            "# 人格档位配置卡",
            "",
            "把下面整段粘贴到其他 AI 工具的系统提示或项目规则里即可生效。",
            "",
            "```text",
            block,
            "```",
            "",
            "## 冲突优先级（从高到低）",
            "1. 安全底线（删除/覆盖、支付、鉴权、密钥、生产变更、不可逆操作）——任何档位都不豁免，一律切回高验证并先确认",
            "2. 验证下限（不随档位下降）：改什么至少验什么；引入新行为、修复缺陷时按领域红线取最严档",
            "3. 当前任务里用户给出的显式指令",
            "4. 其他技能或项目规则中的强制要求",
            "5. 上面这张档位表（只在前四层没有覆盖的维度上生效）",
            "",
        ]
    )
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(card, encoding="utf-8")
        print(f"已写入：{out}")
    else:
        print(card)
    return 0


def hook_config_payload() -> list[tuple[str, list[str]]]:
    """返回 [(事件, 命令)]，命令已用当前解释器与绝对路径组装。

    事件集合按 DSH 的 Claude Code 桥接（@deepseek-ai/dsh-hooks-claude-code）取值：
    可注入的是 SessionStart / UserPromptSubmit，守卫是 PreToolUse，度量是 PostToolUse。
    **不含上下文压缩前事件**——DSH 桥接不支持它，注册了也只会被静默忽略。
    """
    python = sys.executable or "python"
    hook = str(HOOK_SCRIPT).replace("\\", "/")
    py = str(python).replace("\\", "/")
    rows = [
        ("SessionStart", f'"{py}" "{hook}" --event sessionstart'),
        ("UserPromptSubmit", f'"{py}" "{hook}" --event prompt'),
        (
            "PreToolUse",
            f'"{py}" "{hook}" --event pretool',
        ),
        ("PostToolUse", f'"{py}" "{hook}" --event posttool'),
    ]
    return rows


# 钩子按用途分三组。对照实验要求「度量恒定在场、只变处理变量」，
# 所以必须能按组安装——否则对照组没有 PostToolUse，就一条数据都收不到。
MEASUREMENT_EVENTS = ("PostToolUse",)
INJECTION_EVENTS = ("SessionStart", "UserPromptSubmit")
GUARD_EVENTS = ("PreToolUse",)
HOOK_GROUPS = {
    "measurement": MEASUREMENT_EVENTS,
    "injection": INJECTION_EVENTS,
    "guard": GUARD_EVENTS,
}


def resolve_events(spec: str | None) -> set[str]:
    """把 --events 的取值解析成事件名集合。支持组名（measurement/injection/guard/all）与事件名混写。"""
    if not spec:
        return {e for row in hook_config_payload() for e in (row[0],)}
    known = {row[0] for row in hook_config_payload()}
    wanted: set[str] = set()
    for token in (t.strip() for t in spec.split(",")):
        if not token:
            continue
        if token == "all":
            wanted |= known
        elif token in HOOK_GROUPS:
            wanted |= set(HOOK_GROUPS[token])
        elif token in known:
            wanted.add(token)
        else:
            raise ValueError(f"未知的钩子事件或组名：{token}")
    return wanted


def build_hooks_object(events: set[str] | None = None) -> dict:
    """生成 Claude Code 事件表（**裸事件映射**，不套 `{"hooks": ...}` 外壳）。

    DSH 的 @deepseek-ai/dsh-hooks-claude-code 桥接两种形态都认，但裸映射更直观，
    也少一层嵌套；本函数与 cmd_hook_config 一律输出裸映射。
    """
    rows = hook_config_payload()
    hooks: dict[str, list] = {}
    for event, command in rows:
        if events is not None and event not in events:
            continue
        matcher = "*"
        if event == "PreToolUse" or event == "PostToolUse":
            matcher = "*"
        elif event == "SessionStart":
            matcher = "startup|resume|clear|compact"
        hooks[event] = [
            {"matcher": matcher, "hooks": [{"type": "command", "command": command, "timeout": 15}]}
        ]
    return hooks


def cmd_hook_config(args: argparse.Namespace) -> int:
    payload = build_hooks_object()
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"已写入：{out}")
    else:
        print(text)
    return 0


# 桥接钩子配置文件的默认位置（DSH 无「settings.json + hooks 键」这一形态：
# 主投递是已安装的 DSH 插件包，本文件是兼容投递，由桥接插件读取）。
DEFAULT_HOOKS_FILE = HOME_ROOT / "hooks.json"


def unwrap_hooks(data) -> dict:
    """从钩子配置文件里读出**事件表**。

    DSH 的 @deepseek-ai/dsh-hooks-claude-code 同时接受裸事件映射与
    `{"hooks": {...}}` 外壳，这里两者都认，便于接管历史文件。
    """
    if not isinstance(data, dict):
        return {}
    inner = data.get("hooks")
    if isinstance(inner, dict):
        return dict(inner)
    return dict(data)


def cordis_row(hooks_path: Path) -> str:
    """挂载桥接插件的 cordis.patch.yml 行（只打印，不代写用户配置）。"""
    return "\n".join(
        [
            "- insert:",
            "    - id: persona-driven-hooks",
            "      name: '@deepseek-ai/dsh-hooks-claude-code'",
            "      config:",
            f"        configPath: {hooks_path}",
        ]
    )


def cmd_install_hooks(args: argparse.Namespace) -> int:
    settings = Path(args.settings).expanduser() if args.settings else DEFAULT_HOOKS_FILE
    try:
        events = resolve_events(args.events)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    incoming = build_hooks_object(events)

    if settings.exists():
        try:
            with settings.open("r", encoding="utf-8") as fh:
                current = unwrap_hooks(json.load(fh))
        except Exception as exc:
            print(f"无法解析现有配置文件：{exc}", file=sys.stderr)
            return 2
        backup = HOME_ROOT / "backup" / f"hooks.json.{datetime.now():%Y%m%d_%H%M%S}.bak"
    else:
        current = {}
        backup = None

    merged = dict(current)
    added, skipped = [], []
    for event, entries in incoming.items():
        existing = merged.setdefault(event, [])
        for entry in entries:
            command = entry["hooks"][0]["command"]
            already = any(
                command == h.get("command")
                for e in existing
                for h in (e.get("hooks") or [])
            )
            if already:
                skipped.append(f"{event}")
            else:
                existing.append(entry)
                added.append(f"{event}")

    print("## hooks 安装计划")
    print(f"- 配置文件：{settings}")
    print(f"- 文件存在：{'是' if settings.exists() else '否（将新建）'}")
    print(f"- 备份位置：{backup if backup else '不适用（新建文件）'}")
    print(f"- 新增事件：{'、'.join(added) if added else '无'}")
    print(f"- 已存在跳过：{'、'.join(sorted(set(skipped))) if skipped else '无'}")
    print("- 影响：在每次会话启动、用户提交、工具调用前后各执行一个本地 Python 钩子")
    print("- 回滚：把备份文件复制回原路径，或删除配置文件中新增的事件项")
    print("- 生效条件：重启 DSH 后生效（配置不会热加载）")
    print()
    print("## 在 DSH 里启用这份配置（本脚本不代写 YAML）")
    print(f"把下面这段加到 $DSH_HOME/cordis.patch.yml（或对应 profile 的 "
          f"cordis.patch.yml）后重启 DSH：")
    print()
    print(cordis_row(settings))
    print()

    if not args.apply:
        print("以上为预览，未写入任何文件。确认后加 --apply --write-confirmed 执行。")
        return 0
    if not args.write_confirmed:
        print("缺少 --write-confirmed，已停止。", file=sys.stderr)
        return 2

    if backup:
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(settings, backup)
        print(f"已备份：{backup}")
    settings.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(merged, fh, ensure_ascii=False, indent=2)
    tmp.replace(settings)
    print(f"已写入：{settings}")
    print("请重启 DSH 使桥接钩子生效（并确认 cordis.patch.yml 已挂载 "
          "@deepseek-ai/dsh-hooks-claude-code）。")
    return 0


def _is_own_hook(entry: dict) -> bool:
    """只认领本技能自己的钩子：命令里同时出现 hook_persona.py 与 persona-driven。"""
    for h in entry.get("hooks") or []:
        cmd = str(h.get("command") or "").replace("\\", "/")
        if "hook_persona.py" in cmd and "persona-driven" in cmd:
            return True
    return False


def cmd_uninstall_hooks(args: argparse.Namespace) -> int:
    settings = Path(args.settings).expanduser() if args.settings else DEFAULT_HOOKS_FILE
    if not settings.exists():
        print(f"配置文件不存在，无需卸载：{settings}")
        return 0

    try:
        with settings.open("r", encoding="utf-8") as fh:
            current = unwrap_hooks(json.load(fh))
    except Exception as exc:
        print(f"无法解析现有配置文件：{exc}", file=sys.stderr)
        return 2

    hooks = dict(current)
    try:
        scope = resolve_events(args.events)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    removed: list[str] = []
    kept: list[str] = []
    for event in list(hooks.keys()):
        entries = hooks.get(event) or []
        if event not in scope:
            if any(_is_own_hook(e) for e in entries):
                kept.append(f"{event}(保留)")
            continue
        mine = [e for e in entries if _is_own_hook(e)]
        others = [e for e in entries if not _is_own_hook(e)]
        if mine:
            removed.append(f"{event}({len(mine)})")
        if others:
            hooks[event] = others
            kept.append(f"{event}({len(others)})")
        else:
            hooks.pop(event, None)

    changed = hooks

    backup = HOME_ROOT / "backup" / f"hooks.json.{datetime.now():%Y%m%d_%H%M%S}.bak"

    print("## hooks 卸载计划")
    print(f"- 配置文件：{settings}")
    print(f"- 将移除：{'、'.join(removed) if removed else '无（未发现本技能的钩子）'}")
    print(f"- 将保留：{'、'.join(kept) if kept else '无（其他来源的 hooks）'}")
    print("- 其余配置项：全部保留（仅改动本技能认领的事件项）")
    print(f"- 备份位置：{backup}")
    print("- 影响：卸载后 L3 硬约束失效，退化为 L1/L2；档位状态文件保留，可随时重装")
    print()

    if not removed:
        print("无需改动，已停止。")
        return 0
    if not args.apply:
        print("以上为预览，未写入任何文件。确认后加 --apply --write-confirmed 执行。")
        return 0
    if not args.write_confirmed:
        print("缺少 --write-confirmed，已停止。", file=sys.stderr)
        return 2

    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(settings, backup)
    print(f"已备份：{backup}")
    tmp = settings.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(changed, fh, ensure_ascii=False, indent=2)
    tmp.replace(settings)
    print(f"已写入：{settings}")
    print("请重启 DSH 使卸载生效。")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import check_environment as ce
    except Exception as exc:
        print(f"环境检查脚本不可用：{exc}", file=sys.stderr)
        return 2
    result = ce.check()
    print(ce.render(result))
    return 0 if result["status"] in ("ready", "partial") else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="persona.py", description="人格档位状态机与命令行入口"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_set = sub.add_parser("set", help="设置当前人格档位")
    p_set.add_argument("persona", nargs="?", help="类型代码（如 ESTP）或预设名（如 极速）")
    p_set.add_argument("--dials", help="自定义档位，如 P5,V4,R5,S4,D5,E1,ST4,W2")
    p_set.add_argument("--level", choices=["l3", "l2", "off"], default="l3")
    p_set.add_argument("--domain", default="auto", help="领域偏好，默认 auto")
    p_set.add_argument(
        "--scope", default="auto", metavar="SCOPE",
        help="作用范围：会话 / 项目 / 全局 / 自动（或 session/project/global/auto）；"
             "默认自动=只写当前会话，改默认人格须显式 --scope 全局")
    p_set.set_defaults(func=cmd_set)

    p_get = sub.add_parser("get", help="查看当前档位与注入内容")
    p_get.add_argument(
        "--scope", default="auto", metavar="SCOPE",
        help="作用范围：自动 / 会话 / 项目 / 全局（或 auto/session/project/global）；默认自动=三层取最高生效者")
    p_get.set_defaults(func=cmd_get)

    p_off = sub.add_parser("off", help="关闭人格注入（默认只关当前会话）")
    p_off.add_argument(
        "--scope", default="auto", metavar="SCOPE",
        help="作用范围：会话 / 项目 / 全局 / 自动（或 session/project/global/auto）；"
             "默认自动=只关当前会话，关全局默认人格须显式 --scope 全局")
    p_off.set_defaults(func=cmd_off)

    sub.add_parser("list", help="列出全部类型与预设").set_defaults(func=cmd_list)
    sub.add_parser("dials", help="列出 8 颗旋钮的档位定义").set_defaults(func=cmd_dials)
    sub.add_parser("domains", help="列出领域映射文件").set_defaults(func=cmd_domains)
    sub.add_parser("rules", help="列出安全底线规则").set_defaults(func=cmd_rules)
    p_sessions = sub.add_parser("sessions", help="列出各会话的专属档位；--prune 清理陈旧档位")
    p_sessions.add_argument(
        "--prune",
        type=int,
        metavar="N",
        default=None,
        help=f"清理 N 天未更新的会话档位文件（默认只预览，N 建议 {DEFAULT_PRUNE_DAYS}）",
    )
    p_sessions.add_argument("--apply", action="store_true", help="执行删除（需配合 --write-confirmed）")
    p_sessions.add_argument("--write-confirmed", action="store_true", help="确认写盘，防止误触发")
    p_sessions.set_defaults(func=cmd_sessions)

    p_report = sub.add_parser("report", help="汇总埋点数据")
    p_report.add_argument("--json", action="store_true")
    p_report.set_defaults(func=cmd_report)

    p_reset = sub.add_parser("reset-metrics", help="清理埋点数据")
    p_reset.add_argument("--apply", action="store_true")
    p_reset.add_argument("--write-confirmed", action="store_true")
    p_reset.set_defaults(func=cmd_reset_metrics)

    p_compact = sub.add_parser("compact-metrics", help="把溢出分片折回主埋点文件")
    p_compact.add_argument(
        "--min-age",
        type=int,
        default=DEFAULT_COMPACT_MIN_AGE,
        metavar="SEC",
        help=f"只合并 SEC 秒未变动的分片（默认 {DEFAULT_COMPACT_MIN_AGE}）",
    )
    p_compact.add_argument("--apply", action="store_true", help="执行合并（需配合 --write-confirmed）")
    p_compact.add_argument("--write-confirmed", action="store_true", help="确认写盘，防止误触发")
    p_compact.set_defaults(func=cmd_compact_metrics)

    p_export = sub.add_parser("export", help="导出可粘贴到其他工具的配置卡")
    p_export.add_argument(
        "--scope", default="auto", metavar="SCOPE",
        help="作用范围：自动 / 会话 / 项目 / 全局（或 auto/session/project/global）")
    p_export.add_argument("--out", help="输出文件路径，省略则打印到终端")
    p_export.set_defaults(func=cmd_export)

    p_hook = sub.add_parser("hook-config", help="生成 hooks 配置片段")
    p_hook.add_argument("--out", help="输出文件路径，省略则打印到终端")
    p_hook.set_defaults(func=cmd_hook_config)

    p_install = sub.add_parser("install-hooks", help="把 hooks 合并进钩子配置文件（hooks.json）")
    p_install.add_argument(
        "--settings",
        help="hook 配置文件路径，默认 $DSH_HOME/persona/hooks.json",
    )
    p_install.add_argument(
        "--events",
        help="只安装指定事件或组：measurement / injection / guard / all，逗号分隔，默认 all",
    )
    p_install.add_argument("--apply", action="store_true")
    p_install.add_argument("--write-confirmed", action="store_true")
    p_install.set_defaults(func=cmd_install_hooks)

    p_uninstall = sub.add_parser("uninstall-hooks", help="从钩子配置文件移除本技能的 hooks")
    p_uninstall.add_argument(
        "--settings",
        help="hook 配置文件路径，默认 $DSH_HOME/persona/hooks.json",
    )
    p_uninstall.add_argument(
        "--events",
        help="只卸载指定事件或组：measurement / injection / guard / all，逗号分隔，默认 all",
    )
    p_uninstall.add_argument("--apply", action="store_true", help="真正写入（缺省仅预览）")
    p_uninstall.add_argument("--write-confirmed", action="store_true", help="确认写盘，防止误触发")
    p_uninstall.set_defaults(func=cmd_uninstall_hooks)

    sub.add_parser("doctor", help="检查运行环境与钩子状态").set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
