/**
 * `@dsh-external/persona-driven` — Client half（Web / 桌面共用同一份 UI）。
 *
 * 只做一件事：在输入框上方挂一个**只读**状态药丸，显示当前生效的人格档位
 * （人格名 + 八位档位串 + 作用范围）。切档仍然由用户在对话里用人话发起
 * （「用 ENTJ」「果断点」），这里**故意不提供**任何写操作：
 * 从 UI 静默改全局档位会波及用户毫无察觉的其他对话，本技能明确禁止这种做法。
 *
 * 防御性约定（照 cordis-plugin-development 的 ui-plugin / practices）：
 *   - 工厂零副作用；资源在 apply 里注册并返回 disposer（这里无需额外资源）。
 *   - 任何异常都不得让 slot 条目崩掉——fetch 失败、字段缺失一律渲染 null。
 *   - 样式只用主题 token，并带 CSS 变量兜底；token 改名只会降级外观，不会破坏渲染。
 */
window.__ModuleLoader__.load({
	id: '@dsh-external/persona-driven',
	factory(require) {
		const React = require('react');
		const h = React.createElement;
		const ENDPOINT = '/host/persona-driven/state';
		const LAYER_LABEL = {
			session: '仅本对话',
			project: '本项目目录',
			global: '全局默认',
		};
		const STYLE = {
			display: 'inline-flex',
			alignItems: 'center',
			gap: '6px',
			alignSelf: 'flex-start',
			maxWidth: '100%',
			padding: '2px 8px',
			borderRadius: '999px',
			border: '1px solid var(--dsw-alias-border-subtle, currentColor)',
			background: 'var(--dsw-alias-bg-elevated, transparent)',
			color: 'var(--dsw-alias-text-secondary, inherit)',
			fontSize: '11px',
			lineHeight: '18px',
			whiteSpace: 'nowrap',
			overflow: 'hidden',
			textOverflow: 'ellipsis',
		};

		function StatusPill() {
			const [data, setData] = React.useState(null);
			React.useEffect(() => {
				let alive = true;
				const load = () => {
					Promise.resolve()
						.then(() => fetch(ENDPOINT, { headers: { accept: 'application/json' } }))
						.then((res) => (res.ok ? res.json() : null))
						.then((json) => {
							if (alive) setData(json && json.ok ? json : null);
						})
						.catch(() => {
							if (alive) setData(null);
						});
				};
				load();
				const onFocus = () => load();
				window.addEventListener('focus', onFocus);
				return () => {
					alive = false;
					window.removeEventListener('focus', onFocus);
				};
			}, []);

			// 未设定档位、路由不可用或字段缺失：什么都不渲染，绝不占用输入区空间。
			if (!data || typeof data.persona !== 'string' || data.persona === '') return null;
			if (data.level === 'off') return null;
			const dials = typeof data.dialsText === 'string' ? data.dialsText : '';
			const scope = LAYER_LABEL[data.layer] || data.layer || '';
			const title = [data.gist, data.dialsLine].filter(Boolean).join(' · ');
			return h(
				'div',
				{ style: STYLE, title, 'data-persona-driven': 'status' },
				h('span', { style: { opacity: 0.6 } }, '人格'),
				h('span', { style: { fontWeight: 600 } }, data.persona),
				dials ? h('span', null, dials) : null,
				scope ? h('span', { style: { opacity: 0.6 } }, scope) : null,
			);
		}

		return {
			inject: ['slots'],
			apply(ctx) {
				ctx.slots.inject('conversation.composer.dock', () =>
					ctx.slots.register(
						{
							name: 'conversation.composer.dock',
							id: 'persona-driven',
							order: 60,
						},
						StatusPill,
					),
				);
			},
		};
	},
});
