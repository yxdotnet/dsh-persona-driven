/**
 * 跨语言契约同步测试（JS ⇄ Python）。
 *
 * 本项目反复踩过的失效模式是「同一段逻辑存了两份副本，改一边漏改另一边」。
 * 这条用例把三份契约钉死：
 *
 *   1. 三条底线文案（注入给钩子路径 vs 原生插件路径）
 *   2. 工具词表 `TOOL_ALIASES` / `TOOL_PREFIX_RULES`（埋点写入端 vs 看板读取端）
 *   3. 项目根解析 `findProjectRoot` / `find_project_root`（插件读 vs CLI 写）
 *
 * 判据不是「看起来一样」而是逐字节相等：退回后本用例必定失败。
 *
 * 为什么要有 2 和 3，各自都真出过事：词表分叉过一次（JS 把 shell 改成 bash、
 * grep+glob 合并成 search），让看板的探查画像/终端调用/验证回收**全部恒为 0**，
 * 而数据一直在写；项目层分叉过一次（Python 用裸 cwd、JS 用 git 根），让
 * 「在子目录里设的项目档」写进注入读不到的位置。两次都是**静默**的，只有对拍才抓得住。
 */

import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { eq, finish, group, ok } from './_assert.mjs';
import {
	CALL_ECONOMY_LINE,
	L2_FLOOR_HEAD,
	PASS_FIRST_LINE,
	SAFETY_FLOOR_LINE,
	TOOL_ALIASES,
	TOOL_PREFIX_RULES,
	VERIFY_FLOOR_LINE,
	findProjectRoot,
	normalizeTool,
	sha12
} from '../lib/persona-core.js';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const SCRIPTS = join(ROOT, 'skills', 'persona-driven', 'scripts');

group('底线文案 JS ⇄ Python 逐字同步');

const python = process.env.PERSONA_PYTHON ?? 'python';
let raw = null;
let unavailable = '';
try {
	raw = execFileSync(
		python,
		[
			// -B：不要写 .pyc。persona.py 自己也设了 sys.dont_write_bytecode，但那是在它
			// 被导入之后才生效的，拦不住它自己的 __pycache__；这里从解释器层面关掉，
			// 保证跑测试不在技能目录里留下任何生成物。
			'-B',
			'-c',
			'import json,sys; sys.path.insert(0,"."); import persona as p; ' +
				'print(json.dumps({"SAFETY_FLOOR_LINE":p.SAFETY_FLOOR_LINE,"VERIFY_FLOOR_LINE":p.VERIFY_FLOOR_LINE,' +
				'"CALL_ECONOMY_LINE":p.CALL_ECONOMY_LINE,"PASS_FIRST_LINE":p.PASS_FIRST_LINE,"L2_FLOOR_HEAD":p.L2_FLOOR_HEAD}))'
		],
		{ cwd: SCRIPTS, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }
	);
} catch (error) {
	unavailable = String(error?.message ?? error);
}

if (raw === null) {
	// 不静默通过：显式说明这条证据这次没取到，以及怎么取。
	console.log(`  ! 跳过：取不到 Python 侧常量（${unavailable.split('\n')[0]}）`);
	console.log('    补齐方式：确保 `python` 在 PATH 上并 >= 3.10，然后重跑 `npm run test:sync`。');
	console.log('    本用例在缺 Python 时不算通过，也不算失败——请勿据此宣称「两边一致」。');
	eq(existsSync(join(SCRIPTS, 'persona.py')), true, 'Python 侧 persona.py 存在（缺解释器，仅核对文件在位）');
} else {
	const py = JSON.parse(raw);
	eq(py.SAFETY_FLOOR_LINE, SAFETY_FLOOR_LINE, 'SAFETY_FLOOR_LINE 两侧逐字节相同');
	eq(py.VERIFY_FLOOR_LINE, VERIFY_FLOOR_LINE, 'VERIFY_FLOOR_LINE 两侧逐字节相同');
	eq(py.CALL_ECONOMY_LINE, CALL_ECONOMY_LINE, 'CALL_ECONOMY_LINE 两侧逐字节相同');
	eq(py.PASS_FIRST_LINE, PASS_FIRST_LINE, 'PASS_FIRST_LINE 两侧逐字节相同');
	eq(py.L2_FLOOR_HEAD, L2_FLOOR_HEAD, 'L2_FLOOR_HEAD 两侧逐字节相同');
	ok(PASS_FIRST_LINE.includes(CALL_ECONOMY_LINE), '一次到位正文内嵌调用经济（不得再抄一份副本）');
	eq(PASS_FIRST_LINE.split(CALL_ECONOMY_LINE).length - 1, 1, '调用经济在成型文案里只出现一次');
}

group('工具词表 JS ⇄ Python 逐项一致');

const VOCAB_PROBE = [
	'import json, sys',
	'sys.path.insert(0, ".")',
	'import hook_persona as hp',
	'names = json.loads(sys.argv[1])',
	'print(json.dumps({',
	'  "aliases": [[k, sorted(v)] for k, v in hp.TOOL_ALIASES.items()],',
	'  "prefixes": [[a, b] for a, b in hp.TOOL_PREFIX_RULES],',
	'  "normalized": sorted([[n, hp.normalize_tool(n)] for n in names]),',
	'}, ensure_ascii=False, separators=(",", ":")))'
].join('\n');

// 探针清单：两侧词表的全部成员 + DSH 真实工具名 + 应当归 other 的名字。
// 这些名字就是那次事故的现场——它们曾全部落成 other。
const PROBE_NAMES = [...new Set([
	...Object.values(TOOL_ALIASES).flat(),
	'read', 'write', 'edit', 'grep', 'glob', 'pwsh', 'bash', 'subagent', 'subagent_fork',
	'workflow', 'web_fetch', 'web_search', 'skill', 'todo_write', 'present',
	'ask_user_question', 'job_output', 'blender_rt_headless', 'mcp__godot-ai__node_create',
	'update_plan', 'unknown_tool'
])].sort();

let vocabRaw = null;
let vocabErr = '';
try {
	vocabRaw = execFileSync(python, ['-B', '-c', VOCAB_PROBE, JSON.stringify(PROBE_NAMES)], {
		cwd: SCRIPTS,
		encoding: 'utf8',
		stdio: ['ignore', 'pipe', 'pipe']
	});
} catch (error) {
	vocabErr = String(error?.message ?? error);
}

if (vocabRaw === null) {
	console.log(`  ! 跳过词表比对：取不到 Python 侧（${vocabErr.split('\n')[0]}）`);
	console.log('    补齐方式：确保 `python` 在 PATH 上并 >= 3.10，然后重跑 `npm run test:sync`。');
	console.log('    缺解释器时本组不算通过，也不算失败——请勿据此宣称词表已对齐。');
	eq(existsSync(join(SCRIPTS, 'hook_persona.py')), true, 'hook_persona.py 存在（缺解释器，仅核对文件在位）');
} else {
	const py = JSON.parse(vocabRaw);
	// 用 JSON 字符串比较：`eq` 不做深比较，数组必须序列化后再比。
	const jsAliases = Object.entries(TOOL_ALIASES).map(([k, v]) => [k, [...v].sort()]);
	eq(JSON.stringify(jsAliases.map((r) => r[0])), JSON.stringify(py.aliases.map((r) => r[0])),
		'类别名与顺序两侧一致');
	eq(JSON.stringify(jsAliases), JSON.stringify(py.aliases), '每个类别的成员集合两侧一致');
	eq(JSON.stringify(TOOL_PREFIX_RULES.map(([a, b]) => [a, b])), JSON.stringify(py.prefixes),
		'前缀兜底规则（含顺序）两侧一致');
	const mine = PROBE_NAMES.map((name) => [name, normalizeTool(name)]);
	eq(JSON.stringify(mine), JSON.stringify(py.normalized),
		`${PROBE_NAMES.length} 个探针工具名的归类结果两侧一致`);
}

group('项目根解析 JS ⇄ Python 一致');

// 为什么这条重要：Python 写项目层、插件读项目层。两侧解析出不同目录时，
// 「set --scope 项目」会写进注入永远读不到的位置，而且**不报错**。
const ROOT_PROBE = [
	'import json, sys',
	'sys.path.insert(0, ".")',
	'import persona as p',
	'cases = json.loads(sys.argv[1])',
	'print(json.dumps({',
	'  "roots": [str(p.find_project_root(x)) for x in cases],',
	'  "proj": p.proj_id(cases[1]),',
	'}, separators=(",", ":")))'
].join('\n');

// 三组：仓库内的深层子目录（有 .git 祖先）/ 仓库根本身 / 不存在的路径（无 .git，应回起点）
const ROOT_CASES = [
	join(SCRIPTS),
	ROOT,
	join(ROOT, 'no-such-dir', 'deeper')
];

let rootRaw = null;
try {
	rootRaw = execFileSync(python, ['-B', '-c', ROOT_PROBE, JSON.stringify(ROOT_CASES)], {
		cwd: SCRIPTS,
		encoding: 'utf8',
		stdio: ['ignore', 'pipe', 'pipe']
	});
} catch {
	rootRaw = null;
}

if (rootRaw === null) {
	console.log('  ! 跳过项目根比对：取不到 Python 侧（同上的解释器前提）');
	eq(existsSync(join(SCRIPTS, 'persona.py')), true, 'persona.py 存在（缺解释器，仅核对文件在位）');
} else {
	const normalize = (value) => String(value).replace(/\\/gu, '/');
	const parsed = JSON.parse(rootRaw);
	const pyRoots = parsed.roots.map(normalize);
	const jsRoots = ROOT_CASES.map((value) => normalize(findProjectRoot(value)));
	eq(JSON.stringify(jsRoots), JSON.stringify(pyRoots),
		'项目根解析两侧一致（含「找不到 .git 时回起点」的回退分支）');
	eq(normalize(findProjectRoot(join(SCRIPTS))), normalize(ROOT),
		'仓库内子目录解析到仓库根（这就是那条静默失效的现场）');
	// proj 短 ID：看板靠它把「档位流水」与「工具事件」归到同一个项目下。
	// 算法或输入差一点，两边就永远对不上，而且是静默的——所以它也是契约。
	eq(sha12(ROOT), parsed.proj, 'proj 短 ID 两侧同算法同输入');
}

finish('floor-text-sync');
