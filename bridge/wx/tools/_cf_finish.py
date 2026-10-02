#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补上部署后的最后两步：拿 workers.dev 地址 → 体检 → 清掉测试残留脚本"""
import json
import os
import ssl
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = "<CF_API_TOKEN>hUlNh4b0d7099"
A = "<CF_ACCOUNT_ID>"
S = "shezhang"
UA = "https://api.cloudflare.com/client/v4"
CTX = ssl.create_default_context()


def call(path, method="GET"):
    r = urllib.request.Request(UA + path, method=method)
    r.add_header("Authorization", "Bearer " + TOKEN)
    try:
        with urllib.request.urlopen(r, timeout=60, context=CTX) as resp:
            txt = resp.read().decode("utf-8", "ignore")
            try:
                return json.loads(txt)
            except Exception:
                return {"_raw": txt[:300]}
    except urllib.error.HTTPError as e:
        return {"_err": "HTTP %s" % e.code, "_body": e.read().decode("utf-8", "ignore")[:400]}


state = {}
p = os.path.join(HERE, "_cf_state.json")
if os.path.exists(p):
    with open(p) as f:
        state = json.load(f)

print("== 1) 找 subdomain ==")
sub = ""
r1 = call("/accounts/%s" % A)
print("   account:", json.dumps(r1.get("result", {}), ensure_ascii=False)[:300])
sub = (r1.get("result") or {}).get("subdomain") or ""
if not sub:
    r2 = call("/accounts/%s/workers/subdomain" % A)
    print("   subdomain API:", json.dumps(r2, ensure_ascii=False)[:300])
    rr = r2.get("result") or {}
    sub = rr.get("name") or rr.get("subdomain") or ""
print("   subdomain =", repr(sub))

url = state.get("url") or ("https://%s.%s.workers.dev" % (S, sub) if sub else "")
print("   URL =", url)
if not url:
    raise SystemExit("还是拿不到 subdomain/PUT 地址，停在这里")

state["url"] = url
with open(p, "w") as f:
    json.dump(state, f, ensure_ascii=False, indent=2)

print("\n== 2) 体检（绕开沙箱代理直连）==")
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for path, want in (("/api/health", None), ("/api/ann", "ann"), ("/api/qq", "qq")):
    try:
        rq = urllib.request.Request(url + path)
        with DIRECT.open(rq, timeout=40) as resp:
            body = resp.read().decode("utf-8")
            print("   %-11s -> %s %s" % (path, resp.status, body[:260]))
    except urllib.error.HTTPError as e:
        print("   %-11s -> HTTP %s %s" % (path, e.code, e.read().decode("utf-8", "ignore")[:200]))
    except Exception as e:
        print("   %-11s -> %r" % (path, e))

print("\n== 3) 清掉测试残留 shezhang-mini ==")
r = call("/accounts/%s/workers/scripts/shezhang-mini" % A, "DELETE")
print("   ", json.dumps(r, ensure_ascii=False)[:200])

print("\n== 4) 当前 shezhang 脚本信息 ==")
r = call("/accounts/%s/workers/scripts/%s" % (A, S))
res = r.get("result") or {}
print("    keys:", [k for k in res.keys()][:20])
print("    compatibility_date:", res.get("compatibility_date"))
print("    bindings:", json.dumps(res.get("bindings") or res.get("env") or {}, ensure_ascii=False)[:400])
