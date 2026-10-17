/**
 * 看板产物冒烟测试。
 *
 * 为什么需要它：`persona_dashboard.py` 里内嵌着近 3 万字符的 HTML + JS，而这块
 * **没有任何其它用例覆盖**。它被改过十几次，一个括号写错就会让整个看板静默失效——
 * 生成过程照样退出 0、HTML 照样写出来，只是页面打开一片空白。这种失效只有解析一遍才抓得住。
 *
 * 判据（全部确定性，不依赖本机真实埋点）：
 *   1. 空数据也能正常生成、退出码 0（历史上真的因为空 metrics 目录崩过：min() on empty）
 *   2. 内联 `<script>` 能被解析（`new Function` 只解析不执行，所以不会碰 DOM）
 *   3. 关键 payload 字段与空态元素都在产物里
 *
 * 前置：`python` 在 PATH 上且 ≥ 3.10。缺解释器时**跳过**，不算通过也不算失败。
 */

import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { eq, finish, group, ok } from './_assert.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const SCRIPTS = join(ROOT, 'skills', 'persona-driven', 'scripts');
const SCRIPT = join(SCRIPTS, 'persona_dashboard.py');
const PYTHON = process.env.PERSONA_PYTHON ?? 'python';

group('看板产物 · 空数据冒烟');

let pythonOk = true;
try {
	execFileSync(PYTHON, ['-B', '-c', 'print(1)'], { stdio: ['ignore', 'pipe', 'pipe'] });
} catch {
	pythonOk = false;
	console.log(`  ! 跳过：取不到可用的 python（${PYTHON}）`);
	console.log('    补齐方式：确保 python 在 PATH 上且 ≥ 3.10，然后重跑 `npm run test:dashboard`。');
	console.log('    缺解释器时本用例不算通过，也不算失败——请勿据此宣称看板已验证。');
	eq(existsSync(SCRIPT), true, 'persona_dashboard.py 存在（缺解释器，仅核对文件在位）');
}

if (pythonOk) {
	// 空 DSH_HOME：metrics 目录不存在。这正是历史上崩过一次的场景。
	const sandbox = mkdtempSync(join(tmpdir(), 'persona-dash-'));
	const home = join(sandbox, 'home');
	const out = join(sandbox, 'out.html');
	mkdirSync(join(home, 'persona'), { recursive: true });
	try {
		let failure = null;
		try {
			execFileSync(PYTHON, ['-B', SCRIPT], {
				cwd: sandbox,
				env: { ...process.env, DSH_HOME: home, PERSONA_DASHBOARD_OUT: out, PERSONA_PROJECT_ROOTS: '' },
				stdio: ['ignore', 'pipe', 'pipe'],
				encoding: 'utf8'
			});
		} catch (error) {
			failure = error;
		}
		eq(failure, null, '空 metrics 目录下生成看板不抛异常（退出码 0）');
		ok(existsSync(out), '产物 HTML 已写出');

		const html = existsSync(out) ? readFileSync(out, 'utf8') : '';
		ok(html.length > 10000, `产物体量正常（${html.length} 字符）`);

		// 1) 内联脚本必须能解析。只解析不执行，所以不依赖 DOM。
		const blocks = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi)]
			.map((match) => match[1])
			.filter((source) => source.trim() !== '');
		ok(blocks.length > 0, `找到 ${blocks.length} 段内联脚本`);
		let bad = '';
		for (const [index, source] of blocks.entries()) {
			try {
				new Function(source);
			} catch (error) {
				bad = `script[${index}]: ${String(error?.message ?? error)}`;
				break;
			}
		}
		eq(bad, '', '全部内联脚本语法可解析（括号/引号写错会在这里红）');

		// 2) 关键 payload 字段在产物里
		for (const key of ['"families"', '"approvals"', '"perTurn"', '"dialCompare"', '"skills"', '"exploreAll"']) {
			ok(html.includes(key), `payload 含 ${key}`);
		}

		// 3) 本轮修的两个空态元素必须在场（它们就是「空数据时不该画空轴」的载体）
		ok(html.includes('id="rawNa"'), 'raw_tool 空态元素在位');
		ok(html.includes('id="radarNa"'), '雷达空态元素在位');
		ok(html.includes('设档位前/未归属'), '人格分布的可解释标签在位');
	} finally {
		rmSync(sandbox, { recursive: true, force: true });
	}
}

finish('dashboard-smoke');
