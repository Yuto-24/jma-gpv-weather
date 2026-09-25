// Runtime assets may be fetched during setup; weather fixtures and both tests
// are served locally. The browser is prohibited from making external requests.
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { createHash } from "node:crypto";
import { readFile, readdir } from "node:fs/promises";
import { dirname, resolve, basename } from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";
import { chromium } from "playwright";
import { loadPyodide } from "pyodide";

async function main() {
const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const caseDir = resolve(process.env.JMA_RUNTIME_CASE ?? resolve(root, ".runtime-case"));
let wheelPath = process.env.JMA_RUNTIME_WHEEL;
if (!wheelPath) {
  const wheels = (await readdir(resolve(root, "dist"))).filter(n => n.endsWith(".whl") && n.startsWith("jma_gpv_weather-"));
  assert.equal(wheels.length, 1, "Build exactly one project wheel in dist or set JMA_RUNTIME_WHEEL");
  wheelPath = resolve(root, "dist", wheels[0]);
}
const wheelName = basename(wheelPath);
const require = createRequire(import.meta.url);
const runtimeDir = dirname(require.resolve("pyodide/package.json"));
const python = await readFile(resolve(here, "acceptance.py"), "utf8") + "\n" + await readFile(resolve(here, "gsm_acceptance.py"), "utf8");
const files = new Map([
  ["/case/case.json", await readFile(resolve(caseDir, "case.json"))],
  ["/case/prepared.npz", await readFile(resolve(caseDir, "prepared.npz"))],
  ["/case/gsm-case.json", await readFile(resolve(caseDir, "gsm-case.json"))],
  ["/case/gsm-prepared.npz", await readFile(resolve(caseDir, "gsm-prepared.npz"))],
  [`/${wheelName}`, await readFile(wheelPath)],
  ["/acceptance.py", Buffer.from(python)],
]);
// Stage browser dependencies directly from the pinned runtime lock, independently
// of loadPyodide and its Node disk cache. Never serve cached wheels from runtimeDir.
const lock = JSON.parse(await readFile(resolve(runtimeDir, "pyodide-lock.json"), "utf8"));
const packages = new Set(["numpy", "micropip", "tzdata"]);
for (const name of packages) {
  for (const dependency of lock.packages[name].depends) packages.add(dependency);
}
for (const name of packages) {
  const pkg = lock.packages[name];
  const url = `https://cdn.jsdelivr.net/pyodide/v${lock.info.version}/full/${pkg.file_name}`;
  const response = await fetch(url);
  assert.ok(response.ok, `Runtime setup failed: ${response.status} ${url}`);
  const data = Buffer.from(await response.arrayBuffer());
  assert.equal(createHash("sha256").update(data).digest("hex"), pkg.sha256, `Runtime hash: ${name}`);
  files.set(`/pyodide/${pkg.file_name}`, data);
}

const server = createServer(async (request, response) => {
  try {
    const path = new URL(request.url, "http://localhost").pathname;
    if (path === "/") {
      response.setHeader("Content-Type", "text/html");
      response.end('<!doctype html><title>MSM/GSM Pyodide acceptance</title><script src="/pyodide/pyodide.js"></script>');
    } else if (files.has(path)) {
      response.end(files.get(path));
    } else if (!path.endsWith(".whl") && path.startsWith("/pyodide/") && basename(path) === path.slice("/pyodide/".length)) {
      response.setHeader("Content-Type", path.endsWith(".wasm") ? "application/wasm" : path.endsWith(".js") ? "text/javascript" : "application/octet-stream");
      response.end(await readFile(resolve(runtimeDir, basename(path))));
    } else {
      response.writeHead(404).end();
    }
  } catch {
    response.writeHead(500).end();
  }
});
await new Promise(done => server.listen(0, "127.0.0.1", done));
const origin = `http://127.0.0.1:${server.address().port}`;
let browser;
try {
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const unexpected = [];
  await context.route("**/*", route => {
    if (new URL(route.request().url()).origin !== origin) {
      unexpected.push(route.request().url());
      return route.abort();
    }
    return route.continue();
  });
  const page = await context.newPage();
  await page.goto(origin);
  const result = await page.evaluate(async ({ origin, wheelName }) => {
    const pyodide = await window.loadPyodide({ indexURL: origin + "/pyodide/" });
    await pyodide.loadPackage(["numpy", "micropip", "tzdata"]);
    pyodide.runPython(`from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
assert datetime(2026, 1, 1, tzinfo=ZoneInfo("Asia/Tokyo")).utcoffset() == timedelta(hours=9)`);
    await pyodide.runPythonAsync(`import micropip; await micropip.install("${origin}/${wheelName}")`);
    pyodide.FS.mkdir("/case");
    for (const path of ["/case/case.json", "/case/prepared.npz", "/case/gsm-case.json", "/case/gsm-prepared.npz"]) {
      const response = await fetch(origin + path);
      pyodide.FS.writeFile(path, new Uint8Array(await response.arrayBuffer()));
    }
    pyodide.runPython(await (await fetch(origin + "/acceptance.py")).text());
    return pyodide.runPython("acceptance_result");
  }, { origin, wheelName });
  assert.deepEqual(unexpected, [], "Browser must not fetch external weather or runtime dependencies");
  console.log("Chromium Pyodide:", result);
} finally {
  await browser?.close();
  await new Promise(done => server.close(done));
}

// Node runs after Chromium; its package cache is never a browser prerequisite.
const pyodide = await loadPyodide({ indexURL: runtimeDir + "/" });
await pyodide.loadPackage(["numpy", "micropip", "tzdata"]);
for (const name of ["pygrib", "fcntl"]) {
  assert.equal(pyodide.runPython(`import importlib.util; importlib.util.find_spec("${name}") is None`), true);
}
pyodide.FS.mkdir("/case");
for (const [path, data] of files) if (path !== "/acceptance.py" && !path.startsWith("/pyodide/")) pyodide.FS.writeFile(path, data);
// Normal dependency resolution proves emscripten markers, without deps=False.
await pyodide.runPythonAsync(`import micropip; await micropip.install("emfs:/${wheelName}")`);
pyodide.runPython(python);
console.log("Node Pyodide:", pyodide.runPython("acceptance_result"));

}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
