const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { spawn, execFile } = require('node:child_process');
const { promisify } = require('node:util');
const { mkdtemp, rm } = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const exec = promisify(execFile);

test('served approval flow: roles, CSRF, expiry, replay and concurrent replies', async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'aurel-redis-'));
  const socket = path.join(dir, 'redis.sock');
  const redis = spawn('redis-server', ['--port', '0', '--unixsocket', socket, '--unixsocketperm', '700', '--save', '', '--appendonly', 'no'], { stdio: 'ignore' });
  const command = async (...args) => (await exec('redis-cli', ['-s', socket, '--json', ...args.map(String)])).stdout.trim();
  let server;
  try {
    for (let n = 0; n < 50; n++) {
      try { await command('PING'); break; } catch { await new Promise(r => setTimeout(r, 50)); }
    }
    const fixturePassword = 'fixture-approver-only-'.repeat(3);
    const publisherToken = 'fixture-publisher-only-'.repeat(3);
    process.env.AUREL2_APPROVAL_USER = 'approver';
    process.env.AUREL2_APPROVAL_PASSWORD = fixturePassword;
    process.env.AUREL2_APPROVAL_API_TOKEN = publisherToken;
    process.env.UPSTASH_REDIS_REST_URL = 'https://fixture.invalid';
    process.env.UPSTASH_REDIS_REST_TOKEN = 'fixture-not-used';
    const store = {
      get: async key => { const value = JSON.parse(await command('GET', key)); return value ? JSON.parse(value) : null; },
      set: async (key, value, opts) => JSON.parse(await command('SET', key, JSON.stringify(value), ...(opts.nx ? ['NX'] : []), 'EX', opts.ex)),
      eval: async (script, keys, args) => JSON.parse(await command('EVAL', script, keys.length, ...keys, ...args)),
    };
    const { createHandler } = require('../.test-build/api/decision/[id].js');
    const health = require('../.test-build/api/test.js').default;
    const handler = createHandler(store);
    server = http.createServer(async (req, res) => {
      let data = ''; for await (const chunk of req) data += chunk;
      req.body = data ? JSON.parse(data) : {};
      req.query = { id: req.url.split('/').pop() };
      res.status = code => { res.statusCode = code; return res; };
      res.json = object => { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(object)); return res; };
      res.send = content => { res.end(content); return res; };
      await (req.url === '/api/test' ? health : handler)(req, res);
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const base = `http://127.0.0.1:${server.address().port}`;
    process.env.AUREL2_APPROVAL_ORIGIN = base;
    const basic = 'Basic ' + Buffer.from(`approver:${fixturePassword}`).toString('base64');
    const bearer = `Bearer ${publisherToken}`;
    const id = 'a'.repeat(48), endpoint = `${base}/api/decision/${id}`;
    const request = (method, auth, body, headers = {}, url = endpoint) => fetch(url, { method, headers: { ...(auth ? { Authorization: auth } : {}), 'Content-Type': 'application/json', ...headers }, ...(body ? { body: JSON.stringify(body) } : {}) });
    for (const method of ['GET', 'POST', 'PATCH', 'DELETE']) assert.equal((await request(method)).status, 401);
    assert.equal((await request('GET', undefined, undefined, {}, `${base}/api/test`)).status, 401);
    assert.equal((await request('GET', bearer, undefined, {}, `${base}/api/test`)).status, 200);
    const decision = { action: 'hold', symbol: 'FIXTURE', reasoning: '<script>fixture</script>', confidence: .5, strategies: [{ name: '<img src=x onerror=alert(1)>', action: 'HOLD' }] };
    assert.equal((await request('POST', basic, decision)).status, 403);
    assert.equal((await request('POST', bearer, decision)).status, 201);
    assert.equal((await request('POST', bearer, decision)).status, 409);
    assert.equal((await request('PATCH', bearer, { status: 'approved' })).status, 403);
    const page = await request('GET', basic);
    const html = await page.text();
    assert.equal(page.status, 200);
    assert(!html.includes('<script>fixture</script>'));
    assert(!html.includes('<img src=x onerror=alert(1)>'));
    const csrf = html.match(/'X-CSRF-Token': '([a-f0-9]+)'/)[1];
    assert.equal((await request('PATCH', basic, { status: 'approved' })).status, 403);
    assert.equal((await request('PATCH', basic, { status: 'approved' }, { 'X-CSRF-Token': csrf, Origin: 'https://hostile.invalid' })).status, 403);
    const replies = await Promise.all(['approved', 'rejected'].map(status => request('PATCH', basic, { status }, { 'X-CSRF-Token': csrf, Origin: base })));
    assert.deepEqual(replies.map(x => x.status).sort(), [200, 409]);
    assert.equal((await request('PATCH', basic, { status: 'approved' }, { 'X-CSRF-Token': csrf })).status, 409);
    const status = await (await request('GET', bearer)).json();
    assert(['approved', 'rejected'].includes(status.status));
    const expired = { ...status, status: 'pending', expires_at: new Date(Date.now() - 1).toISOString() };
    await store.set(`decision:${id}`, expired, { ex: 600 });
    assert.equal((await request('GET', basic)).status, 410);
    assert.equal((await request('PATCH', basic, { status: 'approved' }, { 'X-CSRF-Token': csrf })).status, 410);
    assert.equal((await store.get(`decision:${id}`)).status, 'pending');
    delete expired.expires_at;
    await store.set(`decision:${id}`, expired, { ex: 600 });
    assert.equal((await request('GET', basic)).status, 410);
    assert.equal((await request('POST', bearer, decision, {}, `${base}/api/decision/short-id`)).status, 400);
    assert.equal((await request('POST', bearer, { ...decision, created_at: '2020-01-01T00:00:00Z' }, {}, `${base}/api/decision/${'b'.repeat(48)}`)).status, 400);
    delete process.env.AUREL2_APPROVAL_PASSWORD;
    assert.equal((await request('GET', basic)).status, 401);
  } finally {
    if (server) await new Promise(resolve => server.close(resolve));
    redis.kill('SIGTERM');
    await new Promise(resolve => redis.once('exit', resolve));
    await rm(dir, { recursive: true, force: true });
  }
});
