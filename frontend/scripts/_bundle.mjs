/**
 * 把 features/ 下的 TS 模块打包成 Node 能直接 import 的 ESM（它们之间用不带扩展名的相对路径互相引用，
 * Node 自己解析不了）。只给测试用。
 *
 * 产物放在 node_modules/.cache 下而不是系统临时目录：`external` 里的包（kokoro-js、transformers.js……）
 * 要靠 Node 的模块解析从 frontend/node_modules 找，产物必须在它的子树里。
 */
import { build } from 'esbuild';
import path from 'node:path';
import fs from 'node:fs';
import { pathToFileURL, fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const OUT = path.join(ROOT, 'node_modules', '.cache', 'vvt-test-bundle');
fs.mkdirSync(OUT, { recursive: true });

export async function loadTs(entry, { external = [] } = {}) {
  const outfile = path.join(OUT, `${path.basename(entry, '.ts')}.mjs`);
  await build({
    entryPoints: [path.join(ROOT, entry)], bundle: true, platform: 'node', format: 'esm',
    outfile, logLevel: 'error', external,
  });
  return import(`${pathToFileURL(outfile).href}?t=${Date.now()}`);
}
