/**
 * 蛇杖一号 · Cloudflare Worker（0 元档 / 不依赖任何设备在线）
 * <CF_API_TOKEN>--------------------------
 * 背景（2026-10-02 主公定：一分钱不花 + 手机电脑不能整天在线）
 *   → 接收端必须放在「永远在线、又要不要钱、还要 HTTPS 443」的地方，
 *     唯一满足的是 Cloudflare Workers 免费档（100k req/天、自带 *.workers.dev HTTPS、不需要信用卡）。
 *
 * 路由：
 *   GET  /             导航页（手机上先点这个）
 *   GET  /ann          公告页面（公开，手机点开就是列表）
 *   GET  /qq?t=        班群消息页面（要 READ_TOKEN，页面源码里不带口令）
 *   POST /           QQ 官方 Webhook 入口（腾讯主动把群消息推过来）
 *   POST /sync        本机/手机 bridge 推公告过来（带 ADMIN_TOKEN）
 *   GET  /api/ann      公开 JSON：只返回公告（给 GitHub Pages 网页版全班看）
 *   GET  /api/qq?token=  私有 JSON：只返回班群消息（不对外、不带 CORS）
 *   GET  /api/health  自检
 *
 * 另外：单条存 q/<ts>-<id>、a/<ts>-<id> 留底，另建索引 idx_q / idx_a（读列表只花 1 个 subrequest，
 * 免费档 50 subrequest 上限不会被撞破）。
 *
 * 存储：Workers KV（免费 100k 读/天、1k 写/天、1GB；班级量级绰绰有余）
 *   key 规则：a/<13位时间戳>-<id> 公告   q/<13位时间戳>-<id> 群消息
 *   用字典序 = 时间序，读取时 list({prefix, limit}) 一次拿全，只花 1 个 subrequest。
 *
 * 环境变量 / Secret（wrangler secret put xxx，或 dashboard 里填）：
 *   BOT_SECRET   必填。QQ 机器人后台的 AppSecret。
 *                用途①：应答 QQ「回调地址验证」（要用私钥签名）。
 *                用途②：普通事件验签时补算公钥（若 PUBKEY 没配）。
 *   PUBKEY       选填。raw 公钥 hex（32 字节）。本地跑 tools/pubkey.js 算出来贴这里。
 *                配了它，Worker 验签名就不需要 BOT_SECRET 参与，泄露风险更小。
 *   ADMIN_TOKEN  必填。自己生成的随机串，本机推送公告时用（/sync 的鉴权）。
 *   READ_TOKEN   选填。自己生成的随机串，查 /api/qq 时带 ?token=。
 *   WATCH_GROUPS 选填。只收指定群，逗号分隔，如 "AbCd1234,Eff4567"。
 *
 * 注意：Workers 免费版每个请求 CPU 上限 10ms（硬限制）。
 *   这条路由只做「验签 + 解析 + 脱敏 + 一次 KV 写」，不做抓取、不做重解析，不会碰线。
 * <CF_API_TOKEN>--------------------------
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

/* ---------------- 脱敏（对齐 bridge.py 的 scrub_qq，顺序不能反） ---------------- */
function scrub(t) {
  if (!t) return ''
  t = String(t)
    .replace(/<faceType=[^>]{0,160}>/g, '')
    .replace(/<@!?\d+>/g, '')
    .replace(/<face\w*[^>]*$/g, '')
  t = t.replace(/\b\d{17}[\dXx]\b/g, '[本人]')   // 身份证（最长，最先）
  t = t.replace(/\b1[3-9]\d{9}\b/g, '[手机]')    // 手机号
  t = t.replace(/\b\d{10,12}\b/g, '[学号]')      // 学号（最短，最后）
  return t.trim()
}

/* ---------------- 取值：QQ 各事件的字段名不一致，深度探测 ---------------- */
function pick(o, keys) {
  if (!o || typeof o !== 'object') return undefined
  for (const k of keys) {
    const v = o[k]
    if (v !== undefined && v !== null && v !== '') return v
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

/* ---------------- Ed25519：应答验证要用「私钥签」，验普通事件用「公钥验」 ---------------- */
/* WebCrypto 规矩：私钥 usages 只给 ['sign']，公钥只给 ['verify']，混着 import 会报
   "Unsupported key usage for a Ed25519 key"（2026-10-02 实测踩过）。 */
const PKCS8_PREFIX = '302e020100300506032b657004220420'

/* AppSecret → 32 字节 seed：短了就 repeat 自己凑够，长了截前 32（对齐官方 Go 实现）。
   ⚠️ Workers 里没有 Buffer；而且不能「写不满就从头重来」，那会死循环（2026-10-02 踩过）。 */
function seedBuffer(secret) {
  const enc = new TextEncoder()
  let s = String(secret || '')
  while (enc.encode(s).byteLength < 32) s += String(secret)
  const all = enc.encode(s).subarray(0, 32)   // AppSecret 是 ASCII，切 32 字节安全
  const bytes = new Uint8Array(32)
  bytes.set(all, 0)
  return bytes
}

async function signHex(msgStr) {
  const secret = env0()
  if (!secret) throw new Error('没配 BOT_SECRET')
  const der = concatBytes(hexBytes(PKCS8_PREFIX), seedBuffer(secret))
  const key = await crypto.subtle.importKey('pkcs8', der, { name: 'Ed25519' }, false, ['sign'])
  const sig = await crypto.subtle.sign({ name: 'Ed25519' }, key, new TextEncoder().encode(msgStr))
  return bytesToHex(new Uint8Array(sig))
}

async function verifyHex(hexSig, msgStr) {
  const pubHex = env0('PUBKEY')
  if (!pubHex) return false // 没配公钥 → 不验（回调地址唯一，风险可控）
  const key = await crypto.subtle.importKey('raw', hexBytes(pubHex), { name: 'Ed25519' }, false, ['verify'])
  return await crypto.subtle.verify({ name: 'Ed25519' }, key, hexBytes(hexSig), new TextEncoder().encode(msgStr))
}

let _env = null
function env0(name) {
  if (!_env) return undefined
  return name === undefined ? _env.BOT_SECRET : _env[name]
}
function hexBytes(h) {
  const s = String(h).replace(/^0x/, '')
  const out = new Uint8Array(s.length / 2)
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

/* ---------------- KV 读写 ----------------
   ⚠️ 免费档每个请求最多 50 个 subrequest。旧的「list 一次 + 每条再 get 一次」
   在 50 条以后就会撞墙（200 条 = 201 subrequests）。
   所以改成：单条存 q/<ts>-<id> 留底，同时维护一个紧凑索引 idx_q / idx_a，
   读列表只读索引（1 subrequest），写入时多花 1 读 1 写，稳得很。 */
const IDX = { qq: 'idx_q', ann: 'idx_a' }
const IDX_MAX = 200

async function kvPut(env, kind, id, obj) {
  const prefix = kind === 'ann' ? 'a/' : 'q/'
  const tail = (kind === 'ann' ? '' : 'qq_') + id
  const key = prefix + String(Date.now()) + '-' + tail.replace(/[^\w.-]/g, '_')
  await env.SHEZHANG.put(key, JSON.stringify(obj))
  return key
}

/* 维护索引：同 id 只留最新一条，最多 200 条，按时间倒序 */
async function bumpIndex(env, kind, item) {
  const key = IDX[kind]
  let arr = []
  try {
    const raw = await env.SHEZHANG.get(key)
    arr = raw ? JSON.parse(raw) : []
  } catch (e) { arr = [] }
  if (!Array.isArray(arr)) arr = []
  arr = arr.filter(x => x && x.id !== item.id)
  arr.push(item)
  arr.sort((a, b) => (Number(b.ts) || 0) - (Number(a.ts) || 0))
  arr = arr.slice(0, IDX_MAX)
  await env.SHEZHANG.put(key, JSON.stringify(arr))
  return arr.length
}

async function readIndex(env, kind) {
  try {
    const raw = await env.SHEZHANG.get(IDX[kind])
    const arr = raw ? JSON.parse(raw) : []
    return Array.isArray(arr) ? arr : []
  } catch (e) { return [] }
}

/* 旧数据兜底：索引还没有时用单条记录（只敢逐条取 50 条，免得爆 subrequest） */
async function kvList(env, kind, limit) {
  const prefix = kind === 'ann' ? 'a/' : 'q/'
  const res = await env.SHEZHANG.list({ prefix, limit: 50 })
  const out = []
  for (const k of res.keys) {
    const raw = await env.SHEZHANG.get(k.name)
    if (raw) {
      try { out.push(JSON.parse(raw)) } catch (e) { /* 坏数据跳过 */ }
    }
  }
  // list 按 key 字典序，时间戳前缀保证这就是时间序
  return out
}

async function listItems(env, kind, limit) {
  let list = await readIndex(env, kind)
  if (!list.length) list = await kvList(env, kind, 50)
  list = list.slice().sort((a, b) => (Number(b.ts) || 0) - (Number(a.ts) || 0))
  return list.slice(0, Math.min(limit, IDX_MAX))
}

/* ---------------- 页面渲染：给手机看的，不是给程序看的 ---------------- */
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]))
}
function fmtBj(ts) {                       // Workers 是 UTC，手动挪成北京时间
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
function pageHtml(kind, items, title, tip) {
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
    '.head h1{margin:0;font-size:17px;font-weight:600}' +
    '.head p{margin:4px 0 0;font-size:12px;color:var(--sub)}' +
    'ul{list-style:none;margin:0;padding:10px 12px 40px}' +
    '.row{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:11px 13px;margin-bottom:9px}' +
    '.meta{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--sub);margin-bottom:5px;flex-wrap:wrap}' +
    '.time{margin-left:auto}' +
    '.tag{border-radius:4px;padding:1px 6px;font-size:11px;color:#fff;background:var(--p2)}' +
    '.p0{background:var(--p0)} .p1{background:var(--p1)}' +
    '.body{white-space:pre-wrap;word-break:break-word}' +
    '.ddl{margin-top:5px;font-size:12px;color:var(--p0)}' +
    '.empty{color:var(--sub);text-align:center;padding:30px 0}' +
    '</style></head><body><div class="head"><h1>' + esc(title) + '</h1>' +
    '<p>' + esc(tip) + '</p></div><ul>' + (rows || empty) + '</ul></body></html>'
}

/* ---------------- 路由处理 ---------------- */
/* 在任意层级里按 key 名找值（QQ 各版本回调结构不一样，硬编码一层会漏） */
function findKey(o, key, depth) {
  if (!o || typeof o !== 'object' || !depth) return undefined
  const v = o[key]
  if (v !== undefined && v !== null && (typeof v === 'string' || typeof v === 'number')) return v
  for (const k of Object.keys(o)) {
    // d 有时是一个「装着 JSON 的字符串」（官方 Go 用 json.RawMessage 解它），拆开再找
    if (typeof o[k] === 'string' && o[k].length < 4000 && /^[\s]*[{[]/.test(o[k])) {
      try { const r = findKey(JSON.parse(o[k]), key, depth - 1); if (r !== undefined) return r } catch (e) { }
    }
    const r = findKey(o[k], key, depth - 1)
    if (r !== undefined) return r
  }
  return undefined
}

/* ---------------- 黑匣子：把最近几次收到的请求录下来，主公一点校验就能看真相 ---------------- */
async function record(env, entry) {
  try {
    let arr = []
    const raw = await env.SHEZHANG.get('dbg')
    if (raw) { const a = JSON.parse(raw); if (Array.isArray(a)) arr = a }
    arr.unshift(entry)
    await env.SHEZHANG.put('dbg', JSON.stringify(arr.slice(0, 10)))
  } catch (e) { /* 记录失败不影响主流程 */ }
}

async function handleQQ(request, env) {
  const body = request.method === 'POST' ? await request.text() : ''
  const url = new URL(request.url)
  let payload = null
  if (body) {
    try { payload = JSON.parse(body) } catch (e) { payload = null }
  }

  // 无论成不成都先记账：来了什么、从哪来、我们怎么回的
  await record(env, {
    at: Date.now(),
    method: request.method,
    path: url.pathname,
    ua: request.headers.get('user-agent') || '',
    appid: request.headers.get('x-bot-appid') || '',
    query: url.search.slice(0, 300),
    body: body.slice(0, 600),
  })

  // QQ 首次配置回调时的验证请求：要回 {plain_token, signature}
  // 兼容三处来源：JSON 任意层级（含 d 是 JSON 字符串）、URL query（GET 探活时只有 query）
  let pt = findKey(payload, 'plain_token', 5)
  let ets = findKey(payload, 'event_ts', 5)
  if (pt === undefined || pt === null) pt = url.searchParams.get('plain_token')
  if (ets === undefined || ets === null) ets = url.searchParams.get('event_ts')
  if (pt !== undefined && pt !== null && ets !== undefined && ets !== null) {
    const msg = String(ets) + String(pt)
    let sig = ''
    try { sig = await signHex(msg) } catch (e) { sig = 'ERR:' + String(e) }
    console.log('[shezhang] 验证 pt=' + String(pt).slice(0, 6) + ' sig=' + (sig ? sig.slice(0, 16) : '空'))
    return json({ plain_token: String(pt), signature: sig })
  }

  if (!payload) {
    return json({ code: 400, msg: 'body 不是合法 JSON' }, 400)
  }

  const ev = parseEvent(payload)
  if (!ev) return json({ code: 0, msg: '非消息事件，忽略' })

  // 只收关注群（没配 WATCH_GROUPS 就全收）
  const watch = (env.WATCH_GROUPS || '').split(',').map(s => s.trim()).filter(Boolean)
  if (watch.length && watch.every(w => ev.gid.indexOf(w) < 0)) {
    return json({ code: 0, msg: '不在关注列表', gid: ev.gid })
  }

  const title = scrub(ev.text)
  if (!title) return json({ code: 0, msg: '脱敏后为空，丢弃' })

  // 普通事件也验一次签（用 PUBKEY）；验不过就拒（配了 PUBKEY 才验）
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
    level: 'P2',   // 群消息默认二级（跑 regrade 可再细化）
    ddl: '',
    status: 'open',
  }
  await kvPut(env, 'qq', item.id, item)
  await bumpIndex(env, 'qq', item)
  return json({ code: 0, msg: '已入库', id: item.id })
}

async function handleSync(request, env) {
  const admin = env.ADMIN_TOKEN || ''
  if (!admin) return json({ code: 500, msg: '服务端没配 ADMIN_TOKEN' }, 500)
  const auth = request.headers.get('authorization') || ''
  if (auth !== 'Bearer ' + admin) return json({ code: 401, msg: 'token 不对' }, 401)

  let payload
  try { payload = JSON.parse(await request.text()) } catch (e) {
    return json({ code: 400, msg: 'body 不是合法 JSON' }, 400)
  }
  const items = Array.isArray(payload.items) ? payload.items : []
  let ok = 0
  for (const it of items) {
    if (!it || !it.id) continue
    it.title = String(it.title || '').slice(0, 200)
    if (!it.ts) it.ts = Date.now()
    await kvPut(env, 'ann', it.id, it)
    await bumpIndex(env, 'ann', it)
    ok++
  }
  return json({ code: 0, msg: '同步完成', count: ok })
}

async function readList(env, kind, limit, cors) {
  const list = await listItems(env, kind, limit)
  return json({ count: list.length, items: list, ts: Date.now() }, 200, cors ? {} : { 'Access-Control-Allow-Origin': 'no' })
}

/* 手机上一眼看懂的页面（服务端渲染，token 只留在地址栏，页面源码里不出现） */
function pageResponse(env, kind, limit) {
  return (async () => {
    const items = await listItems(env, kind, limit)
    if (kind === 'qq') {
      return new Response(pageHtml('qq', items, '蛇杖一号 · 班群消息',
        '只给你看 · 已脱敏 · 手机也能打开'), {
        headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' },
      })
    }
    return new Response(pageHtml('ann', items, '蛇杖一号 · 公告',
      '公开 · 可转给同学 · 与网页版同源'), {
      headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' },
    })
  })()
}

function homeHtml() {
  return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width,initial-scale=1"><title>蛇杖一号</title>' +
    '<style>body{margin:0;display:flex;place-items:center;min-height:100vh;background:#f6f7f9;' +
    'font:15px/1.7 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color:#1f2328}' +
    '.box{background:#fff;border:1px solid #e6e8eb;border-radius:14px;padding:26px 30px;text-align:center}' +
    'a{display:block;margin:7px 0;color:#5b8def;text-decoration:none}' +
    'a:hover{text-decoration:underline}.tip{font-size:12px;color:#6b7280;margin-top:14px}</style></head>' +
    '<body><div class="box"><b>蛇杖一号</b>' +
    '<a href="/ann">📢 公告（公开，可转同学）</a>' +
    '<a href="/qq">💬 班群消息（只有你能看）</a>' +
    '<p class="tip">群消息入口需要带口令；公告入口点开就是列表，不用看 JSON。</p>' +
    '</div></body></html>'
}

/* ---------------- 入口 ---------------- */
export default {
  async fetch(request, env, ctx) {
    _env = env
    const url = new URL(request.url)
    const p = url.pathname.replace(/\/+$/, '') || '/'

    if (request.method === 'OPTIONS') return new Response(null, { status: 204, headers: CORS })
    if (request.method === 'GET' && (p === '/' || p === '/home')) {
      return new Response(homeHtml(), {
        headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' },
      })
    }
    // 手机页面：/qq 要口令（READ_TOKEN，或 ADMIN_TOKEN 也认），/ann 直接看
    if (request.method === 'GET' && p === '/qq') {
      const t = url.searchParams.get('t') || ''
      const rt = env.READ_TOKEN || ''
      if (rt && t !== rt && t !== (env.ADMIN_TOKEN || '')) {
        return json({ code: 401, msg: '页面口令不对，链接复制全一点' }, 401)
      }
      return pageResponse(env, 'qq', Math.min(Number(url.searchParams.get('limit')) || 200, 200))
    }
    if (request.method === 'GET' && p === '/ann') {
      return pageResponse(env, 'ann', Math.min(Number(url.searchParams.get('limit')) || 200, 200))
    }
    if (request.method === 'GET' && p === '/api/health') {
      return json({
        ok: true,
        has_secret: !!env.BOT_SECRET,
        has_pubkey: !!env.PUBKEY,
        has_kv: !!env.SHEZHANG,
        now: Date.now(),
      })
    }
    if (request.method === 'GET' && p === '/api/dbg') {
      // 排错专用：验 BOT_SECRET 到底有没有进到 Worker（只报长度，不漏明文）
      const t = url.searchParams.get('t') || ''
      if (t !== (env.READ_TOKEN || '') && t !== (env.ADMIN_TOKEN || '')) {
        return json({ code: 401, msg: '口令不对' }, 401)
      }
      const probeMsg = '1730000000PROBETOKEN'
      let sig = ''
      try { sig = await signHex(probeMsg) } catch (e) { sig = 'ERR:' + String(e) }
      let log = []
      try { log = JSON.parse(await env.SHEZHANG.get('dbg') || '[]') } catch (e) { log = [] }
      return json({
        has_secret: !!env.BOT_SECRET,
        secret_len: String(env.BOT_SECRET || '').length,
        has_pubkey: !!env.PUBKEY,
        pubkey_len: String(env.PUBKEY || '').length,
        has_kv: !!env.SHEZHANG,
        probe_msg: probeMsg,
        probe_sig: sig,
        qq_index_len: (await readIndex(env, 'qq')).length,
        last_requests: log.slice(0, 5),
      })
    }
    if (request.method === 'GET' && p === '/api/ann') {
      return readList(env, 'ann', Math.min(Number(url.searchParams.get('limit')) || 300, 1000), true)
    }
    if (request.method === 'GET' && p === '/api/qq') {
      const rt = env.READ_TOKEN || ''
      if (rt && url.searchParams.get('token') !== rt) {
        return json({ code: 401, msg: 'token 不对' }, 401)
      }
      return readList(env, 'qq', Math.min(Number(url.searchParams.get('limit')) || 200, 1000), false)
    }
    // QQ 入口：POST 主通道；GET 也收（有些平台用 GET 探回调地址，别让它撞 404）
    if ((p === '/' || p === '/webhook') && request.method !== 'OPTIONS') {
      return handleQQ(request, env)
    }
    if (request.method === 'POST' && p === '/sync') {
      return handleSync(request, env)
    }
    return json({ code: 404, msg: '没有这个接口', path: p }, 404)
  },
}
