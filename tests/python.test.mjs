/** Run the stdlib-only Python suites without installing anything or using network. */
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
const python = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
const result = spawnSync(python, [
	"-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v",
], { cwd: root, stdio: "inherit", shell: false });

if (result.error) {
	console.error(`Unable to run Python tests with ${python}; set PYTHON to Python 3.9+.`);
	console.error(result.error.message);
	process.exit(1);
}
process.exit(result.status ?? 1);
