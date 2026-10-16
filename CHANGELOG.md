# 更新日志

本文件记录 **DSH 插件包 `@dsh-external/persona-driven`** 的版本。
技能内容本身（8 旋钮、16 型、参考文件、实验工具）另有自己的版本线，见
`skills/persona-driven/SKILL.md` 的 `metadata.version`。

版本号约定：**插件大版本对应技能版本**（`1.6.x` = 技能 `1.5.37` + DSH 原生化第一版）。

---

## 1.6.0 — 2026-10（首个 DSH 版本）

从 WorkBuddy 技能 `persona-driven v1.5.37` 完整迁移，并原生化。

### 新增：DSH 插件能力

- **内置技能**：`lib/index.js` 通过 `ctx.skills.registerProvider()` 注册 `persona-driven`，
  模型在会话的 `<available_skills>` 目录里直接看到它。等价于 `@deepseek-ai/dsh-skill-badge`
  的形态（不可变单候选 + 目录资源基）。
- **每轮注入（L3）**：`agent/pre-step` 瀑布按回合注入档位块。DSH 没有 Claude Code 的
  `UserPromptSubmit` 钩子，`agent/pre-step` 是等价拦截点，且在**第一次模型请求之前**执行，
  所以 turn 1 也覆盖得到。同回合多步只注入一次（按 agent + turn 去重）。
- **安全底线强制确认（L3）**：`tools/pre-execute` 命中危险操作时返回 `{kind:'ask'}`，
  由 DSH 的 Approval 服务转成真实的人工确认。**不再依赖 Git Bash**，也不再每轮起 Python 子进程。
- **埋点**：以与 Python 侧完全相同的字段名写入 `$DSH_HOME/persona/metrics/*.jsonl`，
  `persona.py report` 与人格看板照旧可读。
- **网页面板**：只读路由 `/host/persona-driven/state` + `lib/client.js` 的状态药丸，
  显示当前生效的人格与档位串。**故意不提供写操作**：从 UI 静默改全局档位会波及用户
  毫无察觉的其他对话，本技能明确禁止。

### 迁移：平台耦合点全部改到 DSH

- 状态根目录 `~/.workbuddy/persona` → `$DSH_HOME/persona`（`$DSH_HOME` 缺省 `~/.dsh`）。
- 项目层 `<cwd>/.workbuddy/persona/state.json` → `<cwd>/.dsh/persona/state.json`。
- 会话标识 `CODEBUDDY_SESSION_ID` → `DSH_SESSION_ID`（保留旧名作为回退）。
- 钩子配置目标 `~/.workbuddy/settings.json` → `$DSH_HOME/persona/hooks.json`，
  并改为 **Claude Code 事件映射格式**（DSH 的 `@deepseek-ai/dsh-hooks-claude-code` 桥接吃这个格式）。
- **删除 Git Bash 依赖**：DSH 的钩子桥接走 `ctx.shell`，原生插件完全不需要 shell。
- `doctor` 的判定从「L3 钩子是否注册」改为「DSH 插件是否装入 profile」。
- 安全底线里「本机全局配置」的命中规则改为 `.dsh/cordis.patch.yml` 与 `.dsh/persona/hooks.json`。

### 移除：PreCompact 事件

WorkBuddy 版注册了 5 个钩子事件，DSH 的 Claude Code 桥接支持其中 4 个：
`SessionStart`、`UserPromptSubmit`、`PreToolUse`、`PostToolUse`。
**`PreCompact` 在 DSH 侧不受支持**（在桥接的不支持清单里，配置会被静默忽略），
因此已从生成器、事件分组与文档中删除。

**这是一处真实但轻微的能力回退**，如实说明：走**钩子桥接**这条路径时，上下文压缩之后
档位块不再被重新注入。走**原生插件**路径不受影响（`agent/pre-step` 每次都在）。

### 修复

- `persona_dashboard.py`：埋点目录为空时 `min()` 抛 `ValueError`，导致「刚装好、还没用过」的
  用户打开看板直接报错。改为空数据单独分支，如实显示「暂无埋点数据」。
  （这是迁移前就存在的缺陷，不是本次引入的。）

### 新增：可复跑的验证

- `tests/floor-text-sync.mjs`：三条底线的**文案在 JS 与 Python 两侧逐字节比对**。
  起因是本项目反复踩过的失效模式——同一段文案存两份副本、改一边漏改另一边。
- `tests/guard-rules.mjs`、`tests/render-block.mjs`、`tests/state-layers.mjs`、
  `tests/skill-contract.mjs`、`tests/doctor.mjs`，以及 `.github/workflows/ci.yml`
  （Ubuntu + Windows × Node 20/22 × Python 3.10/3.12）。
