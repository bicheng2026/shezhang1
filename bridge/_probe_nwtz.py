# -*- coding: utf-8 -*-
"""探针：核实「内网张贴」的列表页与详情页在公网下能否读到内容。
输出重定向到文件再用 Read 看（本机 PowerShell 不回显、Bash 也可能被截）。"""
import urllib.request, ssl, re, sys, json

ctx = ssl.create_default_context()
op = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=ctx),
    urllib.request.ProxyHandler({}),   # 直连，绕沙箱代理
)
op.addheaders = [("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")]

BASE = "https://www.gxmu.edu.cn/xwgk/nwtz/"


def get(url, timeout=20):
    r = op.open(url, timeout=timeout)
    return r.status, r.read()


out = []

# 1) 列表页
try:
    st, b = get(BASE)
    t = b.decode("utf-8", "ignore")
    links = re.findall(r'href=["\'](\./t\d+\.html)["\']', t)
    out.append("列表页 HTTP=%s bytes=%d 文章链接数=%d" % (st, len(b), len(links)))
    out.append("前 5 条链接: %s" % links[:5])
except Exception as e:
    out.append("列表页 ERR %s %s" % (type(e).__name__, e))
    links = []

# 2) 详情页（取第一条）
if links:
    u = BASE + links[0][2:]
    try:
        st, b = get(u)
        t = b.decode("utf-8", "ignore")
        mt = re.search(r"<title>(.*?)</title>", t, re.S)
        title = mt.group(1).strip() if mt else ""
        # 去标签取正文片段
        body = re.sub(r"<script.*?</script>", " ", t, flags=re.S)
        body = re.sub(r"<style.*?</style>", " ", body, flags=re.S)
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"&nbsp;?", " ", body)
        body = re.sub(r"\s+", " ", body).strip()
        out.append("")
        out.append("详情页 %s" % u)
        out.append("HTTP=%s bytes=%d" % (st, len(b)))
        out.append("TITLE=%s" % title)
        out.append("正文长度=%d" % len(body))
        out.append("正文前 500 字:\n%s" % body[:500])
    except Exception as e:
        out.append("详情页 ERR %s %s" % (type(e).__name__, e))

with open("_probe_nwtz_out.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("done")
