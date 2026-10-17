/**
 * 跑全部用例。子进程逐个执行，任一非零退出即整体失败。
 *
 * 用子进程而不是 import，是为了让每个用例的 `process.exitCode` 与输出互相隔离，
 * 失败时能一眼看出是哪一个文件红的。
 *
 * **跳过 ≠ 通过**：有用例在缺依赖时会主动跳过（例如 `plugin-wiring.mjs` 需要一份
 * DSH 的 node_modules）。它退出码是 0，但输出里带 `SKIP:` 标记——只按退出码汇总会
 * 谎报「全部通过」，所以这里单独统计并显式说明「未验证」。
 */

import { spawnSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));

/** 有顺序要求：契约在前，行为在后。 */
const SUITES = [
	'skill-contract.mjs',
	'render-block.mjs',
	'guard-rules.mjs',
	'state-layers.mjs',
	'floor-text-sync.mjs',
	'plugin-wiring.mjs',
	'dashboard-smoke.mjs'
];

const failures = [];
const skipped = [];
for (const suite of SUITES) {
	const label = `tests/${suite}`;
	// 捕获输出：既原样回显，也用来识别跳过标记。
	const result = spawnSync(process.execPath, [join(HERE, suite)], { encoding: 'utf8' });
	if (result.stdout) process.stdout.write(result.stdout);
	if (result.stderr) process.stderr.write(result.stderr);
	if (result.status !== 0) failures.push(label);
	else if ((result.stdout ?? '').includes('SKIP:')) skipped.push(label);
}

const ran = SUITES.length - skipped.length;
console.log(`\n${'='.repeat(60)}`);
if (failures.length > 0) {
	console.log(`失败 ${failures.length} / ${SUITES.length} 个套件：`);
	for (const failure of failures) console.log(`  ✗ ${failure}`);
	process.exitCode = 1;
} else if (skipped.length > 0) {
	console.log(`通过 ${ran} 个套件；跳过 ${skipped.length} 个（**未验证**）：`);
	for (const suite of skipped) console.log(`  · ${suite}`);
	console.log('跳过的套件没有被真正跑过——不得据此宣称对应能力已验证。');
} else {
	console.log(`全部用例通过（${SUITES.length} 个套件）`);
}

// doctor 只报告，不作为通过判据的一部分（未装入 profile 是正常状态）。
const doctor = spawnSync(process.execPath, [join(HERE, 'doctor.mjs')], { stdio: 'inherit' });
if (doctor.status !== 0) {
	console.log('\n安装自检报告了阻塞项（见上）。这不影响单元用例的结论，但装之前先看它。');
}
