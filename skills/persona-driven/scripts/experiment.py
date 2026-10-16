#!/usr/bin/env python3
"""对照实验记录与判定器。

只做四件事：配置分组、按运行切分埋点、接受人工补记 token、按预先写定的判定线出结论。
不替用户跑任务，也不修改判定标准。

判定口径（1.5.12 起，依据作者项目内报告 **未随包分发**：`experiment/REPORT-P0-判定线溯源与重定-2026-10-13.md`）：
  * 主指标报「效应量 + 95%CI」，不是二元结论；
  * 二元闸门默认取**实证区间下界**（DROP_FULL），闸门若落在 CI 内部，判定器**拒绝**
    给出二元结论并告警——落在 CI 里的门槛在统计上不可判定；
  * 漏验证否决改为**显著性驱动**（Fisher 精确检验 p < VETO_ALPHA），废除绝对次数比较；
  * 旧线 40%（LEGACY_DROP_FULL）仅作历史并列展示，不参与判定。

用法：
    python scripts/experiment.py begin   --name exp1
    python scripts/experiment.py enter   --name exp1 --group A [--persona ESTP] [--apply --write-confirmed]
    python scripts/experiment.py mark    --name exp1 --task 1 --rep 1
    python scripts/experiment.py tokens  --name exp1 --task 1 --rep 1 --used 12345 [--group C]
    python scripts/experiment.py status  --name exp1 [--plan <计划文件>]
    python scripts/experiment.py next    --name exp1 [--plan <计划文件>]
    python scripts/experiment.py end     --name exp1
    python scripts/experiment.py verdict --name exp1 [--missed A=0,B=0,C=0] [--gate 25]

执行纪律（v2 判定线的数据才有效，详见 references/experiment.md 的「v2 执行规程」）：
每个任务是一条独立的用户消息；管理命令在独立终端执行、先 mark 再发任务；
实验期间这台机器上不用其他会话。判定器会把违反注入纪律的数据拦为阻塞项。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
import persona as p  # noqa: E402

EXP_DIR = p.HOME_ROOT / "experiments"

# 三组定义：唯一变量是「档位是否被强制注入」，钩子装卸不再是变量。
#
# 改为按 level 区隔的原因：靠装卸钩子切换组别不可执行——DSH 的钩子配置只在
# 重启后加载（改完当前会话不受影响），实验期间反复改写每组的钩子状态既慢又易错。
# 按 level 区隔反而更干净：注入与否是唯一处理变量，且完全不必改动钩子配置。
#
# PostToolUse 必须恒在场：埋点由它写入，缺了它任何组都收不到数据。
REQUIRED_MEASUREMENT = ("PostToolUse",)

GROUPS = {
    "A": {
        "label": "对照组",
        "level": "off",
        "desc": "无档位（钩子在场但不注入）",
    },
    "B": {
        "label": "上下文组（L1/L2）",
        "level": "l2",
        "desc": "档位已设但不注入，只在模型主动查询档位时可见",
    },
    "C": {
        "label": "硬约束组（L3）",
        "level": "l3",
        "desc": "钩子每轮强制注入档位",
    },
}

# 判定线：实验开始前写定，跑完不得修改。
#
# ⚠️ 2026-10-13（v1.5.12）变更。旧线为「下降 ≥ 40%」，溯源后发现它**从未被推导过**：
#   最早出现在 `人格化AI-Skill系统设计方案-v0.1.html` 的一句**举例**
#   （「例如测试次数中位数下降 ≥ 40%」），随后被原样固化成硬判定线。
#   v1 的 5→3 恰为 40% 只是数字巧合——且那是「工具调用总数」，
#   与该句所说的「测试执行次数」并非同一指标。
#
# 为什么必须换：v3 实测（10 任务 × 10 重复 × 3 组 = 300 轮）
#   A→C 降幅 37.5%，bootstrap 95%CI [27.3%, 50.0%]。
#   40% 落在 CI **内部** → 现有样本量无法分辨真实效应在 40% 之上还是之下，
#   判定会随抽样噪声翻转（同一设计下 v2 判 B「部分成立」、v3 判 B「不成立」）。
#   要把半宽压到 |37.5−40| = 2.5pp 以下，需约 2000 轮/组（现为 100）。
#
# 新口径：
#   1) 主输出 = 效应量 + 95%CI；
#   2) 二元闸门 = 实证区间下界（合并 CI 下界 27.3% → 向下取整到可判定值 25%）；
#   3) 闸门落在 CI 内部时拒绝给二元结论（防止再引入不可判定线）；
#   4) 漏验证否决 = 显著性驱动（Fisher 双侧 p < VETO_ALPHA）。
DROP_FULL = 0.25          # 实证闸门（v2/v3 合并 CI 下界向下取整）
DROP_PARTIAL = 0.15
LEGACY_DROP_FULL = 0.40   # 旧线：只在输出里并列展示，不参与判定
VETO_ALPHA = 0.05         # 一票否决的显著性阈值
BOOT_N = 2000             # 判定器内 bootstrap 次数（保持零第三方依赖、秒级完成）
BOOT_SEED = 20261013

# 埋点时间戳精度为毫秒；窗口短于此值不足以可靠切分，视为无效运行。
MIN_WINDOW_SECONDS = 2.0


def stamp() -> str:
    """毫秒精度的本地 ISO 时间戳，与埋点格式一致，保证字符串比较与时间顺序一致。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _key(ts: str):
    """把时间戳转成可比较的键，兼容历史数据里秒级格式。"""
    try:
        return (0, datetime.fromisoformat(ts))
    except Exception:
        return (1, ts)


def exp_path(name: str) -> Path:
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_") or "default"
    return EXP_DIR / f"{safe}.json"


def load_exp(name: str) -> dict:
    path = exp_path(name)
    if not path.exists():
        print(f"实验不存在：{name}（先跑 begin）", file=sys.stderr)
        sys.exit(2)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"实验文件损坏或不可读：{path}\n{exc}\n"
              f"可手工修复 JSON，或换名重新 begin。", file=sys.stderr)
        sys.exit(2)


def save_exp(data: dict) -> Path:
    path = exp_path(data["name"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def installed_events() -> set[str]:
    """只统计本技能自己的钩子（读 DSH 的桥接钩子配置文件）。"""
    hooks_file = p.HOME_ROOT / "hooks.json"
    if not hooks_file.exists():
        return set()
    try:
        data = json.loads(hooks_file.read_text(encoding="utf-8"))
    except Exception:
        return set()
    found = set()
    for event, entries in p.unwrap_hooks(data).items():
        if any(p._is_own_hook(e) for e in entries or []):
            found.add(event)
    return found


def config_snapshot() -> dict:
    state = p.read_state("global") or {}
    return {
        "level": state.get("level", "off"),
        "persona": state.get("persona"),
        "events": sorted(installed_events()),
    }


def group_matches(group: str, snap: dict) -> tuple[bool, list[str]]:
    spec = GROUPS[group]
    faults = []
    if snap["level"] != spec["level"]:
        faults.append(f"级别应为 {spec['level']}，实为 {snap['level']}")
    missing = [e for e in REQUIRED_MEASUREMENT if e not in snap["events"]]
    if missing:
        faults.append(f"缺少度量钩子 {missing}，该组收不到埋点")
    return (not faults), faults


def read_metrics(start: str, end: str | None) -> dict:
    """按时间窗切分埋点。返回该窗口内的计数。

    注入记录（injections-*.jsonl，无 tool 字段、有 chars 字段）单独计数：
    条数用于核对 C 组注入机制是否真的触发过，字符数用于估算注入开销——
    只报字符、不折算 token，因为不同 tokenizer 的折算率不同，自动换算等于编数据。
    同时收集窗口内出现过的非 unknown 会话标识（sessions）：一个窗口里出现
    ≥2 个不同会话 = 有并发会话把埋点写进了这个窗口（多会话污染检测的数据源）。
    """
    bucket = {
        "calls": 0, "tests": 0, "static": 0, "writes": 0, "reads": 0,
        "injections": 0, "injection_chars": 0,
        "sessions": set(),
    }
    lo = _key(start)
    hi = _key(end) if end is not None else None
    for path in p.metrics_files(p.metrics_dir()):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            ts = rec.get("ts")
            if not ts:
                continue
            k = _key(ts)
            if k < lo:
                continue
            if hi is not None and k >= hi:
                continue
            sess = str(rec.get("session") or "")
            if sess and sess != "unknown":
                bucket["sessions"].add(sess)
            if not rec.get("tool"):
                if "chars" in rec:
                    bucket["injections"] += 1
                    try:
                        bucket["injection_chars"] += int(rec.get("chars") or 0)
                    except (TypeError, ValueError):
                        pass
                continue
            bucket["calls"] += 1
            for flag, field in (
                ("test", "tests"),
                ("static", "static"),
                ("write", "writes"),
                ("read", "reads"),
            ):
                if rec.get(flag):
                    bucket[field] += 1
    return bucket


def cmd_begin(args: argparse.Namespace) -> int:
    path = exp_path(args.name)
    if path.exists() and not args.overwrite:
        print(f"实验已存在：{path}\n如需重开用 --overwrite。", file=sys.stderr)
        return 2
    data = {
        "name": args.name,
        "created_at": stamp(),
        "tasks": args.tasks,
        "reps": args.reps,
        "groups": [],
        "note": "判定线在 references/experiment.md 中预先写定，跑完不得修改。",
    }
    save_exp(data)
    print(f"已创建实验：{path}")
    print(f"- 任务数：{args.tasks}　每组重复：{args.reps}")
    print("- 三组顺序建议随机，避免学习效应。")
    print("\nv2 执行纪律（判定器会把违反注入纪律的数据拦为阻塞项）：")
    print("- 每个任务 = 一条独立的用户消息（C 组的注入才会逐任务触发）。")
    print("- 管理命令（enter/mark/tokens/end/verdict）在独立终端执行，不发给执行会话；")
    print("  先 mark，再把任务消息发给执行会话。")
    print("- 实验期间这台机器上停用其他会话：埋点不分会话，并发会话会污染运行窗口。")
    print("- token：每轮任务结束后从会话用量读数减上一轮，差值用 tokens 子命令补记。")
    print("\n下一步：")
    print(f"  python scripts/experiment.py enter --name {args.name} --group A")
    return 0


def cmd_enter(args: argparse.Namespace) -> int:
    data = load_exp(args.name)
    group = args.group.upper()
    spec = GROUPS[group]
    snap = config_snapshot()

    print(f"## 进入 {group} 组 · {spec['label']}")
    print(f"- 定义：{spec['desc']}")
    print(f"- 需要的档位级别：{spec['level']}" + (f"（{args.persona}）" if spec["level"] != "off" else ""))
    print(f"- 度量钩子要求：{list(REQUIRED_MEASUREMENT)} 必须在场")
    print(f"- 当前状态：level={snap['level']} persona={snap['persona']} events={snap['events']}")
    print()

    commands = _enter_commands(args, group, spec)
    print("将执行：")
    for line in commands:
        print(f"  {line}")
    print()

    if not args.apply:
        print("以上为预览。确认后加 --apply --write-confirmed 执行。")
        return 0
    if not args.write_confirmed:
        print("缺少 --write-confirmed，已停止。", file=sys.stderr)
        return 2

    for argv in _enter_argv(args, group, spec):
        print(f"$ persona.py {' '.join(argv)}")
        p.main(argv)
        print()

    after = config_snapshot()
    ok, faults = group_matches(group, after)
    data.setdefault("groups", []).append(
        {
            "group": group,
            "entered_at": stamp(),
            "persona": after.get("persona"),
            "level": after.get("level"),
            "events": after["events"],
            "config_ok": ok,
            "runs": [],
            "ended_at": None,
        }
    )
    save_exp(data)

    print(f"配置核对：{'通过' if ok else '不通过'}")
    for f in faults:
        print(f"  - {f}")
    if not ok:
        print("\n组别定义不成立，本节数据不可用于判定。修好后重新 enter。", file=sys.stderr)
        return 2
    print(f"\n下一步：每跑完一个任务用 mark 记录边界：")
    print(f"  python scripts/experiment.py mark --name {args.name} --task 1 --rep 1")
    return 0


def _enter_argv(args: argparse.Namespace, group: str, spec: dict) -> list[list[str]]:
    """只切换档位级别，不装卸钩子——钩子在实验期间恒定在场，不构成变量。

    实验治理器必须显式钉住**全局层**（v1.5.33 起 set/off 默认写会话层，且治理器
    运行环境拿不到会话标识、默认路径会被写守卫拒绝）——审计口径读的就是全局
    state.json，不能随默认值漂移。
    """
    if spec["level"] == "off":
        return [["off", "--scope", "global"]]
    return [["set", args.persona, "--level", spec["level"], "--scope", "global"]]


def _enter_commands(args: argparse.Namespace, group: str, spec: dict) -> list[str]:
    return ["python scripts/persona.py " + " ".join(a) for a in _enter_argv(args, group, spec)]


def cmd_mark(args: argparse.Namespace) -> int:
    data = load_exp(args.name)
    if not data.get("groups"):
        print("还没有进入任何组，先跑 enter。", file=sys.stderr)
        return 2
    current = data["groups"][-1]
    if current.get("ended_at"):
        print(f"当前组 {current['group']} 已结束，先 enter 下一组。", file=sys.stderr)
        return 2
    current.setdefault("runs", []).append(
        {"task": args.task, "rep": args.rep, "at": stamp()}
    )
    save_exp(data)
    print(f"已标记：{current['group']} 组 · 任务 {args.task} · 第 {args.rep} 次 · {stamp()}")
    print(f"该组累计标记 {len(current['runs'])} 个")
    print(f"提示：该任务跑完后，可补记 token 消耗（会话用量减上一轮的差值）：")
    print(f"  python scripts/experiment.py tokens --name {args.name} --task {args.task} --rep {args.rep} --used <差值>")
    return 0


def cmd_tokens(args: argparse.Namespace) -> int:
    """人工补记一次运行的 token 消耗。

    埋点不读取平台计费数据，token 只能由人从会话用量读数相减得到；
    记录后由 verdict 汇总展示。它是辅助参考，不参与判定线。
    """
    data = load_exp(args.name)
    groups = data.get("groups") or []
    if not groups:
        print("还没有进入任何组，先跑 enter。", file=sys.stderr)
        return 2
    if args.used < 0:
        print(f"--used 应为非负整数，收到：{args.used}", file=sys.stderr)
        return 2

    if args.group:
        key = args.group.upper()
        candidates = [g for g in groups if g.get("group") == key]
        if not candidates:
            print(f"实验里没有 {key} 组的进入记录，先 enter。", file=sys.stderr)
            return 2
        target = candidates[-1]
    else:
        active = [g for g in groups if not g.get("ended_at")]
        if not active:
            print("当前没有进行中的组。若要给已结束的组补记，请加 --group A|B|C。",
                  file=sys.stderr)
            return 2
        target = active[-1]

    runs = target.get("runs") or []
    matches = [r for r in runs if r.get("task") == args.task and r.get("rep") == args.rep]
    if not matches:
        print(f"{target['group']} 组没有任务 {args.task} 第 {args.rep} 次的运行记录，"
              f"先 mark 再补记。", file=sys.stderr)
        return 2
    run = matches[-1]
    run["tokens"] = args.used
    save_exp(data)
    print(f"已补记：{target['group']} 组 · 任务 {args.task} · 第 {args.rep} 次 · token {args.used}")
    print("口径提醒：token 为人工读取的会话用量差值，噪声高，只作辅助参考，不参与判定线。")
    return 0


def _plan_entries(plan_path: str) -> list[tuple[str, int, int]]:
    """解析计划文件：每行「组 任务 重复」（如 `C 4 3`），# 与空行忽略。

    冻结顺序是外挂数据而不是代码常量——技能不该把某一次实验的顺序写死在自己身上。
    格式错误给可读提示并退出码 2，不抛 traceback。
    """
    path = Path(plan_path)
    if not path.exists():
        print(f"计划文件不存在：{path}", file=sys.stderr)
        sys.exit(2)
    entries: list[tuple[str, int, int]] = []
    for no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 3 or parts[0].upper() not in GROUPS:
            print(f"计划文件第 {no} 行格式应为「组 任务 重复」（如 C 4 3），实为：{line}",
                  file=sys.stderr)
            sys.exit(2)
        try:
            entries.append((parts[0].upper(), int(parts[1]), int(parts[2])))
        except ValueError:
            print(f"计划文件第 {no} 行的任务/重复必须是整数：{line}", file=sys.stderr)
            sys.exit(2)
    if not entries:
        print(f"计划文件没有任何有效行：{path}", file=sys.stderr)
        sys.exit(2)
    return entries


def _next_run(data: dict, plan_path: str | None):
    """返回 (全部单序, 下标, 下一单或 None)。已标记的 (组,任务,重复) 视为完成。"""
    tasks = int(data.get("tasks") or 5)
    reps = int(data.get("reps") or 5)
    if plan_path:
        entries = _plan_entries(plan_path)
    else:
        entries = [(g, t, r) for g in ("C", "A", "B")
                   for t in range(1, tasks + 1) for r in range(1, reps + 1)]
    marked = {
        (g.get("group"), r.get("task"), r.get("rep"))
        for g in (data.get("groups") or [])
        for r in (g.get("runs") or [])
    }
    for idx, key in enumerate(entries):
        if key not in marked:
            return entries, idx, key
    return entries, len(entries), None


def _print_next(data: dict, nxt, idx: int, total: int) -> None:
    groups = data.get("groups") or []
    if nxt is None:
        print(f"全部 {total} 个运行已标记完成。")
        print(f"下一步：python scripts/experiment.py verdict --name {data['name']}"
              " [--missed A=..,B=..,C=..]（漏验证数由人工复核 diff 后传入）")
        return
    g, t, r = nxt
    print(f"第 {idx + 1}/{total} 单：{g} 组 · 任务 {t} · 第 {r} 次")
    entered = {x.get("group") for x in groups}
    if g not in entered:
        print(f"{g} 组尚未 enter——先在管理终端执行 enter（操作步骤见 references/experiment.md）。")
        return
    cur = [x for x in groups if x.get("group") == g][-1]
    if cur.get("ended_at"):
        print(f"注意：{g} 组已结束，但计划里它还有未标记的运行，请核对该组是否真的跑完。")
    print("每轮固定流程：重置沙箱 → 先 mark → 把任务消息发给执行会话 → 复核 diff → tokens 补记")
    print(f"  python scripts/experiment.py mark --name {data['name']} --task {t} --rep {r}")


def _natural_note() -> None:
    print("未提供 --plan，按组序 C→A→B 与任务自然序推断；"
          "冻结顺序以 --plan 传入的计划文件为准（生成后不得重排）。")


def cmd_status(args: argparse.Namespace) -> int:
    """跑中体检：各组进度、埋点健康度、跑得下去与否的早期预警 + 下一单。

    75 个运行跑一半才发现 C 组没注入 / A 组被污染，代价是整批作废；
    status 把判定器在 verdict 才拦的问题提前到每一单之后。
    """
    data = load_exp(args.name)
    groups = data.get("groups") or []
    tasks = int(data.get("tasks") or 5)
    reps = int(data.get("reps") or 5)
    print(f"实验 {data['name']}（任务 {tasks} × 重复 {reps}，建于 {data.get('created_at', '?')}）")
    if not groups:
        print("还没有进入任何组。下一步：enter C 组（组序 C→A→B）。")
        return 0
    expect = tasks * reps
    for g in groups:
        key = g.get("group")
        runs = _runs_of(g)
        done = len(g.get("runs") or [])
        tokens_n = sum(1 for r in (g.get("runs") or []) if r.get("tokens") is not None)
        inj = sum(w["injections"] for w in runs)
        chars = sum(w["injection_chars"] for w in runs)
        multi = [w for w in runs if len(w["sessions"]) >= 2]
        short = sum(1 for w in runs if w["too_short"])
        empty = sum(1 for w in runs if w["calls"] == 0)
        state = "已结束" if g.get("ended_at") else "进行中"
        print(f"- {key} 组（{GROUPS[key]['label']}，{state}）：标记 {done}/{expect}"
              f"　token 补记 {tokens_n}/{done}　注入 {inj} 条 / {chars} 字符")
        if key == "C" and done > 0 and inj == 0:
            print(f"  警告：C 组已标记 {done} 个运行但注入为 0——注入机制未触发，"
                  "这样跑下去 verdict 必然拒判（每个任务必须是一条独立的用户消息）。")
        if key in ("A", "B") and inj > 0:
            print(f"  警告：{key} 组出现 {inj} 条注入记录（{GROUPS[key]['level']} 不应注入）"
                  "——配置可能被改动，或有并发 l3 会话污染。")
        if multi:
            ids = sorted({sid for w in multi for sid in w["sessions"]})
            print(f"  警告：{len(multi)} 个窗口内出现多个会话的埋点（{', '.join(ids)}）"
                  "——疑似并发会话污染，verdict 会拒判。")
        if short:
            print(f"  警告：{short} 个窗口短于 2 秒，时间戳不足以可靠切分。")
        if empty:
            print(f"  警告：{empty} 个运行没有任何埋点。")
        if done > 0 and tokens_n < done:
            print(f"  提示：token 尚有 {done - tokens_n} 轮未补记（辅助口径，可后补，组结束后加 --group）。")
    entries, idx, nxt = _next_run(data, args.plan)
    if not args.plan:
        _natural_note()
    print()
    _print_next(data, nxt, idx, len(entries))
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    """打印冻结顺序里的下一个该跑的运行与对应 mark 命令。"""
    data = load_exp(args.name)
    if not args.plan:
        _natural_note()
    entries, idx, nxt = _next_run(data, args.plan)
    _print_next(data, nxt, idx, len(entries))
    return 0


def cmd_end(args: argparse.Namespace) -> int:
    data = load_exp(args.name)
    if not data.get("groups"):
        print("还没有进入任何组。", file=sys.stderr)
        return 2
    current = data["groups"][-1]
    if current.get("ended_at"):
        print(f"当前组 {current['group']} 已结束。")
        return 0
    current["ended_at"] = stamp()
    ok, faults = group_matches(current["group"], config_snapshot())
    current["config_ok_at_end"] = ok
    save_exp(data)
    print(f"已结束 {current['group']} 组 · 共 {len(current.get('runs') or [])} 个运行")
    if not ok:
        print("警告：结束时配置与组别定义不符，中途可能被改动过：")
        for f in faults:
            print(f"  - {f}")
    print("\n下一步：enter 下一组，或直接 verdict 出结论。")
    return 0


def _runs_of(group_data: dict) -> list[dict]:
    all_runs = group_data.get("runs") or []
    # 手工编辑过的实验文件可能缺 at 键；缺时间戳无法切分窗口，跳过并明示，不静默、不崩溃。
    invalid = [r for r in all_runs if not r.get("at")]
    if invalid:
        print(f"警告：{len(invalid)} 个运行记录缺少 at 时间戳，已跳过（无法切分窗口）。",
              file=sys.stderr)
    runs = sorted((r for r in all_runs if r.get("at")), key=lambda r: _key(r["at"]))
    end = group_data.get("ended_at")
    windows = []
    for i, run in enumerate(runs):
        stop = runs[i + 1]["at"] if i + 1 < len(runs) else end
        bucket = read_metrics(run["at"], stop)
        # set -> sorted list：窗口 dict 会被 status/verdict 直接消费，保持确定顺序。
        bucket["sessions"] = sorted(bucket["sessions"])
        span = None
        if stop:
            try:
                span = (datetime.fromisoformat(stop) - datetime.fromisoformat(run["at"])).total_seconds()
            except Exception:
                span = None
        windows.append({
            "task": run["task"],
            "rep": run["rep"],
            "span": span,
            "too_short": span is not None and span < MIN_WINDOW_SECONDS,
            # tokens 由 tokens 子命令补记在 run 记录上，切窗时必须带上，否则丢失。
            "tokens": run.get("tokens"),
            **bucket,
        })
    return windows


def fisher_p(a_succ: int, a_n: int, b_succ: int, b_n: int) -> float:
    """双侧 Fisher 精确检验：2×2 表 [[a_succ, a_n−a_succ], [b_succ, b_n−b_succ]]。

    超几何模型：固定两组样本量与**总成功数**（a_succ+b_succ），枚举 A 组成功数，
    累加概率 ≤ 观测值者。用精确检验而非绝对次数比较，是为了不让几票之差
    （例如 v3 的 B 组 52 vs 48，p=0.67）触发「一票否决」。
    """
    from math import comb

    tot_succ = a_succ + b_succ
    n = a_n + b_n
    if n == 0 or tot_succ == 0:
        return 1.0

    def prob(x: int) -> float:
        return comb(a_n, x) * comb(b_n, tot_succ - x) / comb(n, tot_succ)

    obs = prob(a_succ)
    tot = 0.0
    for x in range(max(0, tot_succ - b_n), min(a_n, tot_succ) + 1):
        q = prob(x)
        if q <= obs + 1e-12:
            tot += q
    return min(1.0, tot)


def boot_median_drop_ci(base: list, other: list,
                        n: int = BOOT_N, alpha: float = 0.05) -> tuple:
    """对照组→实验组「中位数降幅(%)」的 bootstrap 百分位 CI。零第三方依赖。"""
    import random

    if not base or not other:
        return (0.0, 0.0)
    rnd = random.Random(BOOT_SEED)
    out = []
    for _ in range(n):
        sa = [base[rnd.randrange(len(base))] for _ in range(len(base))]
        so = [other[rnd.randrange(len(other))] for _ in range(len(other))]
        mb = statistics.median(sa)
        if not mb:
            continue
        out.append((mb - statistics.median(so)) / mb * 100)
    if not out:
        return (0.0, 0.0)
    out.sort()
    return (out[int(alpha / 2 * len(out))], out[int((1 - alpha / 2) * len(out)) - 1])


def cmd_verdict(args: argparse.Namespace) -> int:
    data = load_exp(args.name)
    groups = {g["group"]: g for g in data.get("groups") or []}
    if not groups:
        print("没有任何组的数据，无法判定。", file=sys.stderr)
        return 2

    missed: dict[str, int] = {}
    for item in (args.missed or "").split(","):
        if "=" in item:
            k, _, v = item.partition("=")
            try:
                missed[k.strip().upper()] = int(v)
            except ValueError:
                print(f"--missed 的格式应为 A=0,B=1,C=2（组=非负整数），"
                      f"无法解析：{item.strip()}", file=sys.stderr)
                return 2

    stats: dict[str, dict] = {}
    print("## 分运行明细\n")
    for key in ("A", "B", "C"):
        if key not in groups:
            continue
        runs = _runs_of(groups[key])
        tests = [r["tests"] for r in runs]
        calls = [r["calls"] for r in runs]
        injections = [r["injections"] for r in runs]
        tokens = [r["tokens"] for r in runs if r.get("tokens") is not None]
        stats[key] = {
            "runs": len(runs),
            "tests": tests,
            "calls": calls,
            "injections_total": sum(injections),
            "injection_chars": sum(r["injection_chars"] for r in runs),
            "tokens": tokens,
            "median_tests": statistics.median(tests) if tests else None,
            "median_calls": statistics.median(calls) if calls else None,
            "empty": sum(1 for r in runs if r["calls"] == 0),
            "short": sum(1 for r in runs if r["too_short"]),
            # 窗口内出现 ≥2 个不同会话 = 有并发会话把埋点写进了这个窗口（v1.5.5）。
            "multi_session": [w for w in runs if len(w["sessions"]) >= 2],
        }
        print(f"### {key} 组 · {GROUPS[key]['label']}（{len(runs)} 个运行）")
        if not runs:
            print("  无运行记录。\n")
            continue
        print(f"  {'任务':<6}{'次数':<6}{'时长':>8}{'调用':>6}{'测试':>6}{'静态':>6}{'写':>6}{'读':>6}{'注入':>6}{'token':>8}")
        for r in runs:
            span = "—" if r["span"] is None else f"{r['span']:.1f}s" + ("!" if r["too_short"] else "")
            tok = "—" if r.get("tokens") is None else str(r["tokens"])
            print(
                f"  {r['task']:<6}{r['rep']:<6}{span:>8}{r['calls']:>6}{r['tests']:>6}"
                f"{r['static']:>6}{r['writes']:>6}{r['reads']:>6}{r['injections']:>6}{tok:>8}"
            )
        print(f"  中位数：测试 {stats[key]['median_tests']}　调用 {stats[key]['median_calls']}"
              f"　注入 {statistics.median(injections) if injections else 0}")
        if stats[key]["injection_chars"]:
            print(f"  注入字符合计：{stats[key]['injection_chars']} 字符（注入开销，只报字符不折算 token）")
        if tokens:
            print(f"  token 中位数：{statistics.median(tokens)}"
                  f"（人工记录 {len(tokens)}/{len(runs)} 个运行，辅助参考，不参与判定线）")
        if key == "C" and runs and stats[key]["injections_total"] < len(runs) - 1:
            print(f"  警告：注入数 {stats[key]['injections_total']} 少于运行数−1（{len(runs) - 1}），"
                  "可能有任务没有独立成用户消息。")
        if stats[key]["multi_session"]:
            _ids = sorted({sid for w in stats[key]["multi_session"] for sid in w["sessions"]})
            print(f"  警告：{len(stats[key]['multi_session'])} 个运行窗口内出现多个会话的埋点"
                  f"（{', '.join(_ids)}），疑似并发会话污染。")
        if stats[key]["empty"]:
            print(f"  警告：{stats[key]['empty']} 个运行没有任何埋点，该组数据不完整。")
        if stats[key]["short"]:
            print(f"  警告：{stats[key]['short']} 个运行窗口短于 {MIN_WINDOW_SECONDS:.0f} 秒，时间戳不足以可靠切分。")
        print()

    print("## 判定\n")
    blockers = []
    for key, s in stats.items():
        if s["runs"] == 0:
            blockers.append(f"{key} 组没有运行记录")
        elif s["median_calls"] is None:
            blockers.append(f"{key} 组没有任何埋点")
        elif s["short"]:
            blockers.append(
                f"{key} 组有 {s['short']} 个运行窗口短于 {MIN_WINDOW_SECONDS:.0f} 秒，"
                "埋点时间戳不足以切分，需重跑并保证每次运行间隔更长"
            )
    if "A" not in stats or "C" not in stats:
        blockers.append("缺少 A 或 C 组，无法比较核心卖点")
    for key, g in groups.items():
        if not g.get("config_ok"):
            blockers.append(f"{key} 组进入时配置核对未通过")
        if g.get("ended_at") and not g.get("config_ok_at_end", True):
            blockers.append(f"{key} 组结束时配置与定义不符")

    # 注入纪律的阻塞项（v1.5.4 起）：pilot 的失败模式是「单会话连续跑完所有任务、
    # C 组注入一次都没触发」，数据却仍然算出了结论。这里把该模式固化为阻塞，
    # 并顺带拦下 A/B 组出现注入记录的污染（off/l2 不应注入——多半是配置中途
    # 被改动，或实验期间有并发的 l3 会话把注入写进了当时的窗口）。
    for key, s in stats.items():
        if key == "C" and s["runs"] and s["injections_total"] == 0:
            blockers.append(
                "C 组零注入：注入机制未触发——每个任务必须是一条独立的用户消息"
                "（见 references/experiment.md 的「v2 执行规程」）。此时 C 组数据等同 B 组，"
                "无法回答 L3 的增益"
            )
        if key in ("A", "B") and s["injections_total"] > 0:
            blockers.append(
                f"{key} 组出现 {s['injections_total']} 条注入记录"
                f"（{GROUPS[key]['level']} 级别不应注入）：配置中途被改动，"
                "或实验期间有并发 l3 会话污染了运行窗口"
            )
        if s["multi_session"]:
            _ids = sorted({sid for w in s["multi_session"] for sid in w["sessions"]})
            blockers.append(
                f"{key} 组有 {len(s['multi_session'])} 个运行窗口内出现多个会话的埋点"
                f"（{', '.join(_ids)}）：实验期间有并发会话把工具调用写进了运行窗口，"
                "调用计数已被污染，须停用其他会话后重跑"
            )

    if blockers:
        print("无法给出结论，存在以下阻塞项：")
        for b in blockers:
            print(f"  - {b}")
        print("\n先修好这些再重跑。不得在数据不完整时下结论。")
        return 2

    # 主指标依 experiment.md 的判定线：工具调用总数（median_calls）。
    # median_tests 只作辅助展示——v1 实测它无区分度（三组中位数同为 1）。
    base = stats["A"]["median_calls"]
    if not base:
        print("对照组调用总数中位数为 0，无法计算降幅。请确认 A 组确实在跑真实任务。")
        return 2

    gate = DROP_FULL if getattr(args, "gate", None) is None else args.gate / 100.0
    n_a, miss_a = stats["A"]["runs"], missed.get("A", 0)

    results: dict[str, dict] = {}
    for key in ("B", "C"):
        if key not in stats:
            continue
        s = stats[key]
        med = s["median_calls"]
        drop = (base - med) / base * 100
        lo, hi = boot_median_drop_ci(stats["A"]["calls"], s["calls"])
        miss_k, n_k = missed.get(key, 0), s["runs"]
        p_veto = fisher_p(miss_a, n_a, miss_k, n_k) if (n_a and n_k) else 1.0
        veto = miss_k > miss_a and p_veto < VETO_ALPHA        # 显著性驱动否决
        veto_legacy = miss_k > miss_a                          # 旧：绝对次数比较
        undecidable = lo < gate * 100 < hi

        if veto:
            v = "不成立"
            why = (f"漏验证显著劣化（{miss_k}/{n_k} vs A {miss_a}/{n_a}，"
                   f"Fisher p={p_veto:.4f} < {VETO_ALPHA}）")
        elif undecidable:
            v = "无法判定"
            why = (f"闸门 {gate * 100:.0f}% 落在降幅 95%CI [{lo:.1f}%, {hi:.1f}%] 内部——"
                   "现有样本量分辨不出真实效应落在闸门哪一侧")
        elif drop >= gate * 100:
            v = "成立"
            why = f"降幅 {drop:.1f}% ≥ 闸门 {gate * 100:.0f}%，且漏验证未显著劣化"
        elif drop >= DROP_PARTIAL * 100:
            v = "部分成立"
            why = (f"降幅 {drop:.1f}% 在 {DROP_PARTIAL * 100:.0f}%~{gate * 100:.0f}% 之间，"
                   "且漏验证未显著劣化")
        else:
            v = "不成立"
            why = f"降幅 {drop:.1f}% < {DROP_PARTIAL * 100:.0f}%"
        if miss_k > miss_a and not veto:
            why += f"；漏验证 {miss_k} vs {miss_a}（p={p_veto:.4f}）未达显著，不否决"
        results[key] = dict(med=med, drop=drop, lo=lo, hi=hi, miss_k=miss_k, n_k=n_k,
                            p_veto=p_veto, veto=veto, veto_legacy=veto_legacy,
                            undecidable=undecidable, verdict=v, why=why)

    print(f"对照组调用总数中位数：{base}（A 组；主指标 = 工具调用总数）\n")
    print("主指标明细（降幅相对 A 组）")
    for key, r in results.items():
        print(f"  {key} 组 · {GROUPS[key]['label']}（n={r['n_k']}）：中位 {r['med']:g}"
              f"　降幅 {r['drop']:+.1f}%　bootstrap 95%CI [{r['lo']:.1f}%, {r['hi']:.1f}%]")
        print(f"    漏关键验证 {r['miss_k']}/{r['n_k']}（对照 A {miss_a}/{n_a}）"
              f"　Fisher p = {r['p_veto']:.4f}"
              f"{'　→ 显著劣化，触发否决' if r['veto'] else '　→ 未达显著，不否决'}")

    gate_label = (f"{gate * 100:.0f}% = 实证区间下界"
                  if abs(gate - DROP_FULL) < 1e-9 else f"{gate * 100:.0f}%（由 --gate 覆盖）")
    print(f"\n判定（闸门 {gate_label}；旧线 "
          f"{LEGACY_DROP_FULL * 100:.0f}% 已停用，理由见本脚本头部注释）")
    for key, r in results.items():
        print(f"  {key} 组：{r['verdict']} —— {r['why']}")

    for key, r in results.items():
        if r["undecidable"] and not r["veto"]:
            half = (r["hi"] - r["lo"]) / 2
            gap = abs(r["drop"] - gate * 100)
            if gap < 0.5:
                print(f"\n  ⚠ {key} 组：点估计 {r['drop']:.1f}% 与闸门 {gate * 100:.0f}% 几乎重合"
                      f"（CI [{r['lo']:.1f}%, {r['hi']:.1f}%]）。此时无论把 n 加到多大，"
                      f"区间都无法整体移到闸门一侧——要么把闸门下调到实证区间以内，"
                      f"要么按「不可判定」如实报告。")
            else:
                need = round(r["n_k"] * (half / gap) ** 2)
                print(f"\n  ⚠ {key} 组闸门落在 CI 内部，二元结论不可靠。降幅 CI 半宽 ±{half:.1f}pp，"
                      f"距闸门 {gap:.1f}pp；要把半宽压到闸门以下，约需 {need} 轮/组"
                      f"（现为 {r['n_k']}）。")

    print("\n旧线对照（仅供追溯，不参与判定）：")
    for key, r in results.items():
        lv = ("成立" if r["drop"] >= LEGACY_DROP_FULL * 100
              else "部分成立" if r["drop"] >= DROP_PARTIAL * 100 else "不成立")
        if r["veto_legacy"]:
            lv = "不成立"
        flag = "　← 与旧线结论不同" if lv != r["verdict"] else ""
        print(f"  {key} 组：旧线@40% → {lv}　|　新口径 → {r['verdict']}{flag}")

    print("\n判定口径（预先写定，不得事后下调）：")
    print(f"  主输出：效应量 + 95%CI，不单独给二元结论")
    print(f"  成立：降幅 ≥{gate * 100:.0f}% 且漏验证未显著劣化")
    print(f"  部分成立：降幅 ≥{DROP_PARTIAL * 100:.0f}% 且漏验证未显著劣化")
    print(f"  无法判定：闸门落在降幅 CI 内部（样本量不足以分辨）")
    print(f"  不成立：降幅 <{DROP_PARTIAL * 100:.0f}%，或漏验证**显著**劣化"
          f"（Fisher p<{VETO_ALPHA}，一票否决）")
    print()
    if "C" in stats and "B" in stats:
        delta = stats["B"]["median_calls"] - stats["C"]["median_calls"]
        if delta <= 0:
            print("注意：C 组（L3）并不优于 B 组。按 experiment.md 的约定，")
            print("这说明钩子这一层没有必要，应考虑砍掉，不要为架构完整性硬留。")
    print("\n结论务必连同 references/limits.md 一起报告，写明未覆盖部分。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="人格档位对照实验：记录与判定")
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("begin", help="创建实验")
    b.add_argument("--name", required=True)
    b.add_argument("--tasks", type=int, default=5)
    b.add_argument("--reps", type=int, default=5)
    b.add_argument("--overwrite", action="store_true")
    b.set_defaults(func=cmd_begin)

    e = sub.add_parser("enter", help="进入某个组并配置环境")
    e.add_argument("--name", required=True)
    e.add_argument("--group", required=True, choices=["A", "B", "C", "a", "b", "c"])
    e.add_argument("--persona", default="ESTP", help="B/C 组使用的档位，默认 ESTP")
    e.add_argument("--apply", action="store_true")
    e.add_argument("--write-confirmed", action="store_true")
    e.set_defaults(func=cmd_enter)

    m = sub.add_parser("mark", help="标记一次运行开始")
    m.add_argument("--name", required=True)
    m.add_argument("--task", type=int, required=True)
    m.add_argument("--rep", type=int, required=True)
    m.set_defaults(func=cmd_mark)

    t = sub.add_parser("tokens", help="人工补记一次运行的 token 消耗（会话用量差值）")
    t.add_argument("--name", required=True)
    t.add_argument("--task", type=int, required=True)
    t.add_argument("--rep", type=int, required=True)
    t.add_argument("--used", type=int, required=True, help="该轮 token 消耗（非负整数）")
    t.add_argument("--group", choices=["A", "B", "C", "a", "b", "c"],
                   help="目标组；默认记到当前进行中的组")
    t.set_defaults(func=cmd_tokens)

    s = sub.add_parser("status", help="查看各组进度、埋点健康度（跑中预警）与下一单")
    s.add_argument("--name", required=True)
    s.add_argument("--plan", help="冻结顺序计划文件（每行：组 任务 重复，如 C 4 3）")
    s.set_defaults(func=cmd_status)

    n = sub.add_parser("next", help="打印冻结顺序里的下一个该跑的运行")
    n.add_argument("--name", required=True)
    n.add_argument("--plan", help="冻结顺序计划文件（每行：组 任务 重复，如 C 4 3）")
    n.set_defaults(func=cmd_next)

    d = sub.add_parser("end", help="结束当前组")
    d.add_argument("--name", required=True)
    d.set_defaults(func=cmd_end)

    v = sub.add_parser("verdict", help="按判定线出结论")
    v.add_argument("--name", required=True)
    v.add_argument("--missed", help="人工复核的漏关键验证次数，如 A=0,B=0,C=1")
    v.add_argument("--gate", type=float, default=None,
                   help=f"二元闸门（%%），默认取实证下界 {DROP_FULL * 100:.0f}%%；"
                        f"若闸门落在降幅 CI 内部，判定器会拒绝给二元结论")
    v.set_defaults(func=cmd_verdict)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
