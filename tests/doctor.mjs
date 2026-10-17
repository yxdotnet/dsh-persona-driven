/**
 * 安装自检：判定「装好了」而不是「命令跑完了」。
 *
 * 判据（与 README 的验收节一致）：
 *   1. 包结构与 manifest 齐全，`dsh.bundle.patch` 指向的补丁文件存在且能被 YAML 读到行；
 *   2. lib/skill-meta.json 与 skills/persona-driven/SKILL.md 是同一份事实；
 *   3. 技能负载的 5 个 Python 脚本与 8 份领域映射都在位；
 *   4. 本 profile 的 package.json 的 dsh.profile.bundles 里能看到本包（已装入判据）；
 *   5. $DSH_HOME/persona/ 可写。
 *
 * 退出码：0 = 全部就绪；2 = 有阻塞项。未安装进 profile 只算「可选项未完成」，不算阻塞。
 */

import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { dshHome, personaRoot, readSkillMeta } from '../lib/persona-core.js';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const PACKAGE_NAME = '@dsh-external/persona-driven';
const problems = [];
const notes = [];

/**
 * slot → 拥有它的客户端包。`lib/client.js` 用到哪个 slot，`dsh.client.inject` 就必须
 * 声明对应的包——这是官方模板（templates/decoration）的写法，用来把客户端模块排在
 * 提供该 slot 的包之后激活。新增 slot 时在这里补一行；没登记会被自检主动报出来，
 * 以免断言在不知情的情况下退化成摆设。
 */
const SLOT_OWNERS = {
	'conversation.composer.dock': '@deepseek-ai/dsh-client-ui-conversation'
};

const line = (label, value) => console.log(`${label.padEnd(18)}${value}`);

console.log('\npersona-driven · 安装自检');
console.log('─'.repeat(60));

// 1. manifest 与补丁
const manifestFile = join(ROOT, 'package.json');
if (!existsSync(manifestFile)) problems.push('缺 package.json');
const manifest = existsSync(manifestFile) ? JSON.parse(readFileSync(manifestFile, 'utf8')) : {};
line('包', `${manifest.name ?? '(未知)'} v${manifest.version ?? '?'}`);
const patchRel = manifest?.dsh?.bundle?.patch;
if (typeof patchRel !== 'string') problems.push('package.json 缺 dsh.bundle.patch —— 不会被 plugin_manager 当作 profile 层');
else {
	const patchFile = join(ROOT, patchRel);
	if (!existsSync(patchFile)) problems.push(`dsh.bundle.patch 指向的 ${patchRel} 不存在`);
	else {
		const patch = readFileSync(patchFile, 'utf8');
		if (!patch.includes(PACKAGE_NAME)) problems.push(`${patchRel} 里没有 ${PACKAGE_NAME} 这一行`);
	}
}
line('补丁', patchRel ?? '(缺失)');

// 1b. 打包契约（DSH 与 npm 各自会检查的硬条件，逐条固化成断言）
const client = manifest?.dsh?.client;
if (client !== undefined) {
	if (client.platform !== 'web') problems.push(`dsh.client.platform 必须是 "web"（DSH 的 client-modules 只认 web），实际 ${JSON.stringify(client.platform)}`);
	if (manifest?.exports?.['./client'] === undefined) problems.push('声明了 dsh.client 但没有 exports["./client"] —— 客户端 bundle 不会被加载');
	else {
		const clientRel = manifest.exports['./client'];
		if (!existsSync(join(ROOT, clientRel))) problems.push(`exports["./client"] 指向的 ${clientRel} 不存在`);
	}
	if (!Array.isArray(client.inject)) problems.push('dsh.client.inject 必须是数组');
	else {
		// 真正的契约在这里：`lib/client.js` 注册进哪个 slot，就必须声明**拥有该 slot 的包**。
		// 官方模板（templates/decoration）用的是 inject: ["@deepseek-ai/dsh-client-ui-conversation"]，
		// 因为 conversation.composer.dock 是那个包提供的。留空数组不会报错、但客户端模块
		// 不会被排在会话 UI 之后激活——这是一处真实的偏差，必须机检，否则静默退化。
		// 只检查「是数组」等于写了条恒过的断言（[] 也是数组），这里按实际用到的 slot 反查。
		const clientRel = manifest?.exports?.['./client'];
		const clientFile = typeof clientRel === 'string' ? join(ROOT, clientRel) : undefined;
		const source = clientFile !== undefined && existsSync(clientFile) ? readFileSync(clientFile, 'utf8') : '';
		const usedSlots = [...new Set([...source.matchAll(/ctx\.slots\.(?:inject|register)\(\s*'([^']+)'/gu)].map((m) => m[1]))];
		if (usedSlots.length === 0) problems.push('无法从 lib/client.js 里识别出任何 slot —— 这条断言会变成摆设，请更新它的正则');
		for (const slot of usedSlots) {
			const owner = SLOT_OWNERS[slot];
			if (owner === undefined) {
				problems.push(`lib/client.js 用了 slot "${slot}"，但 doctor 的 SLOT_OWNERS 没登记它的属主包 —— 先补登记，否则这条断言无效`);
				continue;
			}
			if (!client.inject.includes(owner)) {
				problems.push(`lib/client.js 注册到 slot "${slot}"，但 dsh.client.inject 没有声明它的属主包 ${owner}（官方模板要求声明）`);
			}
		}
		line('客户端 slot', usedSlots.join(', ') || '(未识别)');
	}
}
line('客户端', client === undefined ? '(未声明)' : `platform=${client.platform} inject=[${(client.inject ?? []).join(',')}]`);

// 图标：DSH 只接受目录内的相对路径，且上限 256 KiB。
if (typeof manifest.icon === 'string') {
	const iconFile = join(ROOT, manifest.icon);
	if (!existsSync(iconFile)) problems.push(`icon 指向的 ${manifest.icon} 不存在`);
	else {
		const bytes = statSync(iconFile).size;
		if (bytes > 256 * 1024) problems.push(`icon ${manifest.icon} 为 ${bytes} 字节，超过 DSH 的 256 KiB 上限`);
	}
	line('图标', `${manifest.icon}（${statSync(iconFile).size} 字节）`);
}

// 1c. 打包面必须排除 Python 字节码缓存。
//
// **这不是洁癖。** npm 的 `files` 字段会压过 `.gitignore`，所以 `__pycache__/` 写在
// .gitignore 里也拦不住它——实测过一次：两个 `.pyc`（19.2 + 85.5 = 104.7 KB）被打进了包，
// 体积从 229 KB 涨到 280 KB，翻文件清单才发现。
// 这里断言的是「排除声明存在」：本地有没有 `__pycache__` 取决于谁跑过什么 python 命令
// （本项目就踩过——一次没带 `-B` 的临时验证命令造出了这两个文件）。
const packFiles = Array.isArray(manifest.files) ? manifest.files : [];
if (!packFiles.some((entry) => String(entry).includes('__pycache__'))) {
	problems.push('package.json 的 files 未声明排除 __pycache__ —— npm 的 files 会压过 .gitignore，.pyc 会被打进包');
}
if (!packFiles.some((entry) => String(entry).includes('.pyc'))) {
	problems.push('package.json 的 files 未声明排除 *.pyc');
}

// 2. 技能元数据
const meta = readSkillMeta(join(ROOT, 'lib', 'skill-meta.json'));
if (meta === null) problems.push('lib/skill-meta.json 缺失或损坏 —— 内置技能不会被注册');
else line('技能', `${meta.name}（描述 ${meta.description.length} 字）`);

// 3. 技能负载
const skillDir = join(ROOT, 'skills', 'persona-driven');
const required = [
	'SKILL.md',
	'assets/personas.json',
	'scripts/persona.py',
	'scripts/hook_persona.py',
	'scripts/check_environment.py',
	'scripts/experiment.py',
	'scripts/persona_dashboard.py'
];
const missing = required.filter((rel) => !existsSync(join(skillDir, rel)));
if (missing.length > 0) problems.push(`技能负载缺文件：${missing.join(', ')}`);
line('负载文件', `${required.length - missing.length}/${required.length}`);

const domains = existsSync(join(skillDir, 'references'))
	? readdirSync(join(skillDir, 'references')).filter((f) => f.startsWith('domains-') && f.endsWith('.md'))
	: [];
if (domains.length !== 8) problems.push(`领域映射应有 8 份，实际 ${domains.length} 份`);
line('领域映射', `${domains.length}/8`);

// 4. 是否已装进某个 profile
const home = dshHome();
const profilesDir = join(home, 'profiles');
let installedIn = null;
if (existsSync(profilesDir)) {
	for (const profile of readdirSync(profilesDir)) {
		const manifestPath = join(profilesDir, profile, 'package.json');
		if (!existsSync(manifestPath)) continue;
		try {
			const bundles = JSON.parse(readFileSync(manifestPath, 'utf8'))?.dsh?.profile?.bundles ?? [];
			if (bundles.includes(PACKAGE_NAME)) installedIn = profile;
		} catch {
			/* 该 profile 的 package.json 不可读：跳过 */
		}
	}
}
line('DSH_HOME', home);
line('装入 profile', installedIn ?? '尚未装入');
if (installedIn === null) {
	notes.push(
		`未装入任何 profile —— 技能仍可用（把 skills/persona-driven 复制到 ${join(home, 'skills')}），` +
			'但 L3 每轮注入与安全底线强制确认需要装入 bundle'
	);
}

// 5. 状态目录可写
const root = personaRoot();
try {
	const { mkdirSync, writeFileSync, rmSync } = await import('node:fs');
	mkdirSync(root, { recursive: true });
	const probe = join(root, '.doctor-probe');
	writeFileSync(probe, 'ok', 'utf8');
	rmSync(probe, { force: true });
	line('状态目录', `可写 ${root}`);
} catch (error) {
	problems.push(`状态目录不可写：${root}（${String(error)}）`);
	line('状态目录', `不可写 ${root}`);
}

console.log('─'.repeat(60));
for (const note of notes) console.log(`· ${note}`);
if (problems.length === 0) {
	console.log('结论：就绪（阻塞项 0）');
} else {
	console.log(`结论：有阻塞项 ${problems.length} 条`);
	for (const problem of problems) console.log(`  ✗ ${problem}`);
}

process.exit(problems.length === 0 ? 0 : 2);
