/**
 * Settings storage for the trip-planner DSH plugin.
 *
 * One JSON file, shared by the settings panel (this module), the Node plugin
 * wrapper and every Python script (`scripts/trip_config.py`). Values are never
 * read from environment variables; `DSH_HOME` only decides *where* the file
 * lives. Secrets are written with mode 0600 and are never logged.
 *
 * @module dsh-trip-planner-cn/settings
 */

import { chmod, copyFile, mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import path from "node:path";

/** Settings schema version written into every file. */
export const SETTINGS_VERSION = 1;

/** Maximum length of one setting value (defensive cap for a local panel). */
export const MAX_VALUE_LENGTH = 4096;

/** Maximum accepted JSON body for one settings POST. */
export const MAX_BODY_BYTES = 64 * 1024;

/** Default settings; secrets start empty and are filled by the user. */
export const DEFAULT_SETTINGS = Object.freeze({
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
});

/** Every accepted setting key, in panel order. */
export const SETTING_KEYS = Object.freeze(Object.keys(DEFAULT_SETTINGS));

/** Keys whose values are secrets and must never be logged. */
export const SECRET_KEYS = Object.freeze([
	"amapKey",
	"baiduMapAk",
	"dashscopeApiKey",
	"siliconflowApiKey",
	"flyaiApiKey"
]);

/** Error with a stable machine-readable code for the HTTP envelope. */
export class SettingsError extends Error {
	/**
	 * @param {string} message - Human-readable message (never contains a value).
	 * @param {string} [code] - Stable error code.
	 */
	constructor(message, code = "bad-request") {
		super(message);
		this.name = "SettingsError";
		this.code = code;
	}
}

/**
 * Directory that owns the settings file: `$DSH_HOME/trip-planner` or
 * `~/.dsh/trip-planner`.
 *
 * @returns {string} Absolute directory path.
 */
export function defaultSettingsDir() {
	const base = (process.env.DSH_HOME ?? "").trim();
	const root = base !== "" ? path.resolve(base) : path.join(homedir(), ".dsh");
	return path.join(root, "trip-planner");
}

/**
 * Absolute settings file path.
 *
 * @returns {string} `~/.dsh/trip-planner/settings.json` by default.
 */
export function defaultSettingsPath() {
	return path.join(defaultSettingsDir(), "settings.json");
}

/**
 * Validate and normalize a settings object.
 *
 * Unknown keys are dropped, `undefined` keeps the current value (the caller
 * merges first), `null`/empty clears a value, non-strings are stringified and
 * every value is trimmed. `mapProvider` falls back to `amap`; `windyBase`
 * must be an http(s) URL when present.
 *
 * @param {Record<string, unknown>} input - Raw settings object.
 * @returns {Record<string, string>} Complete normalized settings.
 * @throws {SettingsError} When a value is too long, contains control
 * characters, or `windyBase` is not a URL.
 */
export function normalizeSettings(input = {}) {
	if (input === null || typeof input !== "object" || Array.isArray(input)) {
		throw new SettingsError("设置必须是 JSON 对象", "invalid-settings");
	}
	/** @type {Record<string, string>} */
	const out = { ...DEFAULT_SETTINGS };
	for (const key of SETTING_KEYS) {
		if (!(key in input)) continue;
		let value = input[key];
		if (value === undefined) continue;
		if (value === null) value = "";
		if (typeof value !== "string") value = String(value);
		value = value.trim();
		if (value.length > MAX_VALUE_LENGTH) {
			throw new SettingsError(`设置项 ${key} 超过 ${MAX_VALUE_LENGTH} 字符`, "too-long");
		}
		// Control characters have no place in a key, URL, path or command.
		// eslint-disable-next-line no-control-regex
		if (/[\u0000-\u001f\u007f]/u.test(value)) {
			throw new SettingsError(`设置项 ${key} 含有控制字符`, "invalid-value");
		}
		out[key] = value;
	}
	if (out.mapProvider !== "amap" && out.mapProvider !== "baidu") {
		out.mapProvider = "amap";
	}
	if (out.windyBase !== "") {
		try {
			const url = new URL(out.windyBase);
			if (url.protocol !== "http:" && url.protocol !== "https:") {
				throw new Error("protocol");
			}
		} catch {
			throw new SettingsError("windyBase 必须是 http(s) URL", "invalid-value");
		}
	}
	return out;
}

/**
 * Read settings, returning defaults when the file is missing.
 *
 * @param {string} [file] - Settings file path.
 * @returns {Promise<Record<string, string>>} Normalized settings.
 * @throws {SettingsError} When the file exists but is not valid JSON.
 */
export async function readSettings(file = defaultSettingsPath()) {
	let raw;
	try {
		raw = await readFile(file, "utf8");
	} catch (error) {
		if (/** @type {NodeJS.ErrnoException} */ (error)?.code === "ENOENT") {
			return { ...DEFAULT_SETTINGS };
		}
		throw error;
	}
	let parsed;
	try {
		parsed = JSON.parse(raw);
	} catch {
		throw new SettingsError("settings.json 不是合法 JSON", "invalid-json");
	}
	return normalizeSettings(parsed);
}

/**
 * Merge a partial patch into the current settings and write it atomically.
 *
 * @param {Record<string, unknown>} patch - Partial settings.
 * @param {string} [file] - Settings file path.
 * @returns {Promise<Record<string, string>>} The normalized settings written.
 */
export async function writeSettings(patch = {}, file = defaultSettingsPath()) {
	const current = await readSettings(file);
	const next = normalizeSettings({ ...current, ...patch });
	const dir = path.dirname(file);
	await mkdir(dir, { recursive: true, mode: 0o700 });
	const temp = path.join(dir, `.${path.basename(file)}.${process.pid}.${Date.now()}.tmp`);
	await writeFile(temp, `${JSON.stringify(next, null, 2)}\n`, {
		encoding: "utf8",
		mode: 0o600
	});
	await chmod(temp, 0o600);
	try {
		await rename(temp, file);
	} catch (error) {
		// Windows can refuse to rename over an existing file; fall back to a
		// copy so the panel still works, then remove the temporary file.
		const code = /** @type {NodeJS.ErrnoException} */ (error)?.code;
		if (code === "EEXIST" || code === "EPERM" || code === "ENOTEMPTY") {
			await copyFile(temp, file);
			await rm(temp, { force: true });
		} else {
			await rm(temp, { force: true });
			throw error;
		}
	}
	return next;
}

/** Whether the request comes from this machine. */
function isLoopbackRequest(req) {
	const address = req.socket?.remoteAddress ?? "";
	return address === "127.0.0.1" || address === "::1" || address === "::ffff:127.0.0.1";
}

/** Loopback hostnames a browser may legitimately use to reach this server. */
const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "::1", "[::1]"]);

/**
 * Hostname of an authority (`host:port`, `[::1]:port`, `host`).
 *
 * @param {string} authority - Host header value.
 * @returns {string} Hostname without port.
 */
function hostnameOf(authority) {
	const trimmed = authority.trim().toLowerCase();
	if (trimmed.startsWith("[")) {
		const end = trimmed.indexOf("]");
		return end === -1 ? trimmed : trimmed.slice(0, end + 1);
	}
	const colon = trimmed.lastIndexOf(":");
	return colon === -1 ? trimmed : trimmed.slice(0, colon);
}

/**
 * Host/Origin fence against DNS rebinding. The DSH panel is local; a page on
 * an attacker hostname must not be able to read or write settings.
 *
 * @param {import("node:http").IncomingMessage} req - Request.
 * @returns {boolean} Whether both Host and Origin are loopback.
 */
export function isTrustedHostRequest(req) {
	const host = req.headers.host;
	if (typeof host === "string" && host.trim() !== "" && !LOOPBACK_HOSTS.has(hostnameOf(host))) {
		return false;
	}
	const origin = req.headers.origin;
	if (typeof origin !== "string" || origin === "") return true;
	try {
		return LOOPBACK_HOSTS.has(hostnameOf(new URL(origin).host));
	} catch {
		return false;
	}
}

/** Whether the request declares a JSON body. */
function isJsonContentType(req) {
	const raw = req.headers["content-type"];
	const value = (Array.isArray(raw) ? raw[0] : raw) ?? "";
	return value.split(";")[0]?.trim().toLowerCase() === "application/json";
}

/**
 * Read a small JSON body (64 KiB cap).
 *
 * @param {import("node:http").IncomingMessage} req - Request.
 * @returns {Promise<{value?: unknown, tooLarge: boolean}>} Parsed body.
 */
async function readJsonBody(req) {
	const chunks = [];
	let total = 0;
	for await (const chunk of req) {
		const buffer = /** @type {Buffer} */ (chunk);
		chunks.push(buffer);
		total += buffer.length;
		if (total > MAX_BODY_BYTES) {
			req.destroy?.();
			return { tooLarge: true };
		}
	}
	const text = Buffer.concat(chunks).toString("utf8");
	if (text === "") return { value: {}, tooLarge: false };
	try {
		return { value: JSON.parse(text), tooLarge: false };
	} catch {
		return { value: {}, tooLarge: false };
	}
}

/**
 * Write a JSON envelope.
 *
 * @param {import("node:http").ServerResponse} res - Response.
 * @param {{ok: boolean, value?: unknown, error?: {code: string, message: string}}} envelope - Body.
 * @param {number} [status] - HTTP status.
 */
function json(res, envelope, status = 200) {
	res.writeHead(status, { "content-type": "application/json; charset=utf-8" });
	res.end(JSON.stringify(envelope));
}

const ok = (value) => ({ ok: true, value });
const fail = (code, message) => ({ ok: false, error: { code, message } });

/**
 * Create the `/trip-planner` HTTP handler for the settings panel.
 *
 * Routes:
 *   GET  /trip-planner/settings  -> { settings, path, version }
 *   POST /trip-planner/settings  -> merged settings (JSON body, partial allowed)
 *
 * @param {{read?: typeof readSettings, write?: typeof writeSettings, settingsPath?: () => string}} [options]
 *   Test seams.
 * @returns {(req: import("node:http").IncomingMessage, res: import("node:http").ServerResponse) => Promise<void>}
 */
export function createSettingsHandler(options = {}) {
	const read = options.read ?? readSettings;
	const write = options.write ?? writeSettings;
	const resolvePath = options.settingsPath ?? defaultSettingsPath;
	return async (req, res) => {
		if (!isLoopbackRequest(req)) {
			json(res, fail("forbidden", "IP not allowed"), 403);
			return;
		}
		if (!isTrustedHostRequest(req)) {
			json(res, fail("forbidden", "untrusted Host/Origin"), 403);
			return;
		}
		const url = new URL(req.url ?? "/", "http://x");
		const pathname = url.pathname;
		if (req.method === "GET" && pathname === "/trip-planner/settings") {
			try {
				const file = resolvePath();
				json(res, ok({
					version: SETTINGS_VERSION,
					path: file,
					settings: await read(file)
				}));
			} catch (error) {
				json(res, fail(error.code ?? "read-failed", error.message), 500);
			}
			return;
		}
		if ((req.method === "POST" || req.method === "PUT") && pathname === "/trip-planner/settings") {
			if (!isJsonContentType(req)) {
				json(res, fail("unsupported-media-type", "POST 需要 application/json"), 415);
				return;
			}
			const { value, tooLarge } = await readJsonBody(req);
			if (tooLarge) {
				json(res, fail("payload-too-large", `请求体超过 ${MAX_BODY_BYTES} 字节上限`), 413);
				return;
			}
			if (value === null || typeof value !== "object" || Array.isArray(value)) {
				json(res, fail("bad-request", "设置必须是 JSON 对象"), 400);
				return;
			}
			try {
				const file = resolvePath();
				const settings = await write(value, file);
				json(res, ok({
					version: SETTINGS_VERSION,
					path: file,
					savedAt: new Date().toISOString(),
					settings
				}));
			} catch (error) {
				json(res, fail(error.code ?? "write-failed", error.message), 400);
			}
			return;
		}
		json(res, fail("not-found", "not found"), 404);
	};
}
