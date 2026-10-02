#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手机端通知栏小守卫：班群来了新消息，弹一条系统通知。

为什么需要它：微信小程序的 WebSocket 一进后台就被掐断，小程序给不了你
"锁屏也弹消息"的能力。所以提醒这一环只能落在手机本地——这个文件每 2 分钟
读一次 bridge.db 里 QQ 来源的最新时间戳，发现新条目就调 termux-notification
弹通知。开销可忽略（一次查询几毫秒，一天约 720 次，基本不叫醒 CPU）。

依赖：Termux:API（F-Droid 装）。没装也能跑，只是不弹通知，不报错。

用法：python phone_alarm.py
"""
import os
import sqlite3
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "bridge.db")
STATE = os.path.join(HERE, "last_ts.txt")
INTERVAL = 120          # 轮询间隔（秒）
URGENT = ("P0", "P1")   # 只有这两级弹通知，免得群聊刷屏


def max_ts():
    """本地库里 QQ 来源的最新时间戳。"""
    if not os.path.exists(DB):
        return 0
    try:
        con = sqlite3.connect(DB)
        cur = con.execute(
            "SELECT MAX(ts) FROM items WHERE source='qq' AND status<>'archived'")
        val = cur.fetchone()[0] or 0
        con.close()
        return int(val)
    except Exception:
        return 0


def newest(ts):
    """取最新那条的等级和标题。"""
    try:
        con = sqlite3.connect(DB)
        cur = con.execute(
            "SELECT level, title FROM items WHERE source='qq' AND ts>=? "
            "AND status<>'archived' "
            "ORDER BY (CASE level WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 "
            "WHEN 'P2' THEN 2 ELSE 3 END), ts DESC LIMIT 1", (ts,))
        row = cur.fetchone()
        con.close()
        return row
    except Exception:
        return None


def notify(title, content):
    try:
        subprocess.run(
            ["termux-notification", "--title", title, "--content", content,
             "--priority", "high"],
            timeout=10)
    except Exception:
        pass


def main():
    last = 0
    if os.path.exists(STATE):
        try:
            last = int(open(STATE, encoding="utf-8").read().strip() or 0)
        except Exception:
            last = 0

    while True:
        now = max_ts()
        if now > last + 5:                    # +5 秒防抖，避开时间戳抖动
            row = newest(now)
            if row:
                level = row[0] or "P3"
                title = (row[1] or "").strip()
                if level in URGENT and title:
                    mark = "[紧急]" if level == "P0" else "[要紧]"
                    notify("蛇杖 %s %s" % (mark, level), title[:42])
                    try:
                        with open(STATE, "w", encoding="utf-8") as f:
                            f.write(str(now))
                    except Exception:
                        pass
                else:
                    # 非紧急也记一下，下次对比用得上
                    try:
                        with open(STATE, "w", encoding="utf-8") as f:
                            f.write(str(now))
                    except Exception:
                        pass
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
