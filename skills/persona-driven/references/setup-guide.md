# 配置指南：L3 注入的安装与撤销

本技能在只有技能包时可用（L1/L2 级影响），装上 DSH 插件后才有硬约束（L3）。

**DSH 上不需要任何钩子。** L3 的三个能力（每轮注入、安全底线拦截、埋点）的载体是插件本身，
走 Cordis 原生扩展点——装完插件就没有 hooks 配置文件、也没有每轮子进程，
`~/.dsh/persona/` 下只会出现 `metrics/` 与 `sessions/`。

所以只有**一条推荐通道：B. 原生插件包**。下文第 5 节的 **C. 兼容桥**（配置文件式钩子）只为
兼容已有的老配置而保留，**不推荐**：它每轮要多起一次 Python 子进程、还要在插件之外多维护一份
钩子配置，能力上没有任何额外收益。

## python-runtime（Python 运行时）

- 用途：档位状态读写、注入与拦截半的执行、埋点汇总、环境自检。
- 支持版本：3.10 及以上；**`python` 必须来自 PATH**。本技能脚本仅使用标准库，无需安装任何第三方包。
- 官方主页：https://www.python.org/
- 官方文档：https://docs.python.org/3/
- 下载地址：https://www.python.org/downloads/
- 登录网址：不需要
- Token/Key 入口：不需要
- 权限与 scope：读写自身状态目录与技能包内文件；另有 **1 处受控子进程**——`doctor` 自检时执行
  本技能自己注册的那条注入命令（argv 列表传参、不使用 shell、15 秒超时，
  见 `check_environment.py` 的 `run_hook_probe()`）。除这一处外不执行任何外部命令。
- 配置步骤：
  1. 运行 `python scripts/check_environment.py`，看「Python 版本」一项。
  2. 版本满足即跳过；不满足时从官方下载页按系统安装。
- 凭据存储：不适用，本技能不使用任何凭据
- 验证方式：`python scripts/check_environment.py` 返回 ready 或 partial
- 轮换和撤销：不适用
- 核验日期：2026-09-24

## dsh-plugin（DSH 插件包，L3 硬约束；推荐通道）

- 用途：每轮把档位强制注入上下文、在安全底线命中时把工具调用转成人工确认、记录验证投入埋点。
- 官方主页：DSH 自身的仓库与文档；本技能不引用任何外部 URL。
- 登录网址：不需要
- Token/Key 入口：不需要
- 权限与 scope：插件以当前用户身份运行，其 Host 半能读写该用户可访问的文件
  （安全底线只做规则匹配并返回 `ask`，不发生实际写入）。
- 包名：`@dsh-external/persona-driven`
- 组件与机制：
  - **技能注册**：`ctx.skills.registerProvider()` ——装了 bundle 就自带技能，不必手动复制目录。
  - **每轮注入**：原生 Cordis Host 半挂在 `agent/pre-step` 瀑布上，**每一轮**注入档位块。
  - **SessionStart 注入**：在 `agent/created` 时经 `agent.inject()` 完成。
  - **安全底线强制确认**：在 `tools/pre-execute` 返回 `{ kind: 'ask', reason }`，
    由 DSH 的 Approval 服务接手——**用户真的会被问一次**（ask 不阻断执行）。
- 安装位置：装进**某个 DSH profile**。用 `plugin_manager`（`install_bundle`），或把该 bundle
  写进该 profile 的 `dsh.profile.bundles` 后执行安装。
- 配置步骤：
  1. 用 `plugin_manager` 的 `install_bundle` 安装 `@dsh-external/persona-driven`；
     或编辑 profile 的 `package.json`，把 bundle 加进 `dsh.profile.bundles`，再执行安装。
  2. **重启 DSH。**
  3. 运行 `python scripts/persona.py doctor` 确认「DSH 插件」一项。
  4. 复核也可以直接看 DSH 的插件清单里有没有这个 bundle。
- 凭据存储：不适用
- 验证方式：三条证据，按可信度从低到高
  1. `doctor` 显示「DSH 插件」已注册且自检通过。
  2. 在任意会话中观察每轮是否出现档位块。
  3. 看埋点是否随工具调用增长：`python scripts/persona.py report`。注入半每执行一次工具调用就写一条，
     行数随时间增长即证明它真的在被平台调起。
- 过期、权限、额度处理：DSH 的插件接口仍可能随版本变化。升级 DSH 后重新跑一次 `doctor`。
- 轮换和撤销：
  - 用 `plugin_manager` 卸载该 bundle，或把它从 profile 的 `dsh.profile.bundles` 里删掉再执行安装，
    然后**重启 DSH**。
  - 只停用注入、保留插件：`python scripts/persona.py off`（默认只关当前对话；
    关全局默认人格须显式 `--scope 全局`，会影响所有未单独设档的对话）
  - 查看安装过什么：`python scripts/persona.py doctor`
  - **DSH 上不写任何 `settings.json`，也不需要额外的 shell 运行时**——这两条上游平台时代的要求已取消。
- 核验日期：2026-09-24

## 可选兼容桥（配置文件式钩子）

- 用途：给偏爱配置文件式钩子的用户一条等价通道。
- 安装位置：`$DSH_HOME/persona/hooks.json`（默认即 `~/.dsh/persona/hooks.json`），
  Claude Code 格式。
- 配置步骤：
  1. 查看安装计划：`python scripts/persona.py install-hooks`
  2. 执行写入：`python scripts/persona.py install-hooks --apply --write-confirmed`
     脚本会先把 `$DSH_HOME/persona/hooks.json` 备份到 `$DSH_HOME/persona/backup/`，
     再合并 hooks 字段。合并是增量的：原有配置键不受影响，重复安装会跳过已存在的事件。
  3. 在 Cordis 配置里加一行，mount `@deepseek-ai/dsh-hooks-claude-code`，
     并把它的 `configPath` 指向该 hooks.json。
  4. **重启 DSH。**
  5. 运行 `python scripts/persona.py doctor` 确认注入通道状态。
- 支持的事件：`SessionStart`、`UserPromptSubmit`、`PreToolUse`、`PostToolUse`。
  **不支持 `PreCompact`**：上游平台用过该事件，DSH 的兼容桥没有它，
  所以**上下文压缩之后档位块不会再被重新注入**——这是桥接路径上一处（轻微的）回退。
  走原生插件（上面的 `dsh-plugin`）不受影响。
- 轮换和撤销：
  - **推荐做法**：`python scripts/persona.py uninstall-hooks --apply --write-confirmed`。
    它只认领命令中含 `hook_persona.py` 且属于本技能目录的项，其他来源的 hooks 与其他配置键一律保留，
    并在写盘前自动备份。写盘前先不带 `--apply` 跑一次可看预览。
  - 手工撤销：把 `$DSH_HOME/persona/backup/` 里最新的备份复制回
    `$DSH_HOME/persona/hooks.json`。
  - 只停用注入、保留脚本：`python scripts/persona.py off`（默认只关当前对话；
    关全局默认人格须显式 `--scope 全局`，会影响所有未单独设档的对话）
  - 查看安装过什么：`python scripts/persona.py doctor` 与 `python scripts/persona.py hook-config`
- 按组安装（仅供对照实验）：`install-hooks --events measurement` 只装度量钩子，
  `uninstall-hooks --events injection,guard` 只卸注入与守卫。可写组名（measurement/injection/guard/all）或事件名。
- 核验日期：2026-09-24

### 一个刻意的设计选择

本技能**不使用写在 SKILL.md frontmatter 里的钩子**：DSH 的 frontmatter 只识别
`name` / `description` / `whenToUse` / `metadata` 等少量字段，**不承载钩子配置**，多写的键会被静默忽略
（少数旧拼写甚至会导致整个技能被丢弃）。所以注入一律走上面的显式通道——
原生插件（B）或自带的 hooks.json（C）。

代价是安装要多做一步（装 bundle，或写一份 hooks 配置并加一行 Cordis 配置）。这是有意的取舍：
不为了让安装省事而把机制塞进会被忽略、且可能触发丢弃的字段里。

### 守卫扫描范围（可选调优）

安全底线是否命中，取决于工具调用参数里出现了什么文本。默认范围 `all` 最保守：
除了命令与路径，还会扫 `content` / `new_string` / `query` 这些**文本内容字段**——
好处是「往文件里写一段危险命令」同样会被拦，代价是写文档时正文里提到 `rm -rf`
这类字样也会弹一次确认。

- 调整方式：设环境变量 `PERSONA_GUARD_SCOPE=exec`（默认 `all`）。
  1. Windows 在「系统属性 → 环境变量」里新建用户变量；macOS / Linux 写入登录级配置
     （如 `~/.zshrc`、`/etc/environment`），不要只放在某个脚本里。
  2. **完全退出并重启 DSH**，让注入通道所在的进程继承到新环境。
  3. 复核生效值：`python scripts/persona.py rules` 会显示当前范围，
     `python scripts/persona.py doctor` 的「守卫扫描范围」一项也会显示。
- 两条必须知道的边界：
  - 只在某个终端里临时 `export` **不会**传给注入通道。`doctor` 显示的是 doctor 进程
    读到的值，与注入通道可能不一致，所以要设成系统 / 用户级变量。
  - 该开关**没有「关闭」档**，最低只到 `exec`（命令与路径仍全扫）。选 `exec` 意味着
    写入含危险命令的脚本文件不再被拦——这是明确的取舍。取值非法时自动回落到 `all`。

## 环境就绪路由

`python scripts/check_environment.py` 返回四种状态，按状态决定下一步：

| 状态 | 含义 | 下一步 |
|---|---|---|
| ready | 依赖齐备且注入通道已注册 | 直接使用，不再讲解配置 |
| partial | 脚本可用但注入未生效 | 正常使用；需要硬约束时按上面的步骤装插件或桥 |
| needs_setup | 缺 Python 或状态目录不可写 | 只处理缺失项，补齐后重新检查 |
| unavailable | Python 版本不达标 | 停止，先解决运行时 |

## 档位放在哪一层：会话 / 项目 / 全局

同一台机器上可以同时存在三层档位，高层覆盖低层，互不污染：

| 层 | 何时用 | 命令 | 存哪 |
|---|---|---|---|
| **会话** | 一个对话一种风格 | `set ENTJ`（默认）或 `--scope 会话` | `$DSH_HOME/persona/sessions/state-<会话>.json` |
| **项目** | 一个仓库一种风格 | `set ENTJ --scope 项目` | `<项目>/.dsh/persona/state.json` |
| **全局** | 默认人格 | `set ENTJ --scope 全局` | `$DSH_HOME/persona/state.json` |

生效优先级：**会话 > 项目 > 全局**。`persona.py get` 会打印这次读的是哪一层。
会话标识取自 DSH 每次 shell 调用都会设置的 `$DSH_SESSION_ID`。

典型用法——同时开三个对话各干各的：

- 对话 A 里说「用 ENTJ」→ 只写会话层，A 变指挥官；
- 对话 B 里说「温柔点」→ 只写会话层，B 变陪伴；
- 没单独设过的对话继续吃全局默认。三者互不影响。

> **v1.5.33 起 `set` / `off` 默认只写当前会话**（省略 `--scope` 是安全行为）；
> scope 支持中文直书：`--scope 会话 / 项目 / 全局`。改全局默认人格必须显式
> `--scope 全局`（或 `--scope global`）——那会把所有没单独设档的对话一起改掉。
> 在**拿不到会话标识**的环境（如手工终端）执行默认 set / off 会被拒绝（rc=2）
> 而不是静默写进全局层——这是 v1.5.30 起的防串档守卫在 v1.5.33 扩展到默认路径。
> 查看与清理会话档位：`persona.py sessions` 列出全部；`persona.py sessions --prune 30`
> 清理 30 天未更新的（默认只预览，确认后加 `--apply --write-confirmed`；
> **当前会话的档位一律保留**，不会被误删）。

## 装完之后：怎么确认真的生效

四步，都在本机可完成，不需要联网：

1. **跑体检**：`python scripts/check_environment.py` → 总体状态 `ready`，且「DSH 插件」一行显示
   **已注册且自检通过**。若显示 `partial` 并提示「安全底线当前未生效」，说明注入通道注册了但执行不了
   （最常见原因是解释器路径变了，或 bundle 没装上）——此时只有软约束，没有硬约束。
2. **真的切一次档**：在对话里说「果断点」，或直接点名「用 ENTJ」。随后问「现在是什么人格」——
   技能会执行 `persona.py get` 读**真实状态**作答，而不是凭印象回答。两句话的答案应当一致。
3. **看注入有没有发生**：`$DSH_HOME/persona/metrics/injections-<日期>.jsonl`，每次成功注入会新增一行
   `{ts, session, event, persona, level, chars}`（**不记录你的提问原文**）。`event` 标明这条记录
   来自哪个事件。**注意口径**：这条记录只证明「内容已提交给平台」，**不等于**「平台已采纳并
   送进了模型」——要判断后者，用 `PERSONA_PROBE_TAG` 探针；结论与步骤见 `limits.md`
   （历史探针在 DSH 之前的平台形态上测过：当时三个事件全部送达；**DSH 的兼容桥不支持
   `PreCompact`**，所以桥接路径现在只剩四个事件）。注意 `UserPromptSubmit`
   只在**你提交消息**时触发，所以同一轮里连续跑命令不会产生新的注入记录——这是机制使然，不是没生效。
4. **看埋点有没有写**：`$DSH_HOME/persona/metrics/<日期>.jsonl` 随每次工具调用增长。
   并发写入不会丢记录（v1.5.2 起为结构性保证，见 `limits.md`）。**极少数情况下会看到
   形如 `<日期>.jsonl.shard-<pid>` 的文件**：那是锁异常时写下的溢出分片，属于正常自保
   行为，`persona.py report` 会自动把它算进去；用
   `python scripts/persona.py compact-metrics` 可把它折回主文件（默认只合并 1 小时未变动的分片）。

> `l3` 档注入**完整块**（档位数值 + 思考步骤 + 三条底线）；`l2` 档注入**三条底线**
> （无档位数值、无思考步骤，v1.5.19 起）；`off` 不注入。v1.5.23 起「一次到位」这条底线里
> 还含**调用经济**（合并调用次数、不重复读同一文件），故 `l2` 与 `l3` 都会收到它。
> 若第 3 步一直没有新行，先确认档位不是 `off`（跑 `persona.py get`）。

## 回滚

- 插件安装失败或行为异常：用 `plugin_manager` 卸载该 bundle（或从 `dsh.profile.bundles` 移除），重启 DSH。
- 兼容桥安装失败或行为异常：优先用 `python scripts/persona.py uninstall-hooks --apply --write-confirmed`；
  若 hooks.json 本身已被破坏，再把 `$DSH_HOME/persona/backup/` 里的备份复制回原位。
- 只想临时停用：`python scripts/persona.py off`（默认只关当前对话），脚本与配置都保留，
  随时可 `set` 恢复。**关全局档位须显式 `--scope 全局`**，那会把所有未单独设档的对话一起关掉。
- 完全卸载：卸载插件 / 钩子 → 删除技能目录（`$DSH_HOME/skills/persona-driven/` 或项目级技能根下的同名目录）
  → 删除 `$DSH_HOME/persona/`（内含档位状态、埋点、实验记录与备份）。
  脚本不在系统里留驻任何后台进程，也不注册任何服务。
- 卸载不会自动清理备份目录。若在意残留，最后手动删除 `$DSH_HOME/persona/` 即可，
  但删掉后就没有可回滚的备份了——建议先确认卸载结果正常。

## 效果宣称的口径

本技能的安装与能力均已实测，但**「档位能减少验证投入」这一核心假设尚未验证**。
对外说明、上架文案与回复用户时，一律以 `references/limits.md` 为准，
不得给出任何未实测的百分比或确定性结论。
