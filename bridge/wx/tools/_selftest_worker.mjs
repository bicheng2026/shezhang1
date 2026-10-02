/**
 * 蛇杖一号 · Worker 端到端自检
 * 目的：不用 Cloudflare 就能验 worker.js 对不对（Kv、验签、脱敏、鉴权、路由）。
 * 跑法：node bridge/wx/tools/_selftest_worker.mjs
 */
import worker from '../worker.js'
import { createPrivateKey, createPublicKey } from 'node:crypto'

const SECRET = 'abcdefgh12345678'
const ADMIN = 'tok123'
const READ = 'read456'

/* ---- 1. 本地算出 raw 公钥（模拟 tools/pubkey.js 的输出） ---- */
let s = SECRET
while (Buffer.byteLength(s, 'utf8') < 32) s += SECRET
const seed = Buffer.from(s.slice(0, 32), 'utf8')
const der = Buffer.concat([Buffer.from('302e020100300506032b657004220420', 'hex'), seed])
const kp = createPrivateKey({ key: der, format: 'der', type: 'pkcs8' })
const spki = createPublicKey({ key: kp }).export({ type: 'spki', format: 'der' })
const PUBHEX = spki.subarray(spki.length - 32).toString('hex')

/* ---- 2. 假 KV ---- */
const store = new Map()
const KV = {
  async put(k, v) { store.set(k, String(v)) },
  async get(k) { return store.has(k) ? store.get(k) : null },
  async list({ prefix, limit }) {
    const ks = [...store.keys()].filter(k => k.startsWith(prefix)).sort().slice(0, limit)
    return { keys: ks.map(name => ({ name })) }
  },
}
const env = { BOT_SECRET: SECRET, PUBKEY: PUBHEX, ADMIN_TOKEN: ADMIN, READ_TOKEN: READ, WATCH_GROUPS: '', SHEZHANG: KV }

const call = (path, init) => worker.fetch(new Request('https://shezhang.workers.dev' + path, init), env, {})
let pass = 0, fail = 0
function check(name, cond, extra = '') {
  if (cond) { console.log('  [ok] ' + name); pass++ }
  else { console.log('  [FAIL] ' + name + (extra ? ' → ' + extra : '')); fail++ }
}

/* ---- 3. QQ 回调验证请求（要能签出来） ---- */
console.log('\n1) QQ 回调地址验证应答')
{
  const body = JSON.stringify({ op: 13, d: { plain_token: 'PT_12345', event_ts: '1730000000' } })
  const res = await call('/webhook', { method: 'POST', body })
  const j = await res.json()
  check('返回 plain_token 原样带回', j.plain_token === 'PT_12345', JSON.stringify(j))
  const sig = Buffer.from(String(j.signature), 'hex')
  const key = await crypto.subtle.importKey('raw', Uint8Array.from(Buffer.from(PUBHEX, 'hex')), { name: 'Ed25519' }, false, ['verify'])
  const ok = await crypto.subtle.verify({ name: 'Ed25519' }, key, sig, new TextEncoder().encode('1730000000PT_12345'))
  check('signature 用公钥验得通', ok && sig.length === 64, 'len=' + sig.length)
}

/* ---- 3.5 回调验证的几种变形（QQ 后台“校验不通过”最常踩） ---- */
console.log('\n1.5) 回调验证应答（POST body / GET query / 嵌套结构）')
{
  const b1 = JSON.stringify({ op: 13, d: { plain_token: 'PT_abc', event_ts: 1730000000 } })
  const r1 = await (await call('/', { method: 'POST', body: b1 })).json()
  check('POST / 能签', r1.plain_token === 'PT_abc' && /^[0-9a-f]{128}$/.test(String(r1.signature)), JSON.stringify(r1).slice(0, 120))

  const r2 = await (await call('/webhook', { method: 'GET', body: null,
    headers: {} })) // 无参 GET /webhook 不应崩
  check('GET /webhook 无参数不崩', r2.status === 400 || r2.status === 404, String(r2.status))

  const r3 = await (await call('/webhook?plain_token=Q1&event_ts=99', { method: 'GET' })).json()
  check('GET query 带 plain_token 也能签', r3.plain_token === 'Q1' && /^[0-9a-f]{128}$/.test(String(r3.signature)),
    JSON.stringify(r3).slice(0, 120))

  const b4 = JSON.stringify({ d: { d: { plain_token: 'DEEP' }, event_ts: 7 } })
  const r4 = await (await call('/', { method: 'POST', body: b4 })).json()
  check('嵌套两层也能挖出 plain_token', r4.plain_token === 'DEEP', JSON.stringify(r4).slice(0, 80))
}

/* ---- 4. 普通群消息：解析 + 脱敏 + 落 KV ---- */
console.log('\n2) 普通群消息（解析/脱敏/入库）')
{
  const body = JSON.stringify({
    op: 0,
    d: { msg_id: 'MSG001', group_id: 'G_2511', event_ts: Date.now(), content: '周三前交材料  电话13800138000  学号20255010729' },
  })
  const res = await call('/', { method: 'POST', body })
  const j = await res.json()
  check('返回已入库', j.code === 0 && j.id === 'qq_MSG001', JSON.stringify(j))
  const qqKeys = [...store.keys()].filter(k => k.startsWith('q/'))
  check('KV 里出现 q/ 记录', qqKeys.length === 1, JSON.stringify([...store.keys()]))
  const item = JSON.parse(store.get(qqKeys[0]))
  check('手机号被打码', item.title.includes('[手机]') && !item.title.includes('13800138000'), item.title)
  check('学号被打码', item.title.includes('[学号]') && !item.title.includes('20255010729'), item.title)
  check('不存发言人昵称', !('nick' in item) && item.sender.startsWith('QQ群'), JSON.stringify(item.sender))
}

/* ---- 5. 本机推公告（鉴权） ---- */
console.log('\n3) 公告推送 /sync 的鉴权')
{
  const items = [{ id: 'ann001', source: '教务处', title: '关于期末考试安排的通知', ts: Date.now(), level: 'P1', ddl: '' }]
  const body = JSON.stringify({ items })
  const bad = await call('/sync', { method: 'POST', body })
  check('没带 token → 401', bad.status === 401, String(bad.status))
  const good = await call('/sync', { method: 'POST', body, headers: { authorization: 'Bearer ' + ADMIN } })
  const j = await good.json()
  check('带 token → 入库', j.code === 0 && j.count === 1, JSON.stringify(j))
  const annKeys = [...store.keys()].filter(k => k.startsWith('a/'))
  check('KV 里出现 a/ 记录', annKeys.length === 1, JSON.stringify(annKeys))
}

/* ---- 6. 读取接口 ---- */
console.log('\n4) 读取接口')
{
  const ann = await (await call('/api/ann?limit=50')).json()
  check('公开公告能读且只有公告', ann.count === 1 && ann.items[0].id === 'ann001', JSON.stringify(ann.items))
  const annCors = await call('/api/ann')
  check('公开区带 CORS（网页版能跨域取）', !!annCors.headers.get('access-control-allow-origin'))

  const qqNo = await call('/api/qq')
  check('群消息不带 token → 拒绝', qqNo.status === 401, String(qqNo.status))
  const qq = await (await call('/api/qq?token=' + READ)).json()
  check('群消息带 token → 能读', qq.count === 1 && qq.items[0].id === 'qq_MSG001', JSON.stringify(qq.items))
  const qqCors = await (await call('/api/qq?token=' + READ))
  check('群消息区不对外开 CORS', qqCors.headers.get('access-control-allow-origin') === 'no',
    String(qqCors.headers.get('access-control-allow-origin')))

  const health = await (await call('/api/health')).json()
  check('health 体检正常', health.ok && health.has_secret && health.has_pubkey && health.has_kv, JSON.stringify(health))
}

/* ---- 6.5 手机页面 /ann /qq ---- */
console.log('\n4.5) 手机页面（HTML 直接可读）')
{
  const annPage = await call('/ann')
  const html = await annPage.text()
  check('/ann 返回 HTML 而不是 JSON', (annPage.headers.get('content-type') || '').includes('text/html'),
    String(annPage.headers.get('content-type')))
  check('/ann 页面上出现公告标题（不是 JSON 乱码）', html.includes('期末考试安排'), '')
  check('/ann 页面上不泄 138/学号等敏感串', !html.includes('13800138000') && !html.includes('20255010729'), '')

  const qqBad = await call('/qq')
  check('/qq 不带口令 → 401', qqBad.status === 401, String(qqBad.status))
  const qqPage = await call('/qq?t=' + READ)
  const html2 = await qqPage.text()
  check('/qq 带口令 → 可读', qqPage.status === 200 && html2.includes('班群消息'), String(qqPage.status))
  check('/qq 页面里不含物种 token 明文（口令只在地址栏）', !html2.includes(READ), '')
  check('/qq 页面上手机号/学号仍是打码的', html2.includes('[手机]') && html2.includes('[学号]'), '')
  const home = await (await call('/')).text()
  check('/ 导航页给出公告和群消息两个入口', home.includes('/ann') && home.includes('/qq'), '')
}

/* ---- 6.6 索引机制：读列表只花 1 个 subrequest（免费档上限 50） ---- */
console.log('\n4.6) 索引机制（避免撞 50 subrequest 上限）')
{
  let putCount = 0, getCount = 0
  const KV2 = {
    async put(k, v) { putCount++; store.set(k, String(v)) },
    async get(k) { getCount++; return store.has(k) ? store.get(k) : null },
    async list({ prefix, limit }) {
      return { keys: [...store.keys()].filter(x => x.startsWith(prefix)).sort().slice(0, limit).map(name => ({ name })) }
    },
  }
  const env3 = { ...env, SHEZHANG: KV2 }
  // 灌 60 条群消息（超过旧的 50 subrequest 上限）
  for (let i = 0; i < 60; i++) {
    const body = JSON.stringify({ op: 0, d: { msg_id: 'M' + i, group_id: 'G1', event_ts: Date.now() + i, content: '第 ' + i + ' 条' } })
    await worker.fetch(new Request('https://shezhang.workers.dev/', { method: 'POST', body }), env3, {})
  }
  getCount = 0
  const res = await worker.fetch(new Request('https://shezhang.workers.dev/api/qq?token=' + READ), env3, {})
  const j = await res.json()
  check('60 条也能读出来', j.count === 61, 'count=' + j.count)
  check('读一次列表只花 1 个 KV 读（不逐条 get）', getCount === 1, 'getCount=' + getCount)
  check('最新的一条排最前', j.items[0].id === 'qq_M59', JSON.stringify(j.items[0] && j.items[0].id))
}

/* ---- 7. 黑匣子 + 排错口 ---- */
console.log('\n4.7) 黑匣子（录下收到的请求）+ 排错口')
{
  await call('/', { method: 'POST', body: JSON.stringify({ op: 0, d: { msg_id: 'DBG1', group_id: 'G1', event_ts: Date.now(), content: '黑匣子测试' } }) })
  const dbg = await call('/api/dbg?t=' + READ)
  const j = await dbg.json()
  check('排错口能开', dbg.status === 200, String(dbg.status))
  check('报 secret 长度但不漏明文', j.secret_len === SECRET.length && !JSON.stringify(j).includes(SECRET),
    JSON.stringify(j).slice(0, 120))
  check('黑匣子录下了刚才那条请求', Array.isArray(j.last_requests) && j.last_requests.length >= 1 &&
    String(j.last_requests[0].body).includes('DBG1'), JSON.stringify(j.last_requests && j.last_requests[0]).slice(0, 160))
  check('群消息索引有值', j.qq_index_len === 62, 'len=' + j.qq_index_len)
  const noAuth = await call('/api/dbg')
  check('排错口也要口令', noAuth.status === 401, String(noAuth.status))
}

/* ---- 7. 关注群过滤 ---- */
console.log('\n5) 只收指定群（WATCH_GROUPS）')
{
  const env2 = { ...env, SHEZHANG: KV, WATCH_GROUPS: 'OTHER_GROUP' }
  const body = JSON.stringify({ op: 0, d: { msg_id: 'MSG002', group_id: 'G_2511', event_ts: Date.now(), content: '别的群的消息' } })
  const res = await worker.fetch(new Request('https://shezhang.workers.dev/', { method: 'POST', body }), env2, {})
  const j = await res.json()
  check('不在关注列表 → 忽略', j.code === 0 && j.msg === '不在关注列表', JSON.stringify(j))
}

console.log('\n==== ' + pass + ' 通过 / ' + fail + ' 失败 ====')
process.exit(fail ? 1 : 0)
