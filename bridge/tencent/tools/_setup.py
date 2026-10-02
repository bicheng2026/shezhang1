#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""蛇杖一号 · 一键把腾讯云 Web 函数配好（代码 + 环境变量 + 网关）

    python _setup.py code   # 传代码 + 环境变量 + 超时
    python _setup.py gw     # 建网关 + API + 发布，拿到三级域名
    python _setup.py all    # 两步都做
    python _setup.py check  # 线上验活
"""
import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SECRET_ID = os.environ.get("TENCENT_SECRET_ID", "<在此填腾讯云 SecretId>")
SECRET_KEY = os.environ.get("TENCENT_SECRET_KEY", "<在此填腾讯云 SecretKey>")
REGION = "ap-guangzhou"          # 主公的函数建在广州
FUNC = "shezhang"
SVC = "shezhang"
ZIP = os.path.join(HERE, "..", "..", "..", "..", "tencent-scf-上传.zip")

CTX = ssl.create_default_context()
D = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                urllib.request.HTTPSHandler(context=CTX))


# ---- 签名（TC3-HMAC-SHA256）----
import hashlib
import hmac


def sign(p):
    alg = "TC3-HMAC-SHA256"
    ts = str(int(time.time()))
    date = time.strftime("%Y-%m-%d", time.gmtime(int(ts)))      # 必须 UTC
    payload = json.dumps(p["payload"], separators=(",", ":"), ensure_ascii=False)
    host = p["host"]
    ct = "application/json; charset=utf-8"
    ch = "content-type:%s\nhost:%s\n" % (ct, host)
    sh = "content-type;host"
    cr = "\n".join(["POST", "/", "", ch, sh,
                    hashlib.sha256(payload.encode("utf-8")).hexdigest()])
    cred = "%s/%s/tc3_request" % (date, p["service"])
    sts = "\n".join([alg, ts, cred, hashlib.sha256(cr.encode("utf-8")).hexdigest()])

    def hm(k, m):
        return hmac.new(k, m.encode("utf-8"), hashlib.sha256).digest()

    sig = hmac.new(hm(hm(hm(("TC3" + SECRET_KEY).encode(), date), p["service"]),
                      "tc3_request"), sts.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "Authorization": "%s Credential=%s/%s, SignedHeaders=%s, Signature=%s"
                         % (alg, SECRET_ID, cred, sh, sig),
        "Content-Type": ct, "Host": host,
        "X-TC-Action": p["action"], "X-TC-Version": p.get("version", ""),
        "X-TC-Timestamp": ts, "X-TC-Region": p.get("region", ""),
    }


def call(action, service="scf", version="2018-04-16",
         host="scf.tencentcloudapi.com", payload=None, region=REGION):
    p = {"service": service, "action": action, "host": host, "version": version,
         "region": region, "payload": payload or {}}
    body = json.dumps(p["payload"], separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request("https://%s/" % host, data=body, method="POST")
    for k, v in sign(p).items():
        req.add_header(k, v)
    try:
        with D.open(req, timeout=90) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode("utf-8", "ignore")[:600]}
    except Exception as e:
        return {"_err": repr(e)}


def s(r, n=400):
    return json.dumps(r, ensure_ascii=False)[:n]


def get_pubkey():
    """本机从 AppSecret 算 raw 公钥（跟 wx/tools/pubkey.js 同一套算法）。
    别去读 _cf_state.json —— 那里面没存 pubkey（踩过，has_pubkey 变 false）。"""
    import subprocess
    pj = os.path.join(HERE, "..", "..", "wx", "tools", "pubkey.js")
    if not os.path.exists(pj):
        return ""
    try:
        out = subprocess.run(["node", pj, ENV["BOT_SECRET"]],
                             capture_output=True, text=True, encoding="utf-8", timeout=60)
        for line in reversed((out.stdout or "").splitlines()):
            line = line.strip()
            if len(line) == 64 and all(c in "0123456789abcdef" for c in line):
                return line
    except Exception as e:
        print("    算公钥失败：%r" % e)
    return ""


ENV = {
    "BOT_SECRET": "<在此填 QQ AppSecret>",
    "PUBKEY": "",                       # 运行时填
    "ADMIN_TOKEN": "<在此填推送口令>",
    "READ_TOKEN": "<在此填查询口令>",
    # 云端直写 GitHub（0 元持久化）：同学看的就是 Pages，云端即数据库
    "GH_TOKEN": "<在此填你的 GitHub token>",
    "GH_REPO": "bicheng2026/shezhang1",
    "GH_BRANCH": "main",
    "GH_PATH": "data/qq_digest.json",
}


def do_code():
    ENV["PUBKEY"] = get_pubkey()
    print("== 1) 读 zip ==")
    if not os.path.exists(ZIP):
        print("    找不到 zip：", ZIP)
        return False
    with open(ZIP, "rb") as f:
        raw = f.read()
    print("    %s  %d 字节" % (os.path.basename(ZIP), len(raw)))
    b64 = base64.b64encode(raw).decode("ascii")

    print("\n== 2) 传代码（UpdateFunctionCode）==")
    # ⚠️ Timeout 不属于 UpdateFunctionCode（会报 UnknownParameter），只能在 Configuration 里设
    r = call("UpdateFunctionCode", payload={
        "FunctionName": FUNC, "ZipFile": b64, "Handler": "index.main_handler",
    })
    ok = (r.get("Response") or {}).get("RequestId")
    print("    ", "成功" if ok else "失败", "->", s(r, 300))
    if not ok:
        return False
    time.sleep(5)

    print("\n== 3) 配环境变量 + 超时（UpdateFunctionConfiguration）==")
    r = call("UpdateFunctionConfiguration", payload={
        "FunctionName": FUNC,
        "Environment": {"Variables": [{"Key": k, "Value": v} for k, v in ENV.items()]},
        "Timeout": 15,
    })
    print("    ", "成功" if r.get("Response", {}).get("RequestId") else "失败", "->", s(r, 300))
    time.sleep(3)

    print("\n== 4) 复查 ==")
    r = call("GetFunction", payload={"FunctionName": FUNC})
    txt = s(r, 100000)
    import re
    for k in ("Runtime", "Handler", "Status", "CodeSize", "Timeout", "MemorySize"):
        mm = re.search(r'"%s"\s*:\s*("([^"]*)"|[0-9]+)' % k, txt)
        print("    %-11s = %s" % (k, mm.group(1) if mm else "?"))
    mm = re.search(r'"Environment"\s*:\s*\{[^}]*"Variables"\s*:\s*\[(.*?)\]', txt, re.S)
    if mm:
        print("    环境变量   =", re.findall(r'"Key"\s*:\s*"([^"]+)"', mm.group(1)))
    return True


def do_gw():
    # ⚠️ 网关的域名是 apigateway.tencentcloudapi.com（不是 apigw / apigw.tencentcloudapi.com）
    #    service 名 = apigateway，版本 2018-08-08
    GH = "apigateway.tencentcloudapi.com"
    GS = "apigateway"
    print("== 1) 建服务 ==")
    r = call("CreateService", service=GS, version="2018-08-08", host=GH,
             region="",
             payload={"ServiceName": SVC, "Protocol": "HTTP,HTTPS",
                      "ServiceDesc": "shezhang", "Region": REGION})
    resp = r.get("Response") or {}
    svc = resp.get("ServiceId")
    if not svc:
        r2 = call("DescribeServicesStatus", service=GS, version="2018-08-08",
                  host=GH, payload={"Limit": 20})
        for x in (r2.get("Response") or {}).get("ServiceList") or []:
            if x.get("ServiceName") == SVC:
                svc = x.get("ServiceId")
                print("    已存在，复用", svc)
                break
    if not svc:
        print("    失败", s(r, 400))
        return False
    print("    ServiceId =", svc)

    print("\n== 2) 建 API（ANY / → SCF）==")
    r = call("CreateApi", service=GS, version="2018-08-08", host=GH,
             payload={"ServiceId": svc, "Name": "shezhang-cb",
                      "Path": "/", "Method": "ANY",
                      "Backend": {"Type": "SCF", "Product": "SCF", "Region": REGION,
                                  "FunctionName": FUNC}})
    print("    ", "成功" if (r.get("Response") or {}).get("RequestId") else s(r, 300))

    print("\n== 3) 查 ApiId ==")
    r = call("DescribeApis", service=GS, version="2018-08-08", host=GH,
             payload={"ServiceId": svc, "Limit": 20})
    apis = (r.get("Response") or {}).get("ApiSet") or []
    api_id = apis[0].get("ApiId") if apis else None
    print("    ApiId =", api_id, " 共", len(apis), "个")
    if not api_id:
        print("    ", s(r, 300))
        return False

    print("\n== 4) 发布服务（ReleaseService，不是 ReleaseApi）==")
    r = call("ModifyEnvironment", service=GS, version="2018-08-08", host=GH,
             payload={"ServiceId": svc, "EnvironmentName": "release", "BasePath": ""})
    print("    配环境:", s(r, 200))
    r = call("ReleaseService", service=GS, version="2018-08-08", host=GH,
             payload={"ServiceId": svc, "EnvironmentName": "release",
                      "ReleaseNotes": "release"})
    print("    发布:", s(r, 250))

    print("\n== 5) 拿三级域名 ==")
    r = call("DescribeService", service=GS, version="2018-08-08", host=GH,
             payload={"ServiceId": svc})
    d = (r.get("Response") or {}).get("Service") or {}
    sub = d.get("SubDomain") or ""
    print("    SubDomain =", sub)
    st = os.path.join(HERE, "_tencent_state.json")
    with open(st, "w", encoding="utf-8") as f:
        json.dump({"service_id": svc, "api_id": api_id, "subdomain": sub,
                   "region": REGION}, f, ensure_ascii=False, indent=2)
    if sub:
        print("\n    ================= 回调地址填这个 =================")
        print("    https://%s/" % sub)
        print("    健康检查：https://%s/health" % sub)
    return bool(sub)


def do_check():
    st = os.path.join(HERE, "_tencent_state.json")
    if not os.path.exists(st):
        print("    没有 _tencent_state.json，先跑 gw")
        return
    sub = json.load(open(st, encoding="utf-8")).get("subdomain", "")
    if not sub:
        print("    state 里 subdomain 为空")
        return
    base = "https://%s" % sub
    print("    探测", base)
    for path in ("/health", "/page?kind=ann",
                 "/dbg?t=<在此填查询口令>"):
        try:
            req = urllib.request.Request(base + path)
            with D.open(req, timeout=40) as resp:
                print("    %-44s -> %s %s" % (path[:44], resp.status,
                                              resp.read().decode("utf-8", "ignore")[:400]))
        except urllib.error.HTTPError as e:
            print("    %-44s -> HTTP %s %s" % (path[:44], e.code,
                                              e.read().decode("utf-8", "ignore")[:250]))
        except Exception as e:
            print("    %-44s -> %r" % (path[:44], e))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("code", "all"):
        do_code()
    if cmd in ("gw", "all"):
        do_gw()
    if cmd == "check":
        do_check()
