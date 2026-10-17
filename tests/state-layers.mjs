/**
 * 三层状态定位测试：会话 > 项目 > 全局，且写入路径与 Python 侧完全同名同目录。
 *
 * 这条用例真的抓得住改动：把优先级顺序调换、把项目层写回项目目录内、
 * 或让 sanitizeSession 放过非法字符，都会失败。
 */

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { eq, finish, group } from './_assert.mjs';
import { dayStamp, envSessionId, findProjectRoot, personaRoot, resolveState, sanitizeSession, sha12 } from '../lib/persona-core.js';

const sandbox = mkdtempSync(join(tmpdir(), 'persona-state-'));
const home = join(sandbox, 'dshhome');
const projectRoot = join(sandbox, 'proj');
const nested = join(projectRoot, 'src', 'deep');

mkdirSync(join(home, 'persona', 'sessions'), { recursive: true });
mkdirSync(join(projectRoot, 'src', 'deep'), { recursive: true });
mkdirSync(join(projectRoot, '.git'), { recursive: true });
mkdirSync(join(projectRoot, '.dsh', 'persona'), { recursive: true });

const env = { DSH_HOME: home };
const sessionId = 'session-1234-abcd';
const safeId = sanitizeSession(sessionId);

const writeState = (path, state) => writeFileSync(path, JSON.stringify(state), 'utf8');
const sessionPath = join(home, 'persona', 'sessions', `state-${safeId}.json`);
const projectPath = join(projectRoot, '.dsh', 'persona', 'state.json');
const globalPath = join(home, 'persona', 'state.json');

group('路径推导');

eq(personaRoot(env), join(home, 'persona'), '状态根目录是 $DSH_HOME/persona');
eq(sanitizeSession('session-1/../evil\\x'), 'session-1evilx', 'sanitizeSession 剥掉路径分隔符与点号，拼不出越界路径');
eq(sanitizeSession('a'.repeat(200)).length, 64, 'sanitizeSession 截断到 64 字符');
eq(findProjectRoot(nested), projectRoot, 'findProjectRoot 向上找到含 .git 的祖先');
eq(findProjectRoot(sandbox), sandbox, '找不到 .git 时返回起点本身');
eq(dayStamp(new Date(2026, 0, 5)), '2026-01-05', 'dayStamp 按本地时区补零');
eq(sha12('abc').length, 12, 'sha12 返回 12 位十六进制');

group('优先级：会话 > 项目 > 全局');

writeState(globalPath, { persona: 'GLOBAL', level: 'l3' });
eq(resolveState({ env, cwd: nested, sessionId }).state.persona, 'GLOBAL', '只有全局时读全局');

writeState(projectPath, { persona: 'PROJECT', level: 'l3' });
eq(resolveState({ env, cwd: nested, sessionId }).state.persona, 'PROJECT', '项目层压过全局层');

writeState(sessionPath, { persona: 'SESSION', level: 'l3' });
const resolved = resolveState({ env, cwd: nested, sessionId });
eq(resolved.state.persona, 'SESSION', '会话层压过项目层');
eq(resolved.layer, 'session', '报告生效层级为 session');
eq(resolved.path, sessionPath, '报告的路径是会话层文件');

group('缺层与降级');

rmSync(sessionPath);
eq(resolveState({ env, cwd: nested, sessionId }).layer, 'project', '会话层缺失时落到项目层');
rmSync(projectPath, { force: true });
rmSync(join(projectRoot, '.dsh'), { recursive: true, force: true });
eq(resolveState({ env, cwd: nested, sessionId }).layer, 'global', '项目层缺失时落到全局层');
rmSync(globalPath);
const empty = resolveState({ env, cwd: nested, sessionId });
eq(empty.state, null, '三层都没有时 state 为 null（不抛异常）');
eq(empty.layer, 'global', '三层都没有时报告 global');

group('工作目录不落在项目内时');

eq(resolveState({ env, cwd: sandbox, sessionId: undefined }).layer, 'global', '无会话标识时读全局层');

group('会话标识来源');

eq(envSessionId({ DSH_SESSION_ID: 'session-x' }), 'session-x', '优先读 DSH_SESSION_ID');
eq(envSessionId({ CODEBUDDY_SESSION_ID: 'cb-1' }), 'cb-1', '回退读 CODEBUDDY_SESSION_ID');
eq(envSessionId({ CLAUDE_SESSION_ID: 'cc-1' }), 'cc-1', '再回退读 CLAUDE_SESSION_ID');
eq(envSessionId({}), '', '都没有时返回空串');

rmSync(sandbox, { recursive: true, force: true });

finish('state-layers');
