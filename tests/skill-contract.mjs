/**
 * 技能契约测试：app 目录名 / skill-meta.json / SKILL.md frontmatter 三者必须是同一份事实，
 * 且全部满足 DSH `dsh-skill-filesystem` 的解析规则。
 *
 * 这条用例真的抓得住改动：把 description 改到 501 字符、把 name 加成下划线、
 * 或把 SKILL.md 的 frontmatter 删掉，都会让它失败。
 */

import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { eq, finish, group, ok } from './_assert.mjs';
import { isSkillName, readSkillMeta } from '../lib/persona-core.js';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const META_FILE = join(ROOT, 'lib', 'skill-meta.json');
const SKILL_DIR = join(ROOT, 'skills', 'persona-driven');
const SKILL_FILE = join(SKILL_DIR, 'SKILL.md');

/** DSH 模型侧技能目录对描述的长度上限（dsh-tool-skill 的 catalogDescriptionMaxLength 默认值）。 */
const CATALOG_DESCRIPTION_MAX = 500;

/** 从 SKILL.md 的 YAML frontmatter 里取一个标量字段（只支持本技能实际用到的形态）。 */
function frontmatterField(raw, key) {
	if (!raw.startsWith('---')) return undefined;
	const end = raw.indexOf('\n---', 3);
	if (end < 0) return undefined;
	const block = raw.slice(raw.indexOf('\n') + 1, end + 1);
	const lines = block.split('\n');
	for (let index = 0; index < lines.length; index += 1) {
		const match = new RegExp(`^${key.replace(/[.*+?^${}()|[\]\\]/gu, '\\$&')}:\\s*(.*)$`, 'u').exec(lines[index]);
		if (match === null) continue;
		const inline = match[1].trim();
		if (inline !== '' && !inline.startsWith('>') && !inline.startsWith('|')) {
			return inline.replace(/^['"]|['"]$/gu, '');
		}
		// 块标量：收集后续更深缩进的行。
		const indent = /^(\s+)/u.exec(lines[index + 1] ?? '')?.[1]?.length ?? 0;
		if (indent === 0) return '';
		const parts = [];
		for (let cursor = index + 1; cursor < lines.length; cursor += 1) {
			const line = lines[cursor];
			if (line.trim() === '') continue;
			const lead = /^(\s*)/u.exec(line)[1].length;
			if (lead < indent) break;
			parts.push(line.trim());
		}
		const folded = inline.startsWith('|') ? parts.join('\n') : parts.join(' ');
		return folded.replace(/^['"]|['"]$/gu, '');
	}
	return undefined;
}

group('技能契约');

ok(existsSync(SKILL_FILE), 'skills/persona-driven/SKILL.md 存在');
ok(existsSync(join(SKILL_DIR, 'assets', 'personas.json')), '技能负载含 assets/personas.json');

const meta = readSkillMeta(META_FILE);
ok(meta !== null, 'lib/skill-meta.json 可解析且含 name 与 description');

if (meta !== null) {
	eq(isSkillName(meta.name), true, `name "${meta.name}" 符合 DSH 的 kebab-case 文法`);

	const normalized = meta.description.replace(/\s+/gu, ' ').trim();
	ok(
		normalized.length > 0 && normalized.length <= CATALOG_DESCRIPTION_MAX,
		`description 归一化后长度 ${normalized.length} ≤ ${CATALOG_DESCRIPTION_MAX}`
	);
	ok(typeof meta.whenToUse === 'string' && meta.whenToUse.length > 0, 'whenToUse 是非空字符串');

	const raw = readFileSync(SKILL_FILE, 'utf8');
	const fmName = frontmatterField(raw, 'name');
	const fmDescription = frontmatterField(raw, 'description');
	eq(fmName, meta.name, 'SKILL.md frontmatter 的 name 与 skill-meta.json 一致');
	eq(
		(fmDescription ?? '').replace(/\s+/gu, ' ').trim(),
		normalized,
		'SKILL.md frontmatter 的 description 与 skill-meta.json 一致（同一份事实，不得各存副本）'
	);

	// DSH 会直接丢掉这三种旧拼写所在的整个技能，所以它们必须不出现。
	for (const legacy of ['disableModelInvocation', 'modelInvocable', 'userInvocable']) {
		eq(new RegExp(`^${legacy}:`, 'mu').test(raw), false, `frontmatter 不含会让 DSH 丢弃技能的旧拼写 "${legacy}"`);
	}
}

finish('skill-contract');
