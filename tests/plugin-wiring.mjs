/**
 * 装配集成测试：用真实的 DSH 包 + 一个假 Cordis 上下文，真的调用 `apply()`，
 * 然后驱动它注册的三个扩展点。
 *
 * 这是**唯一**能证明「接线正确」的用例：
 *   - `ctx.skills.registerProvider()` 给出的候选与正文；
 *   - `tools/pre-execute` 命中安全底线时真的返回 `{kind:'ask'}`，且放行时不吞掉 `next()`；
 *   - `agent/pre-step` 真的把档位块塞进 decision，且同回合不重复注入；
 *   - 只读路由返回的 JSON 与生效档位一致。
 *
 * 前置：能解析 `@deepseek-ai/dsh-llm`。**不要在仓库根建 node_modules**——
 * DSH 的运行时解析器对 link: 挂载的插件按「本地候选包优先」路由，仓库根一旦真有
 * `node_modules/@deepseek-ai`，DSH 就会从那里加载 dsh-llm，已安装的插件与宿主就会
 * 拿到两份不同的模块实例。正确做法是指出那份目录，由本文件用
 * `node --import tests/_dsh-resolver.mjs` 把自己重启一次，在测试子进程里装解析钩子：
 *
 *   $env:PERSONA_DSH_NODE_MODULES = "D:/path/to/dsh/node_modules"; npm run test:wiring
 *
 * 解析不到时本用例**跳过**并明确说明——不算通过，也不算失败，别据此宣称接线已验证。
 */

import { spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { eq, finish, group, ok } from './_assert.mjs';
import { L2_FLOOR_HEAD as L2_HEAD } from '../lib/persona-core.js';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

/**
 * 找一份含 `@deepseek-ai/dsh-llm` 的 node_modules。
 *
 * 只探测两个**通用**位置，不写任何机器私有路径：
 *   1. `PERSONA_DSH_NODE_MODULES` —— 支持的显式入口（DSH 源码检出，或解包后的 app.asar）；
 *   2. 本仓库自己的 `node_modules` —— 本地开发时偶尔会有。
 * 找不到就跳过（见下方提示），不猜、不遍历磁盘。
 */
function findDshModules() {
	const candidates = [
		(process.env.PERSONA_DSH_NODE_MODULES ?? '').trim(),
		join(ROOT, 'node_modules')
	].filter((candidate) => candidate !== '');
	for (const candidate of candidates) {
		if (existsSync(join(candidate, '@deepseek-ai', 'dsh-llm', 'package.json'))) return candidate;
	}
	return '';
}

// 还没装解析钩子就先自己重启一次把钩子装上——这样仓库根不需要、也不应该放 node_modules
// （放了会被 DSH 的运行时解析器当成「本地候选包」，让已安装的插件与宿主加载两份 dsh-llm）。
//
// 判据是**钩子装没装**（本进程设置的哨兵变量），不是「用户有没有设环境变量」。
// 早先版本用后者判断，后果是：照文档设了 PERSONA_DSH_NODE_MODULES 的人**永远不会**触发
// 重启，钩子也就永远装不上，用例恒跳过——文档指的那条路根本走不通。
const HOOK_ACTIVE = 'PERSONA_DSH_WIRING_HOOK_ACTIVE';
if (process.env[HOOK_ACTIVE] !== '1') {
	const found = findDshModules();
	if (found !== '') {
		const result = spawnSync(
			process.execPath,
			// 必须传 file:// URL：传 Windows 绝对路径会被 Node 当成协议 d: 而报
			// ERR_UNSUPPORTED_ESM_URL_SCHEME。
			['--import', new URL('./_dsh-resolver.mjs', import.meta.url).href, fileURLToPath(import.meta.url)],
			{ stdio: 'inherit', env: { ...process.env, PERSONA_DSH_NODE_MODULES: found, [HOOK_ACTIVE]: '1' } }
		);
		process.exit(result.status ?? 1);
	}
}

let plugin;
try {
	plugin = await import('../lib/index.js');
} catch (error) {
	console.log('\n装配集成测试');
	console.log(`  ! 跳过：解析不到 DSH 运行时包（${String(error?.message ?? error).split('\n')[0]}）`);
	console.log('    补齐方式：指出一份含 @deepseek-ai/dsh-llm 的 node_modules（DSH 源码检出，或解包后的 app.asar），例如');
	console.log('      $env:PERSONA_DSH_NODE_MODULES = "D:/path/to/dsh/node_modules"; npm run test:wiring');
	console.log('    本用例在缺依赖时不算通过，也不算失败——请勿据此宣称接线已验证。');
	// 机器可读的跳过标记：run-all 靠它把「跳过」与「通过」区分开，
	// 否则汇总会谎报「全部通过」。
	console.log('SKIP: plugin-wiring 未运行（解析不到 DSH 运行时包）');
	process.exit(0);
}

const skillDir = join(ROOT, 'skills', 'persona-driven');
const sandbox = mkdtempSync(join(tmpdir(), 'persona-wiring-'));
const home = join(sandbox, 'dshhome');
const metrics = join(sandbox, 'metrics');
process.env.DSH_HOME = home;
process.env.PERSONA_METRICS_DIR = metrics;
delete process.env.PERSONA_GUARD_SCOPE;

mkdirSync(join(home, 'persona', 'sessions'), { recursive: true });
mkdirSync(metrics, { recursive: true });

const sessionId = 'session-wiring-0001';
writeFileSync(
	join(home, 'persona', 'state.json'),
	JSON.stringify({ persona: 'ENTJ', kind: 'type', level: 'l3', gist: '指挥官', dials: { PACE: 5, VERIFY: 3, RISK: 4, STRUCT: 2, DETAIL: 4, EXPLORE: 2, STYLE: 4, WARMTH: 1 } }),
	'utf8'
);

/**
 * 默认配置 + 真实 Config 校验结果（保证字段齐全）。
 *
 * 顺序很关键：**字面量必须在展开之前**。写反过一次——`webPanel` 的真实默认从 true 改成
 * false 之后，`...parsed` 覆盖掉了字面量里的 true，于是那条路由用例静默改用 false 跑、
 * 两条断言直接红。字面量在前 = 只在 `Config` 拿不到时兜底，真实默认始终以 `Config` 为准。
 */
const parsed = typeof plugin.Config === 'function' ? plugin.Config({}) : {};
const DEFAULTS = {
	injectBlock: true,
	guard: true,
	guardScope: '',
	metrics: true,
	sessionStartInject: false,
	webPanel: false,
	skillDir: '',
	...parsed
};

/** 极小的假 Cordis 上下文：记录注册，按需把服务交给 `ctx.inject` 的回调。 */
function makeCtx(available = ['skills', 'webServer']) {
	const events = new Map();
	const injections = [];
	const warnings = [];
	const ctx = {
		logger: { warn: (message) => warnings.push(String(message)), info() {}, error() {} },
		on(event, handler) {
			events.set(event, handler);
		},
		inject(services, callback) {
			injections.push({ services, callback });
			if (services.every((service) => available.includes(service))) {
				const child = { logger: ctx.logger };
				if (services.includes('skills')) child.skills = { registerProvider: (factory) => { ctx.__provider = factory(); } };
				if (services.includes('webServer')) child.webServer = { register: (route) => { ctx.__route = route; return () => {}; } };
				callback(child);
			}
		}
	};
	return { ctx, events, injections, warnings };
}

group('装配集成 · 技能提供者');

const a = makeCtx();
plugin.apply(a.ctx, { ...DEFAULTS });
eq(a.ctx.__provider !== undefined, true, 'ctx.skills.registerProvider 被调用');
const provider = a.ctx.__provider;
ok(provider !== undefined, '提供者工厂返回了提供者');
if (provider !== undefined) {
	eq(provider.name, 'persona-driven', '提供者名是 persona-driven');
	const candidates = await provider.list();
	eq(candidates.length, 1, '恰好一个候选（不可变单候选，与 dsh-skill-badge 同形态）');
	const candidate = candidates[0];
	eq(candidate.name, 'persona-driven', '候选名正确');
	eq(candidate.rank, 600, 'rank = BUNDLED_SKILL_RANK(600)');
	eq(candidate.source, 'bundled', 'source = bundled');
	eq(candidate.provider, 'persona-driven', 'provider 与注册名一致（DSH 会校验）');
	eq(candidate.resourceBase.kind, 'directory', 'resourceBase 是目录（技能能定位到 assets/ 与 references/）');
	eq(candidate.resourceBase.path, skillDir, 'resourceBase 指向技能负载目录');
	eq(
		JSON.stringify(candidate.invocation),
		JSON.stringify({ modelInvocable: true, userInvocable: true }),
		'模型与用户都可调用'
	);
	const meta = JSON.parse((await import('node:fs')).readFileSync(join(ROOT, 'lib', 'skill-meta.json'), 'utf8'));
	eq(candidate.description, meta.description, '目录描述取自 skill-meta.json（与 SKILL.md 同步由 skill-contract 守护）');

	const loaded = await provider.get(candidate);
	eq(loaded.name, 'persona-driven', 'get() 返回的 name 与候选一致（不一致会被 DSH 丢弃）');
	eq(loaded.content.startsWith('---'), false, '正文已剥掉 YAML frontmatter');
	eq(loaded.content.includes('# 人格驱动引擎'), true, '正文含技能标题');
	eq(typeof loaded.content, 'string', '正文是字符串');
	ok(loaded.content.length > 1000, `正文长度合理（${loaded.content.length} 字）`);
}

group('装配集成 · 安全底线（tools/pre-execute）');

const b = makeCtx();
plugin.apply(b.ctx, { ...DEFAULTS });
const guard = b.events.get('tools/pre-execute');
eq(typeof guard, 'function', '注册了 tools/pre-execute');

if (typeof guard === 'function') {
	const agent = { session: { header: { id: sessionId, cwd: sandbox } } };

	// 放行：必须原样（同一引用）返回下游决定，既不能吞掉也不能重建它。
	const ALLOW = Object.freeze({ kind: 'allow' });
	let nextCalled = 0;
	const benign = await guard(
		{ name: 'Bash', arguments: { command: 'ls -la' }, agent, signal: undefined },
		async () => { nextCalled += 1; return ALLOW; }
	);
	eq(nextCalled, 1, '安全命令会调用 next()');
	eq(benign, ALLOW, '安全命令按引用原样返回下游决定');

	// 命中：返回 ask，且不得继续调用 next()。
	nextCalled = 0;
	const danger = await guard(
		{ name: 'Bash', arguments: { command: 'git reset --hard HEAD~3' }, agent, signal: undefined },
		async () => { nextCalled += 1; return { kind: 'allow' }; }
	);
	eq(danger.kind, 'ask', '危险命令返回 {kind:"ask"}（DSH 会转成真实的人工确认）');
	eq(nextCalled, 0, '命中后不再调用 next()');
	ok(String(danger.reason).includes('安全底线'), '理由里点明安全底线');
	ok(String(danger.reason).includes('Git 破坏性操作'), '理由里给出命中类别');
	ok(String(danger.reason).includes('git\\s+reset'), '理由里给出命中的正则');

	// 路径字段也参与扫描。
	const pathHit = await guard(
		{ name: 'Read', arguments: { file_path: join(home, 'x', '.ssh', 'id_rsa') }, agent, signal: undefined },
		async () => { nextCalled += 1; return { kind: 'allow' }; }
	);
	eq(pathHit.kind, 'ask', '路径字段命中「密钥与凭据」也转 ask');
}

group('装配集成 · 每轮注入（agent/pre-step）');

const c = makeCtx();
plugin.apply(c.ctx, { ...DEFAULTS });
const preStep = c.events.get('agent/pre-step');
eq(typeof preStep, 'function', '注册了 agent/pre-step');

if (typeof preStep === 'function') {
	const agent = { session: { header: { id: sessionId, cwd: sandbox } } };
	const next = async () => ({ kind: 'enter', messages: [], startsRequestSeries: true });

	const first = await preStep({ agent, messages: [{ role: 'user' }], turn: 1, step: 1 }, next);
	eq(first.kind, 'enter', '注入不改动下游的 kind');
	eq(first.startsRequestSeries, true, 'spread 保住了 startsRequestSeries');
	eq(first.messages.length, 1, '恰好追加一条上下文消息');
	eq(first.messages[0].role, 'user', '追加的是 user 角色消息');
	eq(first.messages[0].source.kind, 'persona-driven', '消息来源标记为 persona-driven');
	const text = first.messages[0].content[0].text;
	ok(text.startsWith('[人格档位 P5/V3/R4/S2/D4/E2/ST4/W1 · ENTJ]'), '注入的是当前生效档位');
	ok(text.includes('安全底线'), '注入块含安全底线');
	ok(text.includes('验证下限'), '注入块含验证下限');

	// 同一回合的后续步：不得重复注入。
	let secondNext = 0;
	const second = await preStep(
		{ agent, messages: [{ role: 'user' }], turn: 1, step: 2 },
		async () => { secondNext += 1; return { kind: 'enter', messages: [] }; }
	);
	eq(secondNext, 1, '同回合第二步直接放行');
	eq(second.messages.length, 0, '同回合第二次不重复注入');

	// 新回合：重新注入。
	const third = await preStep({ agent, messages: [{ role: 'user' }], turn: 2, step: 1 }, next);
	eq(third.messages.length, 1, '新回合重新注入');

	// 下游 reject 时不打扰，且原样传递。
	const REJECT = Object.freeze({ kind: 'reject' });
	let rejectNext = 0;
	const rejected = await preStep(
		{ agent, messages: [{ role: 'user' }], turn: 3, step: 1 },
		async () => { rejectNext += 1; return REJECT; }
	);
	eq(rejected, REJECT, '下游 reject 时按引用原样返回');
	eq(rejectNext, 1, '下游 reject 时仍然只调用一次 next()');

	// 没有用户消息的步骤（工具续跑）不注入。
	const noMessages = await preStep({ agent, messages: [], turn: 4, step: 1 }, next);
	eq(noMessages.messages.length, 0, 'messages 为空的步骤不注入');
}

group('装配集成 · 档位关闭时不注入');

writeFileSync(join(home, 'persona', 'state.json'), JSON.stringify({ persona: null, kind: null, dials: {}, level: 'off' }), 'utf8');
const d = makeCtx();
plugin.apply(d.ctx, { ...DEFAULTS });
const offStep = d.events.get('agent/pre-step');
const offAgent = { session: { header: { id: sessionId, cwd: sandbox } } };
const offResult = await offStep({ agent: offAgent, messages: [{ role: 'user' }], turn: 1, step: 1 }, async () => ({ kind: 'enter', messages: [] }));
eq(offResult.messages.length, 0, 'level=off 时不注入');

// l2 只给三条底线。
writeFileSync(join(home, 'persona', 'state.json'), JSON.stringify({ persona: 'ENTJ', kind: 'type', level: 'l2', dials: { PACE: 5 } }), 'utf8');
const e = makeCtx();
plugin.apply(e.ctx, { ...DEFAULTS });
const l2Result = await e.events.get('agent/pre-step')({ agent: offAgent, messages: [{ role: 'user' }], turn: 1, step: 1 }, async () => ({ kind: 'enter', messages: [] }));
const l2Text = l2Result.messages[0].content[0].text;
eq(l2Text.startsWith(L2_HEAD), true, 'l2 注入以底线抬头开始');
// 注意：底线文案本身会在正文里提到「推进节奏（PACE）」这类词，所以不能用 includes('PACE')
// 判断——要判的是**档位数值串**与思考步骤这两个 l2 不该有的东西。
eq(/\[人格档位 [PVRSTDW\d/]+ · /u.test(l2Text), false, 'l2 不给档位抬头（数值）');
eq(l2Text.includes('思考步骤'), false, 'l2 不给思考步骤');
eq(l2Text.includes('验证强度原文'), false, 'l2 不给验证强度原文');

group('装配集成 · 埋点写入');

const { readdirSync, readFileSync } = await import('node:fs');
const written = readdirSync(metrics);
ok(written.some((name) => name.startsWith('injections-')), '写入了 injections-*.jsonl');
const injectionLines = written
	.filter((name) => name.startsWith('injections-'))
	.flatMap((name) => readFileSync(join(metrics, name), 'utf8').split('\n').filter(Boolean))
	.map((line) => JSON.parse(line));
ok(injectionLines.length >= 3, `注入记录条数合理（${injectionLines.length}）`);
const record = injectionLines[0];
eq(record.event, 'agent/pre-step', '埋点 event 字段正确');
eq(record.layer, 'global', '埋点 layer 字段正确');
eq(record.persona, 'ENTJ', '埋点 persona 字段正确');
ok(typeof record.chars === 'number' && record.chars > 0, '埋点 chars 是正数');
eq(record.session.includes(sessionId) || record.session.length > 0, true, '埋点 session 非空');
ok(!Object.hasOwn(record, 'prompt'), '埋点不含提问原文（隐私条款）');

group('装配集成 · 只读路由（需显式打开，默认关）');

// webPanel 的真实默认是 false（理由见 lib/index.js 的注释：这个 slot 取不到会话标识）。
// 所以这里必须显式打开——用例要如实反映「默认关、显式开」这个契约。
const f = makeCtx(['webServer']);
plugin.apply(f.ctx, { ...DEFAULTS, webPanel: true });
eq(f.ctx.__route?.path, '/host/persona-driven/state', '注册了只读状态路由');
eq(f.ctx.__route?.kind, 'exact', '路由是精确匹配');

// 反向断言：默认配置下**不该**注册路由。少了这条，默认值退化成 true 也不会有人发现。
const fOff = makeCtx(['webServer']);
plugin.apply(fOff.ctx, { ...DEFAULTS });
eq(fOff.ctx.__route, undefined, '默认配置下不注册只读路由（webPanel 默认关）');

if (f.ctx.__route !== undefined) {
	writeFileSync(join(home, 'persona', 'state.json'), JSON.stringify({ persona: 'INTJ', kind: 'type', level: 'l3', gist: '建筑师', dials: { PACE: 2, VERIFY: 5, RISK: 2, STRUCT: 5, DETAIL: 5, EXPLORE: 3, STYLE: 3, WARMTH: 3 }, updated_at: '2026-01-01T00:00:00+08:00' }), 'utf8');
	let captured = null;
	const res = {
		writeHead(status, headers) { captured = { status, headers }; },
		end(body) { captured = { ...captured, body }; }
	};
	f.ctx.__route.handler({ url: `/host/persona-driven/state?session=${sessionId}&cwd=${encodeURIComponent(sandbox)}` }, res);
	const payload = JSON.parse(captured.body);
	eq(captured.status, 200, '路由返回 200');
	eq(payload.ok, true, 'payload.ok 为真');
	eq(payload.persona, 'INTJ', '返回当前生效的人格');
	eq(payload.dialsText, 'P2/V5/R2/S5/D5/E3/ST3/W3', '返回档位串');
	eq(payload.guard.enabled, true, '返回守卫开关状态');
	eq(payload.guard.categories, 8, '返回守卫类别数');
	eq(captured.headers['cache-control'], 'no-store', '路由不缓存');
}

group('装配集成 · 关掉守卫时放行');

const g = makeCtx([]);
plugin.apply(g.ctx, { ...DEFAULTS, guard: false });
eq(g.events.get('tools/pre-execute'), undefined, 'guard:false 时不注册守卫');

group('装配集成 · 服务缺席时不炸');

const h = makeCtx([]);
let threw = null;
try {
	plugin.apply(h.ctx, { ...DEFAULTS });
} catch (error) {
	threw = error;
}
eq(threw, null, 'skills / webServer 都缺席时 apply() 不抛异常');
ok(h.events.has('agent/pre-step') && h.events.has('tools/pre-execute'), '核心能力仍已注册');
eq(h.warnings.length, 0, '没有产生插件警告');

// 收尾：清理临时状态，避免污染真实 HOME。
rmSync(sandbox, { recursive: true, force: true });
delete process.env.DSH_HOME;
delete process.env.PERSONA_METRICS_DIR;

finish('plugin-wiring');
