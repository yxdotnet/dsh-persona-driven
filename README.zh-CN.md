# persona-driven · 人格驱动引擎（DeepSeek Harness 插件版）

> 把「AI 太谨慎、小改动反复测试、烧 token」翻译成 **8 颗可调的旋钮**，
> 一句「果断点」就能切档。本仓库是 [persona-driven](https://github.com/yxdotnet/dsh-persona-driven)
> 技能的 **DeepSeek Harness 原生化移植**：内置技能、每轮注入、安全底线强制确认，
> 全部走 Cordis 原生扩展点，**不需要 Git Bash，也不需要每轮起 Python 子进程**。

[English](README.md) · [更新日志](CHANGELOG.md) · [安装与验收](docs/安装与验收.md) · [架构与移植说明](docs/架构与移植说明.md)

---

## 30 秒了解

不懂 MBTI 也能用。MBTI 只是好记好叫的外壳，真正生效的是旋钮数值。

| 想干的事 | 直接说 |
|---|---|
| 要快、别磨 | 「果断点」「别反复测试了」「token 烧太快」 |
| 点名人格 | 「用 ENTJ」「切到 ESFP」 |
| 用预设 | 「极速模式」「按稳的那套来」「多给我几个思路」 |
| 看 / 关 | 「现在是什么人格」「关掉人格」 |
| 看数据 | 「生成人格看板」 |

八颗旋钮，各 1–5 档：

| 组 | 旋钮 | 回答的问题 |
|---|---|---|
| 执行 | PACE 推进节奏 | 多久停下来问一次 |
| 执行 | **VERIFY 验证强度** | **为这次改动投多少验证** |
| 执行 | RISK 风险偏好 | 愿意承担多大不确定性 |
| 执行 | STRUCT 结构约束 | 能不能动既有结构 |
| 认知 | DETAIL 细节粒度 | 说多细 |
| 认知 | EXPLORE 探索广度 | 给几个方案 |
| 交互 | STYLE 表达风格 | 怎么说话 |
| 交互 | WARMTH 共情温度 | 先照顾人还是先处理事 |

四个预设：**极速**（P5/V4/R4/S4/D5/E1/ST3/W2）、**稳妥**、**探索**、**倾听**。16 型全集同样支持。
八个领域（编码、界面设计、视觉图像、金融、写作、规划、协作、决策）各有一份档位→产出差异映射，按任务只加载相关的那一份。

---

## 两条底线，任何档位都不下降

**安全底线**：命中 删除/覆盖、支付与资金、鉴权与权限、密钥与凭据、生产环境变更、
数据库破坏性语句、不可逆操作 时，强制切回高验证并先问一次。
装了这个插件之后，这条由 `tools/pre-execute` **真的**转成一次人工确认（不是靠模型自觉）。

**验证下限**：档位调的是「额外验证」的多少，**不是「要不要验」**。改什么至少验什么；
引入新行为或修复缺陷时，证据要固化成可复跑的用例，并在收尾一行自报。
它与 `EXPLORE`、`PACE` **解耦**——「只给一个方案」「只求快」都不是少验的理由。

> **省下来的动作只能来自「一次到位」**，不能来自「少做事」。一旦返工，
> 即视为先前验证投入不足，必须**补齐验证**。

---

## 安装

### 方式 A：只要技能（最省事，L1/L2 级）

把 `skills/persona-driven/` 整个目录复制到 DSH 的技能根目录即可，DSH 会自动发现它：

```powershell
# Windows PowerShell（$env:DSH_HOME 缺省是 ~/.dsh）
Copy-Item -Recurse -Force .\skills\persona-driven "$env:USERPROFILE\.dsh\skills\persona-driven"
```

```bash
# macOS / Linux
mkdir -p ~/.dsh/skills && cp -r skills/persona-driven ~/.dsh/skills/
```

DSH 的技能根目录按优先级（数字小者胜）：

| 优先级 | 路径 | 作用范围 |
|---|---|---|
| 100 | `<项目根>/.dsh/skills` | 当前项目 |
| 200 | `<项目根>/.agents/skills` | 当前项目 |
| 300 | 组合里的 `customSkillDirs` | 由配置决定 |
| 400 | `$DSH_HOME/skills` | 本机用户 |
| 500 | `~/.agents/skills` | 本机用户 |
| 600 | 打包技能目录 / `$DSH_BUNDLED_SKILL_DIR` | 由安装体决定 |

`<项目根>` 是最近的含 `.git` 的祖先目录。

**这一级能影响、不能约束**：语气、详略、推进节奏按档位执行，但测试深度没有硬约束，
安全底线也不会强制转人工确认。

### 方式 B：装入插件（L3 级，推荐）

插件会做三件事：注册技能、每轮注入档位块、把安全底线变成真实的人工确认。

```powershell
# 1) 装进某个 profile（把 <profile> 换成你的，例如 desktop / web / tui）
dsh plugin --profile <profile> add <本仓库目录>

# 2) 把本包加进该 profile 的 bundle 列表
#    编辑 $env:USERPROFILE\.dsh\profiles\<profile>\package.json
#    在 dsh.profile.bundles 数组里加入 "@dsh-external/persona-driven"

# 3) 重启 DSH
```

`dsh.profile.bundles` 的样子：

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

也可以从 DSH 的插件面板安装（`plugin_manager` 的 `install_bundle`，`target` 填本目录的绝对路径）。

> 插件是 DSH **启动时**加载的模块，改完必须重启 DSH 才生效。

### 方式 C：钩子桥接（可选，给喜欢配置文件的人）

插件之外还有一条兼容路径：生成一份 Claude Code 格式的 `hooks.json`，
交给 DSH 自带的 `@deepseek-ai/dsh-hooks-claude-code` 桥接执行。

```bash
cd skills/persona-driven
python scripts/persona.py install-hooks                        # 预览，不写盘
python scripts/persona.py install-hooks --apply --write-confirmed
```

然后按脚本打印出来的 YAML，把下面这一行加进 `$DSH_HOME/cordis.patch.yml`
（或 profile 自己的那份），再重启 DSH：

```yaml
- insert:
    - id: persona-driven-hooks
      name: '@deepseek-ai/dsh-hooks-claude-code'
      config:
        configPath: <上面打印出的 hooks.json 绝对路径>
```

**A / B / C 三选一即可，不要同时开 A+B+C 的注入**：方式 B 与方式 C 都会每轮注入，
同时开会把档位块注入两次（内容相同，纯粹浪费 token）。

---

## 怎么确认它真的生效

```powershell
cd <本仓库目录>
npm test        # 223 条断言 + 安装自检（接线套件需 DSH node_modules，未配时如实报「跳过未验证」）
npm run doctor  # 只看安装自检
```

`npm run doctor` 会说清四件事：包与补丁是否齐全、技能负载是否完整、
**本包有没有装进某个 profile**、状态目录是否可写。判据是「装好了」而不是「命令跑完了」。

技能侧的体检：

```bash
cd skills/persona-driven
python scripts/persona.py doctor   # 环境 + DSH 插件 + 可选的钩子桥接
python scripts/persona.py get      # 当前生效的档位与它所在的层
```

---

## 这个插件做了什么（与 WorkBuddy 版的差异）

| | WorkBuddy 版 | 本插件 |
|---|---|---|
| 注入通道 | 钩子命令（`UserPromptSubmit`） | `agent/pre-step` 瀑布（原生，无需子进程） |
| 依赖 | **Git Bash + Python** | **无**（Python 只在用 CLI 时用） |
| 安全底线 | `PreToolUse` 钩子返回 `ask` | `tools/pre-execute` 返回 `{kind:'ask'}`，走 DSH 的 Approval 服务 |
| 状态目录 | `~/.workbuddy/persona` | `$DSH_HOME/persona`（缺省 `~/.dsh/persona`） |
| 项目层 | `<cwd>/.workbuddy/persona` | `<cwd>/.dsh/persona` |
| 会话标识 | `CODEBUDDY_SESSION_ID` | `DSH_SESSION_ID`（保留旧名回退） |
| 网页面板 | 无 | 只读状态药丸（`lib/client.js`） |
| 压缩后重注入 | `PreCompact` | **不支持**（见下） |

### 一处真实的能力回退：`PreCompact`

WorkBuddy 版注册了 5 个钩子事件。DSH 的 Claude Code 桥接只支持 4 个：
`SessionStart`、`UserPromptSubmit`、`PreToolUse`、`PostToolUse`。
**`PreCompact` 在 DSH 侧不受支持**，所以已从钩子生成器里删除。

- 走**方式 C（钩子桥接）**：上下文压缩之后，档位块不再被重新注入。
- 走**方式 B（原生插件）**：不受影响——`agent/pre-step` 每一回合都在。

---

## 目录结构

```
package.json              DSH bundle manifest（dsh.bundle.patch + dsh.client）
cordis.patch.yml          bundle 层：插入 persona-driven 这一行
icon.svg                  插件图标
locale/{zh,en}.json       插件面板的标题与描述
lib/index.js              Host 半边：注册技能 + 每轮注入 + 安全底线 + 埋点 + 只读路由
lib/persona-core.js       平台无关内核（只依赖 Node 内置模块，可脱离 DSH 直接测）
lib/client.js             Client 半边：Web / 桌面共用的只读状态药丸
lib/skill-meta.json       技能名、目录描述、whenToUse 的唯一真相源
skills/persona-driven/    完整技能负载（SKILL.md + assets + references + scripts）
tests/                    可复跑的验证
docs/                     安装与验收、架构与移植说明
```

技能负载内部：

```
SKILL.md                     引擎：触发、旋钮、优先级契约、按需加载
assets/personas.json         唯一真相源：16 型 + 4 预设的档位数值
scripts/persona.py           状态机与 CLI（唯一读写入口）
scripts/hook_persona.py      钩子桥接的入口（方式 C 用）
scripts/check_environment.py 只读环境与安装自检
scripts/experiment.py        对照实验记录与判定器
scripts/persona_dashboard.py 本机数据看板（生成单文件 HTML）
references/                  16 型画像、安全底线、安装、实验、已知限制、8 份领域映射
```

---

## 依赖

- **完全权限（DSH 的 Full access）—— 必须**。技能要执行 shell 命令跑 Python 脚本，
  并写**会话工作区之外**的 `$DSH_HOME/persona/`（档位、埋点、会话档位）。
  缺权限时 `set` / `off` / 看板 / `doctor` 会因写入被拒而失败。装了插件后
  **每轮注入与安全底线不经过 shell**，但这些 CLI 操作仍然需要权限。
- **DSH**（插件形态需要；只用方式 A 的话不需要）。
- **Node ≥ 20**（插件半边；只用 Node 内置模块，没有任何 npm 依赖）。
- **Python ≥ 3.10**（只有用 `scripts/persona.py` 这些 CLI 时才需要；仅标准库，不装第三方包）。
- ~~Git Bash~~ —— **不再需要**。

---

## 隐私

埋点仅记录工具名、分类标记、时间戳、人格名与会话 ID 的**哈希前缀**，
**默认不记录命令原文与提问内容**，**无网络上报**。数据存于本机 `$DSH_HOME/persona/`。
`PERSONA_LOG_COMMANDS=1` 是唯一的例外开关，默认关闭。

---

## 诚实声明：效果未经证实

本技能的核心假设（「档位能减少验证投入」）已跑过三轮对照实验，**按判定线不成立**。
最新一轮 v3（300 轮 = 10 任务 × 10 重复 × 3 组，单一模型）：

- 工具调用中位数 **12 → 7.5（下降 37.5%，95%CI 27%~50%）**，方向成立、剂量-反应单调；
- **但漏验证显著劣化**（C 79/100 vs 对照 48/100，p<0.0001），且效应强烈依赖任务类型（21%~56%）。

因此：

- **不要相信任何 token 降幅数字**；
- **不得宣称「已解决漏验证」或「机制有效」**；
- 分项数据可以如实披露，但**必须同时注明整体判定不成立**。

完整口径见 `skills/persona-driven/references/limits.md`——**对外说明与效果宣称一律以该文件为准**。
另外两条同样要如实说明的限制：**触发是概率性的**（同一句话连说两次，实测一次触发一次没有，
显式点名稳定得多）；**核心假设的三条底线里，只有安全底线有运行时强制力**，
验证下限与「一次到位」是书面规则 + 每轮注入，依赖模型遵守。

---

## 许可

MIT，见 [LICENSE](LICENSE)。
技能内容版权归原作者 **多汁牛排**；DSH 插件移植与打包由 **yxdotnet** 完成。
