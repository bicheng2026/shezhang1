/**
 * 蛇杖一号 · 腾讯云 SCF 版自检（不连腾讯云，本地真跑 main_handler）
 * 跑法：node bridge/tencent/tools/_selftest_scf.mjs
 */
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { createPrivateKey, createPublicKey } from 'node:crypto'

const require = createRequire(import.meta.url)
const HERE = dirname(fileURLToPath(import.meta.url))
const mod = require(join(HERE, '..', 'scf_', 'handler.js'))
const handler = mod.main_handler

const SECRET = 'abcdefgh12345678'
const ADMIN = 'tok123'
const READ = 'read456'
process.env.BOT_SECRET = SECRET
process.env.ADMIN_TOKEN = ADMIN
process.env.READ_TOKEN = READ
process.env.WATCH_GROUPS = ''

/* 本地算 raw 公钥 */
let s = SECRET
while (Buffer.byteLength(s, 'utf8') < 32) s += SECRET
const seed = Buffer.from(s.slice(0, 32), 'utf8')
const der = Buffer.concat([Buffer.from('302e020100300506032b657004220420', 'hex'), seed])
const kp = createPrivateKey({ key: der, format: 'der', type: 'pkcs8' })
const spki = createPublicKey({ key: kp }).export({ type: 'spki', format: 'der' })
const PUBHEX = spki.subarray(spki.length - 32).toString('hex')
process.env.PUBKEY = PUBHEX

let pass = 0, fail = 0
const check = (n, c, e = '') => {
  if (c) { console.log('  [ok] ' + n); pass++ }
  else { console.log('  [FAIL] ' + n + (e ? ' → ' + String(e).slice(0, 200) : '')); fail++ }
}
/* 造一个 SCF Web函数事件 */
const evt = (path, method, { body, headers = {}, query = '' } = {}) => ({
  path, httpMethod: method, headers, queryString: query,
  body: body == null ? '' : (typeof body === 'string' ? body : JSON.stringify(body)),
  isBase64Encoded: false,
})
const call = async (...a) => {
  const r = await handler(evt(...a), {})
  let j = null
  try { j = JSON.parse(r.body) } catch (e) { }
  return { status: r.statusCode, headers: r.headers, text: r.body, json: j }
}

console.log('\n1) QQ 回调验证（QQ 后台点「校验」走这条）')
{
  const r = await call('/', 'POST', { body: { op: 13, d: { plain_token: 'PT_12345', event_ts: '1730000000' } } })
  check('plain_token 原样带回', r.json && r.json.plain_token === 'PT_12345', r.text.slice(0, 120))
  const sig = Buffer.from(String(r.json && r.json.signature), 'hex')
  const key = await crypto.subtle.importKey('raw', Uint8Array.from(Buffer.from(PUBHEX, 'hex')), { name: 'Ed25519' }, false, ['verify'])
  const okv = await crypto.subtle.verify({ name: 'Ed25519' }, key, sig, new TextEncoder().encode('1730000000PT_12345'))
  check('签名用公钥反验得通（和官方 Go 算法一致）', okv && sig.length === 64, 'len=' + sig.length)
  const r2 = await call('/qq', 'POST', { body: { op: 13, d: { plain_token: 'Q1', event_ts: 99 } } })
  check('/qq 路径也认（回调地址放哪都行）', (r2.json || {}).plain_token === 'Q1', r2.text.slice(0, 80))
  const r3 = await call('/webhook', 'POST', { query: 'plain_token=Q2&event_ts=7' })
  check('query 里的 plain_token 也能签（GET 探活兼容）', (r3.json || {}).plain_token === 'Q2', r3.text.slice(0, 80))
}

console.log('\n2) 群消息：解析 + 脱敏 + 落库')
{
  const r = await call('/', 'POST', {
    body: { op: 0, d: { msg_id: 'M1', group_id: 'G_2511', event_ts: Date.now(), content: '周三交材料 手机13800138000 学号20255010729 身份证450103200301011234' } },
  })
  check('返回已入库', (r.json || {}).code === 0 && r.json.id === 'qq_M1', r.text.slice(0, 120))
  const idx = JSON.parse(mod._mem.get('idx_q') || '[]')
  check('群消息索引有 1 条', idx.length === 1, String(idx.length))
  const it = idx[0] || {}
  check('手机号打码', it.title && it.title.includes('[手机]') && !it.title.includes('13800138000'), it.title)
  check('学号打码', it.title && it.title.includes('[学号]'), it.title)
  check('身份证打码', it.title && it.title.includes('[本人]'), it.title)
  check('不存发言人昵称', it.title && !('nick' in it), JSON.stringify(Object.keys(it)))
}

console.log('\n3) 公告推送 /sync')
{
  const items = [{ id: 'ann1', source: '教务处', title: '关于期末考试安排的通知', ts: Date.now(), level: 'P1', ddl: '2026-10-20' }]
  const bad = await call('/sync', 'POST', { body: { items } })
  check('没带 token → 401', bad.status === 401, String(bad.status))
  const good = await call('/sync', 'POST', { body: { items }, headers: { Authorization: 'Bearer ' + ADMIN } })
  check('带 token → 入库', (good.json || {}).code === 0 && good.json.count === 1, good.text.slice(0, 100))
}

console.log('\n4) 读接口 + 鉴权 + CORS')
{
  const ann = await call('/ann', 'GET')
  check('公告能读', (ann.json || {}).count === 1, ann.text.slice(0, 80))
  check('公告带 CORS', (ann.headers || {})['Access-Control-Allow-Origin'] === '*', JSON.stringify(ann.headers))
  const no = await call('/qq', 'GET')
  check('群消息无口令 → 401', no.status === 401, String(no.status))
  const yes = await call('/qq', 'GET', { query: 't=' + READ })
  check('群消息带口令 → 能读', (yes.json || {}).count === 1, yes.text.slice(0, 80))
  check('群消息不开 CORS', yes.headers['Access-Control-Allow-Origin'] === 'no', String(yes.headers['Access-Control-Allow-Origin']))
  const h = await call('/health', 'GET')
  check('health 全 true', h.json && h.json.ok && h.json.has_secret && h.json.has_pubkey && h.json.has_admin, h.text.slice(0, 120))
}

console.log('\n5) 手机页面')
{
  const p = await call('/page', 'GET', { query: 'kind=qq&t=' + READ })
  check('群消息页是 HTML', String(p.headers['Content-Type'] || '').includes('text/html'), p.headers['Content-Type'])
  check('页面有群消息正文', p.text.includes('周三交材料'), '')
  check('页面里手机号仍打码', p.text.includes('[手机]') && !p.text.includes('13800138000'), '')
  check('页面源码不含口令', !p.text.includes(READ), '')
  const pa = await call('/page', 'GET', { query: 'kind=ann' })
  check('公告页公开可看', pa.status === 200 && pa.text.includes('期末考试安排'), String(pa.status))
  const pn = await call('/page', 'GET', { query: 'kind=qq' })
  check('群消息页无口令 → 401', pn.status === 401, String(pn.status))
}

console.log('\n6) 黑匣子 + 排错口')
{
  const d = await call('/dbg', 'GET', { query: 't=' + READ })
  check('排错口能开', d.status === 200, String(d.status))
  check('报 secret 长度不漏明文', d.json && d.json.secret_len === SECRET.length && !d.text.includes(SECRET), String(d.json && d.json.secret_len))
  check('黑匣子录下了验证请求', d.json && Array.isArray(d.json.last_requests) &&
    d.json.last_requests.some(x => String(x.body).includes('PT_12345')), JSON.stringify(d.json && d.json.last_requests).slice(0, 120))
  check('探针签名能算', d.json && /^[0-9a-f]{128}$/.test(String(d.json.probe_sig)), String(d.json && d.json.probe_sig).slice(0, 30))
  check('群消息索引长度对', d.json && d.json.qq_index_len === 1, String(d.json && d.json.qq_index_len))
  const no = await call('/dbg', 'GET')
  check('排错口也要口令', no.status === 401, String(no.status))
}

console.log('\n7) 关注群过滤 + 异常兜底')
{
  process.env.WATCH_GROUPS = 'OTHER'
  const r = await call('/', 'POST', { body: { op: 0, d: { msg_id: 'M2', group_id: 'G_2511', event_ts: Date.now(), content: '别的群' } } })
  check('不在关注列表 → 忽略', (r.json || {}).msg === '不在关注列表', r.text.slice(0, 80))
  process.env.WATCH_GROUPS = ''
  const bad = await call('/', 'POST', { body: 'not-json{' })
  check('坏 JSON → 400 不崩', bad.status === 400, String(bad.status))
  const nf = await call('/', 'GET')
  check('GET 回调路径也不崩', nf.status === 200 || nf.status === 400, String(nf.status))
  const nf2 = await call('/nothing', 'GET')
  check('未知路径给导航页', nf2.status === 200 && nf2.text.includes('蛇杖一号'), String(nf2.status))
}

console.log('\n8) base64 请求体（API 网关可能开这个）')
{
  const body = Buffer.from(JSON.stringify({ op: 0, d: { msg_id: 'B64', group_id: 'G1', event_ts: Date.now(), content: 'base64 测试' } })).toString('base64')
  const r = await handler({ path: '/', httpMethod: 'POST', headers: {}, queryString: '', body, isBase64Encoded: true }, {})
  const j = JSON.parse(r.body)
  check('base64 体能解出消息', j.code === 0 && j.id === 'qq_B64', r.body.slice(0, 100))
}

console.log('\n==== ' + pass + ' 通过 / ' + fail + ' 失败 ====')
process.exit(fail ? 1 : 0)
