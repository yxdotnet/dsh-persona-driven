/**
 * 用 `--import` 装载 {@link ./_dsh-hook.mjs}，把 `@deepseek-ai/*` 指到
 * `PERSONA_DSH_NODE_MODULES` 指定的那份 DSH node_modules。
 *
 * 只在测试子进程里生效；仓库根本身不需要、也不应该出现 `node_modules`。
 */

import { register } from 'node:module';

if ((process.env.PERSONA_DSH_NODE_MODULES ?? '') !== '') {
	register('./_dsh-hook.mjs', import.meta.url);
}
