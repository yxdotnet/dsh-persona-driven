/**
 * 跑全部用例。子进程逐个执行，任一非零退出即整体失败。
 *
 * 用子进程而不是 import，是为了让每个用例的 `process.exitCode` 与输出互相隔离，
 * 失败时能一眼看出是哪一个文件红的。
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
	'plugin-wiring.mjs'
];

const failures = [];
for (const suite of SUITES) {
	const label = `tests/${suite}`;
	const result = spawnSync(process.execPath, [join(HERE, suite)], { stdio: 'inherit' });
	if (result.status !== 0) failures.push(label);
}

console.log(`\n${'='.repeat(60)}`);
if (failures.length === 0) {
	console.log(`全部用例通过（${SUITES.length} 个套件）`);
} else {
	console.log(`失败 ${failures.length} / ${SUITES.length} 个套件：`);
	for (const failure of failures) console.log(`  ✗ ${failure}`);
	process.exitCode = 1;
}

// doctor 只报告，不作为通过判据的一部分（未装入 profile 是正常状态）。
const doctor = spawnSync(process.execPath, [join(HERE, 'doctor.mjs')], { stdio: 'inherit' });
if (doctor.status !== 0) {
	console.log('\n安装自检报告了阻塞项（见上）。这不影响单元用例的结论，但装之前先看它。');
}
