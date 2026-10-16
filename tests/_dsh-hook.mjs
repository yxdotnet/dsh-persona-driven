/**
 * 测试专用的 ESM 解析钩子：把 `@deepseek-ai/*` 映射到一份 DSH 的 node_modules。
 *
 * 为什么需要它，而不是在仓库根建一个 `node_modules` 软链：DSH 自己会在 Node 的
 * ESM/CJS 解析器上装一层运行时拦截（`@deepseek-ai/dsh-app-boot` 的
 * `profile-resolution-bootstrap.js`）。对 `link:` 挂载的插件，它按
 * 「本地候选包优先」的路由规则走——仓库根如果真有一个 `node_modules/@deepseek-ai`，
 * DSH 就会从**那里**加载 `dsh-llm`，而不是从安装体。那会让已安装的插件与宿主
 * 加载到两份不同的模块实例，是要避免的隐患。
 *
 * 所以这个钩子只在**跑测试的子进程**里生效，仓库根本身保持干净。
 * 生产路径（已安装的插件）完全不经过这里。
 */

import { existsSync, readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const ROOT = (process.env.PERSONA_DSH_NODE_MODULES ?? '')
	.replace(/\\/gu, '/')
	.replace(/\/+$/u, '');

/** 读一个包的 package.json，挑出 `exports["."]` 或 `main` 指向的入口文件。 */
function rootEntry(directory) {
	try {
		const manifest = JSON.parse(readFileSync(`${directory}/package.json`, 'utf8'));
		let entry = manifest.exports?.['.'];
		if (Array.isArray(entry)) entry = entry[0];
		if (entry !== null && typeof entry === 'object') entry = entry.default ?? entry.import ?? entry.require;
		if (typeof entry !== 'string') entry = manifest.main ?? 'index.js';
		return `${directory}/${entry.replace(/^\.\//u, '')}`;
	} catch {
		return `${directory}/index.js`;
	}
}

/** 子路径（`@scope/pkg/sub`）优先查 exports，查不到就退回 `sub` 直接当文件。 */
function subEntry(directory, sub) {
	try {
		const manifest = JSON.parse(readFileSync(`${directory}/package.json`, 'utf8'));
		const map = manifest.exports;
		if (map !== null && typeof map === 'object') {
			const key = `./${sub}`;
			let hit = map[key];
			if (hit === undefined) {
				for (const [pattern, target] of Object.entries(map)) {
					const star = pattern.indexOf('*');
					if (star < 0) continue;
					const prefix = pattern.slice(0, star);
					const suffix = pattern.slice(star + 1);
					if (key.startsWith(prefix) && key.endsWith(suffix)) {
						const middle = key.slice(prefix.length, key.length - suffix.length);
						const resolved = typeof target === 'string' ? target.replace('*', middle) : target;
						hit = resolved;
						break;
					}
				}
			}
			if (Array.isArray(hit)) hit = hit[0];
			if (hit !== null && typeof hit === 'object') hit = hit.default ?? hit.import ?? hit.require;
			if (typeof hit === 'string') return `${directory}/${hit.replace(/^\.\//u, '')}`;
		}
	} catch {
		/* 落到下面的兜底 */
	}
	return `${directory}/${sub}`;
}

/**
 * Node 的模块解析钩子。只接管 `@deepseek-ai/*`，其余原样交给下游。
 * @param {string} specifier - 被解析的说明符。
 * @param {object} context - Node 提供的解析上下文。
 * @param {Function} next - 下游解析器。
 */
export async function resolve(specifier, context, next) {
	if (ROOT === '' || !specifier.startsWith('@deepseek-ai/')) return next(specifier, context);
	const segments = specifier.split('/');
	if (segments.length < 2 || segments[1] === '') return next(specifier, context);
	const directory = `${ROOT}/@deepseek-ai/${segments[1]}`;
	if (!existsSync(directory)) return next(specifier, context);
	const sub = segments.slice(2).join('/');
	const file = sub === '' ? rootEntry(directory) : subEntry(directory, sub);
	if (!existsSync(file)) return next(specifier, context);
	return { url: pathToFileURL(file).href, shortCircuit: true, format: 'module' };
}
