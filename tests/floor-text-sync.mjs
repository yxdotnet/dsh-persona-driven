/**
 * 底线文案同步测试（JS ⇄ Python）。
 *
 * 本项目反复踩过的失效模式是「同一段文案存了两份副本，改一边漏改另一边」。
 * 现在三条底线既有 Python 版（注入给钩子路径）又有 JS 版（原生插件路径），
 * 所以用一条用例把两边逐字钉在一起：任何一边改了而另一边没跟上，这里立刻红。
 *
 * 判据不是「看起来一样」而是逐字节相等：文本退回后本用例必定失败。
 */

import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { eq, finish, group, ok } from './_assert.mjs';
import { CALL_ECONOMY_LINE, L2_FLOOR_HEAD, PASS_FIRST_LINE, SAFETY_FLOOR_LINE, VERIFY_FLOOR_LINE } from '../lib/persona-core.js';

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

finish('floor-text-sync');
