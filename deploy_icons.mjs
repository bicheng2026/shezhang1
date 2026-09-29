// 蛇杖一号 · 图标部署脚本（Contents API 直传，二进制安全）
// 用法：node deploy_icons.mjs
// 推送清单：5 个 PNG + icon.svg + sw.js（sw.js 缓存版本号已 bump，强制老用户换图标）
import { readFileSync } from "fs";
import { request } from "https";

const REPO = "bicheng2026/shezhang1";
const FILES = [
  "icon-512.png", "icon-192.png", "icon-180.png", "icon-32.png",
  "icon-maskable-512.png", "icon.svg", "sw.js"
];

let token = "";
try {
  const cfg = readFileSync(new URL(".git/config", import.meta.url), "utf8");
  const m = cfg.match(/(?:https?:\/\/)?bicheng2026:([A-Za-z0-9_-]+)@/);
  if (m) token = m[1];
} catch (e) {}
if (!token) {
  console.error("✗ 找不到 GitHub token（.git/config remote URL）");
  process.exit(1);
}

function api(path, method = "GET", body) {
  return new Promise((resolve, reject) => {
    const data = body ? JSON.stringify(body) : null;
    const req = request({
      host: "api.github.com",
      path: "/repos/" + REPO + "/contents/" + path,
      method,
      headers: {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "User-Agent": "shezhang1-deploy",
        ...(data ? { "Content-Length": Buffer.byteLength(data) } : {})
      }
    }, res => {
      let s = ""; res.on("data", d => s += d);
      res.on("end", () => { try { resolve(JSON.parse(s)); } catch { reject(new Error(s.slice(0, 200))); } });
    });
    req.on("error", reject);
    if (data) req.write(data);
    req.end();
  });
}

(async () => {
  for (const f of FILES) {
    const raw = readFileSync(new URL("./" + f, import.meta.url));
    const b64 = raw.toString("base64");
    let sha;
    try { sha = (await api(f)).sha; } catch (e) {}
    const res = await api(f, "PUT", {
      message: "deploy: new circular badge icons (v2 shell)",
      content: b64,
      ...(sha ? { sha } : {})
    });
    if (res.content) console.log("✓ " + f + "  (" + res.content.size + " B)");
    else console.log("✗ 失败 " + f + ": " + JSON.stringify(res).slice(0, 200));
  }
  console.log("→ 完成：raw 立即生效，Pages 约 1-2 分钟。");
})();
