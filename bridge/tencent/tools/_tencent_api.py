#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""蛇杖一号 · 腾讯云 API 助手（建网关用）

用法：
    python _tencent_api.py probe          # 验密钥 + 查现有函数/网关
    python _tencent_api.py gateway        # 建服务 + API + 路由 + 发布
    python _tencent_api.py env            # 配 4 个环境变量
    python _tencent_api.py check          # 线上验活（打三级域名）

密钥只在本机用过，不上传到任何地方。
"""
import hashlib
import hmac
import json
import os
import random
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SECRET_ID = os.environ.get("TENCENT_SECRET_ID", "<在此填腾讯云 SecretId>")
SECRET_KEY = os.environ.get("TENCENT_SECRET_KEY", "<在此填腾讯云 SecretKey>")
REGION = os.environ.get("TENCENT_REGION", "ap-shanghai")
FUNC = "shezhang"
SVC = "shezhang"
CTX = ssl.create_default_context()
D = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                urllib.request.HTTPSHandler(context=CTX))


def sign(params):
    """TC3-HMAC-SHA256 签名（腾讯云官方 V3 签名法）"""
    alg = "TC3-HMAC-SHA256"
    service = params.get("service", "")
    timestamp = str(int(time.time()))
    date = time.strftime("%Y-%m-%d", time.gmtime(int(timestamp)))   # ⚠️ 必须 UTC
    # ⚠️ ensure_ascii=False 是必须的！腾讯云示例里中文原样传输，
    #    用默认的 \uXXXX 转义会导致 payload 哈希变、签名对不上（2026-10-02 实测踩过）
    payload = json.dumps(params.get("payload", {}), separators=(",", ":"),
                         ensure_ascii=False)
    host = params["host"]
    action = params["action"]
    version = params.get("version", "2020-08-07")

    # ⚠️ canonical_querystring 必须是空串：Action/Version/Timestamp 都走 header，不进 query
    canonical_querystring = ""
    canonical_uri = "/"
    # content-type 这里的空格必须和真实请求头完全一致，否则签名对不上
    ct = "application/json; charset=utf-8"
    canonical_headers = "content-type:%s\nhost:%s\n" % (ct, host)
    signed_headers = "content-type;host"

    canonical_request = "\n".join([
        "POST", canonical_uri, canonical_querystring, canonical_headers, signed_headers,
        hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    ])

    # ⚠️ 格式是 date/service/tc3_request（不是 date/tc3_request/service）
    credential_scope = "%s/%s/tc3_request" % (date, service)
    string_to_sign = "\n".join([
        alg, timestamp, credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    def _hmac(k, m):
        return hmac.new(k, m.encode("utf-8"), hashlib.sha256).digest()

    secret_date = _hmac(("TC3" + SECRET_KEY).encode("utf-8"), date)
    secret_service = _hmac(secret_date, service)
    secret_signing = _hmac(secret_service, "tc3_request")
    signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()

    authorization = (
        "%s Credential=%s/%s, SignedHeaders=%s, Signature=%s"
        % (alg, SECRET_ID, credential_scope, signed_headers, signature))
    return {
        "Authorization": authorization,
        "Content-Type": ct,
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Version": version,
        "X-TC-Timestamp": timestamp,
        "X-TC-Region": params.get("region", ""),
    }


def call(action, service="scf", version="2018-04-16", host="scf.tencentcloudapi.com",
         payload=None, region=REGION):
    p = {"service": service, "action": action, "host": host,
         "version": version, "region": region, "payload": payload or {}}
    url = "https://%s/" % host
    # ⚠️ 这里必须跟 sign() 里算哈希用的字符串**完全一致**：
    #    同为 separators=(",", ":") + ensure_ascii=False。
    #    踩过的坑：签名时用原文、发送时用默认 json.dumps（中文被转成 \uXXXX）→ 签名对不上。
    body = json.dumps(p["payload"], separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    for k, v in sign(p).items():
        req.add_header(k, v)
    try:
        with D.open(req, timeout=40) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode("utf-8", "ignore")[:800]}
    except Exception as e:
        return {"_err": repr(e)}


def short(r):
    return json.dumps(r, ensure_ascii=False)[:600]


# ---------------- 命令 ----------------
def cmd_probe():
    print("== 1) 验密钥（调 SCF ListNamespaces，最轻的一个）==")
    r = call("ListNamespaces", payload={})
    ok = r.get("Response", {}).get("RequestId")
    print("   ", "密钥可用 ✔" if ok else "失败", "->", short(r)[:250])

    print("\n== 2) 查函数是否真的存在、类型对不对 ==")
    r = call("GetFunction", payload={"FunctionName": FUNC})
    res = (r.get("Response") or {}).get("Info") or {}
    if res:
        print("    函数名    :", res.get("FunctionName"))
        print("    状态      :", res.get("Status"))
        print("    运行时    :", res.get("Runtime"))
        print("    handler   :", res.get("Handler"))
        print("    内存/超时 :", res.get("MemorySize"), "MB /", res.get("Timeout"), "秒")
        envs = (res.get("Environment") or {}).get("Variables") or []
        print("    环境变量  :", [e.get("Key") for e in envs] or "（空）")
    else:
        print("    查不到：", short(r)[:400])

    print("\n== 3) 查有没有现成的网关服务 ==")
    r = call("DescribeServices", service="apigw", version="2018-08-08",
             host="apigw.tencentcloudapi.com", payload={"Limit": 20})
    items = (r.get("Response") or {}).get("ServiceList") or []
    if items:
        for s in items:
            print("    -", s.get("ServiceId"), s.get("ServiceName"),
                  s.get("Protocol"), s.get("SubDomain"))
    else:
        print("    还没有网关服务（正常，还没建）", short(r)[:200])


def cmd_env():
    print("== 配 4 个环境变量 ==")
    pub = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "wx", "tools", "_cf_state.json"), encoding="utf-8").read()
    pubhex = ""
    for line in reversed(pub.splitlines()):
        line = line.strip().strip(",")
        if len(line) == 64 and all(c in "0123456789abcdef" for c in line):
            pubhex = line
            break
    variables = {
        "BOT_SECRET": "<在此填 QQ AppSecret>",
        "PUBKEY": pubhex,
        "ADMIN_TOKEN": "<在此填推送口令>",
        "READ_TOKEN": "<在此填查询口令>",
    }
    print("    PUBKEY =", pubhex[:16] + "...")
    r = call("UpdateFunctionConfiguration", payload={
        "FunctionName": FUNC,
        "Environment": {"Variables": [
            {"Key": k, "Value": v} for k, v in variables.items()]},
    })
    print("   ", "配置成功" if r.get("Response") else "失败", "->", short(r)[:400])


def cmd_gateway():
    print("== 1) 建服务 ==")
    r = call("CreateService", service="apigw", version="2018-08-08",
             host="apigw.tencentcloudapi.com",
             payload={"ServiceName": SVC, "Protocol": "HTTP,HTTPS", "Description": "shezhang"})
    resp = r.get("Response") or {}
    svc_id = resp.get("ServiceId")
    print("    ServiceId =", svc_id, "->", short(r)[:300])
    if not svc_id:
        return

    print("\n== 2) 建 API（ANY / → SCF）==")
    r = call("CreateApi", service="apigw", version="2018-08-08",
             host="apigw.tencentcloudapi.com", payload={
                 "ServiceId": svc_id, "Name": "qq-callback", "Description": "shezhang",
                 "Path": "/", "Method": "ANY", "Backend": {
                     "Type": "SCF", "ServiceId": "",
                     "Product": "SCF", "Region": REGION,
                     "FunctionName": FUNC,
                 },
             })
    print("   ", "创建成功" if r.get("Response") else "失败", "->", short(r)[:400])

    print("\n== 3) 发布到 release 环境 ==")
    r = call("ModifyEnvironment", service="apigw", version="2018-08-08",
             host="apigw.tencentcloudapi.com",
             payload={"ServiceId": svc_id, "EnvironmentName": "release",
                      "BasePath": "", "Version": ""})
    print("   ", short(r)[:300])
    r = call("ModifyApi", service="apigw", version="2018-08-08",
             host="apigw.tencentcloudapi.com",
             payload={"ServiceId": svc_id, "EnvironmentName": "release", "ApiId": "xxx",
                      "Path": "/", "Method": "ANY"})
    # ApiId 需要真实值，GetApis 查
    r2 = call("DescribeApis", service="apigw", version="2018-08-08",
              host="apigw.tencentcloudapi.com", payload={"ServiceId": svc_id, "Limit": 10})
    apis = (r2.get("Response") or {}).get("ApiSet") or []
    api_id = apis[0].get("ApiId") if apis else None
    print("    ApiId =", api_id)
    if api_id:
        r3 = call("ModifyEnvironment", service="apigw", version="2018-08-08",
                  host="apigw.tencentcloudapi.com",
                  payload={"ServiceId": svc_id, "EnvironmentName": "release",
                           "BasePath": ""})
        r4 = call("ReleaseApi", service="apigw", version="2018-08-08",
                  host="apigw.tencentcloudapi.com",
                  payload={"ServiceId": svc_id, "EnvironmentName": "release",
                           "ApiId": api_id, "GroupName": "", "Description": "release"})
        print("    发布:", short(r4)[:300])

    print("\n== 4) 拿三级域名 ==")
    r = call("DescribeServiceDetail", service="apigw", version="2018-08-08",
             host="apigw.tencentcloudapi.com", payload={"ServiceId": svc_id})
    d = (r.get("Response") or {}).get("ServiceDetail") or {}
    sub = d.get("SubDomain") or ""
    print("    SubDomain =", sub)
    print("\n    === 回调地址填这个 ===")
    if sub:
        print("    https://%s/" % sub)
        print("    健康检查  https://%s/health" % sub)
        state = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tencent_state.json")
        with open(state, "w", encoding="utf-8") as f:
            json.dump({"service_id": svc_id, "api_id": api_id, "subdomain": sub}, f, indent=2)


def cmd_check():
    state = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tencent_state.json")
    if not os.path.exists(state):
        print("    还没有 _tencent_state.json，先跑 gateway")
        return
    sub = json.load(open(state, encoding="utf-8")).get("subdomain", "")
    if not sub:
        print("    state 里没有 subdomain")
        return
    base = "https://%s" % sub
    print("    探测", base)
    for path in ("/health", "/page?kind=ann",
                 "/dbg?t=<在此填查询口令>"):
        try:
            req = urllib.request.Request(base + path)
            with D.open(req, timeout=30) as resp:
                print("    %-46s -> %s %s" % (path[:46], resp.status,
                                             resp.read().decode("utf-8", "ignore")[:300]))
        except urllib.error.HTTPError as e:
            print("    %-46s -> HTTP %s %s" % (path[:46], e.code,
                                               e.read().decode("utf-8", "ignore")[:200]))
        except Exception as e:
            print("    %-46s -> %r" % (path[:46], e))


if __name__ == "__main__":
    c = sys.argv[1] if len(sys.argv) > 1 else "probe"
    {"probe": cmd_probe, "env": cmd_env, "gateway": cmd_gateway, "check": cmd_check}[c]()
