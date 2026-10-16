/**
 * 极小的断言与用例工具。
 *
 * 测试全部用 node 内置模块跑，不装任何依赖：一份新克隆的仓库 `npm test` 就能出证据。
 */

let passed = 0;
const failures = [];

/** 断言为真。 */
export function ok(condition, label) {
	if (condition) {
		passed += 1;
		console.log(`  ✓ ${label}`);
	} else {
		failures.push(label);
		console.log(`  ✗ ${label}`);
	}
}

/** 断言严格相等。 */
export function eq(actual, expected, label) {
	const same = Object.is(actual, expected);
	if (same) {
		passed += 1;
		console.log(`  ✓ ${label}`);
	} else {
		failures.push(`${label} — 期望 ${JSON.stringify(expected)}，实际 ${JSON.stringify(actual)}`);
		console.log(`  ✗ ${label}\n      期望: ${JSON.stringify(expected)}\n      实际: ${JSON.stringify(actual)}`);
	}
}

/** 断言包含子串。 */
export function contains(haystack, needle, label) {
	eq(String(haystack).includes(needle), true, label);
}

/** 标记一个分组的开始。 */
export function group(title) {
	console.log(`\n${title}`);
}

/** 结束并输出汇总；有失败则以退出码 1 结束。 */
export function finish(suite) {
	console.log(`\n${suite}: ${passed} 通过, ${failures.length} 失败`);
	if (failures.length > 0) {
		for (const failure of failures) console.log(`  - ${failure}`);
		process.exitCode = 1;
	}
	return failures.length === 0;
}
