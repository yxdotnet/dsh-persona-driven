# 更新日志

本文件记录 **DSH 插件包 `@dsh-external/persona-driven`** 的版本。
技能内容本身（8 旋钮、16 型、参考文件、实验工具）另有自己的版本线，见
`skills/persona-driven/SKILL.md` 的 `metadata.version`。

版本号约定：**插件大版本对应技能版本**（`1.6.x` = 技能 `1.5.37` + DSH 原生化第一版）。

---

## 1.6.1 — 2026-10（看板数据修复）

修的是「看板看着没数据、其实数据一直在写」这一类，两处独立缺陷。

### 一、工具词表跨语言不一致（根因）

`lib/persona-core.js` 的 `TOOL_ALIASES` 在移植时被**单方面改了名**：`shell`→`bash`、
`grep`+`glob` 合并成 `search`、`task`→`subagent`。而读取端（`persona_dashboard.py`、
`persona.py report`）按 Python 侧的规范类别名判定，于是「探查画像」「终端调用」
「验证回收」**全部恒为 0**。

叠加第二个原因：DSH 的工具名是**小写**（`read`/`write`/`edit`/`grep`/`glob`/`pwsh`），
而两套词表都停留在 Claude Code 的 `Read`/`Edit`/`Grep`/`Glob`/`Bash` —— 读/改调用整批
落成 `other`（实测 94 行）。

| 面板 | 修前 | 修后（同一份埋点） |
|---|---|---|
| 探查画像 | 读 0 / 改 1 / 探查 0 | 读 59 / 改 62 / 探查 69 |
| 验证遵守 | 0/0 | 1/1 |
| 终端调用 | 恒 0 | 有值 |

修法：

- JS 词表改为与 Python **同构同义**（类别名回到 `read/write/edit/shell/grep/glob/task/web`），
  两侧的类别集合、成员集合、顺序逐项一致。
- 两侧都补 DSH 小写工具名，并加**同一套前缀兜底**（20 条，顺序敏感）。理由：DSH 的工具名
  由插件贡献（`mcp__*`、`blender_rt_*`、内置的 `skill`/`todo_write`/`job_*`…），枚举追不上；
  未识别时仍写 `raw_tool`，所以「不认识」是**可诊断**的，不是静默丢失。
- 插件补写工具事件的 **`layer`** 字段（原先只有注入记录写，导致「层级分布」全归「未归属」）。
- 看板读取端加**历史兼容**：`tool == "other"` 但带 `raw_tool` 的记录用原名重新归类，并把
  `bash` / `search` / `subagent` 一并认作 shell / 搜索类 / 任务类 —— **修复前的数据不必丢弃**。
  （`search` 是被合并过的搜索类，无法再区分 grep 与 glob，按搜索类计入，不猜。）

### 二、看板把「L3 生效」判断成了「装没装钩子」

`persona_dashboard.py` 的 `liteMode()` 用 `P.hook`，只看 `$DSH_HOME/persona/hooks.json`。
DSH 上 L3 的载体是**原生插件**，没装钩子时它判 false 并**强制锁进简洁版、禁用复选框**，
把已经在写的工具数据藏起来。

改为 `P.l3 = l3_active()`：**原生插件**（复用 `check_environment.dsh_plugin_installed()`，
同一件事不写第二份实现）**或**桥接钩子，任一条在跑即算生效；相关文案与提示同步改正。

### 验证

- `npm test`：6 个套件 **224 条断言全绿**，安装自检「就绪（阻塞项 0）」。
- 跨语言词表一致性：比对 8 个类别、20 条前缀规则、47 个探针工具名、4 个埋点字段
  → **0 失败**；变异检验（把 `'read'` 从 JS 成员里删掉）→ **类别成员不一致，被捕获**。
  注意：只看归类**行为**会被前缀兜底掩盖（变异后 `normalize('read')` 两侧仍相同），
  **必须同时比对词表本身**。
- **固化程度说明**：原计划的 `tests/tool-vocab-sync.mjs` **本轮未落地**（新增测试文件未获准），
  故以「命令 + 期望输出」作为证据，契约本身**已知但未自动强制**。命令见
  `docs/架构与移植说明.md` 的「跨语言词表契约」一节。

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
- `tests/plugin-wiring.mjs`：用真实 DSH 包 + 假 Cordis 上下文真的调用 `apply()`，
  驱动 `skills.registerProvider` / `tools/pre-execute` / `agent/pre-step` / 只读路由四条接线。
- `tests/guard-rules.mjs`、`tests/render-block.mjs`、`tests/state-layers.mjs`、
  `tests/skill-contract.mjs`、`tests/doctor.mjs`，以及 `.github/workflows/ci.yml`
  （Ubuntu + Windows × Node 20/22 × Python 3.10/3.12）。

### 文档

- **技能描述与 README 写明权限前提**：本技能需要**完全权限（DSH 的 Full access）**——
  它要执行 shell 命令跑 `python scripts/persona.py …`，并写**会话工作区之外**的
  `$DSH_HOME/persona/`。缺权限时 `set` / `off` / 看板 / `doctor` 会因写入被拒而失败，
  档位落不下去。`SKILL.md` 新增「权限前提」一节列出各操作在受限模式下的实际表现，
  并要求**先告知用户、不要先切档再声称已生效**。
  （注：装了插件后每轮注入与安全底线不经过 shell，但这些 CLI 操作仍然需要权限。）
- **文档不再写死本机路径**：README 与 docs 里 8 处本地绝对路径（`cd <本机路径>`、
  `link:<本机路径>` 形式的示例，以及一处分析用的解包目录）改为 `<本仓库目录>` /
  `<path-to-this-repo>` 占位符。这些示例会随 git 分发，写死作者机器上的路径对读者是错的。
  （`~/.dsh` 下 DSH profile 里的 `link:` 属于安装配置，**必须**含绝对路径，且不随仓库分发——两者性质不同。）

### 修复

- `tests/plugin-wiring.mjs`：不再探测任何机器私有路径，只认 `PERSONA_DSH_NODE_MODULES`
  与本仓库自己的 `node_modules`。
- `tests/plugin-wiring.mjs`：**重启条件写错了**——原先用「用户有没有设
  `PERSONA_DSH_NODE_MODULES`」判断是否要自己重启装解析钩子，导致**照文档设了环境变量的人
  永远不会触发重启、钩子永远装不上、用例恒跳过**，文档指的那条路根本走不通。
  改为按「钩子装没装」（哨兵变量）判断。
- `tests/run-all.mjs`：**跳过不再被算作通过**。此前汇总只看退出码，有用例跳过时仍报
  「全部用例通过（6 个套件）」；现在区分「通过 / 跳过（未验证）/ 失败」，并显式声明
  跳过的套件没有被真正跑过。

### 文档：补上社区分发路径

- README（中英）新增置顶安装方式：**`dsh plugin --profile <profile> add
  github:yxdotnet/dsh-persona-driven`**，一条命令装包并自动写入 `dsh.profile.bundles`。
  实测 pnpm 9.1 秒完成、组合无警告、技能确实注册进会话目录。
- 修正同一处**过时说明**：原文让用户手工编辑 `dsh.profile.bundles`——`dsh plugin add`
  本来就会自己写，这一步是多余的。
- 顶部加 topic / license 徽章。
- 背景：DSH **产品本身没有插件市场**（源码树 `marketplace` 零命中），社区生态围绕
  GitHub topic **`dsh-plugin`** 建立（注册表 `vbarter/dsh-plugin-registry` 按该 topic
  每 2 小时自动发现；另有若干 awesome 目录）。本仓库的清单已满足收录契约
  （根 `package.json` + `dsh.bundle.patch` 为安全相对路径），**只差声明该 topic**。

### 修复：网页面板默认关闭（它在默认档位下取不到数据）

实测发现 `lib/client.js` 的状态药丸**永远不会显示**，不是显示得晚：

| 调用 | 返回 |
|---|---|
| 不带 `?session=`（客户端就是这么调的） | `{"layer":"global","persona":null}` |
| `?session=<本会话>` | `{"layer":"session","persona":"ENTJ","dialsText":"P5/V3/R4/S2/D4/E2/ST4/W1"}` |

根因三条，都查证过：宿主渲染 `conversation.composer.dock` 时传的是**空 props**
（源码里就是 `renderSlot("conversation.composer.dock", {})`）；会话标识不在页面 URL 里；
客户端也没有可注入的 session 服务。于是它只能读全局层，而档位默认写在会话层，
`persona` 为 null，组件 `return null`。

改法：`webPanel` 默认 `false`，并在 `lib/index.js` / `lib/client.js` / `cordis.patch.yml` /
两份 README / `docs/安装与验收.md` 里把实测数据与原因写清楚。只有显式设了**全局**档位
（`--scope 全局`）时，打开 `webPanel: true` 才有意义。

顺带修了测试里一个**真实的构造错误**：`plugin-wiring` 的 `DEFAULTS` 把 `...parsed` 写在
字面量之后，于是真实默认值会覆盖字面量——`webPanel` 默认一改，那条路由用例就静默改用
`false` 跑并红了 2 条。已调整次序（字面量只作兜底），把路由用例改为**显式**
`webPanel: true`，并**新增一条反向断言**「默认配置下不注册路由」。
该断言做过变异检验：把默认改回 `true` 时它确实红（67 通过 / 1 失败），不是摆设。
