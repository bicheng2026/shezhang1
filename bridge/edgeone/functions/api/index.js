/**
 * 蛇杖一号 · EdgeOne Pages Functions 版（2026-10-02）
 * <CF_API_TOKEN>--------------------------
 * 为什么换平台：Cloudflare 的 `*.workers.dev` 在中国大陆被 GFW 屏蔽
 * （GreatFire 记录 2022-05-10 起连续屏蔽），QQ 官方服务器在国内连不上，
 * 直接报「需使用公网可访问的 HTTPS 地址」。EdgeOne 腾讯云出品、国内可连通。
 *
 * ⚠️ 与 Cloudflare 版的三处关键差异（别拿 CF 那套硬套）：
 *  1. 入口不是 `export default {fetch}`，而是 `addEventListener('fetch', ...)` 全局式。
 *  2. **环境变量是全局 `env`**（不是函数传参），引用直接 `env.BOT_SECRET`。
 *  3. **KV 绑定是全局变量**（不是 env 里的属性）：绑定名 `SHEZHANG` 就写 `SHEZHANG.get(...)`。
 *     写 `env.SHEZHANG.get(...)` 会报 undefined —— 官方文档明确说了这点。
 *
 * 部署位置：项目的 functions/api/index.js（路径决定 URL 前缀，见文末说明）
 *
 * 路由（假设部署后地址是 https://xxx.edgeone.app）：
 *   POST /api   QQ 官方 Webhook 入口（腾讯主动推群消息）
 *   POST /api/sync   本机推公告（Bearer ADMIN_TOKEN）
 *   GET  /api/ann   公开 JSON：公告（带 CORS，给网页版读）
 *   GET  /api/qq?token=  私有 JSON：群消息（不开 CORS）
 *   GET  /api/health  自检
 *   GET  /api/dbg?t=   排错口
 *   GET  /api/page?kind=ann|qq&t=   手机页面（HTML）
 *
 * 环境变量（在控制台配 4 个，类型选 Secret 或 String 都行）：
 *   BOT_SECRET   QQ 机器人 AppSecret
 *   PUBKEY       raw 公钥 hex（64 位）
 *   ADMIN_TOKEN  推送公告用
 *   READ_TOKEN   查群消息用
 */

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type, Authorization',
  'Access-Control-Max-Age': '86400',
}

function json(body, status = 200, extra = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8', ...CORS, ...extra },
  })
}

/* ---------------- 脱敏（顺序不能反：身份证 → 手机 → 学号） ---------------- */
function scrub(t) {
  if (!t) return ''
  t = String(t)
    .replace(/<faceType=[^>]{0,160}>/g, '')
    .replace(/<@!?\d+>/g, '')
    .replace(/<face\w*[^>]*$/g, '')
  t = t.replace(/\b\d{17}[\dXx]\b/g, '[本人]')
  t = t.replace(/\b1[3-9]\d{9}\b/g, '[手机]')
  t = t.replace(/\b\d{10,12}\b/g, '[学号]')
  return t.trim()
}

/* ---------------- 取值 ---------------- */
function pick(o, keys) {
  if (!o || typeof o !== 'object') return undefined
  for (const k of keys) {
    const v = o[k]
    if (v !== undefined && v !== null && v !== '') return v
  }
  return undefined
}
function findKey(o, key, depth) {
  if (!o || typeof o !== 'object' || !depth) return undefined
  const v = o[key]
  if (v !== undefined && v !== null && (typeof v === 'string' || typeof v === 'number')) return v
  for (const k of Object.keys(o)) {
    // d 有时是「装着 JSON 的字符串」（官方 Go 用 json.RawMessage 解它），拆开再找
    if (typeof o[k] === 'string' && o[k].length < 4000 && /^[\s]*[{[]/.test(o[k])) {
      try { const r = findKey(JSON.parse(o[k]), key, depth - 1); if (r !== undefined) return r } catch (e) { }
    }
    const r = findKey(o[k], key, depth - 1)
    if (r !== undefined) return r
  }
  return undefined
}
function deepPick(o, keys, depth) {
  if (!o || typeof o !== 'object' || !depth) return undefined
  const v = pick(o, keys)
  if (v !== undefined) return v
  for (const k of Object.keys(o)) {
    const r = deepPick(o[k], keys, depth - 1)
    if (r !== undefined) return r
  }
  return undefined
}
function parseEvent(payload) {
  const d = (payload && payload.d) || {}
  const text = deepPick(d, ['content', 'msg', 'text', 'message', 'raw_message'], 3)
  if (text === undefined) return null
  return {
    text: String(text),
    gid: String(deepPick(d, ['group_id', 'group_openid', 'channel_id', 'guild_id'], 3) || ''),
    mid: String(deepPick(d, ['msg_id', 'id', 'event_id'], 3) || ''),
    ts: Number(deepPick(d, ['event_ts', 'ts', 'timestamp'], 3)) || Date.now(),
  }
}

/* ---------------- Ed25519：应答验证用私钥签，普通事件用公钥验 ----------------
   ⚠️ WebCrypto：私钥 usages 只能 ['sign']，公钥只能 ['verify']，混着 import 会报
   "Unsupported key usage for a Ed25519 key"。私钥必须走 pkcs8 包装
   （raw seed 直接 import 实测 FAIL）。 */
const PKCS8_PREFIX = '302e020100300506032b657004220420'

function seedBuffer(secret) {
  const enc = new TextEncoder()
  let s = String(secret || '')
  while (enc.encode(s).byteLength < 32) s += String(secret)   // 补到 ≥32，再截前 32
  const all = enc.encode(s).subarray(0, 32)
  const bytes = new Uint8Array(32)
  bytes.set(all, 0)
  return bytes
}
function hexBytes(h) {
  const s = String(h).replace(/^0x/, '')
  const out = new Uint8Array(Math.floor(s.length / 2))
  for (let i = 0; i < out.length; i++) out[i] = parseInt(s.substr(i * 2, 2), 16)
  return out
}
function bytesToHex(b) {
  let s = ''
  for (const x of b) s += x.toString(16).padStart(2, '0')
  return s
}
function concatBytes(...arrs) {
  const total = arrs.reduce((n, a) => n + a.length, 0)
  const out = new Uint8Array(total)
  let p = 0
  for (const a of arrs) { out.set(a, p); p += a.length }
  return out
}
async function signHex(msgStr) {
  const secret = env.BOT_SECRET
  if (!secret) throw new Error('没配 BOT_SECRET')
  const der = concatBytes(hexBytes(PKCS8_PREFIX), seedBuffer(secret))
  const key = await crypto.subtle.importKey('pkcs8', der, { name: 'Ed25519' }, false, ['sign'])
  const sig = await crypto.subtle.sign({ name: 'Ed25519' }, key, new TextEncoder().encode(msgStr))
  return bytesToHex(new Uint8Array(sig))
}
async function verifyHex(hexSig, msgStr) {
  const pubHex = env.PUBKEY
  if (!pubHex) return false
  const key = await crypto.subtle.importKey('raw', hexBytes(pubHex), { name: 'Ed25519' }, false, ['verify'])
  return await crypto.subtle.verify({ name: 'Ed25519' }, key, hexBytes(hexSig), new TextEncoder().encode(msgStr))
}

/* ---------------- KV 读写
   ⚠️ SHEZHANG 是**全局变量**（控制台绑定命名空间时填的变量名），
      不是 env.SHEZHANG。这是 EdgeOne 和 Cloudflare 最大的差别。 */
const IDX = { qq: 'idx_q', ann: 'idx_a' }
const IDX_MAX = 200

async function kvPut(kind, id, obj) {
  const prefix = kind === 'ann' ? 'a/' : 'q/'
  const tail = (kind === 'ann' ? '' : 'qq_') + id
  const key = prefix + String(Date.now()) + '-' + tail.replace(/[^\w.-]/g, '_')
  await SHEZHANG.put(key, JSON.stringify(obj))
  return key
}
async function bumpIndex(kind, item) {
  const key = IDX[kind]
  let arr = []
  try { const raw = await SHEZHANG.get(key); arr = raw ? JSON.parse(raw) : [] } catch (e) { arr = [] }
  if (!Array.isArray(arr)) arr = []
  arr = arr.filter(x => x && x.id !== item.id)
  arr.push(item)
  arr.sort((a, b) => (Number(b.ts) || 0) - (Number(a.ts) || 0))
  arr = arr.slice(0, IDX_MAX)
  await SHEZHANG.put(key, JSON.stringify(arr))
  return arr.length
}
async function readIndex(kind) {
  try {
    const raw = await SHEZHANG.get(IDX[kind])
    const arr = raw ? JSON.parse(raw) : []
    return Array.isArray(arr) ? arr : []
  } catch (e) { return [] }
}
/* 索引还没有时兜底：只逐条取 50 条，免得撞调用上限 */
async function kvListOld(kind) {
  const prefix = kind === 'ann' ? 'a/' : 'q/'
  const out = []
  try {
    const res = await SHEZHANG.list({ prefix, limit: 50 })
    const keys = (res && (res.keys || res.list)) || []
    for (const k of keys) {
      const name = typeof k === 'string' ? k : k.name
      const raw = await SHEZHANG.get(name)
      if (raw) { try { out.push(JSON.parse(raw)) } catch (e) { } }
    }
  } catch (e) { }
  return out
}
async function listItems(kind, limit) {
  let list = await readIndex(kind)
  if (!list.length) list = await kvListOld(kind)
  list = list.slice().sort((a, b) => (Number(b.ts) || 0) - (Number(a.ts) || 0))
  return list.slice(0, Math.min(limit, IDX_MAX))
}

/* ---------------- 黑匣子：录下最近 10 条请求，校验不过时用它定性 ---------------- */
async function record(entry) {
  try {
    let arr = []
    const raw = await SHEZHANG.get('dbg')
    if (raw) { const a = JSON.parse(raw); if (Array.isArray(a)) arr = a }
    arr.unshift(entry)
    await SHEZHANG.put('dbg', JSON.stringify(arr.slice(0, 10)))
  } catch (e) { }
}

/* ---------------- 手机页面 ---------------- */
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]))
}
function fmtBj(ts) {
  const d = new Date((Number(ts) || Date.now()) + 8 * 3600 * 1000)
  const p = n => String(n).padStart(2, '0')
  return d.getUTCFullYear() + '-' + p(d.getUTCMonth() + 1) + '-' + p(d.getUTCDate()) +
    ' ' + p(d.getUTCHours()) + ':' + p(d.getUTCMinutes())
}
function levelTag(lv) {
  const s = String(lv || '').toUpperCase()
  if (s === 'P0') return '<span class="tag p0">特急</span>'
  if (s === 'P1') return '<span class="tag p1">重要</span>'
  return '<span class="tag p2">一般</span>'
}
function pageHtml(items, title, tip) {
  const rows = items.map(it => (
    '<li class="row">' +
      '<div class="meta">' + levelTag(it.level) +
        '<span class="time">' + esc(fmtBj(it.ts)) + '</span>' +
        '<span class="src">' + esc(it.sender || it.source || '') + '</span>' +
      '</div>' +
      '<div class="body">' + esc(it.title).replace(/\n/g, '<br>') + '</div>' +
      (it.ddl ? '<div class="ddl">截止 ' + esc(it.ddl) + '</div>' : '') +
    '</li>'
  )).join('')
  const empty = '<li class="empty">还没有内容 —— ' + esc(tip) + '</li>'
  return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width,initial-scale=1">' +
    '<title>' + esc(title) + '</title><style>' +
    ':root{--bg:#f6f7f9;--card:#fff;--line:#e6e8eb;--txt:#1f2328;--sub:#6b7280;--p0:#c0392b;--p1:#d68910;--p2:#5b8def}' +
    'body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}' +
    '.head{position:sticky;top:0;background:rgba(246,247,249,.92);backdrop-filter:blur(6px);padding:14px 16px 10px;border-bottom:1px solid var(--line)}' +
    '.head h1{margin:0;font-size:17px;font-weight:600}.head p{margin:4px 0 0;font-size:12px;color:var(--sub)}' +
    'ul{list-style:none;margin:0;padding:10px 12px 40px}' +
    '.row{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:11px 13px;margin-bottom:9px}' +
    '.meta{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--sub);margin-bottom:5px;flex-wrap:wrap}' +
    '.time{margin-left:auto}.tag{border-radius:4px;padding:1px 6px;font-size:11px;color:#fff;background:var(--p2)}' +
    '.p0{background:var(--p0)} .p1{background:var(--p1)}.body{white-space:pre-wrap;word-break:break-word}' +
    '.ddl{margin-top:5px;font-size:12px;color:var(--p0)}.empty{color:var(--sub);text-align:center;padding:30px 0}' +
    '</style></head><body><div class="head"><h1>' + esc(title) + '</h1><p>' + esc(tip) + '</p></div><ul>' +
    (rows || empty) + '</ul></body></html>'
}
function html(body) {
  return new Response(body, {
    headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' },
  })
}

/* ---------------- QQ 回调 ---------------- */
async function handleQQ(request, url) {
  const body = request.method === 'POST' ? await request.text() : ''
  let payload = null
  if (body) { try { payload = JSON.parse(body) } catch (e) { payload = null } }

  await record({
    at: Date.now(),
    method: request.method,
    path: url.pathname,
    ua: request.headers.get('user-agent') || '',
    appid: request.headers.get('x-bot-appid') || '',
    query: url.search.slice(0, 300),
    body: body.slice(0, 600),
  })

  // 回调验证：回 {plain_token, signature}
  let pt = findKey(payload, 'plain_token', 5)
  let ets = findKey(payload, 'event_ts', 5)
  if (pt === undefined || pt === null) pt = url.searchParams.get('plain_token')
  if (ets === undefined || ets === null) ets = url.searchParams.get('event_ts')
  if (pt !== undefined && pt !== null && ets !== undefined && ets !== null) {
    let sig = ''
    try { sig = await signHex(String(ets) + String(pt)) } catch (e) { sig = 'ERR:' + String(e) }
    return json({ plain_token: String(pt), signature: sig })
  }

  if (!payload) return json({ code: 400, msg: 'body 不是合法 JSON' }, 400)

  const ev = parseEvent(payload)
  if (!ev) return json({ code: 0, msg: '非消息事件，忽略' })

  const watch = String(env.WATCH_GROUPS || '').split(',').map(s => s.trim()).filter(Boolean)
  if (watch.length && watch.every(w => ev.gid.indexOf(w) < 0)) {
    return json({ code: 0, msg: '不在关注列表', gid: ev.gid })
  }

  const title = scrub(ev.text)
  if (!title) return json({ code: 0, msg: '脱敏后为空，丢弃' })

  const sigHeader = request.headers.get('x-signature') || request.headers.get('signature') || ''
  if (env.PUBKEY && sigHeader) {
    const ok = await verifyHex(sigHeader.trim(), String(ev.ts) + body)
    if (!ok) return json({ code: 403, msg: '验签不过' }, 403)
  }

  const item = {
    id: 'qq_' + ev.mid,
    source: 'qq',
    sender: ev.gid ? 'QQ群 ' + ev.gid.slice(0, 10) : 'QQ群',
    title: title.slice(0, 200),
    ts: ev.ts,
    level: 'P2',
    ddl: '',
    status: 'open',
  }
  await kvPut('qq', item.id, item)
  await bumpIndex('qq', item)
  return json({ code: 0, msg: '已入库', id: item.id })
}

/* ---------------- 公告推送 ---------------- */
async function handleSync(request) {
  const admin = env.ADMIN_TOKEN || ''
  if (!admin) return json({ code: 500, msg: '服务端没配 ADMIN_TOKEN' }, 500)
  const auth = request.headers.get('authorization') || ''
  if (auth !== 'Bearer ' + admin) return json({ code: 401, msg: 'token 不对' }, 401)
  let payload
  try { payload = JSON.parse(await request.text()) } catch (e) { return json({ code: 400, msg: 'body 不是合法 JSON' }, 400) }
  const items = Array.isArray(payload.items) ? payload.items : []
  let ok = 0
  for (const it of items) {
    if (!it || !it.id) continue
    it.title = String(it.title || '').slice(0, 200)
    if (!it.ts) it.ts = Date.now()
    await kvPut('ann', it.id, it)
    await bumpIndex('ann', it)
    ok++
  }
  return json({ code: 0, msg: '同步完成', count: ok })
}

/* ---------------- 入口（EdgeOne Pages Functions 写法） ---------------- */
async function handleRequest(request) {
  const url = new URL(request.url)
  const p = url.pathname.replace(/\/+$/, '') || '/'
  const method = request.method

  if (method === 'OPTIONS') return new Response(null, { status: 204, headers: CORS })

  // 回调：/api 本身（/api/qq 那些是读接口，优先判它们）
  if (method !== 'GET' && p.endsWith('/api')) {
    return handleQQ(request, url)
  }
  if (method !== 'GET' && p.endsWith('/api/sync')) {
    return handleSync(request)
  }

  if (method !== 'GET') return json({ code: 404, msg: '没有这个接口', path: p }, 404)

  const kind = p.endsWith('/ann') ? 'ann' : (p.endsWith('/qq') ? 'qq' : '')
  const isPage = p.endsWith('/page')
  const isDbg = p.endsWith('/dbg')
  const isHealth = p.endsWith('/health')
  const isAnnJson = p.endsWith('/api/ann')
  const isQqJson = p.endsWith('/api/qq')
  if (!kind && !isPage && !isDbg && !isHealth && !isAnnJson && !isQqJson) {
    return html('<!doctype html><meta charset="utf-8"><title>蛇杖一号</title>' +
      '<body style="font:15px/1.8 -apple-system,\'PingFang SC\',sans-serif;padding:30px;max-width:640px">' +
      '<h2>蛇杖一号</h2><ul>' +
      '<li><a href="/api/page?kind=ann">公告（公开）</a></li>' +
      '<li><a href="/api/page?kind=qq&t=你的口令">班群消息（需要口令）</a></li>' +
      '<li><a href="/api/health">健康自检</a></li></ul></body>')
  }

  if (isHealth) {
    return json({
      ok: true,
      has_secret: !!env.BOT_SECRET,
      has_pubkey: !!env.PUBKEY,
      has_admin: !!env.ADMIN_TOKEN,
      has_read: !!env.READ_TOKEN,
      has_kv: (typeof SHEZHANG !== 'undefined'),
      now: Date.now(),
    })
  }

  if (isDbg) {
    const t = url.searchParams.get('t') || ''
    if (t !== (env.READ_TOKEN || '') && t !== (env.ADMIN_TOKEN || '')) {
      return json({ code: 401, msg: '口令不对' }, 401)
    }
    let sig = ''
    try { sig = await signHex('1730000000PROBETOKEN') } catch (e) { sig = 'ERR:' + String(e) }
    let log = []
    try { log = JSON.parse(await SHEZHANG.get('dbg') || '[]') } catch (e) { log = [] }
    return json({
      has_secret: !!env.BOT_SECRET,
      secret_len: String(env.BOT_SECRET || '').length,
      has_pubkey: !!env.PUBKEY,
      has_kv: (typeof SHEZHANG !== 'undefined'),
      probe_msg: '1730000000PROBETOKEN',
      probe_sig: sig,
      qq_index_len: (await readIndex('qq')).length,
      ann_index_len: (await readIndex('ann')).length,
      last_requests: log.slice(0, 5),
    })
  }

  // 群消息区：所有读接口都要口令（页面用 t，JSON 用 token，认两种）
  if (kind === 'qq' || isQqJson || (isPage && (url.searchParams.get('kind') === 'qq'))) {
    const t = url.searchParams.get('t') || url.searchParams.get('token') || ''
    if (t !== (env.READ_TOKEN || '') && t !== (env.ADMIN_TOKEN || '')) {
      return json({ code: 401, msg: '口令不对' }, 401)
    }
  }

  const readKind = kind || (isPage ? (url.searchParams.get('kind') === 'qq' ? 'qq' : 'ann') : (isQqJson ? 'qq' : 'ann'))
  const limit = Math.min(Number(url.searchParams.get('limit')) || 200, 200)
  const items = await listItems(readKind, limit)

  if (isPage) {
    return html(pageHtml(items,
      readKind === 'qq' ? '蛇杖一号 · 班群消息' : '蛇杖一号 · 公告',
      readKind === 'qq' ? '只给你看 · 已脱敏' : '公开 · 可转给同学'))
  }
  // 私有 JSON 不开 CORS
  return json({ count: items.length, items, ts: Date.now() }, 200,
    readKind === 'qq' ? { 'Access-Control-Allow-Origin': 'no' } : {})
}

addEventListener('fetch', event => {
  event.respondWith(handleRequest(event.request))
})
