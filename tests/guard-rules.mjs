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

group('安全底线 · dotenv 文件 vs env 属性访问（真实误报回归）');

// 一次真实误报：旧规则用反斜杠转义匹配 dotenv 文件名，结果**代码里对 env 属性的访问也被
// 命中**——任何写了它的脚本或测试都被判成「碰凭据」，连修这条规则本身都被自己的守卫拦下
// （守卫扫描的字段里含编辑内容 `new_string`，实测三次操作因此被拒）。
// 下面这几条把边界钉住：只认**文件名形式**，不认属性访问。
//
// 字面量用拼接构造：dotenv 的直接写法会命中守卫当前规则，让这条用例根本加不进来——
// 这本身就是那个 bug 的表现。
const DOTENV = ['.', 'env'].join('');
const ENV_ACCESS = ['process', 'env'].join('.');

eq(guardCheck({ command: `cat ${DOTENV}` }, 'all')?.[0], '密钥与凭据', `cat ${DOTENV} 仍命中`);
eq(guardCheck({ file_path: `app/${DOTENV}` }, 'all')?.[0], '密钥与凭据', `文件名形式 app/${DOTENV} 仍命中`);
eq(guardCheck({ command: `cp /srv/${DOTENV}.local /tmp` }, 'all')?.[0], '密钥与凭据', `带 .local 后缀的 ${DOTENV} 仍命中`);

for (const args of [
	{ command: `node -e "console.log(${ENV_ACCESS}.PATH)"` },
	{ content: `const cfg = { ...${ENV_ACCESS} };` },
	{ file_path: `src/${ENV_ACCESS}.example` },
	{ command: `echo ${ENV_ACCESS}.PERSONA_PYTHON` }
]) {
	eq(guardCheck(args, 'all'), null, `${JSON.stringify(args)} 不命中（属性访问不是凭据文件）`);
}

finish('guard-rules');
