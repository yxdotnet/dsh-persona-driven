# persona-driven · Persona-Driven Engine (DeepSeek Harness plugin)

> Turns "the AI is too cautious, re-tests every tiny change, burns tokens" into
> **eight adjustable dials**. One sentence — "be decisive" — switches the profile.
> This repository is the **native DeepSeek Harness port** of the `persona-driven` skill:
> a bundled skill, per-turn injection and a hard safety floor, all on native Cordis
> extension points. **No Git Bash, and no per-turn Python subprocess.**

[中文](README.zh-CN.md) · [Changelog](CHANGELOG.md) · [Install & verify](docs/安装与验收.md) · [Architecture & porting notes](docs/架构与移植说明.md)

---

## In 30 seconds

You do not need to know MBTI. MBTI is just a memorable handle; the dial values are what
actually take effect.

| You want | Just say |
|---|---|
| Go faster, stop over-testing | "be decisive", "stop re-testing everything", "too many tokens" |
| Name a type | "use ENTJ", "switch to ESFP" |
| Use a preset | "Fast mode", "use the steady one", "give me a few options" |
| Inspect / turn off | "what persona am I on", "turn off persona" |
| See your data | "persona dashboard" |

Eight dials, each 1–5:

| Group | Dial | Answers |
|---|---|---|
| Execution | PACE | how often to stop and ask |
| Execution | **VERIFY** | **how much verification this change gets** |
| Execution | RISK | how much uncertainty is acceptable |
| Execution | STRUCT | whether existing structure may move |
| Cognition | DETAIL | how much detail to give |
| Cognition | EXPLORE | how many options to offer |
| Interaction | STYLE | how to talk |
| Interaction | WARMTH | attend to the person or the task first |

Four presets — **Fast** (`P5/V4/R4/S4/D5/E1/ST3/W2`), **Steady**, **Explore**, **Companion** —
plus all 16 types. Eight domains (coding, UI design, visual image, finance, writing,
planning, collaboration, decision) each have a dial→output mapping; only the relevant one loads.

---

## Two floors that never come down

**Safety floor.** Deleting/overwriting, payments, auth and permissions, secrets and
credentials, production changes, destructive database statements, irreversible actions —
the profile always reverts to high verification and asks first. Once this plugin is
installed, that becomes a **real** approval prompt via `tools/pre-execute`
(not just the model's good intentions).

**Verification floor.** The profile tunes how much *extra* verification happens, never
*whether* anything is verified. Whatever you change gets verified; introducing new behaviour
or fixing a defect requires fixed, re-runnable evidence, self-reported on the last line.
It is **decoupled** from `EXPLORE` and `PACE` — "only one option" and "just be quick" are
never reasons to verify less.

> **Savings may only come from getting it right the first time**, never from doing less.
> A rework is treated as a signal that earlier verification was under-funded: catch up,
> do not keep skipping.

---

## Install

### Option A — skill only (simplest, L1/L2)

Copy `skills/persona-driven/` into a DSH skill root; DSH discovers it automatically.

```powershell
# Windows PowerShell ($env:DSH_HOME defaults to ~/.dsh)
Copy-Item -Recurse -Force .\skills\persona-driven "$env:USERPROFILE\.dsh\skills\persona-driven"
```

```bash
# macOS / Linux
mkdir -p ~/.dsh/skills && cp -r skills/persona-driven ~/.dsh/skills/
```

DSH skill roots, lowest rank wins:

| Rank | Path | Scope |
|---|---|---|
| 100 | `<projectRoot>/.dsh/skills` | project |
| 200 | `<projectRoot>/.agents/skills` | project |
| 300 | `customSkillDirs` from the composition | configured |
| 400 | `$DSH_HOME/skills` | user |
| 500 | `~/.agents/skills` | user |
| 600 | bundled skill dir / `$DSH_BUNDLED_SKILL_DIR` | installation |

`<projectRoot>` is the nearest ancestor containing `.git`.

**This level can influence, not constrain**: tone, detail and pace follow the profile,
but test depth is not enforced and the safety floor does not force a human confirmation.

### Option B — install the plugin (L3, recommended)

The plugin registers the skill, injects the dial block every turn, and turns the safety
floor into a real approval prompt.

```powershell
# 1) add it to a profile (<profile> is desktop / web / tui / ...)
dsh plugin --profile <profile> add <本机路径>\dsh-persona-driven

# 2) add the bundle to that profile's list
#    edit $env:USERPROFILE\.dsh\profiles\<profile>\package.json
#    and append "@dsh-external/persona-driven" to dsh.profile.bundles

# 3) restart DSH
```

```json
{
  "dsh": {
    "profile": {
      "bundles": [
        "@deepseek-ai/dsh-base",
        "@deepseek-ai/dsh-web-app",
        "@dsh-external/persona-driven"
      ]
    }
  }
}
```

You can also install it from DSH's plugin panel (`plugin_manager` → `install_bundle`,
with this directory's absolute path as `target`).

> Plugins are modules DSH loads **at startup**; restart DSH for changes to take effect.

### Option C — the hook bridge (optional, for config-file people)

A compatibility path: generate a Claude Code-format `hooks.json` and let DSH's bundled
`@deepseek-ai/dsh-hooks-claude-code` bridge run it.

```bash
cd skills/persona-driven
python scripts/persona.py install-hooks                        # preview, writes nothing
python scripts/persona.py install-hooks --apply --write-confirmed
```

Then add the YAML the script prints into `$DSH_HOME/cordis.patch.yml` (or the profile's own)
and restart DSH:

```yaml
- insert:
    - id: persona-driven-hooks
      name: '@deepseek-ai/dsh-hooks-claude-code'
      config:
        configPath: <the hooks.json path printed above>
```

**Pick one of A / B / C.** Options B and C both inject every turn; enabling both injects the
dial block twice (same content, pure token waste).

---

## Verify it actually works

```powershell
cd <本机路径>\dsh-persona-driven
npm test        # 223 assertions + install self-check
npm run doctor  # install self-check only
```

`npm run doctor` reports four things: package and patch integrity, skill payload
completeness, **whether the bundle is installed into any profile**, and whether the state
directory is writable. The criterion is "installed", not "the command exited 0".

Skill-side checks:

```bash
cd skills/persona-driven
python scripts/persona.py doctor   # environment + DSH plugin + optional hook bridge
python scripts/persona.py get      # the effective profile and which layer it came from
```

---

## What this plugin changes vs the WorkBuddy original

| | WorkBuddy | This plugin |
|---|---|---|
| Injection channel | hook command (`UserPromptSubmit`) | `agent/pre-step` waterfall (native, no subprocess) |
| Dependencies | **Git Bash + Python** | **none** (Python only for the CLI) |
| Safety floor | `PreToolUse` hook returns `ask` | `tools/pre-execute` returns `{kind:'ask'}` → DSH Approval service |
| State root | `~/.workbuddy/persona` | `$DSH_HOME/persona` (default `~/.dsh/persona`) |
| Project layer | `<cwd>/.workbuddy/persona` | `<cwd>/.dsh/persona` |
| Session id | `CODEBUDDY_SESSION_ID` | `DSH_SESSION_ID` (older names still fall back) |
| Web panel | none | read-only status pill (`lib/client.js`) |
| Re-inject after compaction | `PreCompact` | **unsupported** (see below) |

### One real, minor regression: `PreCompact`

The WorkBuddy version registered five hook events. DSH's Claude Code bridge supports four:
`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`. **`PreCompact` is not
supported on DSH**, so it has been removed from the hook generator.

- On **Option C (hook bridge)**: after a context compaction the dial block is not re-injected.
- On **Option B (native plugin)**: unaffected — `agent/pre-step` runs every turn.

---

## Layout

```
package.json              DSH bundle manifest (dsh.bundle.patch + dsh.client)
cordis.patch.yml          bundle layer: inserts the persona-driven row
icon.svg                  plugin icon
locale/{zh,en}.json       plugin panel title and description
lib/index.js              Host half: skill provider, per-turn injection, guard, metrics, route
lib/persona-core.js       platform-neutral core (Node builtins only; testable without DSH)
lib/client.js             Client half: read-only status pill, shared by web and desktop
lib/skill-meta.json       single source of truth for skill name / description / whenToUse
skills/persona-driven/    the full skill payload
tests/                    re-runnable verification
docs/                     install & verification, architecture & porting notes
```

---

## Requirements

- **Full access (DSH's Full access mode) — required.** The skill runs shell commands
  (`python scripts/persona.py …`) and writes **outside the session workspace** to
  `$DSH_HOME/persona/` (dials, metrics, session profiles). Without it, `set` / `off` /
  the dashboard / `doctor` fail on a denied write. The plugin removes the shell from
  **per-turn injection and the safety floor**, but these CLI operations still need it.
- **DSH** (for the plugin form; not needed for Option A).
- **Node ≥ 20** (the plugin half; Node builtins only, zero npm dependencies).
- **Python ≥ 3.10** (only for `scripts/persona.py`; standard library only).
- ~~Git Bash~~ — **no longer required**.

---

## Privacy

Metrics record tool names, classification flags, timestamps, the persona name and a
**hashed prefix** of the session id. They do **not** record command text or prompts by
default, and **nothing leaves the machine**. Data lives in `$DSH_HOME/persona/`.
`PERSONA_LOG_COMMANDS=1` is the single opt-in exception, off by default.

---

## Honest disclosure: the effect is unproven

The core hypothesis ("the profile reduces verification investment") has been tested in
three controlled rounds and **fails its pre-registered decision line**. The latest round,
v3 (300 runs = 10 tasks × 10 repetitions × 3 arms, single model):

- median tool calls **12 → 7.5 (down 37.5%, 95% CI 27%–50%)**, direction holds and the
  dose-response is monotonic;
- **but missed verification degrades significantly** (C 79/100 vs control 48/100, p<0.0001),
  and the effect depends heavily on task type (21%–56%).

Therefore:

- **do not trust any token-reduction number**;
- **do not claim** that missed verification is solved or that the mechanism works;
- per-item data may be disclosed, but **always with the overall verdict: not established**.

The authoritative wording lives in `skills/persona-driven/references/limits.md` —
**all public claims must follow that file**. Two further limits must be stated alongside it:
**triggering is probabilistic** (measured: the same sentence twice, one hit one miss; naming a
type explicitly is far more reliable), and of the three floors only the **safety floor has
runtime enforcement** — the verification floor and "first time right" are written rules plus
per-turn injection, and depend on the model following them.

---

## License

MIT — see [LICENSE](LICENSE).
Skill content © **多汁牛排** (original author); DSH plugin port and packaging by **yxdotnet**.
