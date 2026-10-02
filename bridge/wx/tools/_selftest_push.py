"""临时自检：起一个假 Worker，真跑 push_to_wx.post_worker，验证 Bearer / body / 返回解析。"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .../wx
import push_to_wx as P  # noqa: E402

GOT = []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        GOT.append(("GET", self.path, dict(self.headers), b""))
        body = json.dumps({"ok": True, "has_secret": True, "has_pubkey": True, "has_kv": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        GOT.append(("POST", self.path, dict(self.headers), raw))

        if self.path.endswith("/api/qq_no_token"):
            self.send_response(401)
            self.end_headers()
            return
        if self.path.endswith("/sync"):
            if self.headers.get("Authorization") != "Bearer tok123":
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                b = '{"code":401,"msg":"token bubu dui"}'.encode()
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
                return
            body = json.dumps({"code": 0, "msg": "同步完成", "count": len(json.loads(raw)["items"])}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = b'{"code":0}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


srv = HTTPServer(("127.0.0.1", 8931), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

ok_all = True

# 1) 正常推送
ans = P.post_worker("http://127.0.0.1:8931/shezhang", "tok123",
                    [{"id": "ann001", "title": "关于期考的通知", "ts": 1759000000},
                     {"id": "ann002", "title": "创新大赛报名", "ts": 1759000100}])
j = json.loads(ans)
print("1) 正常推送   :", ans)
if j.get("count") == 2:
    print("   [ok] 服务端收到 2 条"); ok_all = ok_all and True
else:
    print("   [FAIL] 服务端没收到预期条数"); ok_all = False

# 2) 验活 + 路径
paths = [g[1] for g in GOT]
if any(p.endswith("/api/health") for p in paths):
    print("2) [ok] 推送前先验活 /api/health")
else:
    print("2) [FAIL] 没先验活"); ok_all = False

post_paths = [g[1] for g in GOT if g[0] == "POST"]
if "/shezhang/sync" in post_paths:
    print("3) [ok] 打到 /sync（worker_url 后的路径不丢）")
else:
    print("3) [FAIL] 路径不对：", post_paths); ok_all = False

# 3) body 结构
body = json.loads([g for g in GOT if g[0] == "POST"][0][3])
if set(body.keys()) == {"items"} and len(body["items"]) == 2:
    print("4) [ok] body 是 {\"items\":[...]}，不带 action/token 老字段")
else:
    print("4) [FAIL] body 结构不对：", list(body.keys())); ok_all = False

# 4) Bearer 头
hdr = [g for g in GOT if g[0] == "POST"][0][2]
if hdr.get("Authorization") == "Bearer tok123":
    print("5) [ok] Authorization: Bearer tok123")
else:
    print("5) [FAIL] Bearer 头不对：", hdr.get("Authorization")); ok_all = False

# 5) token 错 → 401 能被解析成非 ok
bad = P.post_worker("http://127.0.0.1:8931/shezhang", "wrong", [{"id": "x", "title": "t", "ts": 1}])
print("6) 错误 token  :", bad)
if bad.startswith("[HTTP 401]"):
    print("   [ok] 401 如实质返回")
else:
    print("   [FAIL] 没拿到 401"); ok_all = False

srv.shutdown()
print("\n" + ("[PASS] 推送链路全通" if ok_all else "[FAIL] 有问题"))
sys.exit(0 if ok_all else 1)
