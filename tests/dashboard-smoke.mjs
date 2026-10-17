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
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
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

group('看板 · L3 数据源判定（l3_active）');

// 为什么这条重要：`liteMode()` 靠 `l3_active()` 决定要不要把看板锁进「简洁版」。
// 这个判断曾经只看「装没装钩子」（`$DSH_HOME/persona/hooks.json` 里有没有 hook_persona），
// 而 DSH 上 L3 的载体是**原生插件**——没装钩子时它判 false，于是把**已经在写的**工具数据
// 藏起来、强制锁进简洁版。数据在，只是被自己藏了。
//
// 现在认两条通路：原生插件（复用 check_environment.dsh_plugin_installed）或桥接钩子。
// 每个场景必须是**独立进程**：persona_dashboard 与它 import 的 check_environment 都在
// import 时算好 HOME_ROOT，同进程里换 DSH_HOME 不会重新解析。
function probeL3(home) {
	const code = [
		'import sys',
		'sys.path.insert(0, ".")',
		'import persona_dashboard as d',
		'print("RESULT=" + ("1" if d.l3_active() else "0"))'
	].join('\n');
	const output = execFileSync(PYTHON, ['-B', '-c', code], {
		cwd: SCRIPTS,
		encoding: 'utf8',
		stdio: ['ignore', 'pipe', 'pipe'],
		env: { ...process.env, DSH_HOME: home }
	});
	const match = /RESULT=([01])/u.exec(output);
	if (match === null) throw new Error(`L3 探针没有回 RESULT，实际输出：${output}`);
	return match[1] === '1';
}

if (pythonOk) {
	const sandbox = mkdtempSync(join(tmpdir(), 'persona-l3-'));
	try {
		// 场景 1：空环境 —— 既没插件也没钩子，应判 false（看板据此进简洁版）
		const bare = join(sandbox, 'bare');
		mkdirSync(join(bare, 'persona'), { recursive: true });
		eq(probeL3(bare), false, '场景1 空环境（无插件、无钩子）→ false');

		// 场景 2：**只有插件**（DSH 的推荐路径）—— 这是修复的核心，旧实现会判 false
		const plug = join(sandbox, 'plugin');
		const profile = join(plug, 'profiles', 'desktop');
		mkdirSync(profile, { recursive: true });
		writeFileSync(
			join(profile, 'package.json'),
			JSON.stringify({
				name: 'dsh-profile-desktop',
				dsh: { profile: { bundles: ['@deepseek-ai/dsh-base', '@dsh-external/persona-driven'] } }
			}),
			'utf8'
		);
		eq(probeL3(plug), true, '场景2 只有原生插件 → true（旧实现这里会红）');

		// 场景 2b：插件只出现在 cordis.patch.yml 文本里（没写进 bundles）
		const patchOnly = join(sandbox, 'patchonly');
		mkdirSync(join(patchOnly, 'persona'), { recursive: true });
		writeFileSync(
			join(patchOnly, 'cordis.patch.yml'),
			"- insert:\n    - id: persona-driven\n      name: '@dsh-external/persona-driven'\n",
			'utf8'
		);
		eq(probeL3(patchOnly), true, '场景2b 只在 cordis.patch.yml 里出现 → true');

		// 场景 3：**只有钩子桥**（老路径）—— 也必须判 true，不能因为改了判定就丢掉老路径
		const hook = join(sandbox, 'hook');
		mkdirSync(join(hook, 'persona'), { recursive: true });
		writeFileSync(
			join(hook, 'persona', 'hooks.json'),
			JSON.stringify({
				PreToolUse: [{ matcher: '*', hooks: [{ type: 'command', command: 'python hook_persona.py --event pretool' }] }]
			}),
			'utf8'
		);
		eq(probeL3(hook), true, '场景3 只有桥接钩子 → true（老路径不能被改坏）');

		// 场景 4：hooks.json 存在但属于别的技能 —— 不应误判为已装
		const foreign = join(sandbox, 'foreign');
		mkdirSync(join(foreign, 'persona'), { recursive: true });
		writeFileSync(
			join(foreign, 'persona', 'hooks.json'),
			JSON.stringify({ PreToolUse: [{ matcher: '*', hooks: [{ type: 'command', command: 'node other-hook.js' }] }] }),
			'utf8'
		);
		eq(probeL3(foreign), false, '场景4 hooks.json 属于别的技能 → false（不误判）');
	} finally {
		rmSync(sandbox, { recursive: true, force: true });
	}
}

finish('dashboard-smoke');
