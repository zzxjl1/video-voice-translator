import { createServer } from 'node:http';
import { writeFile } from 'node:fs/promises';
createServer(async (req, res) => {
  res.writeHead(200, { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'PUT, OPTIONS', 'Access-Control-Allow-Headers': '*' });
  if (req.method === 'OPTIONS') { res.end(); return; }
  if (req.method === 'PUT') {
    const name = new URL(req.url, 'http://x').pathname.slice(1);
    const c = []; for await (const d of req) c.push(d);
    await writeFile('/tmp/sep-out/' + name, Buffer.concat(c));
    console.log('saved', name);
  }
  res.end('ok');
}).listen(9312, '127.0.0.1', () => console.log('put server up'));
