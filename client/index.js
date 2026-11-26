/**
 * Browser half of the trip-planner DSH plugin: an Apple-white settings card
 * mounted into the DSH Web settings shell (`settings.section` slot).
 *
 * Deliberately dependency-free besides React: DSH's module loader provides
 * `require("react")`, the same contract the first-party settings panels use.
 * The card talks to `/trip-planner/settings` (see lib/settings.js).
 */

window.__ModuleLoader__.load({
	id: "dsh-trip-planner-cn",
	factory: (require) => {
		const React = require("react");
		const { useState, useEffect, useMemo, useRef } = React;
		const h = React.createElement;

		const DEFAULT_FORM = {
			amapKey: "",
			baiduMapAk: "",
			dashscopeApiKey: "",
			siliconflowApiKey: "",
			mapProvider: "amap",
			trainLiveCacheDir: "",
			trainLiveMcpCommand: "",
			flyaiApiKey: "",
			flyaiCli: "",
			windyBase: "https://node.windy.com",
			windyCache: "windycache.json"
		};

		const SECRET_FIELDS = ["amapKey", "baiduMapAk", "dashscopeApiKey", "siliconflowApiKey", "flyaiApiKey"];

		const STYLE_ID = "dsh-trip-planner-cn-style";
		const STYLE = `
.tp-settings{--tp-bg:#f5f5f7;--tp-card:#fff;--tp-text:#1d1d1f;--tp-muted:#6e6e73;--tp-subtle:#86868b;--tp-border:rgba(0,0,0,.08);--tp-accent:#007aff;--tp-accent-hover:#0071e3;--tp-green:#34c759;--tp-orange:#ff9f0a;--tp-red:#ff3b30;box-sizing:border-box;width:100%;max-width:780px;margin:0 auto;padding:20px 20px 18px;color:var(--tp-text);background:linear-gradient(180deg,#fff 0%,#fafafa 100%);border:1px solid rgba(0,0,0,.06);border-radius:22px;box-shadow:0 1px 2px rgba(0,0,0,.03),0 12px 40px rgba(0,0,0,.05);font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","SF Pro Display","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;font-size:13px;line-height:1.5;-webkit-font-smoothing:antialiased;color-scheme:light}
.tp-settings *,.tp-settings *::before,.tp-settings *::after{box-sizing:border-box}
.tp-hero{display:flex;align-items:center;gap:14px;padding:6px 2px 18px}
.tp-app-icon{width:54px;height:54px;flex:0 0 54px;border-radius:15px;background:linear-gradient(145deg,#0a84ff 0%,#0071e3 100%);display:grid;place-items:center;box-shadow:0 10px 24px rgba(0,122,255,.28),inset 0 1px 0 rgba(255,255,255,.28)}
.tp-hero-main{min-width:0;flex:1}
.tp-title{font-size:20px;font-weight:700;letter-spacing:-.02em;line-height:1.2}
.tp-subtitle{margin-top:2px;color:var(--tp-muted);font-size:12.5px}
.tp-hero-badge{display:inline-flex;align-items:center;gap:5px;padding:4px 9px;border-radius:999px;background:rgba(52,199,89,.12);color:#1d7a46;font-size:11px;font-weight:600;white-space:nowrap}
.tp-hero-badge-dot{width:6px;height:6px;border-radius:50%;background:var(--tp-green);box-shadow:0 0 0 3px rgba(52,199,89,.16)}
.tp-chips{display:flex;flex-wrap:wrap;gap:6px;margin:-6px 0 14px;padding:0 2px}
.tp-chip{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;background:#f5f5f7;color:var(--tp-muted);font-size:11.5px;font-weight:500;white-space:nowrap}
.tp-chip-dot{width:6px;height:6px;border-radius:50%;background:#c7c7cc}
.tp-chip.is-ok{background:rgba(52,199,89,.12);color:#1d7a46}
.tp-chip.is-ok .tp-chip-dot{background:var(--tp-green);box-shadow:0 0 0 3px rgba(52,199,89,.15)}
.tp-card{background:var(--tp-card);border:1px solid var(--tp-border);border-radius:18px;padding:18px 18px 16px;margin-bottom:14px;box-shadow:0 1px 2px rgba(0,0,0,.03),0 8px 24px rgba(0,0,0,.035)}
.tp-card-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:14px}
.tp-card-title{font-size:15px;font-weight:600;letter-spacing:-.01em}
.tp-card-desc{margin-top:2px;color:var(--tp-subtle);font-size:12px}
.tp-card-tag{flex:none;padding:2px 8px;border-radius:999px;background:#f5f5f7;color:var(--tp-muted);font-size:10.5px;font-weight:600;letter-spacing:.02em}
.tp-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.tp-field{display:flex;flex-direction:column;gap:6px;min-width:0}
.tp-field.is-full{grid-column:1/-1}
.tp-label{color:var(--tp-muted);font-size:12px;font-weight:500;letter-spacing:.01em}
.tp-hint{color:var(--tp-subtle);font-size:11.5px;line-height:1.45}
.tp-input-wrap{position:relative;display:flex;align-items:center}
.tp-input{width:100%;height:36px;padding:0 11px;border:1px solid #d2d2d7;border-radius:10px;background:#fff;color:var(--tp-text);font:inherit;font-size:13px;outline:none;transition:border-color .18s ease,box-shadow .18s ease}
.tp-input::placeholder{color:#a1a1a6}
.tp-input:focus{border-color:var(--tp-accent);box-shadow:0 0 0 3px rgba(0,122,255,.16)}
.tp-input.is-secret{padding-right:38px}
.tp-input-mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
.tp-eye{position:absolute;right:4px;width:28px;height:28px;display:grid;place-items:center;border:0;border-radius:7px;background:transparent;color:#a1a1a6;cursor:pointer;transition:background .15s ease,color .15s ease}
.tp-eye:hover{background:#f5f5f7;color:var(--tp-text)}
.tp-eye:focus-visible{outline:2px solid var(--tp-accent);outline-offset:1px}
.tp-segmented{display:inline-flex;padding:2px;border-radius:10px;background:#f5f5f7;border:1px solid rgba(0,0,0,.04)}
.tp-segment{border:0;border-radius:8px;padding:6px 14px;background:transparent;color:var(--tp-muted);font:inherit;font-size:12px;font-weight:500;cursor:pointer;transition:background .18s ease,color .18s ease,box-shadow .18s ease}
.tp-segment:hover{color:var(--tp-text)}
.tp-segment.is-active{background:#fff;color:var(--tp-text);box-shadow:0 1px 3px rgba(0,0,0,.12)}
.tp-segment:focus-visible{outline:2px solid var(--tp-accent);outline-offset:1px}
.tp-advanced{margin-top:2px}
.tp-advanced>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:7px;color:var(--tp-muted);font-size:12px;font-weight:500;padding:6px 2px;border-radius:8px}
.tp-advanced>summary::-webkit-details-marker{display:none}
.tp-advanced>summary:hover{color:var(--tp-text)}
.tp-advanced-chevron{transition:transform .18s ease}
.tp-advanced[open] .tp-advanced-chevron{transform:rotate(90deg)}
.tp-advanced-body{padding-top:12px}
.tp-message{display:flex;align-items:flex-start;gap:8px;padding:10px 12px;border-radius:12px;margin-bottom:14px;font-size:12.5px;line-height:1.45;animation:tp-in .22s ease}
.tp-message.is-ok{background:rgba(52,199,89,.1);color:#1d7a46}
.tp-message.is-error{background:rgba(255,59,48,.08);color:#d70015}
.tp-message-icon{flex:none;margin-top:1px}
.tp-actions{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:6px 2px 0}
.tp-actions-note{color:var(--tp-subtle);font-size:11.5px}
.tp-actions-right{display:flex;align-items:center;gap:8px}
.tp-btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;height:34px;padding:0 16px;border-radius:10px;border:1px solid transparent;font:inherit;font-size:12.5px;font-weight:500;cursor:pointer;transition:background .18s ease,border-color .18s ease,transform .08s ease,box-shadow .18s ease}
.tp-btn:active{transform:scale(.985)}
.tp-btn:focus-visible{outline:2px solid var(--tp-accent);outline-offset:2px}
.tp-btn:disabled{opacity:.55;cursor:not-allowed;transform:none}
.tp-btn-primary{background:var(--tp-accent);border-color:var(--tp-accent);color:#fff;box-shadow:0 6px 16px rgba(0,122,255,.22)}
.tp-btn-primary:hover:not(:disabled){background:var(--tp-accent-hover);border-color:var(--tp-accent-hover)}
.tp-btn-secondary{background:#f5f5f7;border-color:#d2d2d7;color:var(--tp-text)}
.tp-btn-secondary:hover:not(:disabled){background:#ebebed}
.tp-btn-ghost{background:transparent;border-color:transparent;color:var(--tp-accent);padding:0 8px}
.tp-btn-ghost:hover:not(:disabled){background:rgba(0,122,255,.08)}
.tp-footer{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:4px 4px 0;color:var(--tp-subtle);font-size:11px}
.tp-path{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:70%;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.tp-skeleton{height:14px;border-radius:6px;background:linear-gradient(90deg,#f0f0f2,#fafafa,#f0f0f2);background-size:200% 100%;animation:tp-shimmer 1.1s linear infinite}
.tp-loading{padding:26px 4px;color:var(--tp-muted);font-size:12.5px}
@keyframes tp-in{from{opacity:0;transform:translateY(-3px)}to{opacity:1;transform:none}}
@keyframes tp-shimmer{to{background-position:-200% 0}}
@media (max-width:640px){.tp-settings{padding:16px 14px 14px;border-radius:18px}.tp-hero{align-items:flex-start}.tp-app-icon{width:46px;height:46px;flex-basis:46px;border-radius:13px}.tp-title{font-size:18px}.tp-hero-badge{display:none}.tp-grid{grid-template-columns:1fr}.tp-card{padding:16px 14px 14px;border-radius:16px}.tp-actions{flex-direction:column;align-items:stretch}.tp-actions-right{justify-content:flex-end}.tp-footer{flex-direction:column;align-items:flex-start;gap:4px}.tp-path{max-width:100%}}
`;

		const icons = {
			eye: h("svg", { viewBox: "0 0 24 24", width: 15, height: 15, fill: "none", stroke: "currentColor", strokeWidth: 1.8, strokeLinecap: "round", strokeLinejoin: "round" },
				h("path", { d: "M2 12s3.5-6.5 10-6.5S22 12 22 12s-3.5 6.5-10 6.5S2 12 2 12Z" }),
				h("circle", { cx: "12", cy: "12", r: "2.6" })),
			eyeOff: h("svg", { viewBox: "0 0 24 24", width: 15, height: 15, fill: "none", stroke: "currentColor", strokeWidth: 1.8, strokeLinecap: "round", strokeLinejoin: "round" },
				h("path", { d: "M3 3l18 18" }),
				h("path", { d: "M10.6 10.6a2.6 2.6 0 0 0 3.6 3.6" }),
				h("path", { d: "M9.9 5.2A10.9 10.9 0 0 1 12 5c6.5 0 10 7 10 7a17.6 17.6 0 0 1-2.2 3.1M6.6 6.6A17.7 17.7 0 0 0 2 12s3.5 7 10 7a10.6 10.6 0 0 0 4.4-1" })),
			check: h("svg", { viewBox: "0 0 24 24", width: 14, height: 14, fill: "none", stroke: "currentColor", strokeWidth: 2.2, strokeLinecap: "round", strokeLinejoin: "round" },
				h("path", { d: "M20 6 9 17l-5-5" })),
			alert: h("svg", { viewBox: "0 0 24 24", width: 14, height: 14, fill: "none", stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round", strokeLinejoin: "round" },
				h("path", { d: "M12 9v4" }),
				h("path", { d: "M12 17h.01" }),
				h("path", { d: "M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" })),
			chevron: h("svg", { className: "tp-advanced-chevron", viewBox: "0 0 24 24", width: 13, height: 13, fill: "none", stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round", strokeLinejoin: "round" },
				h("path", { d: "m9 18 6-6-6-6" })),
			pin: h("svg", { viewBox: "0 0 64 64", width: 30, height: 30, "aria-hidden": "true" },
				h("path", { d: "M32 10c-9.4 0-17 7.6-17 17 0 12.7 17 27 17 27s17-14.3 17-27c0-9.4-7.6-17-17-17Z", fill: "#fff", opacity: ".96" }),
				h("circle", { cx: "32", cy: "27", r: "6.2", fill: "#0a84ff" }),
				h("path", { d: "M22 52c3-4 7-6 10-6s7 2 10 6", fill: "none", stroke: "#fff", strokeWidth: "3.2", strokeLinecap: "round", opacity: ".9" }))
		};

		function Field({ id, label, hint, full, children }) {
			return h("div", { className: full ? "tp-field is-full" : "tp-field" },
				h("label", { className: "tp-label", htmlFor: id }, label),
				children,
				hint ? h("span", { className: "tp-hint" }, hint) : null);
		}

		function SecretInput({ id, value, onChange, placeholder }) {
			const [show, setShow] = useState(false);
			return h("div", { className: "tp-input-wrap" },
				h("input", {
					id,
					className: "tp-input is-secret",
					type: show ? "text" : "password",
					value,
					placeholder,
					autoComplete: "off",
					spellCheck: false,
					onChange: (event) => onChange(event.target.value)
				}),
				h("button", {
					type: "button",
					className: "tp-eye",
					"aria-label": show ? "隐藏密钥" : "显示密钥",
					onClick: () => setShow((current) => !current)
				}, show ? icons.eyeOff : icons.eye));
		}

		function Segmented({ value, options, onChange, ariaLabel }) {
			return h("div", { className: "tp-segmented", role: "group", "aria-label": ariaLabel },
				options.map((option) => h("button", {
					key: option.value,
					type: "button",
					className: value === option.value ? "tp-segment is-active" : "tp-segment",
					"aria-pressed": value === option.value,
					onClick: () => onChange(option.value)
				}, option.label)));
		}

		function ServiceChip({ ok, label }) {
			return h("span", { className: ok ? "tp-chip is-ok" : "tp-chip" },
				h("span", { className: "tp-chip-dot" }),
				label);
		}

		function SettingsCard() {
			const [form, setForm] = useState(DEFAULT_FORM);
			const [loaded, setLoaded] = useState(false);
			const [loadError, setLoadError] = useState(false);
			const [saving, setSaving] = useState(false);
			const [message, setMessage] = useState(null);
			const [path, setPath] = useState("");
			const [resetArmed, setResetArmed] = useState(false);
			const resetTimer = useRef(null);

			const fieldId = React.useId ? React.useId() : "tp";
			const id = (name) => `${fieldId}-${name}`;

			useEffect(() => {
				let alive = true;
				fetch("/trip-planner/settings", { headers: { accept: "application/json" } })
					.then((response) => response.json())
					.then((envelope) => {
						if (!alive) return;
						if (!envelope.ok) {
							setLoadError(true);
							setMessage({ tone: "error", text: envelope.error?.message || "读取设置失败" });
							setLoaded(true);
							return;
						}
						const value = envelope.value || {};
						setForm({ ...DEFAULT_FORM, ...(value.settings || {}) });
						setPath(value.path || "");
						setLoadError(false);
						setLoaded(true);
					})
					.catch((error) => {
						if (!alive) return;
						setLoadError(true);
						setMessage({ tone: "error", text: error instanceof Error ? error.message : "读取设置失败" });
						setLoaded(true);
					});
				return () => {
					alive = false;
					if (resetTimer.current) clearTimeout(resetTimer.current);
				};
			}, []);

			const chips = useMemo(() => [
				{ label: "高德", ok: form.amapKey.trim() !== "" },
				{ label: "百度", ok: form.baiduMapAk.trim() !== "" },
				{ label: "生图", ok: form.dashscopeApiKey.trim() !== "" || form.siliconflowApiKey.trim() !== "" },
				{ label: "12306", ok: form.trainLiveMcpCommand.trim() !== "" },
				{ label: "飞猪", ok: form.flyaiApiKey.trim() !== "" }
			], [form]);

			function update(key, value) {
				setForm((current) => ({ ...current, [key]: value }));
				setMessage(null);
			}

			function flash(messageObject) {
				setMessage(messageObject);
				if (messageObject.tone === "ok") {
					if (resetTimer.current) clearTimeout(resetTimer.current);
					resetTimer.current = setTimeout(() => setMessage(null), 4200);
				}
			}

			async function save(event) {
				event?.preventDefault?.();
				setSaving(true);
				setMessage(null);
				try {
					const response = await fetch("/trip-planner/settings", {
						method: "POST",
						headers: { "content-type": "application/json", accept: "application/json" },
						body: JSON.stringify(form)
					});
					const envelope = await response.json();
					if (!response.ok || !envelope.ok) {
						throw new Error(envelope.error?.message || `HTTP ${response.status}`);
					}
					const value = envelope.value || {};
					setForm({ ...DEFAULT_FORM, ...(value.settings || {}) });
					setPath(value.path || path);
					setResetArmed(false);
					flash({ tone: "ok", text: "设置已保存，下次运行脚本即可生效" });
				} catch (error) {
					flash({ tone: "error", text: error instanceof Error ? error.message : "保存失败" });
				} finally {
					setSaving(false);
				}
			}

			function reset() {
				if (!resetArmed) {
					setResetArmed(true);
					return;
				}
				setForm({ ...DEFAULT_FORM });
				setResetArmed(false);
				flash({ tone: "ok", text: "已恢复默认值，点击保存后生效" });
			}

			if (!loaded) {
				return h("div", { className: "tp-settings" },
					h("div", { className: "tp-loading" }, "正在读取设置…"));
			}

			return h("form", { className: "tp-settings", onSubmit: save },
				h("div", { className: "tp-hero" },
					h("div", { className: "tp-app-icon" }, icons.pin),
					h("div", { className: "tp-hero-main" },
						h("div", { className: "tp-title" }, "境内旅行规划"),
						h("div", { className: "tp-subtitle" }, "Trip Planner CN · 设置")),
					h("span", { className: "tp-hero-badge" },
						h("span", { className: "tp-hero-badge-dot" }),
						"已连接")),
				h("div", { className: "tp-chips" },
					chips.map((chip) => h(ServiceChip, { key: chip.label, ok: chip.ok, label: chip.label }))),

				message ? h("div", { className: message.tone === "ok" ? "tp-message is-ok" : "tp-message is-error", role: "status" },
					h("span", { className: "tp-message-icon" }, message.tone === "ok" ? icons.check : icons.alert),
					h("span", null, message.text)) : null,

				h("section", { className: "tp-card" },
					h("div", { className: "tp-card-head" },
						h("div", null,
							h("div", { className: "tp-card-title" }, "地图与路线"),
							h("div", { className: "tp-card-desc" }, "坐标解析、实时时距、静态地图与导航深链")),
						h("span", { className: "tp-card-tag" }, "高德 / 百度")),
					h("div", { className: "tp-grid" },
						h(Field, { id: id("amap"), label: "高德 Web 服务 Key", full: true, hint: "用于 geocode、实时路线、静态地图与 POI 搜索；个人开发者免费额度充足。" },
							h(SecretInput, { id: id("amap"), value: form.amapKey, placeholder: "未配置时地图与实时路线自动降级", onChange: (value) => update("amapKey", value) })),
						h(Field, { id: id("baidu"), label: "百度地图服务端 AK", full: true, hint: "高德失败时的地理编码回退源，可留空。" },
							h(SecretInput, { id: id("baidu"), value: form.baiduMapAk, placeholder: "可选回退源", onChange: (value) => update("baiduMapAk", value) })),
						h(Field, { id: id("provider"), label: "默认导航深链", full: true, hint: "每一跳地图链接默认使用的服务商。" },
							h(Segmented, {
								value: form.mapProvider,
								ariaLabel: "默认导航深链",
								options: [{ value: "amap", label: "高德" }, { value: "baidu", label: "百度" }],
								onChange: (value) => update("mapProvider", value)
							}))),

				h("section", { className: "tp-card" },
					h("div", { className: "tp-card-head" },
						h("div", null,
							h("div", { className: "tp-card-title" }, "图片生成"),
							h("div", { className: "tp-card-desc" }, "为行程封面与每日配图生成定制插画")),
						h("span", { className: "tp-card-tag" }, "可选")),
					h("div", { className: "tp-grid" },
						h(Field, { id: id("dashscope"), label: "阿里云百炼 API Key", hint: "通义万相生图，国内直连。" },
							h(SecretInput, { id: id("dashscope"), value: form.dashscopeApiKey, placeholder: "可选", onChange: (value) => update("dashscopeApiKey", value) })),
						h(Field, { id: id("siliconflow"), label: "硅基流动 API Key", hint: "Kolors 生图，国内直连。" },
							h(SecretInput, { id: id("siliconflow"), value: form.siliconflowApiKey, placeholder: "可选", onChange: (value) => update("siliconflowApiKey", value) })))),

				h("section", { className: "tp-card" },
					h("div", { className: "tp-card-head" },
						h("div", null,
							h("div", { className: "tp-card-title" }, "铁路与机票"),
							h("div", { className: "tp-card-desc" }, "12306 实时候选查询与飞猪只读机票/酒店参考价")),
						h("span", { className: "tp-card-tag" }, "可选")),
					h("div", { className: "tp-grid" },
						h(Field, { id: id("mcp"), label: "12306 MCP 启动命令", full: true, hint: "本地 stdio MCP 的启动命令（不是密钥）；留空时只使用铁路深链。" },
							h("input", { id: id("mcp"), className: "tp-input tp-input-mono", value: form.trainLiveMcpCommand, placeholder: "node /absolute/path/12306-mcp/build/index.js", autoComplete: "off", spellCheck: false, onChange: (event) => update("trainLiveMcpCommand", event.target.value) })),
						h(Field, { id: id("cache"), label: "12306 缓存目录", hint: "车站表与查询路径缓存，留空用系统临时目录。" },
							h("input", { id: id("cache"), className: "tp-input tp-input-mono", value: form.trainLiveCacheDir, placeholder: "系统临时目录", autoComplete: "off", spellCheck: false, onChange: (event) => update("trainLiveCacheDir", event.target.value) })),
						h(Field, { id: id("flyai"), label: "飞猪 flyai API Key", hint: "解锁完整机票/酒店查询；不配置时 CLI 以体验模式运行。" },
							h(SecretInput, { id: id("flyai"), value: form.flyaiApiKey, placeholder: "可选", onChange: (value) => update("flyaiApiKey", value) })),
						h(Field, { id: id("flyaicli"), label: "flyai CLI 路径", hint: "CLI 不在 PATH 时填写绝对路径。" },
							h("input", { id: id("flyaicli"), className: "tp-input tp-input-mono", value: form.flyaiCli, placeholder: "flyai", autoComplete: "off", spellCheck: false, onChange: (event) => update("flyaiCli", event.target.value) })))),

				h("section", { className: "tp-card" },
					h("details", { className: "tp-advanced" },
						h("summary", null, icons.chevron, "高级设置"),
						h("div", { className: "tp-advanced-body" },
							h("div", { className: "tp-grid" },
								h(Field, { id: id("windybase"), label: "Windy 接口地址", hint: "默认 node.windy.com；境内直连慢时可填代理前缀。", full: true },
									h("input", { id: id("windybase"), className: "tp-input tp-input-mono", value: form.windyBase, placeholder: "https://node.windy.com", autoComplete: "off", spellCheck: false, onChange: (event) => update("windyBase", event.target.value) })),
								h(Field, { id: id("windycache"), label: "Windy 缓存文件", hint: "留空用 windycache.json；设为空字符串可禁用缓存。", full: true },
									h("input", { id: id("windycache"), className: "tp-input tp-input-mono", value: form.windyCache, placeholder: "windycache.json", autoComplete: "off", spellCheck: false, onChange: (event) => update("windyCache", event.target.value) })))))),

				h("div", { className: "tp-actions" },
					h("div", { className: "tp-actions-note" }, "密钥只保存在本机设置文件中，不会写入仓库或日志。"),
					h("div", { className: "tp-actions-right" },
						h("button", { type: "button", className: "tp-btn tp-btn-secondary", onClick: reset },
							resetArmed ? "确认恢复默认" : "恢复默认"),
						h("button", { type: "submit", className: "tp-btn tp-btn-primary", disabled: saving || loadError },
							saving ? "保存中…" : "保存设置"))),
				h("div", { className: "tp-footer" },
					h("span", { className: "tp-path", title: path }, path ? `配置文件：${path}` : ""),
					h("span", null, "v0.3.0"))));
		}

		const name = "dsh-trip-planner-cn";
		const inject = ["slots"];
		let applied = false;

		function injectStyle() {
			if (document.getElementById(STYLE_ID) !== null) return;
			const style = document.createElement("style");
			style.id = STYLE_ID;
			style.textContent = STYLE;
			document.head.appendChild(style);
		}

		/**
		 * Register the settings card into the DSH settings shell.
		 *
		 * @param {{slots: object, effect?: Function}} ctx - DSH client context.
		 */
		function apply(ctx) {
			if (typeof document === "undefined") return;
			if (applied) return;
			applied = true;
			injectStyle();
			try {
				ctx.slots.inject("settings.section", () => {
					const unregister = ctx.slots.register({
						name: "settings.section",
						id: "dsh-trip-planner-cn",
						order: 110,
						label: "Trip Planner"
					}, SettingsCard);
					return () => {
						if (typeof unregister === "function") unregister();
					};
				});
			} catch (error) {
				console.error("[dsh-trip-planner-cn] slot registration failed:", error);
			}
			if (typeof ctx.effect === "function") {
				ctx.effect(() => () => {
					applied = false;
					document.getElementById(STYLE_ID)?.remove();
				}, "dsh-trip-planner-cn: settings card");
			}
		}

		const module = { exports: {} };
		module.exports.apply = apply;
		module.exports.inject = inject;
		module.exports.name = name;
		return module.exports;
	}
});
