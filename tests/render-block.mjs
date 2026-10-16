/**
 * 档位块渲染测试：l3 完整块 / l2 只给三条底线 / off 与无档位不注入。
 *
 * 这条用例真的抓得住改动：把 l2 改回「零注入」、把三条底线里的任意一条删掉、
 * 或让 off 也注入，都会失败。
 */

import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { eq, finish, group, ok } from './_assert.mjs';
import {
	L2_FLOOR_HEAD,
	PASS_FIRST_LINE,
	SAFETY_FLOOR_LINE,
	VERIFY_FLOOR_LINE,
	dialsLine,
	dialsStr,
	renderBlock
} from '../lib/persona-core.js';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const DATA = JSON.parse(readFileSync(join(ROOT, 'skills', 'persona-driven', 'assets', 'personas.json'), 'utf8'));

group('档位块渲染');

eq(dialsStr(DATA.types.ESTP.dials), 'P5/V4/R5/S4/D5/E1/ST4/W2', 'ESTP 档位串');
eq(dialsStr({}), '', '空档位渲染成空串');
eq(dialsStr({ PACE: 3, WARMTH: 2 }), 'P3/W2', '缺项按固定顺序跳过');

eq(
	dialsLine(DATA.types.ISTJ.dials, DATA).startsWith('推进节奏:'),
	true,
	'dialsLine 用数据源里的中文标签'
);

group('l3 完整块');

const l3 = renderBlock({ ...DATA.types.ESTP, level: 'l3', gist: DATA.types.ESTP.gist, persona: 'ESTP', kind: 'type' }, DATA);
ok(l3.startsWith('[人格档位 P5/V4/R5/S4/D5/E1/ST4/W2 · ESTP]'), 'l3 抬头是档位串 + 人格名');
ok(l3.includes('验证强度原文：'), 'l3 含验证强度原文（实时读数据源）');
ok(l3.includes('思考步骤：①'), 'l3 含思考步骤');
eq(l3.includes(SAFETY_FLOOR_LINE), true, 'l3 含安全底线');
eq(l3.includes(VERIFY_FLOOR_LINE), true, 'l3 含验证下限');
eq(l3.includes(PASS_FIRST_LINE), true, 'l3 含一次到位');

group('l2 只给三条底线');

const l2 = renderBlock({ persona: 'ENTJ', level: 'l2', dials: DATA.types.ENTJ.dials, kind: 'type' }, DATA);
eq(l2, [L2_FLOOR_HEAD, SAFETY_FLOOR_LINE, VERIFY_FLOOR_LINE, PASS_FIRST_LINE].join('\n'), 'l2 恰好是抬头 + 三条底线');
eq(l2.includes('P5/'), false, 'l2 不给档位数值');
eq(l2.includes('思考步骤'), false, 'l2 不给思考步骤');

group('off 与无档位');

eq(renderBlock({ level: 'off', persona: 'ESTP', dials: DATA.types.ESTP.dials }, DATA), '', 'off 不注入');
eq(renderBlock(null, DATA), '', '无档位不注入');
eq(renderBlock(undefined, DATA), '', 'undefined 状态不注入');

group('思考步骤的实时读与快照兜底');

// 数据源里改掉文案，注入块必须立刻跟着变（不必重新 set）——这是「实时读」的判据。
const mutated = structuredClone(DATA);
mutated.types.ESTP.steps = ['实时步骤甲', '实时步骤乙'];
const live = renderBlock({ persona: 'ESTP', level: 'l3', kind: 'type', dials: mutated.types.ESTP.dials, steps: ['旧快照'] }, mutated);
eq(live.includes('①实时步骤甲 ②实时步骤乙'), true, 'l3 优先用数据源里的实时步骤');
eq(live.includes('旧快照'), false, '数据源可用时忽略快照');

// 数据源查不到该档位（自定义 dials）时才回落到快照。
const fallback = renderBlock({ persona: '自定义', level: 'l3', kind: 'custom', dials: { PACE: 3 }, steps: ['快照甲'] }, DATA);
eq(fallback.includes('①快照甲'), true, '数据源查不到时回落到已存快照');

group('数据源缺失时的降级');

const degraded = renderBlock({ persona: 'ESTP', level: 'l3', kind: 'type', dials: DATA.types.ESTP.dials }, null);
ok(degraded.startsWith('[人格档位'), '数据源缺失仍能渲染抬头');
eq(degraded.includes('验证强度原文：'), false, '数据源缺失时跳过验证强度原文（不崩）');
eq(degraded.includes(PASS_FIRST_LINE), true, '数据源缺失时一次到位底线仍在');
eq(degraded.includes(SAFETY_FLOOR_LINE), true, '数据源缺失时安全底线仍在');

finish('render-block');
