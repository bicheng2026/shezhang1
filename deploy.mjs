// 蛇杖一号 · 一键部署脚本（站长专用）
// 在网页版目录下运行：
//   node deploy.mjs          部署 index.html（默认，最常用）
//   node deploy.mjs --down   整页停用：全网刷新后即见「本站已停用」，可恢复，不伤文库
//   node deploy.mjs --up     恢复上线（取消停用）
//   node deploy.mjs --all    部署 index.html + data/index.json + .nojekyll
// 说明：走 GitHub Contents API 直传，不依赖 git（规避本网络 TLS 不稳）。
//       文库 lib/ 体量大（百 MB 级），首次与批量更新请用 git push，见 README。
import { readFileSync, existsSync } from "fs";
import { request } from "https";

const REPO = "bicheng2026/shezhang1";
const ARG = process.argv[2] || "";

// token：从本地 .git/config 的 remote URL 解析（绝不外泄、不落盘明文文件）
let token = "";
try {
  const cfg = readFileSync(new URL(".git/config", import.meta.url), "utf8");
  const m = cfg.match(/(?:https?:\/\/)?bicheng2026:([A-Za-z0-9_-]+)@/);
  if (m) token = m[1];
} catch (e) {}
if (!token) {
  console.error("✗ 找不到 GitHub token（请确认 .git/config 的 remote URL 含 bicheng2026:TOKEN@github.com）");
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

async function deployOne(f, rawOverride) {
  const content = rawOverride !== undefined
    ? rawOverride
    : (existsSync(new URL("./" + f, import.meta.url)) ? readFileSync(new URL("./" + f, import.meta.url), "utf8") : null);
  if (content === null) { console.log("· 跳过（本地无）: " + f); return; }
  const b64 = Buffer.from(content, "utf8").toString("base64");
  let sha;
  try { sha = (await api(f)).sha; } catch (e) {}
  const res = await api(f, "PUT", {
    message: "deploy: " + f + (ARG ? " (" + ARG + ")" : ""),
    content: b64,
    ...(sha ? { sha } : {})
  });
  if (res.content) console.log("✓ " + f + "  (" + res.content.size + " B, commit " + (res.commit?.sha || "").slice(0, 7) + ")");
  else console.log("✗ 失败 " + f + ": " + JSON.stringify(res).slice(0, 160));
}

(async () => {
  if (ARG === "--down") {
    console.log("→ 整页停用中…");
    await deployOne("data/status.json", JSON.stringify({
      disabled: true, message: "本站维护中，暂时停用。", updated: new Date().toISOString().slice(0, 10)
    }, null, 2));
    console.log("→ 完成：全网用户刷新后即见「本站已停用」（随时 node deploy.mjs --up 恢复）。");
    return;
  }
  if (ARG === "--up") {
    console.log("→ 恢复上线中…");
    await deployOne("data/status.json", JSON.stringify({
      disabled: false, message: "", updated: new Date().toISOString().slice(0, 10)
    }, null, 2));
    console.log("→ 完成：全网用户刷新即恢复访问。");
    return;
  }
  const list = ARG === "--all" ? ["index.html", "data/index.json", ".nojekyll"] : ["index.html"];
  console.log("→ 部署 " + list.join(", ") + " …");
  for (const f of list) await deployOne(f);
  console.log("→ 完成：GitHub Pages 通常 1 分钟内重建，用户 Ctrl+F5 刷新即见最新。");
})();
