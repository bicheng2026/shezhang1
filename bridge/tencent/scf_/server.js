/**
 * 蛇杖一号 · 腾讯云 Web 函数的 HTTP Server 外壳
 * <CF_API_TOKEN>--------------------------
 * 为什么需要这个文件：腾讯云 Web 函数不是直接调 main_handler，
 * 而是**启动一个监听 0.0.0.0:9000 的 Web Server**（内层 Nginx 转发过来的）。
 * scf_bootstrap 里 exec 的是本文件，本文件负责把请求转给业务 handler。
 *
 * 业务逻辑全在 ./handler.js 的 main_handler 里（与 API 网关版共用同一份）。
 *
 * 环境变量：
 *   SCF_PORT / SCF_RUN_TIME  腾讯云注入的端口（缺省 9000）
 *   BOT_SECRET / PUBKEY / ADMIN_TOKEN / READ_TOKEN / WATCH_GROUPS
 */
'use strict'

const http = require('http')
const { main_handler } = require('./handler.js')

const PORT = Number(process.env.SCF_PORT || process.env.SCF_RUN_TIME || 9000)
const HOST = '0.0.0.0'   // ⚠️ 官方要求必须是 0.0.0.0，不能是 127.0.0.1

function readBody(req) {
  return new Promise((resolve) => {
    const chunks = []
    let n = 0
    req.on('data', (c) => {
      n += c.length
      if (n > 6 * 1024 * 1024) { req.destroy(); return }   // body 上限 6MB
      chunks.push(c)
    })
    req.on('end', () => resolve(Buffer.concat(chunks)))
    req.on('error', () => resolve(Buffer.alloc(0)))
  })
}

const server = http.createServer(async (req, res) => {
  const buf = await readBody(req)
  const url = new URL(req.url, 'http://127.0.0.1')

  // 查询串去掉开头的 ?，SCF 的 event.queryString 不带问号
  const qs = url.search.replace(/^\?/, '')

  // ⚠️ header 可能重复（同名多个），SCF 会收成数组，这里拍平成逗号分隔
  const headers = {}
  for (const [k, v] of Object.entries(req.headers)) {
    headers[k] = Array.isArray(v) ? v.join(',') : v
  }

  const event = {
    path: url.pathname || '/',
    httpMethod: req.method,
    headers,
    queryString: qs,
    body: buf.toString('utf8'),
    isBase64Encoded: false,
  }

  let out
  try {
    out = await main_handler(event, {})
  } catch (e) {
    out = {
      statusCode: 500,
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify({ code: 500, msg: '函数异常', err: String((e && e.message) || e) }),
    }
  }

  const status = out.statusCode || 200
  const hd = out.headers || {}
  try {
    res.writeHead(status, hd)
    res.end(out.body == null ? '' : out.body)
  } catch (e) {
    try { res.end() } catch (e2) { }
  }
})

server.listen(PORT, HOST, () => {
  console.log('[shezhang] web server listening on ' + HOST + ':' + PORT)
})

// SCF 会要求进程常驻，别让它自己退出
process.on('SIGTERM', () => { try { server.close() } catch (e) { } })
process.on('uncaughtException', (e) => { console.error('[shezhang] uncaught', e) })
