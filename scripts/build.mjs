#!/usr/bin/env node
/**
 * Build the installable package and verify the entries DSH needs at runtime.
 *
 * This is intentionally dependency-free: `npm pack` produces the tarball, and
 * `tar -tzf` lists it. It is the "compile" step CI runs before the tests, so a
 * broken package layout fails loudly before anything else.
 *
 *   node scripts/build.mjs
 */

import { execFileSync } from "node:child_process";
import { mkdirSync, readdirSync, readFileSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
const dist = path.join(root, "dist");

/** Files whose syntax is checked before packing. */
const CHECK = [
	"index.js",
	"lib/settings.js",
	"client/index.js"
];

/** Entries that must exist inside the tarball. */
const REQUIRED = [
	"package/package.json",
	"package/index.js",
	"package/lib/settings.js",
	"package/client/index.js",
	"package/cordis.patch.yml",
	"package/icon.svg",
	"package/README.md",
	"package/SKILL.md",
	"package/locale/zh.json",
	"package/locale/en.json",
	"package/references/data-sources.md",
	"package/references/train-live.md",
	"package/scripts/route_tools.py",
	"package/scripts/trip_config.py",
	"package/scripts/train_live.py",
	"package/scripts/flyai_provider.py",
	"package/themes/render_clay2.py",
	"package/assets/plan.example.json",
	"package/settings.example.json"
];

/** Remove generated Python bytecode so it never enters the tarball. */
function cleanBytecode(dir) {
	for (const entry of readdirSync(dir, { withFileTypes: true })) {
		const full = path.join(dir, entry.name);
		if (!entry.isDirectory()) continue;
		if (entry.name === "__pycache__") {
			rmSync(full, { recursive: true, force: true });
			continue;
		}
		cleanBytecode(full);
	}
}

for (const file of CHECK) {
	execFileSync(process.execPath, ["--check", path.join(root, file)], { stdio: "inherit" });
	console.log(`syntax ok  ${file}`);
}

for (const dir of ["scripts", "themes", "tests"]) {
	cleanBytecode(path.join(root, dir));
}

mkdirSync(dist, { recursive: true });
for (const entry of readdirSync(dist)) {
	rmSync(path.join(dist, entry), { recursive: true, force: true });
}

const packOutput = execFileSync(
	"npm",
	["pack", "--pack-destination", "dist", "--json"],
	{ cwd: root, encoding: "utf8", stdio: ["ignore", "pipe", "inherit"] }
);
const [pack] = JSON.parse(packOutput);
if (!pack || typeof pack.filename !== "string") {
	throw new Error("npm pack did not return a filename");
}
const tgz = path.join(dist, pack.filename);
if (!readFileSync(tgz).subarray(0, 2).equals(Buffer.from([0x1f, 0x8b]))) {
	throw new Error(`not a gzip tarball: ${tgz}`);
}

const listed = execFileSync("tar", ["-tzf", tgz], { encoding: "utf8" })
	.split(/\r?\n/u)
	.map((entry) => entry.trim())
	.filter((entry) => entry !== "");
const missing = REQUIRED.filter((entry) => !listed.includes(entry));
if (missing.length > 0) {
	console.error("MISSING in tarball:");
	for (const entry of missing) console.error(`  - ${entry}`);
	process.exit(1);
}

console.log(`Build OK — ${pack.filename} (${listed.length} entries)`);
