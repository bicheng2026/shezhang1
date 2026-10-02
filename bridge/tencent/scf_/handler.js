/**
 * 蛇杖一号 · 腾讯云云函数业务处理（SCF Web 函数 / 函数 URL）
 * <CF_API_TOKEN>--------------------------
 * 接收 QQ 官方机器人 Webhook 回调（公网 HTTPS）→ 验签 → 脱敏 → 落库 → 提供手机页面。
 * 入口形如 https://{APPID}-{随机}.{region}.tencentscf.com（函数 URL，免备案、大陆可连通）
 *
 * 🔴 三个「必须这么写」的坑（都是线上实测踩出来的，改之前先读）：
 *
 * 1) **Node 运行时没有 WebCrypto 的 Ed25519**。
 *    SCF 的 Nodejs16.13：globalThis.crypto 是 undefined；require('crypto').webcrypto.subtle
 *    对 Ed25519 / Ed448 / X25519 **全部 NotSupportedError**。
 *    但**原生 crypto 模块支持**（generateKeyPairSync('ed25519') + sign 正常，签名 64 字节）。
 *    → Ed25519 一律走原生 createPrivateKey / createPublicKey，WebCrypto 只当回退。
 *
 * 2) **环境变量在 process.env**（不是 Web 平台的全局 env 对象）。
 *
 * 3) **存储层当前是内存 Map 兜底，重启丢数据**。生产要绑 CloudBase KV，
 *    换掉 KV() 里的实现即可（已留 setKV 注入口）。
 *
 * 路由：
 *   POST /          QQ Webhook 入口（/qq、/webhook 也认）
 *   POST /sync      本机推公告（Bearer ADMIN_TOKEN）
 *   GET  /health    自检
 *   GET  /probe     运行时能力探测（排错用）
 *   GET  /ann       公告 JSON（公开，带 CORS）
 *   GET  /qq?token= 群消息 JSON（私有，不开 CORS）
 *   GET  /page?kind=ann|qq&t=   手机页面（HTML）
 *   GET  /dbg?t=    排错口（含黑匣子）
 */

'use strict'
const _nc = require('crypto')

const CORS = {
  'Content-Type': 'application/json; charset=utf-8',
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type, Authorization',
  'Access-Control-Max-Age': '86400',
}
const HTML_HEADERS = { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' }

const IDX = { qq: 'idx_q', ann: 'idx_a' }
const IDX_MAX = 200
const PKCS8_PREFIX = '302e020100300506032b657004220420'   // Ed25519 私钥 pkcs8 头
const SPKI_PREFIX = '302a300506032b6570032100'           // Ed25519 公钥 spki 头

/* ==================== 存储层（待接 CloudBase KV） ==================== */
const _mem = new Map()
let _kv = null
function KV() {
  if (_kv) return _kv
  return {
    async get(k) { return _mem.has(k) ? _mem.get(k) : null },
    async put(k, v) { _mem.set(k, String(v)) },
    async list({ prefix, limit }) {
      return { keys: [..._mem.keys()].filter(x => x.startsWith(prefix)).sort().slice(0, limit).map(name => ({ name })) }
    },
  }
}
function setKV(o) { _kv = o }

async function kvPut(kind, id, obj) {
  const prefix = kind === 'ann' ? 'a/' : 'q/'
  const tail = (kind === 'ann' ? '' : 'qq_') + id
  const key = prefix + String(Date.now()) + '-' + tail.replace(/[^\w.-]/g, '_')
  await KV().put(key, JSON.stringify(obj))
  return key
}
async function bumpIndex(kind, item) {
  const key = IDX[kind]
  let arr = []
  try { const raw = await KV().get(key); arr = raw ? JSON.parse(raw) : [] } catch (e) { arr = [] }
  if (!Array.isArray(arr)) arr = []
  arr = arr.filter(x => x && x.id !== item.id)
  arr.push(item)
  arr.sort((a, b) => (Number(b.ts) || 0) - (Number(a.ts) || 0))
  arr = arr.slice(0, IDX_MAX)
  await KV().put(key, JSON.stringify(arr))
  return arr.length
}
async function readIndex(kind) {
  try {
    const raw = await KV().get(IDX[kind])
    const arr = raw ? JSON.parse(raw) : []
    return Array.isArray(arr) ? arr : []
  } catch (e) { return [] }
}
async function listItems(kind, limit) {
  let list = await readIndex(kind)
  if (!list.length) {
    const prefix = kind === 'ann' ? 'a/' : 'q/'
    try {
      const res = await KV().list({ prefix, limit: 50 })   // 只逐条取 50 条，别撞调用上限
      const keys = (res && (res.keys || res.list)) || []
      for (const k of keys) {
        const name = typeof k === 'string' ? k : k.name
        const raw = await KV().get(name)
        if (raw) { try { list.push(JSON.parse(raw)) } catch (e) { } }
      }
    } catch (e) { }
  }
  list = list.slice().sort((a, b) => (Number(b.ts) || 0) - (Number(a.ts) || 0))
  return list.slice(0, Math.min(limit, IDX_MAX))
}

/* ==================== 脱敏（顺序不能反：身份证 → 手机 → 学号） ==================== */
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

/* ==================== 取值 ==================== */
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

/* ==================== Ed25519（走原生 crypto，见文件头坑 1） ==================== */
function hexClean(h) {
  const s = String(h || '').replace(/^0x/, '').trim()
  return s.length % 2 === 0 ? s : s.slice(0, -1)
}
function seedFromSecret(secret) {
  // 官方 Go 做法：botSecret 循环补到 ≥32 字节，再截前 32
  let seed = String(secret || '')
  while (Buffer.byteLength(seed, 'utf8') < 32) seed += String(secret || '')
  return Buffer.from(seed.slice(0, 32), 'utf8')
}
function privateKey(secret) {
  const der = Buffer.concat([Buffer.from(PKCS8_PREFIX, 'hex'), seedFromSecret(secret)])
  return _nc.createPrivateKey({ key: der, format: 'der', type: 'pkcs8' })
}
function publicKeyFromRaw(hex) {
  const der = Buffer.concat([Buffer.from(SPKI_PREFIX, 'hex'), Buffer.from(hexClean(hex), 'hex')])
  return _nc.createPublicKey({ key: der, format: 'der', type: 'spki' })
}
async function signHex(msgStr) {
  const secret = process.env.BOT_SECRET
  if (!secret) throw new Error('没配 BOT_SECRET')
  // 原生优先（Node16 的 WebCrypto 不支持 Ed25519）
  try {
    return _nc.sign(null, Buffer.from(String(msgStr), 'utf8'), privateKey(secret)).toString('hex')
  } catch (e) {
    console.log('[shezhang] 原生签名失败，试 WebCrypto：' + String((e && e.message) || e))
  }
  // 回退 WebCrypto（私钥必须 pkcs8 包装，raw seed 实测 import 会失败）
  const subtle = _nc.webcrypto && _nc.webcrypto.subtle
  if (!subtle) throw new Error('这个运行时既不能用原生 Ed25519，也没有 WebCrypto')
  const der = new Uint8Array(Buffer.concat([Buffer.from(PKCS8_PREFIX, 'hex'), seedFromSecret(secret)]))
  const key = await subtle.importKey('pkcs8', der, { name: 'Ed25519' }, false, ['sign'])
  const sig = await subtle.sign({ name: 'Ed25519' }, key, Buffer.from(String(msgStr), 'utf8'))
  return Buffer.from(sig).toString('hex')
}
async function verifyHex(hexSig, msgStr) {
  const pubHex = process.env.PUBKEY
  if (!pubHex) return false
  try {
    return _nc.verify(null, Buffer.from(String(msgStr), 'utf8'),
      publicKeyFromRaw(pubHex), Buffer.from(hexClean(hexSig), 'hex'))
  } catch (e) {
    console.log('[shezhang] 原生验签失败，试 WebCrypto：' + String((e && e.message) || e))
  }
  const subtle = _nc.webcrypto && _nc.webcrypto.subtle
  if (!subtle) return false
  const key = await subtle.importKey('raw', Buffer.from(hexClean(pubHex), 'hex'),
    { name: 'Ed25519' }, false, ['verify'])
  return await subtle.verify({ name: 'Ed25519' }, key,
    Buffer.from(hexClean(hexSig), 'hex'), Buffer.from(String(msgStr), 'utf8'))
}

/* ==================== 页面渲染 ==================== */
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
    '<li class="row"><div class="meta">' + levelTag(it.level) +
      '<span class="time">' + esc(fmtBj(it.ts)) + '</span>' +
      '<span class="src">' + esc(it.sender || it.source || '') + '</span></div>' +
      '<div class="body">' + esc(it.title).replace(/\n/g, '<br>') + '</div>' +
      (it.ddl ? '<div class="ddl">截止 ' + esc(it.ddl) + '</div>' : '') + '</li>'
  )).join('')
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
    (rows || '<li class="empty">还没有内容 —— ' + esc(tip) + '</li>') + '</ul></body></html>'
}


/* ==================== 提纯：只留「有信息量」的消息（跟本机 pull_qq.py 同一套规则） ==================== */
const KEEP_PATTERNS = [
  /(截止|截至|deadline|ddl|前交|前发|交材料|提交|报名)/, /(考试|测验|补考|重修|成绩|绩点|学分)/,
  /(讲座|培训|会议|开会|活动|比赛|竞赛|答辩| seminar| workshop)/i, /(放假|调休|上课|停课|补课|校历|作息|早八)/,
  /(通知|公告|提醒|请注意|务必|一定要|别忘|记得)/, /(附件|文件|资料|链接|二维码|见附件)/,
  /(报名|招募|志愿者|班委|团委|学生会|社团)/,
]
const DROP_RE = /^(哈+|呵+|嗯+|哦+|啊+|草|6|666|顶|沙发|签到|打卡|早|晚安|午安|收到|好的?|OK|ok|谢谢|thx|谢了|图|表情)$/i

function isMerit(t) {
  if (!t || t.length < 4) return null
  const s = t.trim()
  if (DROP_RE.test(s)) return null
  for (const re of KEEP_PATTERNS) if (re.test(s)) return 'info'
  return null
}
function digestText(t) {
  let s = String(t)
  s = s.replace(/@[\w\u4e00-\u9fa5]{1,12}\s*/g, '')
  s = s.replace(/(?:[哈呵嗯哦啊]{2,}|[!！?？。]{2,})/g, ' ')
  s = s.replace(/(收到|好的?|谢谢|麻烦了|辛苦了|各位|同学们|同学们好)+/g, ' ')
  s = s.replace(/\s{2,}/g, ' ').trim().replace(/^[-—·:：\s]+|[-—·:：\s]+$/g, '')
  if (s.length > 120) s = s.slice(0, 120) + '…'
  return s
}


/* ==================== 云端直接写 GitHub（0 元持久化，关键！）
   为什么：云函数内存必丢，但 **GitHub 仓库是持久的**，
   而且同学看的就是 GitHub Pages —— 云端直接写进去，等于「云端就是数据库」。
   电脑关着也不丢（云端 24h 在岗），且 GitHub 本身 0 元。
   需要环境变量：GH_TOKEN / GH_REPO / GH_BRANCH / GH_PATH（都在控制台配，不进代码）。 */
const GH_API = 'https://api.github.com'

async function ghGetFile(path, token) {
  const url = GH_API + '/repos/' + process.env.GH_REPO + '/contents/' + path
  const r = await httpJson('GET', url, {
    'User-Agent': 'shezhang-bot', 'Accept': 'application/vnd.github+json',
    'Authorization': 'token ' + token,
  }, null, 20000)
  if (r.status !== 200) return null
  try {
    const j = JSON.parse(r.body)
    return { sha: j.sha, content: Buffer.from(j.content || '', 'base64').toString('utf8') }
  } catch (e) { return null }
}

async function ghPutFile(path, token, content, sha, message) {
  const url = GH_API + '/repos/' + process.env.GH_REPO + '/contents/' + path
  const payload = JSON.stringify({
    message: message || 'chore: 群消息摘要自动更新',
    content: Buffer.from(content, 'utf8').toString('base64'),
    branch: process.env.GH_BRANCH || 'main',
    ...(sha ? { sha } : {}),
  })
  const r = await httpJson('PUT', url, {
    'User-Agent': 'shezhang-bot', 'Accept': 'application/vnd.github+json',
    'Authorization': 'token ' + token, 'Content-Type': 'application/json',
  }, payload, 25000)
  return { status: r.status, body: String(r.body).slice(0, 300) }
}

/* 把一条群消息提纯后写进 GitHub 的摘要文件（每次覆盖，保留最近 30 条） */
async function publishToGithub(item) {
  const token = process.env.GH_TOKEN
  const path = process.env.GH_PATH || 'data/qq_digest.json'
  if (!token || !process.env.GH_REPO) return { skipped: '没配 GH_TOKEN / GH_REPO' }

  const cur = await ghGetFile(path, token)
  let obj = { note: '本内容由班委助手自动整理自班群消息（已脱敏：不含姓名/学号/手机号，仅保留有信息量的通知类内容）。如需原文请在班群内翻记录。', count: 0, items: [] }
  if (cur) {
    try { obj = JSON.parse(cur.content) } catch (e) { }
  }
  const text = digestText(item.title)
  // 幂等：同 id 不重复加
  const items = (obj.items || []).filter(x => x.id !== item.id)
  items.unshift({
    id: item.id,
    time: new Date(item.ts).toISOString(),
    timeText: fmtBj(item.ts).slice(5),
    text: text,
  })
  obj.items = items.slice(0, 30)
  obj.count = obj.items.length
  obj.updated = item.ts
  obj.updated_text = fmtBj(item.ts)
  const r = await ghPutFile(path, token, JSON.stringify(obj, null, 2), cur && cur.sha,
    'chore: 班群摘要 +' + String(item.id).slice(-8))
  return r
}

/* ==================== 黑匣子 ==================== */
async function record(entry) {
  try {
    let arr = []
    const raw = await KV().get('dbg')
    if (raw) { const a = JSON.parse(raw); if (Array.isArray(a)) arr = a }
    arr.unshift(entry)
    await KV().put('dbg', JSON.stringify(arr.slice(0, 10)))
  } catch (e) { }
}

/* ==================== 工具 ==================== */
function parseQuery(qs) {
  const out = {}
  const s = String(qs || '').replace(/^\?/, '')
  if (!s) return out
  for (const kv of s.split('&')) {
    if (!kv) continue
    const i = kv.indexOf('=')
    const k = i < 0 ? kv : kv.slice(0, i)
    const v = i < 0 ? '' : kv.slice(i + 1)
    try { out[decodeURIComponent(k)] = decodeURIComponent(v) } catch (e) { out[k] = v }
  }
  return out
}
function headerOf(headers, name) {
  for (const k in headers) {
    if (String(k).toLowerCase() === name) {
      const v = headers[k]
      return Array.isArray(v) ? v.join(',') : (v || '')
    }
  }
  return ''
}

/* ==================== 业务：QQ 回调 ==================== */
async function handleQQ(event, body, qs) {
  let payload = null
  if (body) { try { payload = JSON.parse(body) } catch (e) { payload = null } }

  const headers = event.headers || {}
  await record({
    at: Date.now(),
    method: event.httpMethod,
    path: event.path,
    ua: headerOf(headers, 'user-agent'),
    appid: headerOf(headers, 'x-bot-appid'),
    query: event.queryString || '',
    body: String(body || '').slice(0, 600),
  })

  // 回调验证：回 {plain_token, signature}
  // ⚠️ 查不到时是 null 不是 undefined（不是 undefined），判空必须用 != null
  let pt = findKey(payload, 'plain_token', 5)
  let ets = findKey(payload, 'event_ts', 5)
  if (pt === undefined || pt === null) pt = qs.plain_token
  if (ets === undefined || ets === null) ets = qs.event_ts
  if (pt !== undefined && pt !== null && ets !== undefined && ets !== null) {
    let sig = ''
    try { sig = await signHex(String(ets) + String(pt)) } catch (e) { sig = 'ERR:' + String((e && e.message) || e) }
    console.log('[shezhang] 回调验证 pt=' + String(pt).slice(0, 8) + ' sig=' + (sig || '').slice(0, 16))
    return { plain_token: String(pt), signature: sig }
  }

  if (!payload) return { _status: 400, code: 400, msg: 'body 不是合法 JSON' }

  const ev = parseEvent(payload)
  if (!ev) return { code: 0, msg: '非消息事件，忽略' }

  const watch = String(process.env.WATCH_GROUPS || '').split(',').map(s => s.trim()).filter(Boolean)
  if (watch.length && watch.every(w => ev.gid.indexOf(w) < 0)) {
    return { code: 0, msg: '不在关注列表', gid: ev.gid }
  }

  const title = scrub(ev.text)
  if (!title) return { code: 0, msg: '脱敏后为空，丢弃' }

  const sigHeader = headerOf(headers, 'x-signature') || headerOf(headers, 'signature')
  if (process.env.PUBKEY && sigHeader) {
    const ok = await verifyHex(sigHeader.trim(), String(ev.ts) + body)
    if (!ok) return { _status: 403, code: 403, msg: '验签不过' }
  }

  const item = {
    id: 'qq_' + ev.mid,
    source: 'qq',
    sender: ev.gid ? 'QQ群 ' + ev.gid.slice(0, 10) : 'QQ群',
    title: title.slice(0, 200),
    ts: ev.ts,
    level: 'P2', ddl: '', status: 'open',
  }
  await kvPut('qq', item.id, item)
  await bumpIndex('qq', item)

  // 0 元持久化：云端直接写 GitHub（同学看的就是 Pages，等于云端即数据库）
  let gh = null
  if (process.env.GH_TOKEN && process.env.GH_REPO) {
    const worth = isMerit(title)
    if (worth) {
      try { gh = await publishToGithub(item) } catch (e) { gh = { err: String((e && e.message) || e) } }
    } else {
      gh = { skipped: '无信息量，不进摘要' }
    }
  }
  return { code: 0, msg: '已入库', id: item.id, gh: gh || undefined }
}

/* ==================== 业务：公告推送 ==================== */
async function handleSync(event, body) {
  const admin = process.env.ADMIN_TOKEN || ''
  if (!admin) return { _status: 500, code: 500, msg: '服务端没配 ADMIN_TOKEN' }
  if (headerOf(event.headers || {}, 'authorization') !== 'Bearer ' + admin) {
    return { _status: 401, code: 401, msg: 'token 不对' }
  }
  let payload
  try { payload = JSON.parse(body) } catch (e) { return { _status: 400, code: 400, msg: 'body 不是合法 JSON' } }
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
  return { code: 0, msg: '同步完成', count: ok }
}

/* ==================== 运行时探测（排错用） ==================== */
async function probeRuntime() {
  const out = { node: process.version, globalCrypto: false, webcrypto: false, algos: {}, nodeEd25519: '' }
  out.globalCrypto = !!(typeof globalThis !== 'undefined' && globalThis.crypto && globalThis.crypto.subtle)
  const wc = _nc.webcrypto && _nc.webcrypto.subtle
  out.webcrypto = !!wc
  if (wc) {
    for (const name of ['Ed25519', 'Ed448', 'X25519']) {
      try { await wc.importKey('raw', new Uint8Array(32), { name }, false, ['sign']); out.algos[name] = 'OK' }
      catch (e) { out.algos[name] = String((e && e.name) || e).slice(0, 50) }
    }
  }
  try {
    const kp = _nc.generateKeyPairSync('ed25519')
    out.nodeEd25519 = 'OK siglen=' + _nc.sign(null, Buffer.from('x'), kp.privateKey).length
  } catch (e) { out.nodeEd25519 = String((e && e.message) || e).slice(0, 80) }
  return out
}


/* ---------------- 出网探测 + 主动写 GitHub
   ⚠️ Node 16 没有全局 fetch（要 Node 18+），用 https 模块。
   如果这条通了，就能在**云端直接推 GitHub** → 电脑关着也不丢，且 0 元。 */
const _https = require('https')
const _http = require('http')

function httpGet(urlStr, headers, timeoutMs) {
  return new Promise((resolve) => {
    try {
      const u = new URL(urlStr)
      const lib = u.protocol === 'http:' ? _http : _https
      const req = lib.request(
        { hostname: u.hostname, port: u.port || (u.protocol === 'http:' ? 80 : 443),
          path: u.pathname + u.search, method: 'GET',
          headers: headers || {}, timeout: timeoutMs || 20000 },
        (res) => {
          let body = ''
          res.on('data', (d) => { body += d.toString() })
          res.on('end', () => resolve({ status: res.statusCode, body }))
        })
      req.on('error', (e) => resolve({ status: 0, body: 'ERR ' + String(e && e.message || e) }))
      req.on('timeout', () => { req.destroy(); resolve({ status: 0, body: 'TIMEOUT' }) })
      req.end()
    } catch (e) {
      resolve({ status: 0, body: 'ERR ' + String(e && e.message || e) })
    }
  })
}

function httpJson(method, urlStr, headers, payload, timeoutMs) {
  return new Promise((resolve) => {
    try {
      const u = new URL(urlStr)
      const lib = u.protocol === 'http:' ? _http : _https
      const data = payload == null ? null : Buffer.from(payload, 'utf8')
      const hd = Object.assign({}, headers || {})
      if (data) hd['Content-Length'] = data.length
      const req = lib.request(
        { hostname: u.hostname, port: u.port || (u.protocol === 'http:' ? 80 : 443),
          path: u.pathname + u.search, method, headers: hd, timeout: timeoutMs || 20000 },
        (res) => {
          let body = ''
          res.on('data', (d) => { body += d.toString() })
          res.on('end', () => resolve({ status: res.statusCode, body }))
        })
      req.on('error', (e) => resolve({ status: 0, body: 'ERR ' + String(e && e.message || e) }))
      req.on('timeout', () => { req.destroy(); resolve({ status: 0, body: 'TIMEOUT' }) })
      if (data) req.write(data)
      req.end()
    } catch (e) {
      resolve({ status: 0, body: 'ERR ' + String(e && e.message || e) })
    }
  })
}

async function probeOutbound() {
  const out = {}
  const g = await httpGet('https://api.github.com/rate_limit', {
    'User-Agent': 'shezhang-probe',
  })
  out.github = g.status
  out.github_body = String(g.body).slice(0, 120)
  const q = await httpGet('https://www.qq.com/', { 'User-Agent': 'probe' })
  out.qq = q.status
  return out
}


/* ==================== 管理员接口（后台按钮的中间层）====================
   为什么必须有这一层：**GitHub token 不能放前端**（放了就人人可读，等于仓库敞开）。
   云函数已经握着 GH_TOKEN，所以让它代写 data/access.json —— 前端只发口令，不碰 token。
   鉴权：管理员密码哈希比对（跟网页同一个算法：SHA-256(salt::pw)）。
   动作：
     gen  —— 生成随机口令、24h 后失效、标记 temp=true（对方首次登录必须改密码）
     lock —— 立即作废（锁死）
     open —— 关闭密码锁（任何人可进）
     adminpw —— 改管理员密码 */
async function adminApi(action, bodyObj) {
  const token = process.env.GH_TOKEN
  const repo = process.env.GH_REPO
  if (!token || !repo) return { _status: 500, code: 500, msg: '服务端没配 GH_TOKEN/GH_REPO' }

  const path = process.env.GH_PATH || 'data/qq_digest.json'
  const accPath = 'data/access.json'
  const salt = (bodyObj && bodyObj.salt) || 'sz1'

  // 1) 读现有 access.json
  const cur = await ghGetFile(accPath, token)
  let acc = { lock: true, salt: salt, admin: '', user: '', temp: false, issuedAt: 0, until: 0 }
  if (cur) { try { acc = JSON.parse(cur.content) } catch (e) { } }

  // 2) 鉴权（改口令时用旧口令；其余动作也要管理员口令）
  const given = (bodyObj && bodyObj.pw) || ''
  if (!acc.admin) return { _status: 403, code: 403, msg: 'access.json 里没有 admin 哈希，先手工填一次' }
  const h = _nc.createHash('sha256').update(salt + '::' + given).digest('hex')
  if (h !== acc.admin) return { _status: 401, code: 401, msg: '管理员口令不对' }

  const now = Date.now()
  let msg = ''
  if (action === 'gen') {
    const gen = 'sz' + _nc.randomBytes(3).toString('hex') + '-' + _nc.randomBytes(2).toString('hex')
    const until = now + 24 * 3600 * 1000
    acc.user = _nc.createHash('sha256').update(salt + '::' + gen).digest('hex')
    acc.temp = true
    acc.issuedAt = now
    acc.until = until
    acc.lock = true
    msg = gen
  } else if (action === 'lock') {
    acc.lock = true; acc.user = ''; acc.temp = false; acc.until = 0
    msg = 'locked'
  } else if (action === 'open') {
    acc.lock = false; acc.temp = false; acc.until = 0
    msg = 'open'
  } else if (action === 'check') {
    /* 只校验口令，不改任何状态（后台打开时用它，别拿 gen 去校验——
       gen 会真的重置全网口令，等于「看一眼后台」就把门锁上了） */
    return { action: 'check', ok: true, until: acc.until || 0,
             acc_public: { lock: acc.lock, temp: acc.temp, until: acc.until } }
  } else if (action === 'adminpw') {
    const np = (bodyObj && bodyObj.newpw) || ''
    if (np.length < 6) return { _status: 400, code: 400, msg: '新管理员口令至少 6 位' }
    acc.admin = _nc.createHash('sha256').update(salt + '::' + np).digest('hex')
    msg = 'adminpw-updated'
  } else {
    return { _status: 400, code: 400, msg: '未知动作' }
  }
  acc.salt = salt
  acc.note = '运行时口令配置。由管理员后台经云函数写入（token 不经过前端）。'

  const r = await ghPutFile(accPath, token, JSON.stringify(acc, null, 2), cur && cur.sha,
    'chore: 管理员更新全网口令')
  return { action: action, msg: msg, until: acc.until || 0, gh: r, acc_public: { lock: acc.lock, temp: acc.temp, until: acc.until } }
}

/* ==================== 入口 ==================== */
exports.main_handler = async (event) => {
  const path = String(event.path || '/').replace(/\/+$/, '') || '/'
  const method = String(event.httpMethod || 'GET').toUpperCase()
  const qs = parseQuery(event.queryString || '')
  let body = event.body || ''
  if (event.isBase64Encoded) {
    try { body = Buffer.from(body, 'base64').toString('utf8') } catch (e) { body = '' }
  }

  const ok = (obj, status) => ({
    statusCode: status || 200, headers: CORS,
    body: JSON.stringify(obj), isBase64Encoded: false,
  })
  const page = (html) => ({
    statusCode: 200, headers: HTML_HEADERS, body: html, isBase64Encoded: false,
  })

  try {
    if (method === 'OPTIONS') return { statusCode: 204, headers: CORS, body: '', isBase64Encoded: false }

    if (method !== 'GET' && (path === '/' || path === '/qq' || path === '/webhook')) {
      const r = await handleQQ(event, body, qs)
      if (r.plain_token !== undefined) return ok(r)          // 验证应答不带 _status
      return ok(r, r._status || 200)
    }
    if (method !== 'GET' && path.endsWith('/admin')) {
      let b2 = null
      try { b2 = JSON.parse(body) } catch (e) { b2 = null }
      const act = qs.action || (b2 && b2.action) || ''
      const r = await adminApi(act, b2 || {})
      return ok(r, r._status || 200)
    }
    if (method !== 'GET' && path === '/sync') {
      const r = await handleSync(event, body)
      return ok(r, r._status || 200)
    }
    if (method !== 'GET') return ok({ code: 404, msg: '没有这个接口', path }, 404)

    if (path === '/probe') return ok(await probeRuntime())
    if (path === '/out') return ok(await probeOutbound())

    if (path === '/health') {
      return ok({
        ok: true,
        has_secret: !!process.env.BOT_SECRET,
        has_pubkey: !!process.env.PUBKEY,
        has_admin: !!process.env.ADMIN_TOKEN,
        has_read: !!process.env.READ_TOKEN,
        node: process.version,
        now: Date.now(),
      })
    }

    if (path === '/dbg') {
      const t = qs.t || ''
      if (t !== (process.env.READ_TOKEN || '') && t !== (process.env.ADMIN_TOKEN || '')) {
        return ok({ code: 401, msg: '口令不对' }, 401)
      }
      let sig = ''
      try { sig = await signHex('1730000000PROBETOKEN') } catch (e) { sig = 'ERR:' + String((e && e.message) || e) }
      let log = []
      try { log = JSON.parse(await KV().get('dbg') || '[]') } catch (e) { log = [] }
      return ok({
        has_secret: !!process.env.BOT_SECRET,
        secret_len: String(process.env.BOT_SECRET || '').length,
        has_pubkey: !!process.env.PUBKEY,
        probe_sig: sig,
        qq_index_len: (await readIndex('qq')).length,
        ann_index_len: (await readIndex('ann')).length,
        last_requests: log.slice(0, 5),
      })
    }

    const isAnn = path === '/ann'
    const isQq = path === '/qq' || (path === '/page' && qs.kind === 'qq')
    if (isQq) {
      const t = qs.t || qs.token || ''
      if (t !== (process.env.READ_TOKEN || '') && t !== (process.env.ADMIN_TOKEN || '')) {
        return ok({ code: 401, msg: '口令不对' }, 401)
      }
    }
    if (!isAnn && !isQq && path !== '/page') {
      return page('<!doctype html><meta charset="utf-8"><title>蛇杖一号</title>' +
        '<body style="font:15px/1.8 -apple-system,\'PingFang SC\',sans-serif;padding:30px;max-width:640px">' +
        '<h2>蛇杖一号</h2><ul><li><a href="/page?kind=ann">公告（公开）</a></li>' +
        '<li><a href="/page?kind=qq&t=你的口令">班群消息（需要口令）</a></li>' +
        '<li><a href="/health">健康自检</a></li></ul></body>')
    }

    const kind = isQq ? 'qq' : 'ann'
    const items = await listItems(kind, Math.min(Number(qs.limit) || 200, 200))
    if (path === '/page') {
      return page(pageHtml(items,
        kind === 'qq' ? '蛇杖一号 · 班群消息' : '蛇杖一号 · 公告',
        kind === 'qq' ? '只给你看 · 已脱敏' : '公开 · 可转给同学'))
    }
    return {
      statusCode: 200,
      headers: kind === 'qq' ? { ...CORS, 'Access-Control-Allow-Origin': 'no' } : CORS,
      body: JSON.stringify({ count: items.length, items, ts: Date.now() }),
      isBase64Encoded: false,
    }
  } catch (e) {
    return ok({ code: 500, msg: '函数异常', err: String((e && e.message) || e) }, 500)
  }
}

exports._setKV = setKV
exports._mem = _mem
