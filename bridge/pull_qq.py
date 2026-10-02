#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""蛇杖一号 · 群消息拉取 + 提纯 + 发布（0 元方案，2026-10-02）

链路：
    腾讯云函数（验签+脱敏，已跑通）
        └─ GET /qq?token=…      拉云端收到的群消息
             ├─▶ 本机存档 raw/群-YYYY-MM.jsonl    （10G 文件夹，你给的）
             ├─▶ 提纯：只留「有信息量」的消息 → digest/群摘要-YYYY-MM.jsonl
             └─▶ 发布：摘要写进网页版 data/，同学刷新网页就能看到

为什么这么做（不推翻今天进度）：
    - 云函数实测**内存会丢**（写入返回成功，下一个请求读出来是空）。
      所以云端只当「过路」，**主数据落本机**——本机是你 10G 文件夹，不花钱。
    - 群消息**只发摘要不发原话**：遵守主公 2026-09-30 定的红线
      「QQ 群内容绝不进公开仓库」。摘要里去掉闲聊、去掉人名、去掉学号手机号。

怎么用：
    python pull_qq.py              # 拉最近 3 天 + 生成摘要 + 发布
    python pull_qq.py --days 7     # 拉最近 7 天
    python pull_qq.py --no-publish # 只拉取存档，不推网页
    python pull_qq.py --daemon     # 常驻，每 10 分钟拉一次（电脑开着时）
"""
import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = HERE                                          # 本脚本就放在 bridge/ 下
WXDIR = os.path.join(BRIDGE, "wx")
STORE = os.environ.get("SHEZHANG_STORE", r"E:\白求恩一号\群消息存档")
RAW = os.path.join(STORE, "raw")
DIGEST = os.path.join(STORE, "digest")
CFG = os.path.join(BRIDGE, "config.json")
WEB_ROOT = os.path.dirname(BRIDGE)                    # .../蛇杖一号
WEB_DATA = os.path.join(WEB_ROOT, "网页版", "data")
TOKEN_FILE = os.path.join(WXDIR, "tools", "_cf_state.json")

CTX = ssl.create_default_context()
# 云函数（腾讯云）在大陆可直连；CF 那边被墙，所以这里只走腾讯云
OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=CTX))


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg))


# ---------------- 脱敏（跟 bridge.py / worker.js 同规则，顺序不能反） ----------------
def scrub(t):
    if not t:
        return ""
    t = str(t)
    t = re.sub(r"\[CQ:.*?\]", " ", t)            # CQ 码
    t = re.sub(r"<faceType=[^>]{0,160}>", " ", t)
    t = re.sub(r"<@!?\d+>", " ", t)
    t = re.sub(r"\b\d{17}[\dXx]\b", "[已隐去]", t)   # 身份证
    t = re.sub(r"\b1[3-9]\d{9}\b", "[已隐去]", t)    # 手机
    t = re.sub(r"\b\d{10,12}\b", "[已隐去]", t)      # 学号
    return re.sub(r"\s{2,}", " ", t).strip()


# ---------------- 提纯：只留「有信息量」的消息 ----------------
# 判定思路：命中这些模式才算「值得给全班看」，其余（闲聊/表情/签到/无意义）丢弃
KEEP_PATTERNS = [
    (r"(截止|截至|deadline|前交|前发|交材料|提交|报名|报名截止)", "ddl"),
    (r"(考试|测验|补考|重修|成绩|绩点|学分)", "exam"),
    (r"(讲座|培训|会议|开会| seminar| workshop|活动|比赛|竞赛|答辩)", "event"),
    (r"(放假|调休|上课|停课|补课|校历|作息|早八|晚安|早睡)", "schedule"),
    (r"(通知|公告|提醒|请注意|务必|一定要|别忘|记得)", "notice"),
    (r"(附件|文件|资料|链接|二维码|见附件|如下)", "material"),
    (r"(报名|招募|志愿者|班委|团委|学生会|社团)", "signup"),
    (r"(截止|ddl)", "ddl"),
]
# 明确丢掉（隐私 + 噪声）
DROP_PATTERNS = re.compile(
    r"^(哈+|呵+|嗯+|哦+|啊+|草|6|666|顶|沙发|签到|打卡|早|晚安|午安|"
    r"收到|好的?|OK|ok|谢谢|thx|谢了|图|表情|\[.{0,12}\]|.+jpg.*|.*\.png.*)$")

MAX_LEN = 120          # 单条摘要最多 120 字，超了截断（网页排版好看）
MAX_PER_DAY = 12       # 每天最多 12 条摘要，防止刷屏


def is_merit(text):
    """判断一条消息值不值得给全班看。返回 (是否保留, 类别)"""
    if not text:
        return False, None
    if len(text) < 4:
        return False, None
    if DROP_PATTERNS.match(text.strip()):
        return False, None
    for pat, kind in KEEP_PATTERNS:
        if re.search(pat, text, re.I):
            return True, kind
    return False, None


def digest_text(text):
    """提纯：去寒暄、去「@某人」、压缩空白"""
    t = re.sub(r"@[\w一-龥]{1,12}\s*", "", text)
    t = re.sub(r"(?:[哈呵嗯哦啊]{2,}|[!！?？。]{2,})", " ", t)
    t = re.sub(r"(收到|好的?|谢谢|麻烦了|辛苦了|各位|同学们|同学们好)+", " ", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" -—·:：")
    if len(t) > MAX_LEN:
        t = t[:MAX_LEN] + "…"
    return t


# ---------------- 拉云端群消息 ----------------
def pull_from_cloud(read_token, base=None):
    if not base:
        base = read_base_from_cfg()
    if not base:
        log("config 里没有 wx.worker_url，先跳过拉取")
        return []
    url = base.rstrip("/") + "/qq?token=" + read_token + "&limit=200"
    req = urllib.request.Request(url)
    try:
        with OPENER.open(req, timeout=40) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log("拉云端失败：%r" % e)
        return []
    items = data.get("items") or []
    log("云端拉到 %d 条群消息" % len(items))
    return items


def read_base_from_cfg():
    try:
        with open(CFG, encoding="utf-8") as f:
            c = json.load(f)
        return (c.get("wx") or {}).get("worker_url") or ""
    except Exception:
        return ""


def read_token():
    """拿查询口令：先看 config.json 的 wx.read_token，没有再退回 Cloudflare 那个状态文件。"""
    t = read_cfg_token()
    if t:
        return t
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return json.load(f).get("read_token", "")
    except Exception:
        return ""


def read_cfg_token():
    try:
        with open(CFG, encoding="utf-8") as f:
            c = json.load(f)
        return (c.get("wx") or {}).get("read_token", "")
    except Exception:
        return ""


# ---------------- 存本机（10G 文件夹） ----------------
def save_raw(items, days):
    """原始消息（已脱敏）按月存 jsonl，追加不覆盖。"""
    os.makedirs(RAW, exist_ok=True)
    since = time.time() - days * 86400
    n = 0
    by_month = {}
    for it in items:
        if (it.get("ts") or 0) / 1000 < since:
            continue
        ts = (it.get("ts") or 0) / 1000
        month = time.strftime("%Y-%m", time.localtime(ts))
        by_month.setdefault(month, []).append({
            "ts": it.get("ts"),
            "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)),
            "from": it.get("sender", "QQ群"),
            "text": scrub(it.get("title", "")),
        })
        n += 1
    for month, rows in by_month.items():
        p = os.path.join(RAW, "群-%s.jsonl" % month)
        exist = set()
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                for line in f:
                    try:
                        exist.add(json.loads(line)["ts"])
                    except Exception:
                        pass
        with open(p, "a", encoding="utf-8") as f:
            for r in rows:
                if r["ts"] not in exist:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
    log("本机存档：新增 %d 条 → %s" % (n, RAW))
    return n


# ---------------- 提纯 ----------------
def build_digest(days):
    """从本机存档读原始消息，提纯成摘要。"""
    os.makedirs(DIGEST, exist_ok=True)
    since = time.time() - days * 86400
    picked = []
    if not os.path.isdir(RAW):
        return []
    for fn in sorted(os.listdir(RAW)):
        if not fn.endswith(".jsonl"):
            continue
        with open(os.path.join(RAW, fn), encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if (r.get("ts") or 0) / 1000 < since:
                    continue
                ok, kind = is_merit(r.get("text", ""))
                if not ok:
                    continue
                picked.append({"ts": r["ts"], "date": r.get("time", "")[:10],
                               "text": digest_text(r["text"]), "kind": kind})
    # 按天限流
    cnt = {}
    out = []
    for r in sorted(picked, key=lambda x: -x["ts"]):
        d = r["date"]
        cnt[d] = cnt.get(d, 0) + 1
        if cnt[d] > MAX_PER_DAY:
            continue
        out.append(r)
    out.sort(key=lambda x: -x["ts"])
    if out:
        p = os.path.join(DIGEST, "群摘要-%s.jsonl" % time.strftime("%Y-%m"))
        with open(p, "w", encoding="utf-8") as f:
            for r in out:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    log("提纯：%d 条进摘要（闲聊/签到/表情已丢）" % len(out))
    return out


# ---------------- 发布到网页（同学刷新就能看到） ----------------
def publish(digest_rows, days=3):
    """把摘要写进网页版的 data/qq_digest.json，同学刷新网页即可看到。

    只放**提纯后的摘要**，不放原话——遵守「群内容绝不进公开仓库」的红线。
    """
    if not os.path.isdir(WEB_DATA):
        log("网页版 data 目录不存在：%s" % WEB_DATA)
        return False
    since = time.time() - days * 86400
    rows = [r for r in digest_rows if (r["ts"] or 0) / 1000 >= since]
    rows.sort(key=lambda x: -x["ts"])
    out = {
        "note": "本内容由班委助手自动整理自班群消息（已脱敏：不含姓名/学号/手机号，且仅保留有信息量的通知类内容）。"
                "如需查看原文请在班群内翻记录。",
        "count": len(rows),
        "updated": int(time.time() * 1000),
        "updated_text": time.strftime("%Y-%m-%d %H:%M"),
        # ⚠️ 字段名跟云函数版保持一致（云端写 GitHub，本机写本地，两边会被网页一起读）
        "items": [{"id": "local_" + str(r["ts"]),
                   "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime((r["ts"] or 0) / 1000)),
                   "timeText": time.strftime("%m-%d %H:%M", time.localtime((r["ts"] or 0) / 1000)),
                   "text": r["text"], "kind": r["kind"]} for r in rows],
    }
    p = os.path.join(WEB_DATA, "qq_digest.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    log("已发布到网页：%s（%d 条）" % (p, len(rows)))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--no-publish", action="store_true")
    ap.add_argument("--daemon", action="store_true", help="常驻，每 10 分钟拉一次")
    a = ap.parse_args()

    def once():
        rt = read_token()
        if not rt:
            log("拿不到 READ_TOKEN，先去 config.json 的 wx.read_token 填上")
            return
        items = pull_from_cloud(rt)
        save_raw(items, a.days)
        dig = build_digest(a.days)
        if not a.no_publish:
            publish(dig, a.days)

    if a.daemon:
        log("常驻模式：每 10 分钟拉一次，Ctrl+C 停")
        while True:
            try:
                once()
            except Exception as e:
                log("本轮出错：%r" % e)
            time.sleep(600)
    else:
        once()


if __name__ == "__main__":
    main()
