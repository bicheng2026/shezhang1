# -*- coding: utf-8 -*-
"""用 GitHub REST API 推送改动（git push 走不通时的替代方案）

本机坑：WorkBuddy 沙箱注入了 HTTP_PROXY=http://127.0.0.1:61737，
git 走这个代理推 GitHub 会 "CONNECT tunnel failed, response 502"；
直连又超时。但 Python 用 ProxyHandler({}) 直连 api.github.com 是通的。

流程（Git Data API，一次提交搞定，不会像逐文件 PUT 那样刷 37 个 commit）：
  1. 取 main 最新 commit / tree
  2. 为每个改动文件创建 blob
  3. 基于原 tree 创建新 tree
  4. 创建 commit
  5. 更新 refs/heads/main
"""
import os
import ssl
import json
import sys
import time
import base64
import subprocess
import urllib.request

ROOT = r"E:\白求恩一号\蛇杖一号\网页版"
REPO = "bicheng2026/shezhang1"
TOKEN = "<在此填你的 GitHub token>"

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
OP = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=CTX))


def api(method, path, data=None, tries=6):
    """调 GitHub API，带重试。

    这台机器到 api.github.com 的连接不稳：常出现
    WinError 10054（连接被重置）或 10060（超时）。
    重试是安全的——blob/tree 的 sha 由内容决定，重复创建会复用；
    commit 即使重复也只会留下一个无人指向的孤儿，不影响 main。
    """
    last = None
    for i in range(tries):
        try:
            url = "https://api.github.com/repos/%s%s" % (REPO, path)
            body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data else None
            req = urllib.request.Request(url, data=body, method=method, headers={
                "Authorization": "token " + TOKEN,
                "Accept": "application/vnd.github+json",
                "User-Agent": "shezhang1-bridge",
                "Content-Type": "application/json"})
            with OP.open(req, timeout=90) as r:
                raw = r.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {}
        except Exception as e:
            last = e
            print("   [重试 %d/%d] %s %s ← %r" % (i + 1, tries, method, path, e))
            time.sleep(2 + 2 * i)
    raise last


def changed_files():
    """取上一次 commit 改动的文件（-z 避免中文路径被引号包裹）"""
    out = subprocess.run(["git", "diff", "--name-only", "-z", "HEAD~1", "HEAD"],
                         cwd=ROOT, capture_output=True)
    raw = out.stdout.decode("utf-8")
    return [p for p in raw.split("\0") if p.strip()]


def main():
    # 手动指定：python push_via_api.py 网页版/index.html data/todo.json
    # 不指定就退回「上一次 commit 改动的文件」
    manual = [a for a in sys.argv[1:] if not a.startswith("-")]
    files = manual or changed_files()
    print("待推送 %d 个文件" % len(files))
    for p in files:
        fp = os.path.join(ROOT, p)
        if not os.path.exists(fp):
            print("  (跳过，已删除) %s" % p)
        else:
            print("  %-52s %8.1f KB" % (p, os.path.getsize(fp) / 1024))

    # 1. 当前 main
    ref = api("GET", "/git/ref/heads/main")
    head_sha = ref["object"]["sha"]
    commit = api("GET", "/git/commits/" + head_sha)
    base_tree = commit["tree"]["sha"]
    print("\n远端 main = %s，base tree = %s" % (head_sha[:10], base_tree[:10]))

    # 2. 逐个创建 blob
    tree = []
    for p in files:
        fp = os.path.join(ROOT, p)
        if not os.path.exists(fp):
            continue
        with open(fp, "rb") as f:
            content = f.read()
        b64 = base64.b64encode(content).decode("ascii")
        blob = api("POST", "/git/blobs",
                   {"content": b64, "encoding": "base64"})
        # 路径统一用 / 分隔，GitHub API 要求
        tree.append({"path": p.replace("\\", "/"), "mode": "100644",
                     "type": "blob", "sha": blob["sha"]})
        print("  blob %-50s %s" % (p[:50], blob["sha"][:10]))

    print("\n创建 tree（%d 项）…" % len(tree))
    new_tree = api("POST", "/git/trees", {"base_tree": base_tree, "tree": tree})

    # 提交说明：--msg 指定，没指定就用文件清单拼一个
    msg = "更新 " + "、".join(os.path.basename(p) for p in files)
    for a in sys.argv[1:]:
        if a.startswith("--msg="):
            msg = a[len("--msg="):]
    print("创建 commit…")
    new_commit = api("POST", "/git/commits", {
        "message": msg,
        "tree": new_tree["sha"],
        "parents": [head_sha]
    })

    print("更新 main 引用…")
    api("PATCH", "/git/refs/heads/main", {"sha": new_commit["sha"]})
    print("\n✓ 推送完成")
    print("  commit: %s" % new_commit["sha"][:12])
    print("  https://github.com/%s/commit/%s" % (REPO, new_commit["sha"][:12]))


if __name__ == "__main__":
    main()
