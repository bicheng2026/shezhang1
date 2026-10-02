// 蛇杖一号 · QQ 机器人 Webhook 接收端（微信云开发云函数，URL 化）
// <CF_API_TOKEN>--------------------------
// 这个函数的意义：QQ 官方机器人支持「Webhook 回调」——腾讯服务器会主动把群
// 消息 HTTP POST 到这里，不需要任何常驻进程、不需要手机开机、不需要自己守 WebSocket。
// 主公 2026-10-02 定的需求（不弹窗、不实时、接收归一处、展示放小程序）用它最贴。
//
// 官方依据：https://bot.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/webhook.html
//   回调地址端口仅允许 80 / 443 / 8080 / 8443，且必须是 HTTPS。
//
// 环境变量（云函数 → 配置 → 环境变量）：
//   BOT_SECRET  必须。QQ 机器人后台的 AppSecret，用于 Ed25519 验签/应答。
//   DEBUG_RAW   设 1 会把每条原始 payload 存进 shezhang_raw，用来摸清事件结构。
//   WATCH_GROUPS 可选。只收指定群，逗号分隔，如 "AbCd1234,Eff4567"。
//
// 后台配置：QQ 开放平台 → 机器人 → 开发设置 → 事件订阅 / 回调地址
//   连接方式选「Webhook（HTTP 回调）」，回调地址填本函数 URL 化的 HTTPS 地址。
// <CF_API_TOKEN>--------------------------

const crypto = require('crypto')
const cloud = require('wx-server-sdk')
cloud.init({ env: cloud.DYNAMIC_CURRENT_ENV })
const db = cloud.database()
const COLL = 'shezhang_items'
const RAW = 'shezhang_raw'

/* ---------------- Ed25519 签名（QQ 用这套，不是 HMAC） ----------------
   官方 Go 参考实现：
     seed = botSecret，长度不足 32 就重复自身直到 >= 32，再截断前 32 字节
     privateKey = ed25519.NewKeyFromSeed(seed)
     msg = event_ts + plain_token（字符串拼接，先 ts 后 token）
     signature = hex(ed25519.Sign(privateKey, msg))                        */
function ed25519SignHex(secret, msgStr) {
  let s = String(secret || '')
  if (!s) throw new Error('没配 BOT_SECRET')
  while (Buffer.byteLength(s, 'utf8') < 32) s += String(secret)
  let seed = Buffer.from(s.slice(0, 32), 'utf8')

  // 拼 PKCS#8 前缀：302e020100300506032b657004220420 + 32 字节 seed
  const der = Buffer.concat([Buffer.from('302e020100300506032b657004220420', 'hex'), seed])
  const key = crypto.createPrivateKey({ key: der, format: 'der', type: 'pkcs8' })
  return crypto.sign(null, Buffer.from(msgStr, 'utf8'), key).toString('hex')
}

/* ---------------- 脱敏（对齐 bridge.py 的 scrub_qq） ----------------
   顺序必须 身份证18 → 手机11 → 学号10-12，反了手机号会被当学号吃掉。 */
function scrub(t) {
  if (!t) return ''
  t = String(t)
    .replace(/<faceType=[^>]{0,160}>/g, '')
    .replace(/<@!?\d+>/g, '')
    .replace(/<face\w*[^>]*$/g, '')
  t = t.replace(/\b\d{17}[\dXx]\b/g, '[本人]')      // 身份证
  t = t.replace(/\b1[3-9]\d{9}\b/g, '[手机]')       // 手机号
  t = t.replace(/\b\d{10,12}\b/g, '[学号]')         // 学号（最后）
  return t.trim()
}

/* ---------------- 通用取值：不同事件的字段名不一致，按优先级试 ---------------- */
function pick(obj, keys) {
  for (const k of keys) {
    if (obj && obj[k] !== undefined && obj[k] !== null && obj[k] !== '') return obj[k]
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
  const gid = deepPick(d, ['group_id', 'group_openid', 'channel_id', 'guild_id'], 3)
  const mid = deepPick(d, ['msg_id', 'id', 'event_id'], 3)
  const ts = deepPick(d, ['event_ts', 'ts', 'timestamp'], 3)
  const nick = deepPick(d, ['user_name', 'nick', 'nickname', 'user_nick'], 3)
  return { text: String(text), gid: String(gid || ''), mid: String(mid || ''), ts: Number(ts) || Date.now(), nick: String(nick || '') }
}

/* ---------------- 主流程 ---------------- */
exports.main = async (event, context) => {
  const method = (event.httpMethod || 'POST').toUpperCase()
  if (method !== 'POST') {
    return { statusCode: 405, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: 405, msg: '只接受 POST' }) }
  }
  let body = event.body
  if (typeof body === 'object') body = JSON.stringify(body)
  let payload
  try {
    payload = JSON.parse(body)
  } catch (e) {
    return { statusCode: 400, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: 400, msg: 'body 不是合法 JSON' }) }
  }

  const secret = process.env.BOT_SECRET || ''
  const d = payload.d || {}

  // ---- 1. 回调地址验证请求：必须按 Ed25519 回 plain_token + signature ----
  const plainToken = d.plain_token
  const eventTs = d.event_ts
  if (plainToken !== undefined && eventTs !== undefined) {
    let sig = ''
    try {
      sig = ed25519SignHex(secret, String(eventTs) + String(plainToken))
    } catch (e) {
      // 验签失败也要回结构，但签名空着——平台会记告警
      sig = ''
    }
    return {
      statusCode: 200,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ plain_token: String(plainToken), signature: sig })
    }
  }

  // ---- 2. 普通事件 ----
  if (process.env.DEBUG_RAW === '1') {
    try {
      await db.collection(RAW).add({ data: { ts: Date.now(), payload: payload } })
    } catch (e) { /* 调试不影响主流程 */ }
  }

  const ev = parseEvent(payload)
  if (!ev) {
    return { statusCode: 200, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: 0, msg: '非消息事件，忽略' }) }
  }

  // 只收指定群（没配 WATCH_GROUPS 就全收）
  const watch = (process.env.WATCH_GROUPS || '').split(',').map(s => s.trim()).filter(Boolean)
  if (watch.length && watch.every(w => ev.gid.indexOf(w) < 0)) {
    return { statusCode: 200, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: 0, msg: '不在关注列表', gid: ev.gid }) }
  }

  const title = scrub(ev.text)
  if (!title) {
    return { statusCode: 200, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: 0, msg: '脱敏后为空，丢弃' }) }
  }

  const item = {
    id: 'qq_' + ev.mid,
    source: 'qq',
    sender: ev.gid ? 'QQ群 ' + ev.gid.slice(0, 10) : 'QQ群',
    title: title.slice(0, 200),
    ts: ev.ts,
    level: 'P2',          // 群消息默认二级；跑 regrade 可再细化
    ddl: '',
    status: 'open'
  }

  try {
    const exist = await db.collection(COLL).where({ id: item.id }).get()
    if (exist && exist.data && exist.data.length) {
      return { statusCode: 200, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: 0, msg: '已存在，跳过' }) }
    }
    await db.collection(COLL).add({ data: item })
    return { statusCode: 200, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: 0, msg: '已入库', id: item.id }) }
  } catch (e) {
    return { statusCode: 500, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: 500, msg: '写库失败', err: String(e && e.message) }) }
  }
}
