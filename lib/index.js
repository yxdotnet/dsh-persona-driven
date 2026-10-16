/**
 * `@dsh-external/persona-driven` — Host half。
 *
 * 把 WorkBuddy 版「人格驱动引擎」的 L3 能力原生化地搬到 DeepSeek Harness 上，不再依赖
 * Bash，也不需要每轮起一个 Python 子进程：
 *
 *   1. 技能注册：`ctx.skills.registerProvider()` 内置 `persona-driven` 技能，模型在
 *      `<available_skills>` 目录里直接看到它（与 dsh-skill-badge 同形态）。
 *   2. 每轮注入：`agent/pre-step` 瀑布里按「本回合第一次」注入档位块。DSH 没有 Claude Code
 *      的 `UserPromptSubmit` 钩子，但 `agent/pre-step` 是等价拦截点，且**在第一次模型请求
 *      之前**执行，所以 turn 1 也覆盖得到。
 *   3. 安全底线：`tools/pre-execute` 返回 `{kind:'ask'}`，由 DSH 的 Approval 服务转成真实
 *      的人工确认。它保证的是「一定会问你一次」，不是「一定不让你做」。
 *   4. 埋点：写入与 Python 侧同一套字段名的 JSONL，使 `persona.py report` 与人格看板照旧可读。
 *   5. 网页面板：只读路由 `/host/persona-driven/state`，供 `lib/client.js` 渲染状态药丸。
 *
 * 状态文件与 Python 侧**完全共用**（`$DSH_HOME/persona/`），所以 `persona.py set` 落盘的档位
 * 本插件立刻读得到，反之亦然。平台无关的内核在 `./persona-core.js`。
 *
 * 设计约束（照 cordis-plugin-development 的 references/practices.md）：
 *   - 只注册自己拥有的资源，全走 `ctx.effect` / `ctx.on`；不拥有该决定时一律 `return next()`。
 *   - 改写 `agent/pre-step` 时 spread 原 decision，保住 `startsRequestSeries` 等字段。
 *   - 可选服务（`skills` / `webServer`）走 `ctx.inject([...])`，缺席时插件其余部分照常工作。
 *   - 任何失败都不得影响会话启动或工具执行：注入与埋点全部包在 try/catch 里。
 */

import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createUserMessage } from '@deepseek-ai/dsh-llm';
import z from '@deepseek-ai/schemastery';
import {
	BUNDLED_SKILL_RANK,
	GUARD_RULES,
	appendJsonl,
	dayStamp,
	dialsLine,
	dialsStr,
	envSessionId,
	guardCheck,
	guardReason,
	isoMillis,
	loadData,
	metricsDir,
	metricsTag,
	readSkillBody,
	readSkillMeta,
	renderBlock,
	resolveGuardScope,
	resolveState,
	sha12,
	toolMetric
} from './persona-core.js';

/** Cordis 插件名。 */
export const name = 'persona-driven';

/** 不硬依赖任何服务；`skills` / `webServer` 由 `ctx.inject` 按需接入。 */
export const inject = [];

const HERE = dirname(fileURLToPath(import.meta.url));
const PKG_ROOT = resolve(HERE, '..');
const META_FILE = join(HERE, 'skill-meta.json');

/** 提供者名；`"runtime"` 是 DSH 保留名，不能用。 */
const PROVIDER_NAME = 'persona-driven';

/** 注入上下文的消息来源标记。 */
const CONTEXT_SOURCE = { kind: 'persona-driven' };

export const Config = z.object({
	/** 关掉每轮注入（技能本身仍然加载）。 */
	injectBlock: z.boolean().default(true),
	/** 关掉安全底线的强制确认。 */
	guard: z.boolean().default(true),
	/** 守卫扫描范围：`all` 含文本内容字段，`exec` 只扫命令与路径。非法值回落 `all`。 */
	guardScope: z.string().default(''),
	/** 关掉埋点写入。 */
	metrics: z.boolean().default(true),
	/** 追加 `agent/created` 期注入（默认关：turn 1 已由 pre-step 覆盖，再注入是纯重复）。 */
	sessionStartInject: z.boolean().default(false),
	/** 注册只读网页面板路由。 */
	webPanel: z.boolean().default(true),
	/** 技能负载目录；默认取包内 `skills/persona-driven`。 */
	skillDir: z.string().default('')
});

/**
 * 注册内置技能提供者。
 * @param {object} ctx - 绑定了 `skills` 服务的上下文。
 * @param {string} skillDir - 技能负载目录。
 */
function registerSkill(ctx, skillDir) {
	const meta = readSkillMeta(META_FILE);
	if (meta === null) {
		ctx.logger?.warn?.('persona-driven: lib/skill-meta.json 缺失或损坏，未注册内置技能');
		return;
	}
	const skillFile = join(skillDir, 'SKILL.md');
	const candidate = {
		name: meta.name,
		description: meta.description,
		...(typeof meta.whenToUse === 'string' && meta.whenToUse !== '' ? { whenToUse: meta.whenToUse } : {}),
		invocation: { modelInvocable: true, userInvocable: true },
		provider: PROVIDER_NAME,
		source: 'bundled',
		resourceBase: { kind: 'directory', path: skillDir },
		rank: BUNDLED_SKILL_RANK,
		locator: { path: skillFile }
	};
	ctx.skills.registerProvider(() => ({
		name: PROVIDER_NAME,
		list: () => Promise.resolve([candidate]),
		get: async () => ({
			name: candidate.name,
			description: candidate.description,
			...(candidate.whenToUse !== undefined ? { whenToUse: candidate.whenToUse } : {}),
			invocation: candidate.invocation,
			source: candidate.source,
			provider: PROVIDER_NAME,
			resourceBase: candidate.resourceBase,
			path: skillFile,
			content: readSkillBody(skillFile)
		})
	}));
}

/**
 * 装配人格驱动插件。
 * @param {object} ctx - Cordis 上下文。
 * @param {object} config - 由 {@link Config} 校验过的行配置。
 */
export function apply(ctx, config) {
	const skillDir = config.skillDir !== '' ? resolve(config.skillDir) : join(PKG_ROOT, 'skills', 'persona-driven');
	const scope = resolveGuardScope(config.guardScope);

	ctx.inject(['skills'], (skillsCtx) => {
		try {
			registerSkill(skillsCtx, skillDir);
		} catch (error) {
			skillsCtx.logger?.warn?.(`persona-driven: 注册内置技能失败：${String(error)}`);
		}
	});

	/** 每个 agent 已注入过的回合号，避免同回合多步重复注入。 */
	const injectedTurn = new WeakMap();

	/** 该 agent 的定位坐标。 */
	function locate(agent) {
		return {
			cwd: agent?.session?.header?.cwd,
			sessionId: agent?.session?.header?.id
		};
	}

	/** 读出生效档位；任何异常都当作「无档位」。 */
	function stateOf(agent) {
		try {
			return resolveState(locate(agent));
		} catch {
			return { layer: 'global', path: '', state: null };
		}
	}

	function logInjection(state, layer, block, event, agent) {
		if (!config.metrics) return;
		const where = locate(agent);
		appendJsonl(join(metricsDir(), `injections-${dayStamp()}.jsonl`), {
			ts: isoMillis(),
			session: String(where.sessionId ?? 'unknown').slice(0, 24),
			proj: where.cwd ? sha12(where.cwd) : '',
			layer,
			event,
			persona: state?.persona ?? null,
			level: state?.level ?? null,
			chars: block.length
		});
	}

	if (config.injectBlock) {
		ctx.on('agent/pre-step', async ({ agent, messages, turn }, next) => {
			if (messages.length === 0) return next();
			if (injectedTurn.get(agent) === turn) return next();
			const downstream = await next();
			// 只在本步确实会进入请求时才注入；reject 之类的决定不打扰。
			if (downstream.kind !== 'enter') return downstream;
			let block = '';
			let state = null;
			let layer = 'global';
			try {
				const resolved = stateOf(agent);
				state = resolved.state;
				layer = resolved.layer;
				block = renderBlock(state, loadData(skillDir));
			} catch (error) {
				ctx.logger?.warn?.(`persona-driven: 渲染档位块失败：${String(error)}`);
				return downstream;
			}
			if (block === '') return downstream;
			injectedTurn.set(agent, turn);
			const context = createUserMessage({
				content: [{ type: 'text', text: block }],
				source: CONTEXT_SOURCE
			});
			logInjection(state, layer, block, 'agent/pre-step', agent);
			// spread 原 decision，保住 startsRequestSeries 等字段。
			return { ...downstream, messages: [...downstream.messages, context] };
		});

		if (config.sessionStartInject) {
			ctx.on('agent/created', ({ agent }) => {
				try {
					const resolved = stateOf(agent);
					const block = renderBlock(resolved.state, loadData(skillDir));
					if (block === '') return;
					agent.inject(createUserMessage({
						content: [{ type: 'text', text: block }],
						source: CONTEXT_SOURCE
					}));
					logInjection(resolved.state, resolved.layer, block, 'agent/created', agent);
				} catch (error) {
					ctx.logger?.warn?.(`persona-driven: SessionStart 注入失败：${String(error)}`);
				}
			});
		}
	}

	if (config.guard) {
		ctx.on('tools/pre-execute', async (exec, next) => {
			let hit = null;
			try {
				hit = guardCheck(exec.arguments, scope);
			} catch {
				hit = null;
			}
			if (hit === null) return next();
			const [category, pattern] = hit;
			if (config.metrics) {
				const where = locate(exec.agent);
				const tag = metricsTag();
				appendJsonl(join(metricsDir(), `${dayStamp()}.jsonl`), {
					ts: isoMillis(),
					session: String(where.sessionId ?? 'unknown').slice(0, 24),
					proj: where.cwd ? sha12(where.cwd) : '',
					ev: 'guard',
					category,
					tool: exec.name,
					...(tag !== '' ? { bucket: tag } : {})
				});
			}
			return { kind: 'ask', reason: guardReason(category, pattern) };
		});
	}

	if (config.metrics) {
		ctx.on('tools/result', (exec) => {
			try {
				const where = locate(exec.agent);
				appendJsonl(join(metricsDir(), `${dayStamp()}.jsonl`), toolMetric({
					toolName: exec.name,
					args: exec.arguments,
					sessionId: where.sessionId,
					cwd: where.cwd,
					persona: stateOf(exec.agent).state?.persona ?? null
				}));
			} catch {
				/* 埋点失败不影响工具执行 */
			}
		});
	}

	if (config.webPanel) {
		ctx.inject(['webServer'], (serverCtx) => {
			try {
				serverCtx.webServer.register({
					kind: 'exact',
					path: '/host/persona-driven/state',
					handler: (req, res) => {
						let payload;
						try {
							const url = new URL(req.url ?? '/', 'http://localhost');
							const resolved = resolveState({
								cwd: url.searchParams.get('cwd') ?? process.cwd(),
								sessionId: url.searchParams.get('session') ?? envSessionId()
							});
							const state = resolved.state;
							const data = loadData(skillDir);
							payload = {
								ok: true,
								plugin: '@dsh-external/persona-driven',
								layer: resolved.layer,
								level: state?.level ?? null,
								persona: state?.persona ?? null,
								gist: state?.gist ?? '',
								dials: state?.dials ?? {},
								dialsText: state ? dialsStr(state.dials ?? {}) : '',
								dialsLine: state ? dialsLine(state.dials ?? {}, data) : '',
								updatedAt: state?.updated_at ?? null,
								guard: { enabled: config.guard, scope, categories: Object.keys(GUARD_RULES).length }
							};
						} catch (error) {
							payload = { ok: false, error: String(error) };
						}
						const body = JSON.stringify(payload);
						res.writeHead(200, {
							'content-type': 'application/json; charset=utf-8',
							'cache-control': 'no-store',
							'content-length': Buffer.byteLength(body)
						});
						res.end(body);
					}
				});
			} catch (error) {
				serverCtx.logger?.warn?.(`persona-driven: 注册网页面板路由失败：${String(error)}`);
			}
		});
	}
}
