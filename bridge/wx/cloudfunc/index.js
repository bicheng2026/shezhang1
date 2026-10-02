// 蛇杖一号 · 微信小程序云端接收函数
// <CF_API_TOKEN>--------------------
// 用途：把手机/电脑上跑好的桥接数据，推到云开发数据库，供小程序只读展示。
//
// 为什么必须用云开发：小程序的 wx.request 只能打「后台白名单里的 HTTPS 域名」。
// 自己买域名要备案（20 天起）+ 买证书 + 买服务器；用云开发的云函数 URL，
// 域名是微信自己的，天然在白名单里，不用备案、不用服务器。
//
// 部署：
//   1. 微信公众平台 → 云开发 → 云函数 → 新建「shezhangSync」
//   2. 把本文件整份放进云函数根目录（含 package.json）
//   3. 云函数「配置 → 函数 URL 化」开启，记下 URL
//   4. 权限：云数据库 shezhang_items 权限设为「所有用户可读，仅管理端可写」
//
// 安全：POST 带 token 校验，token 只在手机端 config.json 存一份，不上小程序。

const cloud = require('wx-server-sdk')
cloud.init({ env: cloud.DYNAMIC_CURRENT_ENV })
const db = cloud.database()

const TOKEN = process.env.SHEZHANG_TOKEN || ''
const COLL = 'shezhang_items'

function ok(msg, extra) {
  return Object.assign({ ok: true, msg: msg }, extra || {})
}
function bad(msg) {
  return { ok: false, msg: msg }
}

exports.main = async (event, context) => {
  // ---- 1. 鉴权 ----
  if (!TOKEN) return bad('服务端没配 SHEZHANG_TOKEN，拒绝写入')
  if (event.token !== TOKEN) return bad('token 不对')

  const action = event.action || 'sync'

  // ---- 2. 同步条目 ----
  if (action === 'sync') {
    const items = Array.isArray(event.items) ? event.items : []
    if (!items.length) return ok('无新条目')

    const coll = db.collection(COLL)
    let added = 0
    // 逐条 upsert：按 id 去重，重推不会重复
    for (const it of items) {
      if (!it || !it.id) continue
      try {
        const exist = await coll.where({ id: it.id }).get()
        if (exist && exist.data && exist.data.length) {
          await coll.doc(exist.data[0]._id).update({ data: it })
        } else {
          await coll.add({ data: it })
          added += 1
        }
      } catch (e) {
        // 单条失败不影响其余，返回时报告条数
      }
    }
    const ts = Date.now()
    await db.collection('shezhang_meta').where({ key: 'last_sync' }).get()
      .then(async res => {
        const payload = { key: 'last_sync', ts: ts }
        if (res && res.data && res.data.length) {
          await db.collection('shezhang_meta').doc(res.data[0]._id).update({ data: payload })
        } else {
          await db.collection('shezhang_meta').add({ data: payload })
        }
      }).catch(() => {})

    return ok('已同步', { total: items.length, added: added })
  }

  // ---- 3. 拉一下最新同步点（小程序冷启动用）----
  if (action === 'ping') {
    try {
      const r = await db.collection('shezhang_meta').where({ key: 'last_sync' }).get()
      return ok('在线', { ts: (r.data && r.data[0] && r.data[0].ts) || 0 })
    } catch (e) {
      return bad('库还没初始化')
    }
  }

  return bad('未知 action：' + action)
}
