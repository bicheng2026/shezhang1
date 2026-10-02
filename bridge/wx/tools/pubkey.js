/**
 * 蛇杖一号 · 算 QQ 机器人 Ed25519 的 raw 公钥（给 Cloudflare Worker 用）
 *
 * 用法（在本机 Termux / 电脑都行，需要 Node ≥16）：
 *     node bridge/wx/tools/pubkey.js 你的AppSecret
 *   或者：BOT_SECRET=你的AppSecret node bridge/wx/tools/pubkey.js
 *
 * 输出一行 64 字符的 hex，粘到 Worker 的环境变量 PUBKEY 里。
 * 好处：AppSecret 只在本地用过，云端只存「公钥」，验签用公钥、泄露也无所谓。
 * （应答 QQ 首次回调验证要私钥签名，那一步必须放 BOT_SECRET 环境变量，见 worker.js 注释）
 */
import { createPrivateKey, createPublicKey } from 'node:crypto'

const secret = process.argv[2] || process.env.BOT_SECRET || ''
if (!secret) {
  console.error('[x] 没给 AppSecret。用法：node pubkey.js <AppSecret>')
  process.exit(1)
}

// AppSecret 补到 32 字节（对齐官方 Go 的 ed25519.NewKeyFromSeed）
let s = secret
const enc = new TextEncoder ? null : null // 占位，Node 里用 Buffer
while (Buffer.byteLength(s, 'utf8') < 32) s += secret
const seed = Buffer.from(s.slice(0, 32), 'utf8')

// seed + PKCS#8 前缀 → 私钥 → 取公钥
const der = Buffer.concat([Buffer.from('302e020100300506032b657004220420', 'hex'), seed])
const kp = createPrivateKey({ key: der, format: 'der', type: 'pkcs8' })
const spki = createPublicKey({ key: kp }).export({ type: 'spki', format: 'der' })
const raw = spki.subarray(spki.length - 32) // 裸公钥就是最后 32 字节

console.log('\nPUBKEY =')
console.log(raw.toString('hex'))
console.log('\n把这 64 个字符粘进 Cloudflare Worker 的环境变量 PUBKEY；AppSecret 留在你本机，不用填云端。')
