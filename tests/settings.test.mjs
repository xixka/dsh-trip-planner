/**
 * Unit tests for the settings storage and HTTP handler.
 *
 *   node --test tests/settings.test.mjs
 *
 * Uses temporary directories only; nothing touches the user's real
 * ~/.dsh/trip-planner/settings.json.
 */

import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import {
	DEFAULT_SETTINGS,
	SettingsError,
	createSettingsHandler,
	isTrustedHostRequest,
	normalizeSettings,
	readSettings,
	writeSettings
} from "../lib/settings.js";

async function temporarySettingsPath() {
	const dir = await mkdtemp(path.join(tmpdir(), "dsh-trip-settings-"));
	return path.join(dir, "settings.json");
}

/** Minimal fake IncomingMessage. */
function request({ method = "GET", url = "/trip-planner/settings", body, headers = {}, remoteAddress = "127.0.0.1" } = {}) {
	const chunks = body === undefined
		? []
		: [Buffer.from(typeof body === "string" ? body : JSON.stringify(body), "utf8")];
	return {
		method,
		url,
		headers,
		socket: { remoteAddress },
		destroy() {},
		async *[Symbol.asyncIterator]() {
			for (const chunk of chunks) yield chunk;
		}
	};
}

/** Minimal fake ServerResponse that collects the JSON body. */
function response() {
	return {
		statusCode: 200,
		headers: {},
		body: "",
		writeHead(status, headers) {
			this.statusCode = status;
			this.headers = headers;
			return this;
		},
		end(chunk) {
			if (chunk) this.body += chunk;
			return this;
		}
	};
}

test("readSettings returns defaults for a missing file", async () => {
	const file = await temporarySettingsPath();
	assert.deepEqual(await readSettings(file), { ...DEFAULT_SETTINGS });
});

test("writeSettings merges a partial patch and persists mode 0600", async () => {
	const file = await temporarySettingsPath();
	const saved = await writeSettings({ amapKey: "  test-key  ", mapProvider: "baidu" }, file);
	assert.equal(saved.amapKey, "test-key");
	assert.equal(saved.mapProvider, "baidu");
	assert.equal(saved.windyBase, DEFAULT_SETTINGS.windyBase);
	const onDisk = JSON.parse(await readFile(file, "utf8"));
	assert.equal(onDisk.amapKey, "test-key");
	if (process.platform !== "win32") {
		const mode = (await stat(file)).mode & 0o777;
		assert.equal(mode, 0o600);
	}
});

test("normalizeSettings validates types, control characters and URLs", () => {
	assert.equal(normalizeSettings({ mapProvider: "not-a-provider" }).mapProvider, "amap");
	assert.throws(() => normalizeSettings([]), SettingsError);
	assert.throws(() => normalizeSettings({ amapKey: "bad\nvalue" }), SettingsError);
	assert.throws(() => normalizeSettings({ windyBase: "ftp://example.com" }), SettingsError);
	assert.throws(() => normalizeSettings({ amapKey: "x".repeat(4097) }), SettingsError);
});

test("handler GET returns settings and the resolved path", async () => {
	const file = await temporarySettingsPath();
	await writeSettings({ amapKey: "abc" }, file);
	const handler = createSettingsHandler({ settingsPath: () => file });
	const res = response();
	await handler(request(), res);
	assert.equal(res.statusCode, 200);
	const envelope = JSON.parse(res.body);
	assert.equal(envelope.ok, true);
	assert.equal(envelope.value.settings.amapKey, "abc");
	assert.equal(envelope.value.path, file);
});

test("handler POST saves a partial patch", async () => {
	const file = await temporarySettingsPath();
	const handler = createSettingsHandler({ settingsPath: () => file });
	const res = response();
	await handler(request({
		method: "POST",
		headers: { "content-type": "application/json" },
		body: { baiduMapAk: "baidu" }
	}), res);
	assert.equal(res.statusCode, 200);
	const envelope = JSON.parse(res.body);
	assert.equal(envelope.ok, true);
	assert.equal(envelope.value.settings.baiduMapAk, "baidu");
	assert.equal((await readSettings(file)).baiduMapAk, "baidu");
});

test("handler POST rejects a non-JSON body and a non-object body", async () => {
	const file = await temporarySettingsPath();
	const handler = createSettingsHandler({ settingsPath: () => file });
	const res = response();
	await handler(request({ method: "POST", headers: {}, body: { amapKey: "x" } }), res);
	assert.equal(res.statusCode, 415);
	const res2 = response();
	await handler(request({ method: "POST", headers: { "content-type": "application/json" }, body: "[]" }), res2);
	assert.equal(res2.statusCode, 400);
});

test("handler refuses non-loopback and untrusted Host", async () => {
	const file = await temporarySettingsPath();
	const handler = createSettingsHandler({ settingsPath: () => file });
	const res = response();
	await handler(request({ remoteAddress: "203.0.113.9" }), res);
	assert.equal(res.statusCode, 403);
	const res2 = response();
	await handler(request({ headers: { host: "evil.example" } }), res2);
	assert.equal(res2.statusCode, 403);
	assert.equal(isTrustedHostRequest({ headers: { host: "127.0.0.1:3080" } }), true);
	assert.equal(isTrustedHostRequest({ headers: { host: "evil.example" } }), false);
});
