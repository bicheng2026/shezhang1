/**
 * 蛇杖一号 · Web 函数整链路自检（真起 HTTP server，照腾讯云那套跑）
 * 跑法：node bridge/tencent/tools/_selftest_webfunc.mjs
 *
 * 验的是：scf_bootstrap 的启动方式能不能真起服务、server.js 转不转对、
 * 以及 9000 端口监听约定。发现问题在这一步暴露，别等部署完才发现。
 */
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { spawn } from 'node:child_process'
import { createPublicKey, createPrivateKey } from 'node:crypto'

const require = createRequire(import.meta.url)
const HERE = dirname(fileURLToPath(import.meta.url))
const SCF = join(HERE, '..', 'scf_')

const SECRET = 'abcdefgh12345678'
process.env.BOT_SECRET = SECRET
process.env.ADMIN_TOKEN = 'tok123'
process.env.READ_TOKEN = 'read456'
process.env.WATCH_GROUPS = ''

let s = SECRET
while (Buffer.byteLength(s, 'utf8') < 32) s += SECRET
const der = Buffer.concat(
  [Buffer.from('302e020100300506032b657004220420', 'hex'), Buffer.from(s.slice(0, 32), 'utf8')])
const kp = createPrivateKey({ key: der, format: 'der', type: 'pkcs8' })
const spki = createPublicKey({ key: kp }).export({ type: 'spki', format: 'der' })
process.env.PUBKEY = spki.subarray(spki.length - 32).toString('hex')

let pass = 0, fail = 0
const check = (n, c, e = '') => {
  if (c) { console.log('  [ok] ' + n); pass++ }
  else { console.log('  [FAIL] ' + n + (e ? ' → ' + String(e).slice(0, 200) : '')); fail++ }
}

/* 1. 模拟腾讯云启动：exec node server.js */
console.log('\n1) 按 scf_bootstrap 的方式启动 server.js（监听 9000）')
const PORT = 9000
const child = spawn(process.execPath, [join(SCF, 'server.js')], {
  env: { ...process.env, SCF_PORT: String(PORT) },
  stdio: ['ignore', 'pipe', 'pipe'],
})
let out = ''
child.stdout.on('data', d => { out += d.toString() })
child.stderr.on('data', d => { out += d.toString() })

const base = 'http://127.0.0.1:' + PORT
let ready = false
for (let i = 0; i < 40; i++) {
  try { await fetch(base + '/health'); ready = true; break } catch (e) { await new Promise(r => setTimeout(r, 150)) }
}
check('server.js 能起来并监听 ' + PORT, ready, out.slice(0, 200))
check('启动日志里有 listening', /listening on 0\.0\.0\.0:9000/.test(out), out.slice(0, 200))

if (ready) {
  const call = async (p, opt) => {
    const r = await fetch(base + p, opt)
    const t = await r.text()
    let j = null
    try { j = JSON.parse(t) } catch (e) { }
    return { status: r.status, text: t, json: j, headers: r.headers }
  }

  console.log('\n2) 走 HTTP 的 QQ 回调验证')
  {
    const r = await call('/', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ op: 13, d: { plain_token: 'PT_1', event_ts: '1730000000' } }) })
    check('HTTP 回调能签', r.json && r.json.plain_token === 'PT_1', r.text.slice(0, 120))
    const sig = Buffer.from(String(r.json && r.json.signature), 'hex')
    const key = await crypto.subtle.importKey('raw', Uint8Array.from(Buffer.from(process.env.PUBKEY, 'hex')), { name: 'Ed25519' }, false, ['verify'])
    const okv = await crypto.subtle.verify({ name: 'Ed25519' }, key, sig, new TextEncoder().encode('1730000000PT_1'))
    check('签名公钥反验通过', okv && sig.length === 64, 'len=' + sig.length)
  }

  console.log('\n3) 群消息 + 脱敏')
  {
    const r = await call('/', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ op: 0, d: { msg_id: 'H1', group_id: 'G1', event_ts: Date.now(), content: '明天开会 手机13900139000' } }) })
    check('群消息入库', r.json && r.json.code === 0, r.text.slice(0, 100))
    const q = await call('/qq?t=read456')
    check('群消息能读且打码', q.json && q.text.includes('[手机]') && !q.text.includes('13900139000'), q.text.slice(0, 150))
  }

  console.log('\n4) health / 页面 / 鉴权')
  {
    const h = await call('/health')
    check('health 全 true', h.json && h.json.ok && h.json.has_secret && h.json.has_pubkey, h.text.slice(0, 120))
    const p = await call('/page?kind=ann')
    check('公告页是 HTML', String(p.headers.get('content-type') || '').includes('text/html'), p.headers.get('content-type'))
    const nq = await call('/qq')
    check('群消息无口令 401', nq.status === 401, String(nq.status))
    const d = await call('/dbg?t=read456')
    check('排错口能开', d.status === 200 && d.json && d.json.secret_len === SECRET.length, d.text.slice(0, 120))
  }

  console.log('\n5) 中文 Content-Type 不乱码（Web 层容易踩）')
  {
    const r = await call('/page?kind=qq&t=read456')
    check('页面声明 charset=utf-8', String(r.headers.get('content-type') || '').toLowerCase().includes('charset=utf-8'), r.headers.get('content-type'))
    check('中文正文正常（没变成乱码）', r.text.includes('明天开会') || r.text.includes('还没有内容'), r.text.slice(0, 120))
  }

  console.log('\n6) 未知路径 / OPTIONS')
  {
    const nf = await call('/nothing')
    check('未知路径给导航页', nf.status === 200 && nf.text.includes('蛇杖一号'), String(nf.status))
    const opt = await call('/ann', { method: 'OPTIONS' })
    check('OPTIONS 204', opt.status === 204, String(opt.status))
  }
}

child.kill()
console.log('\n==== ' + pass + ' 通过 / ' + fail + ' 失败 ====')
process.exit(fail ? 1 : 0)
