#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""蛇杖一号 · 把桥接数据推给 Cloudflare Worker（0 元路线，2026-10-02 起的主路线）。

背景：主公定「一分钱不花 + 手机电脑不整天在线」→ 不能再走微信云开发
（云开发免费环境小程序发布后 15 天到期，之后 19.9 元/月）。接收端改成
Cloudflare Workers 免费档（永久 0 元、自带 *.workers.dev HTTPS、不需要信用卡），
群消息由 QQ Webhook 直接推过去，本机只负责推公告。

怎么用：
    python push_to_wx.py --apply        # 先在 config.json 生成 wx 段模板
    # 填好 wx.worker_url 和 wx.admin_token
    python push_to_wx.py                # 推近 7 天
    python push_to_wx.py --days 3
    python push_to_wx.py --include-qq   # 想连班群消息也放上去（需你本人拍板）

前置：config.json 里补一段 wx：
    "wx": {
      "worker_url": "https://shezhang-xxxx.your-subdomain.workers.dev",
      "admin_token": "一串你自己生成的随机串（openssl rand -hex 24）",
      "include_qq": false
    }
（老云函数路线仍然留着：留 wx.cloud_url + wx.token 就自动走那条。）

去重：已推过的 id 记在 wx_sent.txt，不重复推。
隐私：默认不推 source='qq'（班群消息）和不推 export_exclude_sources 里的源，
      这是 2026-09-30 定的红线，别手滑打开。
      另外 群消息不从 Worker 的公开接口出（/api/qq 要 token），网页版只读 /api/ann。
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.dirname(HERE)           # .../bridge
DB = os.path.join(BRIDGE, "bridge.db")
CFG = os.path.join(BRIDGE, "config.json")
SENT = os.path.join(BRIDGE, "wx_sent.txt")


def die(msg):
    print("[x] %s" % msg)
    sys.exit(1)


def load_cfg():
    if not os.path.exists(CFG):
        die("找不到 config.json（%s）" % CFG)
    with open(CFG, encoding="utf-8") as f:
        return json.load(f)


def save_cfg(cfg):
    with open(CFG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def read_sent():
    if not os.path.exists(SENT):
        return set()
    with open(SENT, encoding="utf-8") as f:
        return set(x.strip() for x in f if x.strip())


def write_sent(s):
    # id 可能是 int（sqlite 里某些源用整数主键），统一转 str 写，否则 join 会 TypeError
    with open(SENT, "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(str(x) for x in s)))


def fetch_items(days, include_qq, excludes):
    if not os.path.exists(DB):
        die("找不到 bridge.db（%s）" % DB)
    since = int(time.time()) - days * 86400
    sql = ("SELECT id, source, sender, title, ts, level, ddl, status "
           "FROM items WHERE ts>=? AND status<>'archived' ORDER BY ts DESC")
    con = sqlite3.connect(DB)
    rows = con.execute(sql, (since,)).fetchall()
    con.close()

    out = []
    for r in rows:
        d = dict(zip(("id", "source", "sender", "title", "ts", "level", "ddl", "status"), r))
        src = (d.get("source") or "")
        if not include_qq and str(d.get("source", "")).lower() == "qq":
            continue
        if src in excludes:
            continue
        d.pop("sender", None)
        d["title"] = (d.get("title") or "")[:200]
        out.append(d)
    return out


def post_worker(url, admin, items):
    """推给云端的 /sync（0 元路线：腾讯云 SCF 函数 URL）。

    服务端鉴权是 Bearer，body 只要 {"items":[...]}。
    探活打 /health（腾讯云版有它），token 填错时给一句人话，别让主公对着 401 发呆。
    """
    data = json.dumps({"items": items}).encode("utf-8")
    base = url.rstrip("/")
    try:
        with urllib.request.urlopen(base + "/health", timeout=15) as resp:
            resp.read()
    except Exception as e:
        return "[ERROR] 连不上云端（%s）：%r。检查 worker_url 和云函数有没有部署" % (base, e)

    req = urllib.request.Request(
        base + "/sync", data=data,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + str(admin)},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return "[HTTP %s] %s" % (e.code, e.read().decode("utf-8", "ignore"))
    except Exception as e:
        return "[ERROR] %r" % e


def post_cloud(url, token, items):
    """推给微信云函数（老路线，只有 config 里留着 cloud_url 才走这条）。"""
    payload = {"action": "sync", "token": token, "items": items}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return "[HTTP %s] %s" % (e.code, e.read().decode("utf-8", "ignore"))
    except Exception as e:
        return "[ERROR] %r" % e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--include-qq", action="store_true")
    ap.add_argument("--force", action="store_true", help="忽略 wx_sent.txt 去重，重推全量")
    ap.add_argument("--apply", action="store_true",
                    help="把 wx 段写进 config.json（只写 cloud_url/token/include_qq）")
    a = ap.parse_args()

    cfg = load_cfg()
    wxcfg = cfg.get("wx") or {}

    if a.apply:
        wxcfg.setdefault("worker_url", "")
        wxcfg.setdefault("admin_token", "")
        wxcfg.setdefault("include_qq", False)
        cfg["wx"] = wxcfg
        save_cfg(cfg)
        print("[ok] config.json 已写入 wx 段（0 元路线：worker_url + admin_token）")
        print("     填好 wx.worker_url（Workers 地址）和 wx.admin_token（自己生成的随机串）再用")
        print("     admin_token 可以重新生成：openssl rand -hex 24")
        return

    # 0 元路线优先：worker_url + admin_token
    wurl = (wxcfg.get("worker_url") or "").strip()
    wtoken = (wxcfg.get("admin_token") or "").strip()
    if wurl and wtoken:
        target = "worker"
        url, token = wurl, wtoken
    else:
        # 老路线兜底：微信云函数 URL 化
        url = (wxcfg.get("cloud_url") or "").strip()
        token = (wxcfg.get("token") or "").strip()
        if not url or not token:
            die("config.json 里 wx.worker_url + wx.admin_token 没填（0 元路线），"
                "或者 wx.cloud_url + wx.token 没填（老云函数路线）")
        target = "cloud"

    include_qq = bool(wxcfg.get("include_qq", False)) or a.include_qq
    excludes = cfg.get("export_exclude_sources") or []

    items = fetch_items(a.days, include_qq, excludes)
    sent = set() if a.force else read_sent()
    todo = [x for x in items if str(x["id"]) not in sent]

    if not todo:
        print("[=] 没有新条目要推（共 %d 条已在库）" % len(items))
        return

    print("[i] 目标：%s  %s" % (target, url))
    print("[i] 待推 %d 条（库里共 %d 条，去重后跳过 %d）"
          % (len(todo), len(items), len(items) - len(todo)))
    # 分批，避免单请求过大被超时掐掉
    ok_count = 0
    batch = 200
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        if target == "worker":
            ans = post_worker(url, token, chunk)
        else:
            ans = post_cloud(url, token, chunk)
        try:
            res = json.loads(ans)
        except Exception:
            print("[x] 返回不是 JSON：%s" % ans[:200])
            return
        # Worker 的 /sync 回 {"code":0,"msg":"同步完成","count":n}；云函数回 {"ok":true}
        good = (res.get("code") == 0) if target == "worker" else bool(res.get("ok"))
        if good:
            print("[ok] 第 %d-%d 批成功：%s" % (i, i + len(chunk), ans))
            ok_count += len(chunk)
        else:
            print("[x] 第 %d-%d 批失败：%s" % (i, i + len(chunk), ans[:200]))
            return

    write_sent(sent | set(str(x["id"]) for x in todo))
    print("[ok] 完成，本次推送 %d 条，已推记录存 wx_sent.txt" % ok_count)


if __name__ == "__main__":
    main()
