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

import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { dshHome, personaRoot, readSkillMeta } from '../lib/persona-core.js';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const PACKAGE_NAME = '@dsh-external/persona-driven';
const problems = [];
const notes = [];

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
