/**
 * 蛇杖一号 · EdgeOne 版自检（不用腾讯云就能验逻辑对不对）
 * 跑法：node bridge/edgeone/tools/_selftest_edgeone.mjs
 *
 * 验的是：验签应答、脱敏、落库、索引、鉴权、页面渲染、路由分发
 * 不依赖平台，所以「签名逻辑对不对」这件事在本地就能定论。
 */
import { readFileSync } from 'node:fs'
import { createPrivateKey, createPublicKey } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const HERE = dirname(fileURLToPath(import.meta.url))
const CODE = readFileSync(join(HERE, '..', 'functions', 'api', 'index.js'), 'utf8')

const SECRET = 'abcdefgh12345678'
const ADMIN = 'tok123'
const READ = 'read456'

/* 1. 本地算 raw 公钥（跟 tools/pubkey.js 一样） */
let s = SECRET
while (Buffer.byteLength(s, 'utf8') < 32) s += SECRET
const seed = Buffer.from(s.slice(0, 32), 'utf8')
const der = Buffer.concat([Buffer.from('302e020100300506032b657004220420', 'hex'), seed])
const kp = createPrivateKey({ key: der, format: 'der', type: 'pkcs8' })
const spki = createPublicKey({ key: kp }).export({ type: 'spki', format: 'der' })
const PUBHEX = spki.subarray(spki.length - 32).toString('hex')

/* 2. 造 EdgeOne 的运行环境：
      - env 是全局的（环境变量）
      - SHEZHANG 是全局的 KV 变量（不是 env.SHEZHANG！） */
const store = new Map()
globalThis.SHEZHANG = {
  async put(k, v) { store.set(k, String(v)) },
  async get(k) { return store.has(k) ? store.get(k) : null },
  async list({ prefix, limit }) {
    const ks = [...store.keys()].filter(k => k.startsWith(prefix)).sort().slice(0, limit)
    return { keys: ks.map(name => ({ name })) }
  },
}
globalThis.env = {
  BOT_SECRET: SECRET, PUBKEY: PUBHEX,
  ADMIN_TOKEN: ADMIN, READ_TOKEN: READ, WATCH_GROUPS: '',
}

/* 3. 把代码塞进函数体，模拟 EdgeOne 的 addEventListener 运行环境 */
let HANDLER = null
globalThis.addEventListener = (type, cb) => {
  if (type === 'fetch') {
    HANDLER = async (request) => {
      const evt = { request, respondWith: (r) => { evt._r = Promise.resolve(r) } }
      cb(evt)
      return evt._r
    }
  }
}
new Function(CODE)()
if (!HANDLER) { console.error('代码里没找到 addEventListener("fetch") —— EdgeOne 入口写法不对'); process.exit(1) }

/* 4. 起个本地 server，直接按真实 URL 路径打 */
import http from 'node:http'
const srv = http.createServer(async (req, res) => {
  const chunks = []
  for await (const c of req) chunks.push(c)
  const body = Buffer.concat(chunks).toString('utf8')
  const url = `http://127.0.0.1:8932${req.url}`
  try {
    const r = await HANDLER(new Request(url, {
      method: req.method,
      headers: req.headers,
      body: ['GET', 'HEAD'].includes(req.method) ? undefined : body,
    }))
    res.writeHead(r.status, Object.fromEntries(r.headers.entries()))
    res.end(await r.text())
  } catch (e) {
    res.writeHead(500); res.end(JSON.stringify({ error: String(e), stack: e.stack }))
  }
})
await new Promise(r => srv.listen(8932, r))

const call = async (p, init) => {
  const r = await fetch('http://127.0.0.1:8932' + p, init)
  const t = await r.text()
  let j = null
  try { j = JSON.parse(t) } catch (e) { }
  return { status: r.status, text: t, json: j, headers: r.headers }
}

let pass = 0, fail = 0
const check = (name, cond, extra = '') => {
  if (cond) { console.log('  [ok] ' + name); pass++ }
  else { console.log('  [FAIL] ' + name + (extra ? ' → ' + String(extra).slice(0, 200) : '')); fail++ }
}

console.log('\n1) 回调验证应答（QQ 后台点「校验」走这条）')
{
  const body = JSON.stringify({ op: 13, d: { plain_token: 'PT_12345', event_ts: '1730000000' } })
  const r = await call('/api', { method: 'POST', body, headers: { 'content-type': 'application/json' } })
  const j = r.json || {}
  check('plain_token 原样带回', j.plain_token === 'PT_12345', r.text.slice(0, 120))
  const sig = Buffer.from(String(j.signature), 'hex')
  const key = await crypto.subtle.importKey('raw', Uint8Array.from(Buffer.from(PUBHEX, 'hex')), { name: 'Ed25519' }, false, ['verify'])
  const ok = await crypto.subtle.verify({ name: 'Ed25519' }, key, sig, new TextEncoder().encode('1730000000PT_12345'))
  check('signature 用公钥验得通（QQ 侧就认这个）', ok && sig.length === 64, 'len=' + sig.length)
}

console.log('\n2) 普通群消息：解析 + 脱敏 + 落库')
{
  const body = JSON.stringify({
    op: 0,
    d: { msg_id: 'MSG001', group_id: 'G_2511', event_ts: Date.now(), content: '周三前交材料  电话13800138000  学号20255010729' },
  })
  const r = await call('/api', { method: 'POST', body, headers: { 'content-type': 'application/json' } })
  const j = r.json || {}
  check('返回已入库', j.code === 0 && j.id === 'qq_MSG001', r.text.slice(0, 120))
  const qk = [...store.keys()].filter(k => k.startsWith('q/'))
  check('KV 出现 q/ 记录', qk.length === 1, JSON.stringify([...store.keys()]))
  const item = JSON.parse(store.get(qk[0]))
  check('手机号打码', item.title.includes('[手机]') && !item.title.includes('13800138000'), item.title)
  check('学号打码', item.title.includes('[学号]') && !item.title.includes('20255010729'), item.title)
  check('不存发言人昵称', !('nick' in item), JSON.stringify(item.sender))
}

console.log('\n3) 公告推送 /api/sync')
{
  const body = JSON.stringify({ items: [{ id: 'ann001', source: '教务处', title: '关于期末考试安排的通知', ts: Date.now(), level: 'P1' }] })
  const bad = await call('/api/sync', { method: 'POST', body })
  check('没带 token → 401', bad.status === 401, String(bad.status))
  const good = await call('/api/sync', { method: 'POST', body, headers: { authorization: 'Bearer ' + ADMIN } })
  check('带 token → 入库', (good.json || {}).code === 0 && good.json.count === 1, good.text.slice(0, 120))
}

console.log('\n4) 读取接口 + 鉴权')
{
  const ann = await call('/api/ann')
  check('公开公告能读', (ann.json || {}).count === 1, ann.text.slice(0, 100))
  check('公开区带 CORS', !!ann.headers.get('access-control-allow-origin'))

  const noTok = await call('/api/qq')
  check('群消息不带口令 → 401', noTok.status === 401, String(noTok.status))
  const withTok = await call('/api/qq?t=' + READ)
  check('群消息带口令 → 能读', (withTok.json || {}).count === 1, withTok.text.slice(0, 100))
  check('群消息区不开 CORS', withTok.headers.get('access-control-allow-origin') === 'no',
    String(withTok.headers.get('access-control-allow-origin')))

  const h = await call('/api/health')
  const hj = h.json || {}
  check('health 全 true', hj.ok && hj.has_secret && hj.has_pubkey && hj.has_kv && hj.has_admin, JSON.stringify(hj).slice(0, 150))
}

console.log('\n5) 手机页面（HTML，微信里点开就是列表）')
{
  const p = await call('/api/page?kind=qq&t=' + READ)
  check('/page 群消息返回 HTML', (p.headers.get('content-type') || '').includes('text/html'),
    String(p.headers.get('content-type')))
  check('页面上有群消息正文', p.text.includes('周三前交材料'), p.text.slice(0, 100))
  check('页面上手机号/学号仍打码', p.text.includes('[手机]') && p.text.includes('[学号]'))
  check('页面源码不含口令明文', !p.text.includes(READ), '')
  const pa = await call('/api/page?kind=ann')
  check('公告页公开可看', pa.status === 200 && pa.text.includes('期末考试安排'), String(pa.status))
  const pNo = await call('/api/page?kind=qq')
  check('群消息页没口令 → 401', pNo.status === 401, String(pNo.status))
}

console.log('\n6) 排错口 + 黑匣子')
{
  const d = await call('/api/dbg?t=' + READ)
  const j = d.json || {}
  check('排错口能开', d.status === 200, String(d.status))
  check('报 secret 长度不漏明文', j.secret_len === SECRET.length && !d.text.includes(SECRET), String(j.secret_len))
  check('黑匣子录下了验证请求', Array.isArray(j.last_requests) &&
    j.last_requests.some(x => String(x.body).includes('PT_12345')),
    JSON.stringify(j.last_requests && j.last_requests[0]).slice(0, 120))
  check('探针签名能算出来', /^[0-9a-f]{128}$/.test(String(j.probe_sig)), String(j.probe_sig).slice(0, 40))
  const no = await call('/api/dbg')
  check('排错口也要口令', no.status === 401, String(no.status))
}

console.log('\n7) 关注群过滤')
{
  globalThis.env.WATCH_GROUPS = 'OTHER_GROUP'
  const body = JSON.stringify({ op: 0, d: { msg_id: 'MSG002', group_id: 'G_2511', event_ts: Date.now(), content: '别的群' } })
  const r = await call('/api', { method: 'POST', body })
  check('不在关注列表 → 忽略', (r.json || {}).msg === '不在关注列表', r.text.slice(0, 100))
  globalThis.env.WATCH_GROUPS = ''
}

srv.close()
console.log('\n==== ' + pass + ' 通过 / ' + fail + ' 失败 ====')
process.exit(fail ? 1 : 0)
