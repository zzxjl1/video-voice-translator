/**
 * 把 features/ 下的 TS 模块打包成 Node 能直接 import 的 ESM（它们之间用不带扩展名的相对路径互相引用，
 * Node 自己解析不了）。只给测试用，产物放系统临时目录。
 */
import { build } from 'esbuild';
import os from 'node:os';
import path from 'node:path';
import fs from 'node:fs';
import { pathToFileURL, fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const OUT = fs.mkdtempSync(path.join(os.tmpdir(), 'vvt-bundle-'));

export async function loadTs(entry) {
  const outfile = path.join(OUT, `${path.basename(entry, '.ts')}.mjs`);
  await build({
    entryPoints: [path.join(ROOT, entry)], bundle: true, platform: 'node', format: 'esm',
    outfile, logLevel: 'error',
  });
  return import(pathToFileURL(outfile).href);
}
