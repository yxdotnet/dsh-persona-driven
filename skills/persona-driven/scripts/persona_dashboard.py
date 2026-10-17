# -*- coding: utf-8 -*-
"""persona-driven 本机数据看板生成器。

只读 $DSH_HOME/persona/metrics（默认 ~/.dsh/persona/metrics）（+state.json），
聚合为单文件 HTML 看板。不写任何埋点、不改安装位。数据披露原则：
  - 回归仪器会话（reg*/unknown）硬排除；
  - 统计范围限定为「最新活跃的前 RECENT_SESSIONS 个会话」（按最后事件日期）；
  - 大型会话（>=LARGE_EVENTS 事件）照常计入（不提供剔除开关）；
  - 埋点没有的字段（触发词文本、文件路径、危险操作）在看板上标「无数据」，不编造；
  - proj/layer 字段 1.5.33 起才有，旧数据缺字段归「未归属」，看板如实分层展示。

环境变量：
  DSH_HOME                DSH 主目录，默认 ~/.dsh
  PERSONA_PROJECT_ROOTS   项目级档位扫描根目录，多根用 os.pathsep 分隔
                          （Windows「;」/ 类 Unix「:」）；未设置时只扫当前
                          工作目录——在哪个项目里生成就识别哪个项目。
  PERSONA_DASHBOARD_OUT   输出 HTML 路径；默认写到当前目录 persona-dashboard.html。
"""
import sys

# 本模块要 import check_environment（复用「插件是否装入 profile」的判定，不另抄一份）。
# 先关掉字节码缓存，免得在技能目录里留下 __pycache__——persona.py 自己也设了同一个开关，
# 但那是在它被导入之后才生效的，拦不住它自己的 .pyc。__main__ 脚本本身不生成缓存。
sys.dont_write_bytecode = True

import json
import os
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

import hook_persona as hp

# 有效类别的判定分组。历史埋点里同一语义可能有两个名字，判定处一律同时接受。
SHELL_CLASSES = ("shell", "bash")
EXPLORE_CLASSES = ("grep", "glob", "search")
WRITE_CLASSES = ("write", "edit")


def effective_class(e: dict) -> str:
    """一条工具事件的**有效类别**（规范词表 + 历史兼容）。

    规范来源是 `lib/persona-core.js` 与 `hook_persona.py` 的 `TOOL_ALIASES`。
    这里额外做一层历史兼容，**否则修复前的数据会永远显示为 0**：

      1. `tool == "other"` 但带 `raw_tool` 的记录，用原始工具名重新归类。这条救的是
         最主要的一批：DSH 的小写工具名（`read`/`write`/`edit`/`grep`/`glob`）当时不在
         词表里，读/改调用整批落成了 `other`（实测 94 行）。
      2. 类别名兼容：JS 写入端曾用过 `bash` / `search` / `subagent`。`search` 是被合并
         过的搜索类，**无法再区分 grep 与 glob**——只能按「搜索类」计入探查，不猜具体是哪个。
    """
    tool = e.get("tool") or ""
    if tool == "other" and e.get("raw_tool"):
        return hp.normalize_tool(str(e["raw_tool"]))
    return tool


# 工具族的名字前缀（DSH 的工具生态比 WorkBuddy 宽得多：MCP、blender_rt_*、内置的
# skill / job_*…）。**只用已落盘的字段归类，不新增埋点。**
FAMILY_PREFIXES = (
    ("mcp__", "MCP"),
    ("blender_rt_", "Blender"),
    ("skill", "技能"),
    ("job_", "后台作业"),
    # 计划与待办：这两个是「结构约束」旋钮的行为代理指标——用了多少计划/待办。
    # 工具名取自 DSH 自身（dsh-tool-todo 注册的是 todo_write；exit_plan_mode 是内置工具）。
    ("todo_write", "待办"),
    ("exit_plan", "计划模式"),
    ("enter_plan", "计划模式"),
)


def parse_ts(value) -> "datetime | None":
    """容错解析埋点时间戳。

    **必须真解析，不能比字符串**：档位记录由 `persona.py` 写成 `+08:00` 形式，
    而插件用 `isoMillis()` 写成 `Z` 形式。两种写法逐字符比较会得到错误的大小关系，
    所以凡是「按时间切分」的地方都走这里。
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except Exception:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def tool_family(e: dict) -> str:
    """工具族。

    **`_cls`（有效类别）优先**：归类成功的都是 DSH 内置工具。历史记录里 `raw_tool` 与
    `_cls` 会同时存在（旧写入端漏了词表、读取端事后回溯修正），此时若先看 `raw_tool`，
    会把已归类的读/改调用误扔进「其他」——实测过，一次就错了 190 条。
    只有归类失败的才去看原始工具名，用来认出插件工具族。
    """
    cls = e.get("_cls") or ""
    if cls == "task":
        return "子代理"   # subagent / subagent_fork / workflow 都归一成 task
    if cls == "web":
        return "网络"
    # 注意 `_cls` 可能是字符串 "other"——它也是真值。把它当成「已归类」会让
    # blender_rt_* / skill / todo_write 全被误判成内置（实测一次错 190 条）。
    if cls and cls != "other":
        return "内置"
    raw = str(e.get("raw_tool") or "")
    low = raw.lower()
    for prefix, name in FAMILY_PREFIXES:
        if low.startswith(prefix):
            return name
    return "其他" if raw else "未归属"


def dsh_home() -> Path:
    """DSH 主目录：$DSH_HOME 优先，缺省 ~/.dsh。"""
    override = (os.environ.get("DSH_HOME") or "").strip()
    return Path(override).expanduser() if override else Path.home() / ".dsh"


HOME_ROOT = dsh_home() / "persona"
METRICS = HOME_ROOT / "metrics"
STATE = HOME_ROOT / "state.json"
HOME_SESSIONS = HOME_ROOT / "sessions"
# 项目级档位扫描根目录：扫 <root>/.dsh/persona/state.json（root 自身
# 即项目）与 <root>/*/.dsh/persona/state.json（root 为多项目根）。
PROJECT_ROOTS = [Path(p) for p in (
    s.strip() for s in os.environ.get("PERSONA_PROJECT_ROOTS", "").split(os.pathsep))
    if p.strip()] or [Path.cwd()]
# 输出路径：环境变量优先；默认写到当前目录。
OUT = Path(os.environ.get("PERSONA_DASHBOARD_OUT") or Path.cwd() / "persona-dashboard.html")
LARGE_EVENTS = 300
RECENT_SESSIONS = 50

TOOL_LABEL = {
    "shell": "终端", "edit": "编辑", "write": "写文件", "read": "读文件",
    "other": "其他", "search": "搜索", "task": "子任务", "browser": "浏览器",
}

# 层级取值（hook_persona.py：state.layer ∈ session/project/global）；
# 字段缺失或取值不符（旧数据 / 钩子外调用）一律归「未归属」。
LAYER_ORDER = ["session", "project", "global", "未归属"]
LAYER_LABEL = {"session": "session 层", "project": "project 层", "global": "global 层"}


def layer_of(rec) -> str:
    v = rec.get("layer")
    return v if v in LAYER_LABEL else "未归属"


# 会话事件（不是工具调用）。必须与工具事件分开读——审批记录带 `tool` 字段，
# 混进工具事件会被「工具分布」与「探查画像」算成一次真实调用，那是污染不是统计。
SESSION_MARK_EVENTS = ("approval", "turn")


def event_files():
    """事件流文件（排除注入/档位文件与历史清理产物）。"""
    for f in sorted(METRICS.glob("*.jsonl")):
        # 注入/档位文件由各自 loader 读取；事件流只收工具事件文件
        #（不限年份，避免跨年后漏数据；purged/shard 为历史清理产物）。
        if (f.name.startswith(("injections-", "dials-"))
                or "purged" in f.name or "shard" in f.name):
            continue
        yield f


def load_events():
    """**工具**事件（含守卫命中）。会话事件走 `load_session_marks()`。"""
    events = []
    for f in event_files():
        day = f.name.split(".")[0]
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("ev") in SESSION_MARK_EVENTS:
                continue
            r["_day"] = day
            r["_cls"] = effective_class(r)
            events.append(r)
    return events


def load_session_marks():
    """会话事件：审批结局与每轮边界（DSH 专属维度）。

    这两类只有原生插件会写（`ctx.on('session/event')`），钩子桥那条路没有，
    所以旧数据里一条都不会有——看板对空集如实显示「暂无」，不编造。
    """
    marks = []
    for f in event_files():
        day = f.name.split(".")[0]
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("ev") not in SESSION_MARK_EVENTS:
                continue
            r["_day"] = day
            marks.append(r)
    return marks


def load_injections():
    rows = []
    for f in sorted(METRICS.glob("injections-*.jsonl")):
        if "purged" in f.name or "shard" in f.name:
            continue
        day = f.name.replace("injections-", "").split(".")[0]
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            r["_day"] = day
            rows.append(r)
    return rows


def load_dials():
    rows = []
    for f in sorted(METRICS.glob("dials-*.jsonl")):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            rows.append(r)
    return rows


def is_instrument(sid: str) -> bool:
    return (not sid) or sid == "unknown" or sid.startswith("reg")


DIAL_ABBR = {"PACE": "快", "VERIFY": "验", "RISK": "险", "STRUCT": "构",
             "DETAIL": "细", "EXPLORE": "探", "STYLE": "格", "WARMTH": "温"}


def dial_combo(dials) -> str:
    """八旋钮级别组合的紧凑串（如 快2验1险1构1细2探2格2温2）。
    dials 兼容 dict（会话/项目层文件）与 list[{k,v}]（全局 state.json）。"""
    if isinstance(dials, dict):
        items = list(dials.items())
    elif isinstance(dials, list):
        items = [(d.get("k"), d.get("v")) for d in dials]
    else:
        return ""
    try:
        return "".join(f"{DIAL_ABBR.get(k, str(k)[:1])}{int(v)}" for k, v in items if k)
    except (TypeError, ValueError):
        return ""


def display_persona(p, st=None):
    """仅当 persona 对不上 MBTI（自定义）时，用八旋钮级别组合区分；MBTI 名原样保留。"""
    if p == "自定义" and st:
        c = dial_combo(st.get("dials"))
        if c:
            return c
    return p


HOOK_CFG = HOME_ROOT / "hooks.json"


def hook_installed() -> bool:
    """看板口径的「已装钩子」：DSH 的桥接钩子配置里引用了本技能的 hook_persona.py。

    配置文件是 Claude Code 事件表形态（裸事件映射或 {"hooks": ...} 外壳，两者都认）。

    **注意这只看兼容桥这一条通路。** DSH 上工具事件同样可以由原生插件写
    （见 `l3_active()`），所以「没装钩子」**不等于**「没有工具数据」。
    """
    try:
        cfg = json.loads(HOOK_CFG.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(cfg, dict):
        return False
    hooks = cfg.get("hooks")
    if not isinstance(hooks, dict):
        hooks = cfg
    return "hook_persona" in json.dumps(hooks, ensure_ascii=False)


def l3_active() -> bool:
    """看板口径的「L3 数据源已生效」。

    工具事件 / 注入 / 触发词 / 危险拦截有**两条**写入通路，任一条在跑就算生效：

      1. **原生插件**（DSH 上的推荐路径）：`@dsh-external/persona-driven` 装进了某个
         profile。判定直接复用 `check_environment.dsh_plugin_installed()`——同一件事
         不写第二份实现，否则两边迟早不一致；
      2. **兼容桥**：`$DSH_HOME/persona/hooks.json` 里引用了 `hook_persona.py`。

    三层档位状态文件与档位变更记录（`dials-*.jsonl`）由 `set` / `off` 写入，两条都不依赖。

    迁移遗留（修的就是它）：这个判断以前只看钩子。DSH 上插件才是推荐路径，
    只认钩子会把**已经在写的**工具数据判成没有，看板于是强制锁进简洁版并把复选框禁用——
    数据在，只是被自己藏起来了。
    """
    if hook_installed():
        return True
    try:
        import check_environment as ce

        return bool(ce.dsh_plugin_installed())
    except Exception:
        return False


def load_layer_states():
    """读会话层与项目层档位状态文件（当前值，非历史快照）。

    会话层：sessions/state-<完整sid>.json，键=完整 sid（埋点里的 sid 是前 24 位
    截断，JS 侧用前缀匹配）。项目层：扫描 PROJECT_ROOTS（root 自身与其直接子
    目录）下 .dsh/persona/state.json，键=cwd 哈希候选（分隔符/大小写变体
    全算，与 hook 埋点 proj 对齐），另存 dir 字段（目录名，供项目视角展示——
    来自本机扫描，不来自埋点）。
    """
    import hashlib
    sess = {}
    if HOME_SESSIONS.exists():
        for f in sorted(HOME_SESSIONS.glob("state-*.json")):
            try:
                sess[f.stem[len("state-"):]] = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
    proj = {}
    for root in PROJECT_ROOTS:
        rp = Path(root)
        if not rp.exists():
            continue
        # root 自身即项目（在项目目录里生成的场景）+ root 直接子目录（多项目根场景）
        cands = [rp / ".dsh" / "persona" / "state.json"]
        cands += sorted(rp.glob("*/.dsh/persona/state.json"))
        for f in cands:
            try:
                st = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            d = f.parent.parent.parent.resolve()
            keys = {str(d), d.as_posix(), str(d).replace("\\", "/")}
            keys |= {k.lower() for k in list(keys)} | {k.upper() for k in list(keys)}
            for k in keys:
                proj.setdefault(hashlib.sha256(k.encode("utf-8", "replace")).hexdigest()[:12],
                                {"dir": d.name, **st})
    return sess, proj


def main():
    events = load_events()
    marks = load_session_marks()
    injections = load_injections()
    dial_rows = load_dials()
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    sess_states, proj_states = load_layer_states()

    # 过滤：回归仪器会话硬排除；实验分桶（bucket=exp-*，v1.5.31 起 runner 注入
    # PERSONA_METRICS_TAG）精确排除，取代单纯靠会话事件数的启发式。
    def is_exp(e):
        return str(e.get("bucket", "")).startswith("exp-")

    events = [e for e in events
              if not is_instrument(str(e.get("session", ""))) and not is_exp(e)]
    injections = [r for r in injections if not is_instrument(str(r.get("session", "")))]
    marks = [m for m in marks
             if not is_instrument(str(m.get("session", ""))) and not is_exp(m)]

    # ---- DSH 专属维度（1.7.0 起）--------------------------------------------
    # 全部只用**已落盘的数据**算，不新增采集。

    family = Counter(tool_family(e) for e in events)

    # 审批结局：这是「档位是否让人更冒进」最直接的观测值。拒绝率只在**用户真的做了选择**
    # 时才有意义——`cancelled`（撤销）与 `unavailable`（没有审批通道）都不是拒绝，
    # 把它们算进分母会把「环境没提示」误读成「用户挺满意」。
    approval_rows = [m for m in marks if m.get("ev") == "approval"]
    approval_by_outcome = Counter(str(m.get("outcome") or "?") for m in approval_rows)
    granted = approval_by_outcome.get("allowed-once", 0)
    rejected = approval_by_outcome.get("rejected", 0)
    decided = granted + rejected
    reject_rate = round(100 * rejected / decided) if decided else None
    approval_by_tool = Counter(str(m.get("tool") or "(工具名未知)") for m in approval_rows)

    # 每轮调用数：判定线关心的相对量。
    #
    # **必须按「首条 turn 之后」截断**：turn 埋点是 1.7.0 才上线的，老会话的历史调用
    # 一条 turn 都没有。若拿整段历史调用除以刚观察到的轮数，会算出荒谬值——
    # 实测过一次：332 次/轮（同一会话历史 332 次调用 ÷ 重启后 1 轮）。
    #
    # 另要求**至少 2 轮**：1 轮算不出「每轮」，把噪声显示成指标就是编造结论。
    turn_rows = [m for m in marks if m.get("ev") == "turn"]
    turns_by_session = Counter(str(m.get("session") or "") for m in turn_rows)
    first_turn_ts = {}
    for m in sorted(turn_rows, key=lambda r: str(r.get("ts") or "")):
        sid = str(m.get("session") or "")
        ts = str(m.get("ts") or "")
        if sid and ts and sid not in first_turn_ts:
            first_turn_ts[sid] = ts
    calls_by_session = Counter(
        str(e.get("session") or "") for e in events
        # turn 与工具记录都由插件用同一格式（ISO + Z）写出，逐字符比较成立。
        if str(e.get("ts") or "") >= first_turn_ts.get(str(e.get("session") or ""), "9999")
    )
    per_turn = [
        {"session": sid[:13] + "…", "turns": n, "calls": calls_by_session[sid],
         "callsPerTurn": round(calls_by_session[sid] / n, 2)}
        for sid, n in turns_by_session.items() if n >= 2 and calls_by_session.get(sid)
    ]
    per_turn.sort(key=lambda r: r["callsPerTurn"], reverse=True)

    # 技能调用排行：只看 `skill` 字段（只有 skill 工具会带）。这是检验本技能触发词
    # 是否真的生效的直接依据——被调用了才有这一行。
    skill_rows = Counter(str(e.get("skill")) for e in events if e.get("skill"))

    # ---- 档位切换前后对照（判定线真正关心的那一块）--------------------------
    #
    # 「切档之后每轮调用数有没有降」是相对量，无法从总量看出来。两侧各至少 2 轮才算，
    # 且窗口只取该会话**首条 turn 之后**（理由同 per_turn：turn 埋点是 1.7.0 才上线的）。
    dial_changes = [d for d in dial_rows if str(d.get("ev") or "") == "dial_change"]
    dial_cut = {}
    for d in sorted(dial_changes, key=lambda r: str(r.get("ts") or "")):
        sid = str(d.get("session") or "")
        moment = parse_ts(d.get("ts"))
        if sid and moment is not None and sid not in dial_cut:
            dial_cut[sid] = moment

    dial_compare = []
    for sid, cut in dial_cut.items():
        start = parse_ts(first_turn_ts.get(sid))
        if start is None or cut <= start:
            continue   # 没有 turn 网格，或切档发生在观察窗之前——算不出前后
        before_turns = after_turns = before_calls = after_calls = 0
        for m in turn_rows:
            if str(m.get("session") or "") != sid:
                continue
            moment = parse_ts(m.get("ts"))
            if moment is None:
                continue
            if moment < cut:
                before_turns += 1
            else:
                after_turns += 1
        for e in events:
            if str(e.get("session") or "") != sid:
                continue
            moment = parse_ts(e.get("ts"))
            if moment is None or moment < start:
                continue
            if moment < cut:
                before_calls += 1
            else:
                after_calls += 1
        if before_turns < 2 or after_turns < 2:
            continue   # 两侧不足 2 轮，比值是噪声
        dial_compare.append({
            "session": sid[:13] + "…",
            "before": {"turns": before_turns, "calls": before_calls,
                       "perTurn": round(before_calls / before_turns, 2)},
            "after": {"turns": after_turns, "calls": after_calls,
                      "perTurn": round(after_calls / after_turns, 2)},
        })
    # 别按「方向」排序——那会诱导只看想要的结果。按会话排，稳定。
    dial_compare.sort(key=lambda r: r["session"])

    # 「自定义」档位的显示名：用该会话层档位文件的八旋钮级别组合区分（仅此情形，
    # MBTI 名原样保留）。事件级预计算 _pd，供所有 persona 聚合统一取用。
    sess_state_by_pfx = {fs[:24]: st for fs, st in sess_states.items()}
    for e in events:
        if e.get("persona") == "自定义":
            e["_pd"] = display_persona(
                "自定义", sess_state_by_pfx.get(str(e.get("session", ""))[:24]))

    # proj/layer 字段覆盖率（排除仪器/实验会话后的全量样本，供诚实条款引用；
    # 1.5.33 之前的旧数据没有这两个字段）。
    def cov(rows, key):
        n = len(rows)
        return {"n": n, "with": sum(1 for r in rows if r.get(key))}

    coverage = {"eventsProj": cov(events, "proj"), "eventsLayer": cov(events, "layer"),
                "injProj": cov(injections, "proj"), "injLayer": cov(injections, "layer")}

    # 统计范围：只保留最新活跃的前 RECENT_SESSIONS 个会话（按最后事件日期，其次事件量）。
    last_day, cnt = {}, Counter()
    for e in events:
        sid = str(e.get("session", ""))
        cnt[sid] += 1
        if e["_day"] > last_day.get(sid, ""):
            last_day[sid] = e["_day"]
    pool = set(sorted(cnt, key=lambda k: (last_day[k], cnt[k]), reverse=True)[:RECENT_SESSIONS])
    events = [e for e in events if e.get("session", "") in pool]
    injections = [r for r in injections if r.get("session", "") in pool]
    # 空池（埋点目录还没有任何事件）必须走单独分支：`min()` 对空序列抛 ValueError，
    # 此前会让「刚装好、还没用过」的用户直接看到一个崩掉的看板。空数据不是错误状态，
    # 页面上如实说明「暂无埋点」即可。
    if pool:
        scope = (f"统计范围：最新活跃的前 {RECENT_SESSIONS} 个会话"
                 f"（{min(last_day[k] for k in pool)} ~ {max(last_day[k] for k in pool)}）")
    else:
        scope = "统计范围：暂无埋点数据（还没有可统计的会话，先用一阵子再看）"

    # 按会话聚合（供前端开关重算）。persona 事件众数单独收（sess_pmix），
    # 不放进 sessions 值里——sess_list 会展开 **v，Counter 无法 JSON 序列化。
    sessions = {}
    sess_pmix = defaultdict(Counter)
    for e in events:
        sid = str(e.get("session", ""))
        if e.get("persona"):
            sess_pmix[sid][e.get("_pd") or e["persona"]] += 1
        s = sessions.setdefault(sid, {"day": e["_day"], "n": 0, "write": 0, "test": 0,
                                      "read": 0, "shell": 0, "edit": 0, "explore": 0,
                                      "persona": e.get("persona", "")})
        s["n"] += 1
        cls = e["_cls"]
        if cls in WRITE_CLASSES: s["write"] += 1
        if e.get("test"): s["test"] += 1
        if cls == "read": s["read"] += 1
        if cls in SHELL_CLASSES: s["shell"] += 1
        if cls == "edit": s["edit"] += 1
        if cls in EXPLORE_CLASSES: s["explore"] += 1
    sess_list = [{"id": k[:13] + "…", "last": last_day[k], **v} for k, v in sessions.items()]

    def days_of(sel):
        c = Counter(e["_day"] for e in sel)
        return sorted(c.items())

    def persona_mix(sel):
        c = Counter(e.get("_pd") or e.get("persona", "") or "未知" for e in sel)
        return c.most_common(8)

    def tool_mix(sel):
        c = Counter(TOOL_LABEL.get(e["_cls"], e["_cls"]) for e in sel)
        return c.most_common(8)

    days_all = days_of(events)
    inj_days = Counter(r["_day"] for r in injections)
    inj_levels = Counter(r.get("level", "?") for r in injections)
    raw_top = Counter(e["raw_tool"] for e in events if e.get("raw_tool")).most_common(10)

    # 重复读（v1.5.33 起 hook 记录 ph=被操作文件路径 sha256 前 12 位，不落原文）：
    # 浪费部分 = 总读次数 - 唯一文件数。旧数据无 ph，reads=0 时前端如实显示暂无。
    def dup_of(sel):
        phc = Counter(e["ph"] for e in sel if e["_cls"] == "read" and e.get("ph"))
        n_ph = sum(phc.values())
        return {"files": len(phc), "reads": n_ph,
                "repeat": sum(c - 1 for c in phc.values() if c > 1)}

    # 探查画像：按日对齐的三条序列（读 / 改 / 搜索探查），供前端开关在全部/排除大型两组间切换
    def explore_view(sel):
        def series(pred):
            c = Counter(e["_day"] for e in sel if pred(e))
            return [c.get(d, 0) for d, _ in days_all]
        rd = series(lambda e: e["_cls"] == "read")
        ed = series(lambda e: e["_cls"] == "edit")
        se = series(lambda e: e["_cls"] in EXPLORE_CLASSES)
        se_n = sum(se)
        return {"read": rd, "edit": ed, "search": se,
                "ratio": (round(sum(rd) / sum(ed), 2) if sum(ed) else None),
                "searchShare": (round(100 * se_n / max(1, sum(rd) + sum(ed) + se_n)),),
                "dup": dup_of(sel)}

    small_events = [e for e in events if sessions[str(e.get("session", ""))]["n"] < LARGE_EVENTS]

    # 单会话视角：事件数 Top 20 的会话 + **所有有专属档位文件的会话**（无条件入列，
    # 否则小事件量的会话永远选不到，档位面板看不到会话层数据）。逐日序列只给这部分
    # 会话附上（全量 1800+ 会话会让 HTML 膨胀）。
    def build_sess_detail(k):
        sel = [e for e in events if e.get("session", "") == k]

        def s_series(pred, src=sel):
            c = Counter(e["_day"] for e in src if pred(e))
            return [c.get(d, 0) for d, _ in days_all]

        top_proj = Counter(e.get("proj") for e in sel if e.get("proj")).most_common(1)
        return {
            "id": k[:13] + "…", "sid": k[:24], "proj": top_proj[0][0] if top_proj else "",
            "day": sessions[k]["day"], "last": last_day[k], "n": sessions[k]["n"],
            "persona": (sess_pmix[k].most_common(1)[0][0] if sess_pmix.get(k)
                        else sessions[k]["persona"] or ""),
            "pmix": sess_pmix.get(k, Counter()).most_common(8),
            "events": s_series(lambda e: True),
            "inj": s_series(lambda e: True, src=[r for r in injections if r.get("session") == k]),
            "read": s_series(lambda e: e["_cls"] == "read"),
            "edit": s_series(lambda e: e["_cls"] == "edit"),
            "search": s_series(lambda e: e["_cls"] in EXPLORE_CLASSES),
            "tools": tool_mix(sel),
            "raw": Counter(e["raw_tool"] for e in sel if e.get("raw_tool")).most_common(10),
            "dup": dup_of(sel),
        }

    top20 = {k for k, _ in sorted(sessions.items(), key=lambda kv: kv[1]["n"], reverse=True)[:20]}
    with_state = set()
    for full_sid in sess_states:
        m = next((sk for sk in sessions if full_sid.startswith(sk) and sk), None)
        if m and m not in top20:
            with_state.add(m)
    sess_detail = [build_sess_detail(k) for k in sorted(top20, key=lambda k: sessions[k]["n"], reverse=True)]
    for k in sorted(with_state, key=lambda k: sessions[k]["n"], reverse=True):
        row = dict(build_sess_detail(k), own=True)
        # 有专属档位但事件里没有任何非空 persona（先干活后设档位）——回退到
        # 会话层档位文件的权威值，否则「人格使用分布」显示未知。
        if not row["pmix"]:
            st = next((v for fs, v in sess_states.items() if fs.startswith(k)), None)
            if st and st.get("persona"):
                row["persona"] = display_persona(st["persona"], st)
        sess_detail.append(row)

    # 有专属档位但在统计池里零事件的会话（设完档位没干活）：也入列，事件序列全零，
    # 活跃日取档位文件 updated_at——否则这些档位在看板上永远不可见。
    covered = {d["sid"] for d in sess_detail}
    for full_sid, st in sess_states.items():
        pfx = full_sid[:24]
        if any(pfx.startswith(c) for c in covered):
            continue
        upd = str(st.get("updated_at", ""))[:10] or "未知"
        sess_detail.append({
            "id": pfx[:13] + "…", "sid": pfx, "proj": "", "day": upd, "last": upd, "n": 0,
            "persona": display_persona(st.get("persona") or "", st),
            "events": [0] * len(days_all), "inj": [0] * len(days_all),
            "read": [0] * len(days_all), "edit": [0] * len(days_all), "search": [0] * len(days_all),
            "tools": [], "raw": [], "pmix": [], "dup": {"files": 0, "reads": 0, "repeat": 0}, "own": True})

    # 两个下拉统一按「最近活跃」倒序排（日期字符串字典序即时间序，并列看事件量）。
    sess_detail.sort(key=lambda d: (d["last"], d["n"]), reverse=True)

    # 项目视角：按 proj（cwd 哈希前 12 位，1.5.33 起才有）取事件数 Top 20 的项目。
    proj_events = defaultdict(list)
    for e in events:
        p = e.get("proj", "")
        if p:
            proj_events[p].append(e)
    # 免钩子版的项目档位行：从项目层状态文件直接构建（零事件序列，与 projDetail
    # 同构，前端渲染逻辑共用）。按目录名去重（同一目录的哈希变体只留一行）。
    lite_proj, seen_dirs = [], set()
    for k, st in proj_states.items():
        if st.get("dir") in seen_dirs:
            continue
        seen_dirs.add(st.get("dir"))
        lite_proj.append({
            "id": k, "dir": st.get("dir", ""), "n": 0, "last": str(st.get("updated_at", ""))[:10] or "—",
            "sessions": 0, "tw": 0, "ok": 0,
            "events": [0], "inj": [0], "read": [0], "edit": [0], "search": [0],
            "persona": display_persona(st.get("persona") or "", st),
            "level": st.get("level", ""), "gist": st.get("gist", ""),
            "tools": [], "raw": [], "dup": {"files": 0, "reads": 0, "repeat": 0},
        })
    proj_detail = []
    for p in sorted(proj_events, key=lambda k: len(proj_events[k]), reverse=True)[:20]:
        sel = proj_events[p]
        inj_sel = [r for r in injections if r.get("proj") == p]
        sids = {str(e.get("session", "")) for e in sel}

        def p_series(pred, src=sel):
            c = Counter(e["_day"] for e in src if pred(e))
            return [c.get(d, 0) for d, _ in days_all]

        tw = sum(1 for s in sids if s in sessions and sessions[s]["write"] > 0)
        ok = sum(1 for s in sids if s in sessions and sessions[s]["write"] > 0 and sessions[s]["test"] > 0)
        lay = Counter(layer_of(e) for e in sel)
        inj_lay = Counter(layer_of(r) for r in inj_sel)
        proj_detail.append({
            "id": p, "n": len(sel), "last": max(e["_day"] for e in sel), "sessions": len(sids),
            "tw": tw, "ok": ok,
            "events": p_series(lambda e: True),
            "inj": p_series(lambda e: True, src=inj_sel),
            "read": p_series(lambda e: e["_cls"] == "read"),
            "edit": p_series(lambda e: e["_cls"] == "edit"),
            "search": p_series(lambda e: e["_cls"] in EXPLORE_CLASSES),
            "persona": persona_mix(sel),
            "tools": tool_mix(sel),
            "raw": Counter(e["raw_tool"] for e in sel if e.get("raw_tool")).most_common(10),
            "dup": dup_of(sel),
            "layers": [(LAYER_LABEL.get(k, k), lay[k]) for k in LAYER_ORDER if lay[k]],
            "injLayers": [(LAYER_LABEL.get(k, k), inj_lay[k]) for k in LAYER_ORDER if inj_lay[k]],
        })
    proj_detail.sort(key=lambda d: (d["last"], d["n"]), reverse=True)

    # 全局视角：数据层级分布（1.5.33 起新增字段，旧数据归「未归属」）。
    ev_lay = Counter(layer_of(e) for e in events)
    inj_lay = Counter(layer_of(r) for r in injections)
    layers_all = {
        "ev": [(LAYER_LABEL.get(k, k), ev_lay[k]) for k in LAYER_ORDER if ev_lay[k]],
        "inj": [(LAYER_LABEL.get(k, k), inj_lay[k]) for k in LAYER_ORDER if inj_lay[k]],
    }

    dials = state.get("dials", {})
    dial_labels = {"PACE": "快慢", "VERIFY": "验证", "RISK": "风险", "STRUCT": "结构",
                   "DETAIL": "细节", "EXPLORE": "探索", "STYLE": "风格", "WARMTH": "温度"}

    # 行为遥测（v1.5.31 新埋点；旧数据无这些字段，如实显示 0/无）
    trig_hit = sum(1 for e in events if e.get("ev") == "trigger_scan" and e.get("hit"))
    trig_miss = sum(1 for e in events if e.get("ev") == "trigger_scan"
                    and not e.get("hit") and e.get("scan"))
    guard_rows = [e for e in events if e.get("ev") == "guard"]
    guard_top = Counter(e.get("category", "?") for e in guard_rows).most_common(5)
    # 原始行入 payload，供 JS 按视角（全局/项目/会话）过滤。
    trig_rows = [{"session": str(e.get("session", ""))[:24], "proj": e.get("proj", ""),
                  "hit": bool(e.get("hit")), "kinds": list(e.get("kinds") or [])}
                 for e in events if e.get("ev") == "trigger_scan" and e.get("scan")]
    guard_detail = [{"session": str(e.get("session", ""))[:24], "proj": e.get("proj", ""),
                     "cat": e.get("category", "?"), "tool": e["_cls"]}
                    for e in guard_rows]
    # 注入构成（事件类型 + 块长度）：SessionStart=会话开始，UserPromptSubmit=每轮对话。
    # 统计池口径，不随视角切换（分视角事件类型分布未入 payload）。
    inj_chars = [r.get("chars") or 0 for r in injections]
    inj_events = Counter(r.get("event", "?") for r in injections)
    dial_recent = [{"ts": r.get("ts", "")[:16].replace("T", " "),
                    "action": r.get("action"), "persona": r.get("persona") or "—",
                    "level": r.get("level"), "scope": r.get("scope"),
                    "session": str(r.get("session", ""))[:13]}
                   for r in sorted(dial_rows, key=lambda r: r.get("ts", ""))[-50:]][::-1]

    payload = {
        "large": LARGE_EVENTS,
        "sessions": sess_list,
        "days": [d for d, _ in days_all],
        "eventsAll": [c for _, c in days_all],
        "injDays": [d for d in [x for x, _ in days_all] if d in inj_days],
        "injAll": [inj_days.get(d, 0) for d, _ in days_all],
        "personaAll": persona_mix(events),
        "personaBig": persona_mix([e for e in events if sessions[str(e.get("session", ""))]["n"] >= LARGE_EVENTS]),
        "personaSmall": persona_mix([e for e in events if sessions[str(e.get("session", ""))]["n"] < LARGE_EVENTS]),
        "toolAll": tool_mix(events),
        "rawTop": raw_top,
        "injLevels": inj_levels.most_common(),
        "injEvents": inj_events.most_common(),
        "injChars": {"avg": (sum(inj_chars) // max(len(inj_chars), 1)), "n": len(inj_chars)},
        "exploreAll": explore_view(events),
        "families": family.most_common(),
        "approvals": {
            "total": len(approval_rows),
            "byOutcome": approval_by_outcome.most_common(),
            "byTool": approval_by_tool.most_common(10),
            "rejectRate": reject_rate,
            "granted": granted,
            "rejected": rejected,
        },
        "perTurn": per_turn[:20],
        "turnsTotal": len(turn_rows),
        "skills": skill_rows.most_common(15),
        "dialCompare": dial_compare,
        "exploreSmall": explore_view(small_events),
        "sessDetail": sess_detail,
        "projDetail": proj_detail,
        "layersAll": layers_all,
        "coverage": coverage,
        "trig": {"hit": trig_hit, "miss": trig_miss},
        "guards": {"total": len(guard_rows), "top": guard_top},
        "trigRows": trig_rows,
        "hook": hook_installed(),
        "l3": l3_active(),
        "liteProj": lite_proj,
        "guardRows": guard_detail,
        "dialRows": dial_recent,
        "dialTotal": len(dial_rows),
        "state": {"persona": state.get("persona"), "level": state.get("level"),
                  "gist": state.get("gist"),
                  "dials": [{"k": dial_labels.get(k, k), "v": v} for k, v in dials.items()]},
        "sessStates": sess_states,
        "projStates": proj_states,
        "updated": str(date.today()),
    }

    def pct(c):
        return round(100 * c["with"] / c["n"], 1) if c["n"] else 0.0

    cov_note = (f"proj 字段覆盖：事件 {pct(coverage['eventsProj'])}% / 注入 {pct(coverage['injProj'])}%；"
                f"layer 字段覆盖：事件 {pct(coverage['eventsLayer'])}% / 注入 {pct(coverage['injLayer'])}%"
                "（排除仪器与实验会话后的全量样本）")

    # 样本量提示动态生成：随事件量增长自动去掉「尚小」限定（阈值 1 万工具事件），
    # 不写死结论——现在样本小不代表以后小。展示在视角选择器上方。
    SAMPLE_SMALL_LINE = 10000
    sample_note = (f"当前样本：最近 {len(sess_list)} 个会话 · {len(events)} 个工具事件"
                   + ("（样本量尚小）" if len(events) < SAMPLE_SMALL_LINE else ""))

    html = (HTML.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
                .replace("__UPD__", str(date.today()))
                .replace("__SCOPE__", scope)
                .replace("__SAMPLE__", sample_note))
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_bytes(html.encode("utf-8"))

    n_all = len(events)
    n_small = sum(1 for e in events if sessions[str(e.get("session", ""))]["n"] < LARGE_EVENTS)
    tw_all = sum(1 for s in sessions.values() if s["write"] > 0)
    ok_all = sum(1 for s in sessions.values() if s["write"] > 0 and s["test"] > 0)
    small_ids = [s for s, v in sessions.items() if v["n"] < LARGE_EVENTS]
    tw_s = sum(1 for s in small_ids if sessions[s]["write"] > 0)
    ok_s = sum(1 for s in small_ids if sessions[s]["write"] > 0 and sessions[s]["test"] > 0)
    print(scope)
    print(f"事件 {n_all}（排除大型后 {n_small}） | 会话 {len(sess_list)} | 注入事件 {len(injections)}"
          f" | 项目（有 proj） {len(proj_events)}（入看板 Top {len(proj_detail)}）")
    print(f"验证遵守（含写入的会话中带 test 的占比）：全部 {ok_all}/{tw_all}"
          + (f" | 排除大型后 {ok_s}/{tw_s}" if tw_s else ""))
    print(f"层级分布（事件）：{dict(ev_lay)} | （注入）：{dict(inj_lay)}")
    print(cov_note)
    ea = payload["exploreAll"]
    print(f"探查画像（全部）：读/改比 {ea['ratio']} | 搜索探查占比 {ea['searchShare'][0]}%"
          f"（读 {sum(ea['read'])} / 改 {sum(ea['edit'])} / 探查 {sum(ea['search'])}）")
    print(f"工具族：{dict(family.most_common())}")
    print("审批结局：" + (f"{dict(approval_by_outcome)}"
                          + (f" | 拒绝率 {reject_rate}%（{rejected}/{decided}）" if decided
                             else " | 拒绝率 暂无（还没有用户做过选择）")
                          if approval_rows else "暂无（需原生插件写 session/event）"))
    print("每轮调用数：" + (f"{len(per_turn)} 个会话可算 | 最高 {per_turn[0]['session']} "
                            f"{per_turn[0]['callsPerTurn']}"
                            if per_turn else
                            f"暂无（turn 记录 {len(turn_rows)} 条；同一会话至少 2 轮才算，"
                            f"且只数首条 turn 之后的调用）"))
    print("技能调用：" + ("、".join(f"{name} {n}" for name, n in skill_rows.most_common(5))
                          if skill_rows else "暂无（没有 skill 工具调用）"))
    print("档位前后对照：" + ("；".join(
        f"{r['session']} {r['before']['perTurn']}→{r['after']['perTurn']} 次/轮"
        for r in dial_compare) if dial_compare else
        "暂无（需同一会话首条 turn 之后有切档，且切档前后各至少 2 轮）"))
    print(f"写出 {OUT}")


HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>人格驱动 · 本机数据看板</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
body{font-family:system-ui,'Segoe UI','Microsoft YaHei',sans-serif;margin:0;padding:24px;background:#fafaf8;color:#2c2c2a;}
.wrap{max-width:960px;margin:0 auto;}
h1{font-size:18px;font-weight:500;margin:0 0 4px;}
.sub{font-size:13px;color:#888780;margin:0 0 20px;}
.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:16px;}
.card{background:#fff;border:0.5px solid rgba(0,0,0,.15);border-radius:10px;padding:14px 16px;}
.card .lab{font-size:13px;color:#888780;margin:0;}
.card .num{font-size:24px;font-weight:500;margin:4px 0 0;}
.card .hint{font-size:12px;color:#b4b2a9;margin:2px 0 0;}
.panel{background:#fff;border:0.5px solid rgba(0,0,0,.15);border-radius:12px;padding:16px 20px;margin-bottom:16px;}
.panel h3{font-size:14px;font-weight:500;margin:0 0 2px;}
.panel .note{font-size:12px;color:#888780;margin:0 0 10px;}
.grid2{display:grid;grid-template-columns:minmax(0,5fr) minmax(0,7fr);gap:16px;margin-bottom:16px;}
.ch{position:relative;width:100%;height:260px;}
.nodata{font-size:13px;color:#888780;background:#f1efe8;border-radius:8px;padding:10px 12px;}
label{font-size:13px;color:#5f5e5a;}
select{font-size:13px;padding:2px 4px;max-width:340px;}
.foot{font-size:12px;color:#888780;line-height:1.6;}
</style>
</head>
<body>
<div class="wrap">
<h1>人格驱动 · 本机数据看板</h1>
<p class="sub">只读本机数据 · 不出网 · <span id="scopeNote">__SCOPE__</span> · 生成于 __UPD__</p>

<div id="viewBar" style="margin:-10px 0 16px;font-size:13px;color:#5f5e5a;">
  <div id="sampleNote" class="hk" style="margin-bottom:8px;color:#888780;">__SAMPLE__</div>
  <div>视角：</div>
  <div style="margin:4px 0;"><label><input type="radio" name="vw" value="all" checked> 全局</label></div>
  <div style="margin:4px 0;"><label><input type="radio" name="vw" value="proj"> 项目</label> <select id="projPick" disabled></select></div>
  <div style="margin:4px 0;"><label><input type="radio" name="vw" value="sess"> 会话</label> <select id="sessPick" disabled></select></div>
  <div style="margin:4px 0;"><label><input type="checkbox" id="lite"> 简洁版（仅档位数据）</label>
    <span id="liteNote" style="color:#b4b2a9;margin-left:8px;display:none;">未检测到 L3 数据源——仅展示档位数据；工具事件、注入与行为遥测需要装入 <b>DSH 插件</b>（@dsh-external/persona-driven）<b>或</b>配置桥接钩子后才有。</span></div>
  <div id="viewNote" style="margin-top:6px;color:#b4b2a9;">全部会话聚合（行为遥测随视角过滤）</div>
</div>

<div class="cards" id="cards"></div>

<div class="panel hk">
  <h3>每日事件量</h3>
  <p class="note">工具事件与人格注入次数（每次对话自动触发）</p>
  <div class="ch"><canvas id="trend" role="img" aria-label="每日事件量折线图">每日事件量</canvas></div>
  <p class="note" id="injComp"></p>
</div>

<div class="grid2">
  <div class="panel">
    <h3>当前档位 · 8 旋钮 <span id="stPersona" style="font-weight:400;color:#888780;"></span></h3>
    <p class="note" id="stGist"></p>
    <div class="ch" id="radarWrap" style="height:250px;"><canvas id="radar" role="img" aria-label="当前档位八旋钮雷达图">旋钮雷达</canvas></div>
    <p class="note" id="radarNa" style="display:none;">该视角没有对应的档位状态文件。</p>
    <p class="note" id="radarSrc">取自状态文件实时值（1–9 刻度）</p>
  </div>
  <div class="panel hk">
    <h3>人格使用分布</h3>
    <div class="ch"><canvas id="persona" role="img" aria-label="人格使用分布环形图">人格分布</canvas></div>
  </div>
</div>

<div class="panel hk">
  <h3>探查画像 · 每日（读 / 改 / 搜索探查）</h3>
  <p class="note">读改比 <b id="exRatio">—</b> · 搜索探查占比 <b id="exShare">—</b>（grep/glob 占三类合计）· 重复读 <b id="exDup">—</b></p>
  <div class="ch"><canvas id="explore" role="img" aria-label="探查画像每日折线图">探查画像</canvas></div>
  <p class="note">口径：重复读按文件路径哈希统计（同一文件读多次的浪费部分，不落路径原文），自该埋点上线起累积，旧数据不计入；读改比高=探查相对改动的开销大，仅个人趋势参考。</p>
</div>

<div class="panel hk" id="dshPanel">
  <h3>DSH 维度 · 工具族 / 审批结局 / 每轮调用数</h3>
  <p class="note" id="dshFam">—</p>
  <div class="ch"><canvas id="dshFamily" role="img" aria-label="工具族分布柱状图">工具族</canvas></div>
  <p class="note" id="dshAppr">—</p>
  <p class="note" id="dshTurn">—</p>
  <div class="ch"><canvas id="dshTurnChart" role="img" aria-label="每轮调用数与档位前后对照">每轮调用数</canvas></div>
  <p class="note" id="dshSkill">—</p>
  <p class="note">口径：工具族按已落盘字段归类，不新增采集；审批结局与每轮边界来自 DSH 的会话事件
  （只有原生插件会写，钩子桥没有），所以旧数据一律显示「暂无」而不编造。</p>
</div>

<div class="grid2 hk">
  <div class="panel">
    <h3>工具类型构成</h3>
    <p class="note">归一化 tool 字段</p>
    <div class="ch"><canvas id="tools" role="img" aria-label="工具类型构成条形图">工具构成</canvas></div>
  </div>
  <div class="panel">
    <h3>具体工具 Top 10</h3>
    <p class="note">raw_tool 字段（部分事件缺失该字段）——**只记录未被归一化识别的工具调用**，用于诊断新工具名</p>
    <p class="note" id="rawNa" style="display:none;">✓ 当前范围内没有未识别的工具调用（全部事件已正常归类到左侧「工具类型构成」），所以这里没有条目。</p>
    <div class="ch"><canvas id="raw" role="img" aria-label="具体工具排行横向条形图">工具排行</canvas></div>
  </div>
</div>

<div class="panel hk" id="layerPanel">
  <h3>数据层级分布</h3>
  <p class="note" id="layerNote">「未归属」= 早于层级埋点上线或无法判定来源的历史记录</p>
  <div style="display:flex;gap:20px;flex-wrap:wrap;">
    <div style="flex:1;min-width:220px;">
      <p class="note">人格注入按层</p>
      <div class="ch" style="height:200px;"><canvas id="layerInj" role="img" aria-label="注入按层分布环形图">注入层级</canvas></div>
    </div>
    <div style="flex:1;min-width:220px;">
      <p class="note">工具事件按层</p>
      <div class="ch" style="height:200px;"><canvas id="layerEv" role="img" aria-label="事件按层分布环形图">事件层级</canvas></div>
    </div>
  </div>
</div>

<div class="panel">
  <h3><span id="telTitle">行为遥测 · 触发词 / 危险拦截 / 档位变更</span> <span id="telScope" style="font-weight:400;color:#888780;"></span></h3>
  <p class="note hk">随视角过滤；触发词只记命中类别，不记录原文</p>
  <div class="cards" style="grid-template-columns:repeat(3,minmax(0,1fr));margin-bottom:10px;">
    <div class="card hk"><p class="lab">触发词命中</p><p class="num" id="trigHit">—</p><p class="hint" id="trigHint"></p></div>
    <div class="card hk"><p class="lab">危险拦截</p><p class="num" id="guardN">—</p><p class="hint" id="guardTop"></p></div>
    <div class="card"><p class="lab">档位变更</p><p class="num" id="dialN">—</p><p class="hint" id="dialHint">set / off 全量历史</p></div>
  </div>
  <div id="dialList" style="font-size:12px;color:#5f5e5a;line-height:1.7;"></div>
</div>

<p class="foot">
<span class="hk">口径说明：① 层级分布中「未归属」= 早于层级埋点上线的历史记录；</span>
<span class="hk">②</span> 隐私：埋点不含路径原文，项目以匿名代号显示；当前档位读的是对应层的真实状态文件（全局 / 项目 / 会话）。
</p>
</div>
<script>
const P = __PAYLOAD__;
let charts = {};
const pal = ['#0F6E56', '#185FA5', '#BA7517', '#534AB7', '#D4537E', '#888780', '#639922', '#D85A30'];

function mk(id, cfg) { if (charts[id]) charts[id].destroy(); charts[id] = new Chart(document.getElementById(id), cfg); }
function drop(id) { if (charts[id]) { charts[id].destroy(); delete charts[id]; } }

function sum(a) { return a.reduce((x, y) => x + y, 0); }

function setCards(items) {
  document.getElementById('cards').innerHTML = items
    .map(c => `<div class="card"><p class="lab">${c[0]}</p><p class="num">${c[1]}</p><p class="hint">${c[2]}</p></div>`).join('');
}

function setViewNote(t) { document.getElementById('viewNote').textContent = t; }

function renderTrend(evData, injData) {
  mk('trend', { type: 'line', data: { labels: P.days, datasets: [
      { label: '工具事件', data: evData, borderColor: '#0F6E56', backgroundColor: 'rgba(29,158,117,.12)', fill: true, tension: .3, pointRadius: 2 },
      { label: '人格注入', data: injData, borderColor: '#185FA5', backgroundColor: 'rgba(55,138,221,.10)', fill: true, tension: .3, pointRadius: 2 }] },
    options: { maintainAspectRatio: false, plugins: { legend: { display: true, labels: { font: { size: 11 } } },
        tooltip: { mode: 'index', intersect: false } },
      scales: { x: { grid: { color: 'rgba(0,0,0,.05)' } }, y: { grid: { color: 'rgba(0,0,0,.05)' } } } } });
}

function renderTools(tools, raw) {
  mk('tools', { type: 'bar', data: { labels: tools.map(x => x[0]),
      datasets: [{ data: tools.map(x => x[1]), backgroundColor: '#0F6E56', borderRadius: 4 }] },
    options: { indexAxis: 'y', maintainAspectRatio: false, plugins: { legend: { display: false } },
      scales: { x: { grid: { color: 'rgba(0,0,0,.06)' } }, y: { grid: { display: false } } } } });
  // raw_tool 是**诊断口径**：只有未被归一化识别的调用才记录。空集时不能画空轴——
  // 用户会把它误读成「没有数据」，而事实恰恰相反（全部已正常归类）。改显空态文案。
  const rawEl = document.getElementById('rawNa');
  if (!(raw || []).length) {
    drop('raw');
    if (rawEl) rawEl.style.display = '';
    return;
  }
  if (rawEl) rawEl.style.display = 'none';
  mk('raw', { type: 'bar', data: { labels: raw.map(x => x[0]),
      datasets: [{ data: raw.map(x => x[1]), backgroundColor: '#185FA5', borderRadius: 4 }] },
    options: { indexAxis: 'y', maintainAspectRatio: false, plugins: { legend: { display: false } },
      scales: { x: { grid: { color: 'rgba(0,0,0,.06)' } }, y: { grid: { display: false }, ticks: { font: { size: 10 } } } } } });
}

function renderDoughnut(id, pairs) {
  if (!pairs.length) { drop(id); return false; }
  mk(id, { type: 'doughnut', data: { labels: pairs.map(x => x[0]),
      datasets: [{ data: pairs.map(x => x[1]), backgroundColor: pal, borderWidth: 1 }] },
    options: { maintainAspectRatio: false, plugins: { legend: { position: 'right', labels: { font: { size: 11 } } } } } });
  return true;
}

function renderLayers(injPairs, evPairs) {
  const okI = renderDoughnut('layerInj', injPairs), okE = renderDoughnut('layerEv', evPairs);
  document.getElementById('layerNote').textContent =
    (okI || okE)
      ? '「未归属」= 早于层级埋点上线或无法判定来源的历史记录'
      : '样本内没有带 layer 字段的记录（全部为旧数据或未归属），无法分层';
}

function renderExplore(ex) {
  document.getElementById('exRatio').textContent = ex.ratio === null ? '—' : ex.ratio + ' : 1';
  document.getElementById('exShare').textContent = ex.searchShare[0] + '%';
  const dup = ex.dup || { files: 0, reads: 0, repeat: 0 };
  document.getElementById('exDup').textContent = dup.reads > 0
    ? dup.repeat + ' 次 / ' + dup.reads + ' 次读 · ' + dup.files + ' 个文件'
    : '暂无（新埋点起累积）';
  mk('explore', { type: 'line', data: { labels: P.days, datasets: [
      { label: '读文件', data: ex.read, borderColor: '#185FA5', backgroundColor: 'rgba(55,138,221,.10)', fill: true, tension: .3, pointRadius: 2 },
      { label: '编辑', data: ex.edit, borderColor: '#0F6E56', backgroundColor: 'rgba(29,158,117,.10)', fill: true, tension: .3, pointRadius: 2 },
      { label: '搜索探查', data: ex.search, borderColor: '#BA7517', backgroundColor: 'rgba(186,117,23,.10)', fill: true, tension: .3, pointRadius: 2 }] },
    options: { maintainAspectRatio: false, plugins: { legend: { display: true, labels: { font: { size: 11 } } },
        tooltip: { mode: 'index', intersect: false } },
      scales: { x: { grid: { color: 'rgba(0,0,0,.05)' } }, y: { grid: { color: 'rgba(0,0,0,.05)' } } } } });
}

function renderGlobal() {
  if (liteMode()) {
    setViewNote('简洁版 · 全局档位（无需钩子）');
    const pj = Object.keys(P.projStates || {}).length, sn = Object.keys(P.sessStates || {}).length;
    setCards([
      ['全局档位', P.state.persona ? pn(P.state) : '未设置', P.state.level || '—'],
      ['项目档位', String(pj), '已设档位的项目数'],
      ['会话档位', String(sn), '已设专属档位的会话数'],
      ['档位变更', String((P.dialRows || []).length), 'set / off 累计'],
    ]);
    if (P.state.dials && (P.state.dials.length || Object.keys(P.state.dials).length)) {
      drawRadar(P.state, '取自全局层状态文件实时值（1–9 刻度）');
    } else {
      hideRadar('全局层未设置档位');
    }
    renderTelemetry('all');
    return;
  }
  setViewNote('全部会话聚合（行为遥测随视角过滤）');
  const ss = P.sessions;
  const tw = ss.filter(s => s.write > 0);
  const ok = tw.filter(s => s.test > 0);
  const rate = tw.length ? Math.round(100 * ok.length / tw.length) : null;
  setCards([
    ['工具事件', sum(P.eventsAll).toLocaleString(), '全期累计'],
    ['活跃会话', ss.length.toLocaleString(), '排除仪器会话'],
    ['人格注入', sum(P.injAll).toLocaleString(), '每次对话自动触发'],
    ['写入会话带验证', rate === null ? '—' : rate + '%', '分母 ' + tw.length + ' 个含写入会话'],
  ]);
  renderTrend(P.eventsAll, P.injAll);
  renderDoughnut('persona', P.personaAll);
  renderTools(P.toolAll, P.rawTop);
  renderExplore(P.exploreAll);
  renderLayers(P.layersAll.inj, P.layersAll.ev);
  renderDsh();
  drawRadar(P.state, '取自全局层状态文件实时值（1–9 刻度）');
  renderTelemetry('all');
  document.getElementById('layerPanel').style.display = '';
}

function renderProject(d) {
  if (!d) {
    if (liteMode()) setViewNote('简洁版 · 暂无项目档位（在项目里说「设置人格为X，项目级别生效」创建）');
    return;
  }
  const lite = liteMode();
  if (lite) {
    const pst = P.projStates && P.projStates[d.id];
    setViewNote('简洁版 · 项目档位' + (d.dir ? '：' + d.dir : ''));
    setCards([
      ['项目', d.dir || d.id, ''],
      ['当前人格', pst ? (pn(pst) || '未设置') : '未设置', pst ? (pst.level || '') : ''],
      ['要点', pst ? (pst.gist || '—') : '—', ''],
      ['工具事件', '需装 DSH 插件（或桥接钩子）', '简洁版不统计'],
    ]);
    if (pst && pst.dials && Object.keys(pst.dials).length) {
      drawRadar(pst, '取自该项目层状态文件实时值（1–9 刻度，项目层）');
    } else {
      hideRadar('该项目未设项目级档位');
    }
    renderTelemetry('proj', d);
    return;
  }
  setViewNote('项目视角：proj ' + d.id + '（' + d.sessions + ' 个会话）');
  const ev = sum(d.events), inj = sum(d.inj);
  const r = sum(d.read), e = sum(d.edit), s = sum(d.search);
  const rate = d.tw ? Math.round(100 * d.ok / d.tw) : null;
  setCards([
    ['工具事件', ev.toLocaleString(), '项目 ' + d.id],
    ['会话数', d.sessions.toLocaleString(), '最近活跃 ' + d.last],
    ['人格注入', inj.toLocaleString(), '每次对话自动触发'],
    ['写入会话带验证', rate === null ? '—' : rate + '%', '分母 ' + d.tw + ' 个含写入会话'],
  ]);
  renderTrend(d.events, d.inj);
  renderDoughnut('persona', d.persona);
  renderTools(d.tools, d.raw);
  renderExplore({ read: d.read, edit: d.edit, search: d.search,
    ratio: e ? Math.round(100 * r / e) / 100 : null,
    searchShare: [(r + e + s) ? Math.round(100 * s / (r + e + s)) : 0],
    dup: d.dup });
  renderLayers(d.injLayers, d.layers);
  const pst = P.projStates && P.projStates[d.id];
  if (pst && pst.dials && Object.keys(pst.dials).length) {
    drawRadar(pst, '取自该项目层状态文件实时值（1–9 刻度，项目层）');
  } else if (P.state.dials && P.state.dials.length) {
    drawRadar(P.state, '该项目未设项目级档位——显示继承的全局层（实时值，1–9 刻度）');
  } else {
    hideRadar('该项目无任何档位来源（项目层与全局层均未设置）');
  }
  renderTelemetry('proj', d);
  document.getElementById('layerPanel').style.display = '';
}

const DL = {PACE:'快慢',VERIFY:'验证',RISK:'风险',STRUCT:'结构',DETAIL:'细节',EXPLORE:'探索',STYLE:'风格',WARMTH:'温度'};
function dl(k) { return DL[k] || k; }
// 「自定义」档位的显示名：用八旋钮级别组合区分（对得上 MBTI 的原样保留）
const AB = {PACE:'快',VERIFY:'验',RISK:'险',STRUCT:'构',DETAIL:'细',EXPLORE:'探',STYLE:'格',WARMTH:'温'};
function comboOf(dials) {
  if (!dials) return '';
  const rows = Array.isArray(dials) ? dials.map(x => [x.k, x.v]) : Object.entries(dials);
  return rows.map(([k, v]) => (AB[k] || String(k)[0] || '?') + v).join('');
}
function pn(st) {
  if (!st) return '';
  return (st.persona === '自定义' && comboOf(st.dials)) || st.persona || '';
}

function drawRadar(st, srcNote) {
  document.getElementById('radarWrap').style.display = '';
  document.getElementById('radarNa').style.display = 'none';
  // 来源行必须随雷达一起恢复：之前只在 drawRadar 里写文案、不恢复 display，
  // 于是从「有数据视角」切到「无数据视角」时，hideRadar 藏了图却没藏来源行，
  // 「该视角没有对应的档位状态文件」和「取自…实时值」会同时出现，互相打脸。
  document.getElementById('radarSrc').style.display = '';
  document.getElementById('stPersona').textContent = pn(st) ? ('· ' + pn(st) + (st.level === 'off' ? '（注入 off）' : '（' + st.level + '）')) : '';
  document.getElementById('stGist').textContent = st.gist || '';
  document.getElementById('radarSrc').textContent = srcNote;
  const rows = (st.dials && st.dials.length) ? st.dials
    : (st.dials ? Object.keys(st.dials).map(k => ({ k: dl(k), v: st.dials[k] })) : []);
  if (rows.length) {
    mk('radar', { type: 'radar', data: { labels: rows.map(d => d.k),
        datasets: [{ label: '当前值', data: rows.map(d => d.v), backgroundColor: 'rgba(29,158,117,.15)',
          borderColor: '#0F6E56', pointBackgroundColor: '#0F6E56', pointRadius: 3, borderWidth: 2 }] },
      options: { maintainAspectRatio: false, plugins: { legend: { display: false } },
        scales: { r: { min: 0, max: 9, ticks: { display: false, stepSize: 3 },
          grid: { color: 'rgba(0,0,0,.08)' }, angleLines: { color: 'rgba(0,0,0,.08)' },
          pointLabels: { font: { size: 11 }, color: '#444441' } } } } });
  }
}

function renderSession(i) {
  const d = P.sessDetail[i];
  if (!d) return;
  const lite = liteMode();
  const sent = Object.entries(P.sessStates || {}).find(([k]) => d.sid && k.startsWith(d.sid));
  if (lite) {
    setViewNote('简洁版 · 会话档位');
    setCards([
      ['会话', d.id, d.own ? '有会话专属档位' : ''],
      ['当前人格', sent ? (pn(sent[1]) || '未设置') : (d.persona || '未设置'), sent ? '取自会话层档位文件' : '继承上层'],
      ['要点', sent ? (sent[1].gist || '—') : '—', ''],
      ['工具事件', '需装 DSH 插件（或桥接钩子）', '简洁版不统计'],
    ]);
    if (sent && sent[1].dials && Object.keys(sent[1].dials).length) {
      drawRadar(sent[1], '取自该会话专属档位文件实时值（1–9 刻度，会话层）');
    } else {
      hideRadar('该会话未单独设档位');
    }
    renderTelemetry('sess', d);
    return;
  }
  setViewNote('单会话视角 · 明细 Top 20 + 专属档位会话 · 行为遥测随本会话过滤');
  const ev = sum(d.events), inj = sum(d.inj);
  const r = sum(d.read), e = sum(d.edit), s = sum(d.search);
  setCards([
    ['工具事件', ev.toLocaleString(), '会话 ' + d.id],
    ['最近活跃', d.last, '首日 ' + d.day + (d.persona ? ' · persona: ' + d.persona : '')],
    ['人格注入', inj.toLocaleString(), '每次对话自动触发'],
    ['读 / 改 / 探', r + ' / ' + e + ' / ' + s, '搜索探查占比 ' + ((r + e + s) ? Math.round(100 * s / (r + e + s)) : 0) + '%'],
  ]);
  renderTrend(d.events, d.inj);
  // 人格使用分布：优先画该会话事件的 persona 分布（众数序）；
  // 无非空事件 persona 时回退会话层档位文件值；都没有才是未知。
  const pm = (d.pmix || []).filter(x => x[0] && x[1] > 0);
  mk('persona', { type: 'doughnut',
    data: { labels: pm.length ? pm.map(x => x[0]) : [d.persona || '未知'],
      datasets: [{ data: pm.length ? pm.map(x => x[1]) : [Math.max(ev, 1)],
        backgroundColor: pm.length ? pm.map((_, i) => pal[i % pal.length]) : [pal[0]], borderWidth: 1 }] },
    options: { maintainAspectRatio: false, plugins: { legend: { position: 'right', labels: { font: { size: 11 } } } } } });
  renderTools(d.tools, d.raw);
  renderExplore({ read: d.read, edit: d.edit, search: d.search,
    ratio: e ? Math.round(100 * r / e) / 100 : null,
    searchShare: [(r + e + s) ? Math.round(100 * s / (r + e + s)) : 0],
    dup: d.dup });
  drop('layerInj'); drop('layerEv');
  document.getElementById('layerPanel').style.display = 'none';
  if (sent && sent[1].dials && Object.keys(sent[1].dials).length) {
    drawRadar(sent[1], '取自该会话专属档位文件实时值（1–9 刻度，会话层）');
  } else {
    const pst = d.proj && P.projStates && P.projStates[d.proj];
    if (pst && pst.dials && Object.keys(pst.dials).length) {
      drawRadar(pst, '该会话未单独设档位——显示其生效的项目层（实时值，1–9 刻度）');
    } else if (P.state.dials && P.state.dials.length) {
      drawRadar(P.state, '该会话未单独设档位——显示其生效的全局层（实时值，1–9 刻度）');
    } else {
      hideRadar('该会话无任何档位来源（会话/项目/全局三层均未设置）');
    }
  }
  renderTelemetry('sess', d);
}

function renderTelemetry(mode, d) {
  let t = P.trigRows || [], g = P.guardRows || [], dl = P.dialRows || [];
  let note = '全局口径', dialHint = 'set / off 最近 50 条', showSess = true;
  if (mode === 'sess' && d) {
    t = t.filter(r => r.session && d.sid && r.session.startsWith(d.sid));
    g = g.filter(r => r.session && d.sid && r.session.startsWith(d.sid));
    dl = dl.filter(r => r.session && d.sid && d.sid.startsWith(r.session));
    note = '会话视角'; dialHint = '该会话的 set / off'; showSess = false;
  } else if (mode === 'proj' && d) {
    t = t.filter(r => r.proj === d.id);
    g = g.filter(r => r.proj === d.id);
    dl = dl.filter(r => r.scope === 'project');  // 档位变更记录无 proj 字段，按写入层过滤
    note = '项目视角'; dialHint = '仅 scope=project 的变更（记录不含项目归属）'; showSess = false;
  }
  const hit = t.filter(r => r.hit).length;
  const kt = {}; t.forEach(r => (r.kinds || []).forEach(k => kt[k] = (kt[k] || 0) + 1));
  const ktop = Object.entries(kt).sort((a, b) => b[1] - a[1]).slice(0, 3);
  document.getElementById('telScope').textContent = '· ' + note;
  document.getElementById('trigHit').textContent = t.length ? Math.round(100 * hit / t.length) + '%' : '0%';
  document.getElementById('trigHint').textContent = '命中 ' + hit + ' / 扫描 ' + t.length
    + (ktop.length ? ' · 类别: ' + ktop.map(x => x[0] + '×' + x[1]).join('、') : '');
  const cats = {}; g.forEach(r => cats[r.cat] = (cats[r.cat] || 0) + 1);
  const top = Object.entries(cats).sort((a, b) => b[1] - a[1]).slice(0, 5);
  const gt = {}; g.forEach(r => { if (r.tool) gt[r.tool] = (gt[r.tool] || 0) + 1; });
  const gtop = Object.entries(gt).sort((a, b) => b[1] - a[1]).slice(0, 3);
  document.getElementById('guardN').textContent = g.length;
  document.getElementById('guardTop').textContent = (top.length ? 'Top: ' + top.map(x => x[0] + '×' + x[1]).join('、') : '尚无拦截')
    + (gtop.length ? ' · 工具: ' + gtop.map(x => x[0] + '×' + x[1]).join('、') : '');
  document.getElementById('dialN').textContent = dl.length;
  document.getElementById('dialHint').textContent = dialHint;
  document.getElementById('dialList').innerHTML = dl.length
    ? dl.map(r => '<code>' + r.ts + '</code> ' + r.action + ' → ' + r.persona + '（' + r.level + '，' + r.scope + '）'
        + (showSess && r.session ? ' <span style="color:#9a9994;">' + r.session + '…</span>' : '')).join('<br>')
    : '<span style="color:#888780;">该范围暂无档位变更记录</span>';
}

function hideRadar(msg) {
  if (charts.radar) { charts.radar.destroy(); delete charts.radar; }
  document.getElementById('radarWrap').style.display = 'none';
  // 同理：空态时必须把来源行一起藏掉，否则上一视角的来源文案会残留。
  document.getElementById('radarSrc').style.display = 'none';
  document.getElementById('radarNa').style.display = '';
  document.getElementById('radarNa').textContent = msg || '该视角没有对应的档位状态文件。';
  document.getElementById('stPersona').textContent = '';
  document.getElementById('stGist').textContent = '';
}

function renderDsh() {
  const fam = P.families || [];
  document.getElementById('dshFam').innerHTML = fam.length
    ? '工具族（' + fam.reduce((n, r) => n + r[1], 0) + ' 次调用）：'
      + fam.map((r) => r[0] + ' ' + r[1]).join(' · ')
    : '工具族：暂无';

  const a = P.approvals || {};
  let appr;
  if (!a.total) {
    appr = '审批结局：暂无 —— 这两类只有原生插件会写会话事件，重启 DSH 后开始积累';
  } else {
    appr = '审批结局：' + (a.byOutcome || []).map((r) => r[0] + ' ' + r[1]).join(' · ')
      + (a.rejectRate === null
        ? ' · 拒绝率 —（还没有用户做过选择；撤销与无通道不计入）'
        : ' · 拒绝率 ' + a.rejectRate + '%（' + a.rejected + '/' + (a.granted + a.rejected) + '）');
    if ((a.byTool || []).length) {
      appr += '；按工具：' + a.byTool.map((r) => r[0] + ' ' + r[1]).join(' · ');
    }
  }
  document.getElementById('dshAppr').innerHTML = appr;

  const t = P.perTurn || [];
  document.getElementById('dshTurn').innerHTML = t.length
    ? '每轮调用数：' + t.slice(0, 5)
      .map((r) => r.session + ' ' + r.callsPerTurn + '（' + r.calls + ' 次 / ' + r.turns + ' 轮）')
      .join(' · ')
    : '每轮调用数：暂无（turn 记录 ' + (P.turnsTotal || 0) + ' 条；同一会话至少 2 轮才算，'
      + '且只数首条 turn 之后的调用）—— 这是「档位是否让人一次到位」的相对量';

  const sk = P.skills || [];
  document.getElementById('dshSkill').innerHTML = sk.length
    ? '技能调用：' + sk.map((r) => r[0] + ' ' + r[1]).join(' · ')
    : '技能调用：暂无（本区间没有 skill 工具调用）';

  // 工具族柱状图
  if (fam.length) {
    mk('dshFamily', { type: 'bar',
      data: { labels: fam.map((r) => r[0]),
        datasets: [{ label: '调用次数', data: fam.map((r) => r[1]),
          backgroundColor: 'rgba(29,158,117,.55)', borderColor: '#0F6E56', borderWidth: 1 }] },
      options: { maintainAspectRatio: false, plugins: { legend: { display: false } },
        scales: { y: { beginAtZero: true, ticks: { precision: 0 } } } } });
  }

  // 每轮调用数：有档位前后对照就画成对柱（判定线关心的正是这个对比），否则画绝对量
  const dc = P.dialCompare || [];
  if (dc.length) {
    mk('dshTurnChart', { type: 'bar',
      data: { labels: dc.map((r) => r.session),
        datasets: [
          { label: '切档前（次/轮）', data: dc.map((r) => r.before.perTurn),
            backgroundColor: 'rgba(24,95,165,.55)', borderColor: '#185FA5', borderWidth: 1 },
          { label: '切档后（次/轮）', data: dc.map((r) => r.after.perTurn),
            backgroundColor: 'rgba(186,117,23,.55)', borderColor: '#BA7517', borderWidth: 1 }] },
      options: { maintainAspectRatio: false, plugins: { legend: { display: true } },
        scales: { y: { beginAtZero: true } } } });
  } else if (t.length) {
    mk('dshTurnChart', { type: 'bar',
      data: { labels: t.map((r) => r.session),
        datasets: [{ label: '次/轮', data: t.map((r) => r.callsPerTurn),
          backgroundColor: 'rgba(55,138,221,.55)', borderColor: '#185FA5', borderWidth: 1 }] },
      options: { maintainAspectRatio: false, plugins: { legend: { display: false } },
        scales: { y: { beginAtZero: true } } } });
  }
}

function renderAll() {
  const lite = liteMode();
  const mode = document.querySelector('input[name=vw]:checked').value;
  document.getElementById('projPick').disabled = mode !== 'proj';
  document.getElementById('sessPick').disabled = mode !== 'sess';
  if (mode === 'proj') renderProject((lite ? (P.liteProj || []) : P.projDetail)[+document.getElementById('projPick').value || 0]);
  else if (mode === 'sess') renderSession(+document.getElementById('sessPick').value || 0);
  else renderGlobal();
}

function liteMode() { return !P.l3 || document.getElementById('lite').checked; }

function applyMode() {
  const lite = liteMode();
  document.querySelectorAll('.hk').forEach(e => e.style.display = lite ? 'none' : '');
  document.getElementById('liteNote').style.display = lite ? '' : 'none';
  document.getElementById('telTitle').textContent = lite ? '档位变更' : '行为遥测 · 触发词 / 危险拦截 / 档位变更';
  fillProjPick();
  renderAll();
}

function fillProjPick() {
  const arr = liteMode() ? (P.liteProj || []) : P.projDetail;
  const el = document.getElementById('projPick');
  el.innerHTML = arr.length
    ? arr.map((d, i) => `<option value="${i}">${d.dir || ('proj ' + d.id)}${d.n ? ' · ' + d.n + ' 事件' : ''} · 活跃至 ${d.last}</option>`).join('')
    : '<option value="0">（暂无项目数据）</option>';
}

(function init() {
  const liteBox = document.getElementById('lite');
  if (!P.l3) { liteBox.checked = true; liteBox.disabled = true; }
  liteBox.addEventListener('change', applyMode);

  const projPick = document.getElementById('projPick');
  projPick.addEventListener('change', renderAll);

  const pick = document.getElementById('sessPick');
  pick.innerHTML = P.sessDetail.map((d, i) => {
    const own = d.sid && Object.keys(P.sessStates || {}).some(k => k.startsWith(d.sid));
    return `<option value="${i}">${own ? '★ ' : ''}${d.id} · 活跃至 ${d.last} · ${d.n} 事件${d.persona ? ' · ' + d.persona : ''}${own ? ' · 有专属档位' : ''}</option>`;
  }).join('');
  pick.addEventListener('change', renderAll);

  document.querySelectorAll('input[name=vw]').forEach(r => r.addEventListener('change', renderAll));

  // 注入构成（统计池口径）：会话开始 / 每轮对话 + 平均块长
  (function injComp() {
    const ie = Object.fromEntries(P.injEvents || []);
    const parts = [];
    if (ie.SessionStart) parts.push('会话开始 ' + ie.SessionStart);
    if (ie.UserPromptSubmit) parts.push('每轮对话 ' + ie.UserPromptSubmit);
    const ic = P.injChars || { avg: 0, n: 0 };
    document.getElementById('injComp').textContent = ic.n
      ? '注入构成（统计池内）：' + parts.join(' · ') + ' · 共 ' + ic.n + ' 次 · 平均块长 ' + ic.avg + ' 字'
      : '暂无注入记录';
  })();

  applyMode();
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    sys.exit(main())
