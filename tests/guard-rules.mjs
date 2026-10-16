/**
 * 安全底线规则测试。
 *
 * 每条用例都真的抓得住改动：放宽或收紧任何一条正则、把 `exec` 范围写错、
 * 或让非对象参数抛异常，都会让它失败。
 */

import { eq, finish, group } from './_assert.mjs';
import { GUARD_FIELD_KEYS, GUARD_PATH_KEYS, GUARD_RULES, collectHaystack, guardCheck, resolveGuardScope } from '../lib/persona-core.js';

group('安全底线 · 命中（应当转人工确认）');

const POSITIVE = [
	[{ command: 'rm -rf build' }, '破坏性文件操作'],
	[{ command: 'Remove-Item -Recurse -Force .\\dist' }, '破坏性文件操作'],
	[{ command: 'git reset --hard HEAD~3' }, 'Git 破坏性操作'],
	[{ command: 'git push origin main --force' }, 'Git 破坏性操作'],
	[{ command: 'kubectl delete pod api-0' }, '生产环境变更'],
	[{ command: 'terraform destroy' }, '生产环境变更'],
	[{ command: 'DROP TABLE users;' }, '数据库破坏性语句'],
	[{ command: 'truncate table orders' }, '数据库破坏性语句'],
	[{ command: 'stripe charges create --amount 100' }, '支付与资金'],
	[{ command: 'cat ~/.ssh/id_rsa' }, '密钥与凭据'],
	[{ command: 'cat .env' }, '密钥与凭据'],
	[{ command: 'vim .dsh/cordis.patch.yml' }, '本机全局配置'],
	[{ command: 'chmod 777 /var/www' }, '系统级变更'],
	[{ file_path: 'src/a.ts', content: 'rm -rf /' }, '破坏性文件操作'],
	[{ command: 'reg delete HKLM\\Software\\Foo /f' }, '系统级变更']
];

for (const [args, expected] of POSITIVE) {
	const hit = guardCheck(args, 'all');
	eq(hit?.[0] ?? null, expected, `${JSON.stringify(args)} → ${expected}`);
}

group('安全底线 · 不命中（应当放行）');

const NEGATIVE = [
	{ command: 'echo hello' },
	{ command: 'ls -la' },
	{ command: 'git status' },
	{ command: 'git commit -m "fix"' },
	{ command: 'delete from users where id = 1' },
	{ command: 'npm run build' },
	{ command: 'cat README.md' },
	{ file_path: 'src/index.ts', content: 'export const a = 1;' },
	{},
	[],
	null,
	'rm -rf /'
];

for (const args of NEGATIVE) {
	const hit = guardCheck(args, 'all');
	eq(hit, null, `${JSON.stringify(args)} 不命中`);
}

group('安全底线 · 扫描范围');

eq(guardCheck({ content: 'rm -rf /' }, 'exec'), null, 'exec 范围跳过 content 字段');
eq(guardCheck({ content: 'rm -rf /' }, 'all')?.[0], '破坏性文件操作', 'all 范围扫描 content 字段');
eq(guardCheck({ file_path: 'x/.env' }, 'exec')?.[0], '密钥与凭据', '路径字段在任何范围下都参与扫描');

eq(resolveGuardScope('', { PERSONA_GUARD_SCOPE: 'exec' }), 'exec', '范围可由环境变量给出');
eq(resolveGuardScope('all', { PERSONA_GUARD_SCOPE: 'exec' }), 'all', '显式配置压过环境变量');
eq(resolveGuardScope('nonsense', {}), 'all', '非法取值回落 all，绝不放大放行');

group('安全底线 · 规则表完整性');

eq(Object.keys(GUARD_RULES).length, 8, '共 8 个类别');
eq(Object.values(GUARD_RULES).flat().length, 52, '共 52 条正则');
eq(
	Object.entries(GUARD_RULES).map(([k, v]) => `${k}:${v.length}`).join(' '),
	'破坏性文件操作:10 Git 破坏性操作:7 生产环境变更:10 数据库破坏性语句:3 支付与资金:4 密钥与凭据:7 本机全局配置:4 系统级变更:7',
	'各类别的条数与 persona.py 的 GUARD_RULES 一一对应'
);
eq(GUARD_FIELD_KEYS.all.length, 6, 'all 范围扫 6 个内容字段');
eq(GUARD_FIELD_KEYS.exec.length, 3, 'exec 范围扫 3 个内容字段');
eq(GUARD_PATH_KEYS.length, 5, '5 个路径字段');

for (const [category, patterns] of Object.entries(GUARD_RULES)) {
	for (const pattern of patterns) {
		let compiled = true;
		try {
			new RegExp(pattern);
		} catch {
			compiled = false;
		}
		eq(compiled, true, `${category} 的正则 /${pattern}/ 可编译`);
	}
}

group('安全底线 · 参数摊平');

eq(collectHaystack({ command: 'a', path: 'b' }, 'all'), 'a\nb', '字符串字段按顺序拼接');
eq(collectHaystack({ command: 'a', secret: 'zzz' }, 'all'), 'a', '未列入的字段不参与扫描');
eq(collectHaystack({ content: { nested: 'rm -rf /' } }, 'all'), '{"nested":"rm -rf /"}', '对象字段按 JSON 摊平');

finish('guard-rules');
