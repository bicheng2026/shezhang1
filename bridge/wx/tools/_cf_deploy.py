#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""蛇杖一号 · 用 Cloudflare API 一键建好 Worker（不碰网页界面）

一次 multipart PUT 把「代码 + KV 绑定 + 4 个 secret」全部传上去：
  - 模块 part 的 Content-Type 必须是 application/javascript+module，否则 CF 按 SW 语法解析，报 export 非法
  - secret 走 metadata.bindings 的 secret_text 类型，绕开独立的 secrets 接口（那个对 API Token 报 405）
AppSecret 只在这台机器上出现过（拿它算公钥），进云端的只有 BOT_SECRET + PUBKEY。
"""
import json
import os
import secrets as _sec
import ssl
import subprocess
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
WX = os.path.dirname(HERE)                      # .../bridge/wx
STATE = os.path.join(HERE, "_cf_state.json")

CF_TOKEN = "<CF_API_TOKEN>hUlNh4b0d7099"
ACCOUNT_ID = "<CF_ACCOUNT_ID>"
SCRIPT_NAME = "shezhang"
KV_NAME = "SHEZHANG"
APP_SECRET = "<在此填 QQ AppSecret>"
API = "https://api.cloudflare.com/client/v4"
CTX = ssl.create_default_context()

ok_count = 0
fail = []


def check(name, cond, extra=""):
    global ok_count
    if cond:
        print("  [ok] " + name)
        ok_count += 1
    else:
        print("  [FAIL] " + name + (" -> " + extra if extra else ""))
        fail.append(name)


def call(path, method="GET", payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + CF_TOKEN)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60, context=CTX) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_error": "HTTP %s" % e.code, "_body": e.read().decode("utf-8", "ignore")[:600]}
    except Exception as e:
        return {"_error": repr(e)}


state = {}
if os.path.exists(STATE):
    try:
        with open(STATE) as f:
            state = json.load(f)
    except Exception:
        state = {}

print("=" * 60)
print("1) 本机算 raw 公钥（AppSecret 只在这台机器上过一遍）")
pubkey = ""
try:
    out = subprocess.run(
        ["node", os.path.join(HERE, "pubkey.js"), APP_SECRET],
        capture_output=True, text=True, encoding="utf-8", timeout=60)
    for ln in reversed([x.strip() for x in (out.stdout or "").splitlines() if x.strip()]):
        if len(ln) == 64 and all(c in "0123456789abcdef" for c in ln):
            pubkey = ln
            break
except Exception as e:
    print("  [FAIL] 跑 pubkey.js 失败：%r" % e)
print("  PUBKEY =", pubkey)
check("公钥拿到且是 64 位 hex", len(pubkey) == 64 and all(c in "0123456789abcdef" for c in pubkey))

print("\n2) KV 命名空间 %s" % KV_NAME)
kv_id = state.get("kv_id", "")
existing = call("/accounts/%s/storage/kv/namespaces" % ACCOUNT_ID)
for ns in (existing.get("result") or []):
    if ns.get("title") == KV_NAME:
        kv_id = ns["id"]
        print("  [i] 已存在，复用", kv_id)
if not kv_id:
    r = call("/accounts/%s/storage/kv/namespaces" % ACCOUNT_ID, "POST", {"title": KV_NAME})
    if r.get("success"):
        kv_id = r["result"]["id"]
        print("  [ok] 新建成功", kv_id)
check("KV 命名空间就位", bool(kv_id))

print("\n3) 一次 PUT 传：代码 + KV 绑定 + 4 个 secret")
admin_token = state.get("admin_token") or _sec.token_hex(24)
read_token = state.get("read_token") or _sec.token_hex(24)
state["admin_token"] = admin_token
state["read_token"] = read_token
state["kv_id"] = kv_id
with open(STATE, "w") as f:
    json.dump(state, f, ensure_ascii=False, indent=2)

with open(os.path.join(WX, "worker.js")) as f:
    code = f.read()

metadata = {
    "main_module": "worker.js",
    "compatibility_date": "2026-10-02",
    "bindings": [
        {"type": "kv_namespace", "name": "SHEZHANG", "namespace_id": kv_id},
        {"type": "secret_text", "name": "BOT_SECRET", "text": APP_SECRET},
        {"type": "secret_text", "name": "PUBKEY", "text": pubkey},
        {"type": "secret_text", "name": "ADMIN_TOKEN", "text": admin_token},
        {"type": "secret_text", "name": "READ_TOKEN", "text": read_token},
        {"type": "plain_text", "name": "PUBLIC", "text": "1"},
    ],
}

b = "----shezhangupload"
body = b""
body += ("--%s\r\n" % b).encode()
body += b'Content-Disposition: form-data; name="metadata"; filename="metadata"\r\n'
body += b"Content-Type: application/json\r\n\r\n"
body += json.dumps(metadata, ensure_ascii=False).encode("utf-8") + b"\r\n"
body += ("--%s\r\n" % b).encode()
body += b'Content-Disposition: form-data; name="worker.js"; filename="worker.js"\r\n'
body += b"Content-Type: application/javascript+module\r\n\r\n"
body += code.encode("utf-8") + b"\r\n"
body += ("--%s--\r\n" % b).encode()

req = urllib.request.Request(
    API + "/accounts/%s/workers/scripts/%s" % (ACCOUNT_ID, SCRIPT_NAME),
    data=body, method="PUT")
req.add_header("Authorization", "Bearer " + CF_TOKEN)
req.add_header("Content-Type", "multipart/form-data; boundary=" + b)
try:
    with urllib.request.urlopen(req, timeout=90, context=CTX) as resp:
        up = json.loads(resp.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    up = {"_error": "HTTP %s" % e.code, "_body": e.read().decode("utf-8", "ignore")[:600]}
except Exception as e:
    up = {"_error": repr(e)}
check("脚本上传 + 绑定 + secret", up.get("success"), json.dumps(up, ensure_ascii=False)[:400])
if not up.get("success"):
    print(json.dumps(up, ensure_ascii=False)[:600])
time.sleep(3)

print("\n4) 取访问地址")
info = call("/accounts/%s/workers/scripts/%s" % (ACCOUNT_ID, SCRIPT_NAME))
hosts = (info.get("result") or {}).get("hosts") or []
url = ""
for h in hosts:
    if "workers.dev" in h:
        url = "https://" + h
        break
if not url:
    tail = ""
    for h in hosts:
        if h.startswith("shezhang"):
            tail = h.split(".")[-2] if False else ""
    sub = ""
    if hosts:
        # https://shezhang.<subdomain>.workers.dev
        sub = hosts[0].split(".")[1] if len(hosts[0].split(".")) > 2 else ""
        url = "https://%s.%s.workers.dev" % (SCRIPT_NAME, sub) if sub else ""
if not url:
    url = state.get("url", "")
print("   hosts =", hosts, " 用:", url)
check("拿到 workers.dev 地址", bool(url))

if url:
    state["url"] = url
    with open(STATE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    print("\n5) 体检")
    for path in ("/api/health", "/api/ann", "/api/qq"):
        try:
            rq = urllib.request.Request(url + path)
            with urllib.request.urlopen(rq, timeout=30, context=CTX) as resp:
                print("   %-11s -> %s %s" % (path, resp.status, resp.read().decode("utf-8")[:200]))
        except urllib.error.HTTPError as e:
            print("   %-11s -> HTTP %s %s" % (path, e.code, e.read().decode("utf-8", "ignore")[:160]))
        except Exception as e:
            print("   %-11s -> %r" % (path, e))

print("\n==== %d 项通过 / %d 项失败 ====" % (ok_count, len(fail)))
if fail:
    print("失败项：", fail)
print("WORKER_URL  =", url)
print("ADMIN_TOKEN =", admin_token)
print("READ_TOKEN  =", read_token)
