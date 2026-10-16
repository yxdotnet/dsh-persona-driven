/**
 * `@dsh-external/persona-driven` — 平台无关内核。
 *
 * 这个模块**只依赖 Node 内置模块**，因此可以在 DSH 之外直接跑测试。
 * 它持有与 `persona.py` 逐字对应的常量与算法：
 *   - 八颗旋钮的顺序与缩写；
 *   - 三条底线的文案；
 *   - 档位块渲染（`render_block` 的等价实现）；
 *   - 安全底线规则表与扫描口径（`GUARD_RULES` / `guard_check` 的等价实现）；
 *   - 三层状态定位（会话 > 项目 > 全局）与埋点字段。
 *
 * 「同一段文案不得存两份副本」是本项目反复踩过的失效模式，所以 JS 侧的底线文案与
 * Python 侧由测试 `tests/floor-text-sync.mjs` 逐字比对，改一边而漏改另一边会立刻失败。
 */

import { createHash } from 'node:crypto';
import { appendFileSync, mkdirSync, readFileSync, statSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';

/** 八颗旋钮的固定顺序。 */
export const DIAL_ORDER = ['PACE', 'VERIFY', 'RISK', 'STRUCT', 'DETAIL', 'EXPLORE', 'STYLE', 'WARMTH'];

/** 档位串里的缩写。 */
export const DIAL_SHORT = {
	PACE: 'P',
	VERIFY: 'V',
	RISK: 'R',
	STRUCT: 'S',
	DETAIL: 'D',
	EXPLORE: 'E',
	STYLE: 'ST',
	WARMTH: 'W'
};

/** 打包技能提供者的标准优先级（= `@deepseek-ai/dsh-skill` 的 `BUNDLED_SKILL_RANK`）。 */
export const BUNDLED_SKILL_RANK = 600;

// ---------------------------------------------------------------------------
// 三条底线（与 persona.py 的对应常量逐字一致，由测试守护）
// ---------------------------------------------------------------------------

export const SAFETY_FLOOR_LINE =
	'安全底线（任何档位都不得绕过）：命中 删除/覆盖、支付与资金、鉴权与权限、' +
	'密钥与凭据、生产环境变更、数据库破坏性语句、不可逆操作 时，' +
	'强制切回高验证并先确认，不得按快档直接执行。';

export const VERIFY_FLOOR_LINE =
	'验证下限（不随档位下降，且与全部旋钮解耦）：档位调的是「额外验证」的多少，不是「要不要验」。' +
	'改什么至少验什么——本次交付对象本身必须被真实验证并给出可复核证据；' +
	'改动一旦引入新的行为、事实主张、数据推导或交互状态，即不满足低档前提，按领域红线取最严档；' +
	'修复缺陷必须留下能复现该缺陷的验证证据。' +
	'证据要固化、可复跑，不能跑一次就丢：引入新行为或修复缺陷时，' +
	'把验证固化为新增或修改一条可复跑的测试用例（不便写测试则留命令+期望输出）。' +
	'而这条用例必须真的抓住这次改动——把本次改动退回后它应当失败；' +
	'只跑一遍命令、或写一条无论如何都通过的用例，都不算证据。' +
	'并在收尾最后一行自报「验证：<证据与复跑方式>」，纯机械改动自报「验证：不适用」。' +
	'探索广度（EXPLORE）与推进节奏（PACE）只决定「给几个方案」「多久停一次」，' +
	'不参与本下限的计算——不得因「只给一个方案」或「只求快」而少验。';

export const CALL_ECONOMY_LINE =
	'调用经济（只减次数，不减内容）：探查够用即止——先定清改动面再动手，' +
	'不读与本次改动无关的文件；同一个文件不要重复完整读取，改完看一次差异即可确认。' +
	'一次调用能做完的事不要拆成多次：多条验证命令（跑用例、把改动退回后看是否失败、看差异）' +
	'尽量写进同一次调用。合并的是调用次数，不是验证内容——验证下限要求的证据一件都不能少。' +
	'多处同类修改（跨文件重命名、同一常量或文案的多处替换）按文件合并处理：' +
	'每个文件用一次批量编辑完成该文件内的全部改动，或用一条脚本一次改写完，' +
	'逐处单独编辑同一段代码是调用浪费；批量改写后仍按验证下限核对全部证据。' +
	'任务已点名要改的文件就直接读，不要先列目录找它；只在确实不知道位置时才搜索。' +
	'同一套用例跑通一次就够，不要换个框架再跑一遍（如 pytest 跑过又用 unittest）。' +
	'改动完成且验证通过即结束，不再回看代码、不再复述改动。';

export const PASS_FIRST_LINE =
	'一次到位（省调用的合法来源）：本档位省下的调用必须来自「一次改对」，不得来自「少做事」。' +
	'动手前把改动面与验收判据一次定全，再一次改到位。' +
	'若出现返工——同一处改了又改、或验证失败后回头再改——即视为先前的验证投入不足：' +
	'此时必须补齐验证，而不是继续跳过。' +
	CALL_ECONOMY_LINE;

export const L2_FLOOR_HEAD = '[人格底线 · 不随档位下降 · 与全部旋钮解耦]';

// ---------------------------------------------------------------------------
// 路径与状态
// ---------------------------------------------------------------------------

/** DSH 主目录：`$DSH_HOME` 优先，缺省 `~/.dsh`（与 dsh-home-paths 一致）。 */
export function dshHome(env = process.env) {
	const override = (env.DSH_HOME ?? '').trim();
	return override ? resolve(override) : join(homedir(), '.dsh');
}

/** 人格状态根目录。 */
export function personaRoot(env = process.env) {
	return join(dshHome(env), 'persona');
}

/** 埋点目录（`PERSONA_METRICS_DIR` 旁路，与 Python 侧同一套语义）。 */
export function metricsDir(env = process.env) {
	const override = (env.PERSONA_METRICS_DIR ?? '').trim();
	return override ? resolve(override) : join(personaRoot(env), 'metrics');
}

/** 会话标识只保留安全字符，避免拼出越界路径（与 persona.py 的 sanitize_session 等价）。 */
export function sanitizeSession(id) {
	return String(id ?? '').replace(/[^0-9A-Za-z_-]/gu, '').slice(0, 64);
}

/** 当前进程的会话标识；取不到返回空串。 */
export function envSessionId(env = process.env) {
	for (const key of ['DSH_SESSION_ID', 'CODEBUDDY_SESSION_ID', 'CLAUDE_SESSION_ID']) {
		const value = (env[key] ?? '').trim();
		if (value !== '') return sanitizeSession(value);
	}
	return '';
}

/** 读一个 JSON 文件；不存在、不可读或不是对象时返回 null。 */
export function readJson(path) {
	try {
		const parsed = JSON.parse(readFileSync(path, 'utf8'));
		return parsed !== null && typeof parsed === 'object' ? parsed : null;
	} catch {
		return null;
	}
}

/** 项目根：最近的含 `.git` 的祖先，找不到就是起点本身（与 DSH 的 findProjectRoot 一致）。 */
export function findProjectRoot(start) {
	const origin = resolve(start);
	let current = origin;
	for (;;) {
		try {
			if (statSync(join(current, '.git')) !== undefined) return current;
		} catch {
			/* 该层没有 .git，继续向上 */
		}
		const parent = dirname(current);
		if (parent === current) return origin;
		current = parent;
	}
}

/**
 * 按 `会话 > 项目 > 全局` 读出生效的那一层。
 * @param {{cwd?: string, sessionId?: string, env?: NodeJS.ProcessEnv}} where - 定位坐标。
 * @returns {{layer: string, path: string, state: object|null}}
 */
export function resolveState(where = {}) {
	const env = where.env ?? process.env;
	const cwd = where.cwd ?? process.cwd();
	const sid = sanitizeSession(where.sessionId ?? envSessionId(env));
	const globalPath = join(personaRoot(env), 'state.json');
	const projectPath = join(findProjectRoot(cwd), '.dsh', 'persona', 'state.json');

	if (sid !== '') {
		const sessionPath = join(personaRoot(env), 'sessions', `state-${sid}.json`);
		const state = readJson(sessionPath);
		if (state !== null) return { layer: 'session', path: sessionPath, state };
	}
	const project = readJson(projectPath);
	if (project !== null) return { layer: 'project', path: projectPath, state: project };
	const global = readJson(globalPath);
	if (global !== null) return { layer: 'global', path: globalPath, state: global };
	return { layer: 'global', path: globalPath, state: null };
}

// ---------------------------------------------------------------------------
// 数据源与档位块渲染
// ---------------------------------------------------------------------------

/** 读 assets/personas.json；失败返回 null（调用方按「无数据源」降级）。 */
export function loadData(skillDir) {
	return readJson(join(skillDir, 'assets', 'personas.json'));
}

/** 档位串，如 `P5/V4/R5/S4/D5/E1/ST4/W2`。 */
export function dialsStr(dials = {}) {
	return DIAL_ORDER
		.filter((key) => dials[key] !== undefined)
		.map((key) => `${DIAL_SHORT[key]}${dials[key]}`)
		.join('/');
}

/** 取「轻：只跑…」这类档位原文的短标签。 */
export function shortLevel(text) {
	return String(text ?? '').split('：')[0].trim();
}

/** `推进节奏:直通 验证强度:轻 …` */
export function dialsLine(dials = {}, data = null) {
	const parts = [];
	for (const key of DIAL_ORDER) {
		if (dials[key] === undefined) continue;
		const meta = data?.dials?.[key];
		const level = meta?.levels?.[String(dials[key])];
		if (meta === undefined || level === undefined) continue;
		parts.push(`${meta.label}:${shortLevel(level)}`);
	}
	return parts.join(' ');
}

/** 按档位名从数据源实时取思考步骤；查不到返回空数组。 */
export function liveSteps(state, data) {
	const table = state?.kind === 'type' ? data?.types : state?.kind === 'preset' ? data?.presets : undefined;
	const steps = table?.[state?.persona]?.steps;
	return Array.isArray(steps) && steps.length > 0 ? steps : [];
}

/**
 * 渲染每轮注入的紧凑档位块——与 `persona.py` 的 `render_block` 同语义。
 * l3 给完整块（档位 + 思考步骤 + 三条底线）；l2 只给三条底线；`off` / 无档位不注入。
 * @param {object|null} state - 生效档位状态。
 * @param {object|null} data - personas.json 内容。
 * @returns {string} 注入文本；空串表示不注入。
 */
export function renderBlock(state, data) {
	if (state === null || typeof state !== 'object') return '';
	const level = state.level ?? 'l3';
	if (level === 'off') return '';
	if (level === 'l2') {
		return [L2_FLOOR_HEAD, SAFETY_FLOOR_LINE, VERIFY_FLOOR_LINE, PASS_FIRST_LINE].join('\n');
	}
	const dials = state.dials ?? {};
	const lines = [
		`[人格档位 ${dialsStr(dials)} · ${state.persona ?? '自定义'}]`,
		`定位：${state.gist ?? ''}`,
		`档位：${dialsLine(dials, data)}`
	];
	const verifyText = data?.dials?.VERIFY?.levels?.[String(dials.VERIFY ?? '')];
	if (verifyText !== undefined) lines.push(`验证强度原文：${verifyText}`);
	if (level === 'l3') {
		const steps = liveSteps(state, data);
		const effective = steps.length > 0 ? steps : (Array.isArray(state.steps) ? state.steps : []);
		if (effective.length > 0) {
			const marks = '①②③④⑤⑥';
			const joined = effective
				.slice(0, marks.length)
				.map((step, index) => `${marks[index]}${step}`)
				.join(' ');
			lines.push(`思考步骤：${joined}`);
		}
		lines.push(SAFETY_FLOOR_LINE, VERIFY_FLOOR_LINE, PASS_FIRST_LINE);
	}
	return lines.join('\n');
}

// ---------------------------------------------------------------------------
// 安全底线
// ---------------------------------------------------------------------------

/** 八个类别；正则与 `persona.py` 的 `GUARD_RULES` 逐条对应（匹配前文本先转小写）。 */
export const GUARD_RULES = {
	'破坏性文件操作': [
		'rm\\s+-[a-zA-Z]*[rf]',
		'\\brmdir\\s+/s',
		'\\bdel\\s+/[fq]',
		'remove-item.*-recurse',
		'shutil\\.rmtree',
		'\\bmkfs\\b',
		'\\bdd\\s+if=',
		'\\bformat\\s+[a-z]:',
		'>\\s*/dev/sd[a-z]',
		'find\\s+.*-delete'
	],
	'Git 破坏性操作': [
		'git\\s+push\\s+.*--force',
		'git\\s+push\\s+-f\\b',
		'git\\s+reset\\s+--hard',
		'git\\s+clean\\s+-[a-z]*f',
		'git\\s+branch\\s+-D\\b',
		'git\\s+filter-branch',
		'git\\s+checkout\\s+--\\s+\\.'
	],
	'生产环境变更': [
		'kubectl\\s+delete',
		'kubectl\\s+apply',
		'terraform\\s+(apply|destroy)',
		'docker\\s+(system|volume|image)\\s+(prune|rm)',
		'aws\\s+s3\\s+rm',
		'aws\\s+.*\\bterminate',
		'npm\\s+publish',
		'twine\\s+upload',
		'vercel\\s+.*--prod',
		'netlify\\s+deploy\\s+.*--prod'
	],
	'数据库破坏性语句': [
		'drop\\s+(table|database|schema)',
		'truncate\\s+table',
		'delete\\s+from\\s+(?!.*\\bwhere\\b)'
	],
	'支付与资金': [
		'stripe\\s+.*(charge|refund|transfer|payout)',
		'\\bpaypal\\b',
		'alipay|支付宝|微信支付',
		'\\btransfer\\s+funds\\b'
	],
	'密钥与凭据': [
		'\\.ssh/',
		'id_rsa',
		'\\.aws/credentials',
		'\\.npmrc',
		'\\.env(\\b|$)',
		'keychain',
		'\\.netrc'
	],
	'本机全局配置': [
		'\\.dsh[/\\\\](cordis\\.patch\\.yml|persona[/\\\\]hooks\\.json)',
		'\\.claude[/\\\\]settings\\.json',
		'settings\\.local\\.json',
		'\\.git[/\\\\]config'
	],
	'系统级变更': [
		'chmod\\s+777',
		'chown\\s+-R',
		'\\bcrontab\\b',
		'systemctl\\s+(disable|stop)',
		'reg\\s+delete',
		'schtasks\\s+/delete',
		'takeown\\s+/f'
	]
};

/** 各扫描范围下参与匹配的字段名。 */
export const GUARD_FIELD_KEYS = {
	all: ['command', 'cmd', 'script', 'content', 'new_string', 'query'],
	exec: ['command', 'cmd', 'script']
};

/** 路径字段在任何范围下都参与扫描。 */
export const GUARD_PATH_KEYS = ['file_path', 'filePath', 'path', 'target_file', 'notebook_path'];

/**
 * 生效的扫描范围：配置 > 环境变量 > `all`。
 * 取值非法一律回落 `all`——绝不因配置写错而放大放行。
 */
export function resolveGuardScope(configured, env = process.env) {
	const raw = (configured ?? '').trim().toLowerCase() || (env.PERSONA_GUARD_SCOPE ?? '').trim().toLowerCase();
	return Object.hasOwn(GUARD_FIELD_KEYS, raw) ? raw : 'all';
}

/** 把工具参数摊平成被扫描文本：按字段名取值，非字符串值按 JSON 序列化。 */
export function collectHaystack(args, scope = 'all') {
	if (args === null || typeof args !== 'object' || Array.isArray(args)) return '';
	const parts = [];
	for (const key of [...GUARD_FIELD_KEYS[scope], ...GUARD_PATH_KEYS]) {
		const value = args[key];
		if (typeof value === 'string' && value !== '') parts.push(value);
		else if (value !== undefined && value !== null && typeof value === 'object') {
			try {
				parts.push(JSON.stringify(value));
			} catch {
				/* 循环引用等：跳过该字段 */
			}
		}
	}
	return parts.join('\n');
}

/**
 * 命中安全底线时返回 `[类别, 命中的正则]`。
 * @param {unknown} args - 工具调用参数。
 * @param {string} scope - 扫描范围。
 * @returns {[string, string]|null}
 */
export function guardCheck(args, scope = 'all') {
	const haystack = collectHaystack(args, scope);
	if (haystack === '') return null;
	const lowered = haystack.toLowerCase();
	for (const [category, patterns] of Object.entries(GUARD_RULES)) {
		for (const pattern of patterns) {
			let hit = false;
			try {
				hit = new RegExp(pattern).test(lowered);
			} catch {
				hit = false;
			}
			if (hit) return [category, pattern];
		}
	}
	return null;
}

/** 命中后给用户的确认理由（与 hook_persona.py 的文案同义）。 */
export function guardReason(category, pattern) {
	return (
		`[安全底线] 命中「${category}」（规则 ${pattern}）。` +
		'这是不可逆或高影响操作，任何人格档位都不豁免：' +
		'请先向用户说明影响面与回滚方式，取得明确确认后再执行；' +
		'本次执行不得按快档直接放行。'
	);
}

// ---------------------------------------------------------------------------
// 埋点
// ---------------------------------------------------------------------------

/** 本地 ISO 毫秒时间戳（与 hook_persona.py 的口径一致）。 */
export function isoMillis(now = new Date()) {
	return now.toISOString();
}

/** `YYYY-MM-DD`（本地时区，与 persona.py 的 `%Y-%m-%d` 一致）。 */
export function dayStamp(now = new Date()) {
	const pad = (value) => String(value).padStart(2, '0');
	return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

/** 任意文本的 sha256 前 12 位（用于不落路径原文的项目/文件标识）。 */
export function sha12(text) {
	return createHash('sha256').update(String(text ?? ''), 'utf8').digest('hex').slice(0, 12);
}

/** 追加一条 JSONL；任何失败都被吞掉——埋点绝不能影响注入或工具执行。 */
export function appendJsonl(file, record) {
	try {
		mkdirSync(dirname(file), { recursive: true });
		appendFileSync(file, `${JSON.stringify(record)}\n`, 'utf8');
		return true;
	} catch {
		return false;
	}
}

/** 实验流量桶标签（`PERSONA_METRICS_TAG`）。 */
export function metricsTag(env = process.env) {
	const raw = (env.PERSONA_METRICS_TAG ?? '').trim();
	if (raw === '') return '';
	return raw.replace(/[^0-9A-Za-z_-]/gu, '').slice(0, 32) || 'default';
}

const TEST_PATTERN = /\b(pytest|py\.test|unittest|jest|vitest|mocha|rspec|phpunit|go\s+test|cargo\s+test|dotnet\s+test|mvn\s+test|gradle\s+test|npm\s+(run\s+)?test|yarn\s+test|pnpm\s+test|rake\s+test|test_runner|run_tests)\b/iu;
const STATIC_PATTERN = /\b(tsc|mypy|pyright|eslint|ruff|flake8|golangci-lint|cargo\s+check|go\s+build|go\s+vet|dotnet\s+build|npm\s+run\s+(lint|build|typecheck))\b/iu;

/** 工具名到统计类别的别名表（与 hook_persona.py 的 TOOL_ALIASES 同义）。 */
export const TOOL_ALIASES = {
	write_to_file: 'write', Write: 'write', replace_in_file: 'edit', Edit: 'edit',
	multi_edit: 'edit', MultiEdit: 'edit', apply_patch: 'edit', edit_file: 'edit',
	execute_command: 'bash', Bash: 'bash', run_command: 'bash', shell: 'bash', bash: 'bash',
	pwsh: 'bash', PowerShell: 'bash',
	read_file: 'read', Read: 'read', get_cell_ranges: 'read',
	Glob: 'search', Grep: 'search', glob: 'search', grep: 'search',
	TodoWrite: 'todo', Task: 'subagent', WebSearch: 'web', WebFetch: 'web'
};

/** 统计类别。 */
export function normalizeTool(toolName) {
	return TOOL_ALIASES[toolName] ?? 'other';
}

/** 参数里第一个非空路径字段的 sha256 前 12 位（不落路径原文）。 */
export function pathHash(args) {
	if (args === null || typeof args !== 'object') return '';
	for (const key of GUARD_PATH_KEYS) {
		const value = args[key];
		if (typeof value === 'string' && value.trim() !== '') return sha12(value.trim());
	}
	return '';
}

/**
 * 构造一条工具调用埋点记录；字段名与 hook_persona.py 的 `record` 对齐，
 * 使 `persona.py report` 与人格看板照旧可读。
 * @param {{toolName: string, args: unknown, sessionId?: string, cwd?: string, persona?: string|null, env?: NodeJS.ProcessEnv, now?: Date}} input
 */
export function toolMetric(input) {
	const env = input.env ?? process.env;
	const args = input.args;
	const tool = normalizeTool(input.toolName);
	const command = typeof args?.command === 'string'
		? args.command
		: typeof args?.cmd === 'string' ? args.cmd : '';
	const record = {
		ts: isoMillis(input.now ?? new Date()),
		session: String(input.sessionId ?? 'unknown').slice(0, 24),
		proj: input.cwd ? sha12(input.cwd) : '',
		persona: input.persona ?? null,
		tool,
		test: command !== '' && TEST_PATTERN.test(command),
		static: command !== '' && STATIC_PATTERN.test(command),
		write: tool === 'write' || tool === 'edit',
		read: tool === 'read'
	};
	const ph = pathHash(args);
	if (ph !== '') record.ph = ph;
	if (tool === 'other' && input.toolName !== 'other') record.raw_tool = String(input.toolName).slice(0, 24);
	const tag = metricsTag(env);
	if (tag !== '') record.bucket = tag;
	return record;
}

// ---------------------------------------------------------------------------
// 技能负载
// ---------------------------------------------------------------------------

/** 取 SKILL.md 正文：剥掉 YAML frontmatter，与 dsh-skill-filesystem 的口径一致。 */
export function stripFrontmatter(raw) {
	if (!raw.startsWith('---')) return raw.trim();
	const end = raw.indexOf('\n---', 3);
	if (end < 0) return raw.trim();
	const after = raw.indexOf('\n', end + 1);
	return (after < 0 ? '' : raw.slice(after + 1)).trim();
}

/** 读 SKILL.md 正文。 */
export function readSkillBody(file) {
	return stripFrontmatter(readFileSync(file, 'utf8'));
}

/** 读 skill-meta.json（技能目录名、目录描述、whenToUse 的唯一真相源）。 */
export function readSkillMeta(file) {
	const meta = readJson(file);
	if (meta === null) return null;
	if (typeof meta.name !== 'string' || meta.name === '') return null;
	if (typeof meta.description !== 'string' || meta.description === '') return null;
	return meta;
}

/**
 * 校验一个技能名是否符合 DSH 的 kebab-case 文法
 * （`@deepseek-ai/dsh-skill` 的 `SKILL_NAME = /^[a-z0-9]+(?:-[a-z0-9]+)*$/`）。
 */
export function isSkillName(name) {
	return /^[a-z0-9]+(?:-[a-z0-9]+)*$/u.test(String(name ?? ''));
}
