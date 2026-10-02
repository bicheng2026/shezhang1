# -*- coding: utf-8 -*-
"""QQ 官方机器人接入（WebSocket 长连接）

标准库实现，不装任何第三方包。

协议流程：
  1. POST https://bots.qq.com/app/getAppAccessToken  拿 access_token
  2. GET  https://api.sgroup.qq.com/gateway          拿 wss 网关地址
  3. 连 WebSocket → 收到 op=10 Hello（含心跳周期）
  4. 发 op=2 Identify（带 intents）→ 鉴权
  5. 收到 op=0 Dispatch 事件（群消息在这里来）
  6. 周期性发 op=1 心跳（带最新序列号 s）

Intents 位（官方文档）：
  1 << 0   GUILDS
  1 << 25  GROUP_AND_C2C_EVENT  ← 群消息全量模式靠它
  1 << 30  AT_MESSAGES
  收不到消息时，把 intents 改成 1 | (1<<25) | (1<<30) 再试。
"""
import os
import ssl
import json
import time
import base64
import struct
import socket
import threading
import urllib.parse
import urllib.request

INTENT_GUILDS = 1 << 0
INTENT_GROUP_AND_C2C = 1 << 25
INTENT_AT_MESSAGES = 1 << 30

BASE_INTENTS = INTENT_GUILDS | INTENT_GROUP_AND_C2C

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"


# ==================== 精简 WebSocket 客户端 ====================
class WSClient(object):
    """够用就好的 WebSocket 客户端：文本帧 + ping/pong + close。
    刻意不引第三方包，免得给主公机器塞一堆依赖。"""

    def __init__(self, url, on_message, on_log=None, timeout=20):
        self.url = url
        self.on_message = on_message
        self.log = on_log or (lambda *a: None)
        self.timeout = timeout
        self.sock = None
        self.buf = b""
        self.stop = threading.Event()

    # ---- 握手 ----
    def connect(self):
        u = urllib.parse.urlparse(self.url)
        host = u.hostname
        port = u.port or (443 if u.scheme == "wss" else 80)
        path = (u.path or "/") + (("?" + u.query) if u.query else "")

        s = socket.create_connection((host, port), timeout=self.timeout)
        if u.scheme == "wss":
            ctx = ssl.create_default_context()
            s = ctx.wrap_socket(s, server_hostname=host)
        self.sock = s

        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, key))
        s.sendall(req.encode())

        head = b""
        while b"\r\n\r\n" not in head:
            c = s.recv(4096)
            if not c:
                raise IOError("握手时被对方断开")
            head += c
        head, self.buf = head.split(b"\r\n\r\n", 1)
        if b"101" not in head.split(b"\r\n")[0]:
            raise IOError("握手失败：" + head[:200].decode("utf-8", "replace"))
        self.log("WebSocket 已连接")

    # ---- 收帧 ----
    def _need(self, n):
        while len(self.buf) < n:
            c = self.sock.recv(65536)
            if not c:
                raise IOError("连接已断开")
            self.buf += c

    def read_frame(self):
        self._need(2)
        b0, b1 = self.buf[0], self.buf[1]
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        ln = b1 & 0x7F
        off = 2
        if ln == 126:
            self._need(4)
            ln = struct.unpack(">H", self.buf[2:4])[0]
            off = 4
        elif ln == 127:
            self._need(10)
            ln = struct.unpack(">Q", self.buf[2:10])[0]
            off = 10
        mask = b""
        if masked:
            self._need(off + 4)
            mask = self.buf[off:off + 4]
            off += 4
        self._need(off + ln)
        payload = bytearray(self.buf[off:off + ln])
        if masked:
            for i in range(len(payload)):
                payload[i] ^= mask[i % 4]
        self.buf = self.buf[off + ln:]
        return opcode, bytes(payload)

    # ---- 发帧（客户端必须掩码）----
    def send_frame(self, opcode, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        hdr = bytearray([0x80 | opcode])
        n = len(data)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        body = bytearray(data)
        for i in range(len(body)):
            body[i] ^= mask[i % 4]
        self.sock.sendall(bytes(hdr) + bytes(body))

    def send_text(self, s):
        self.send_frame(0x1, s)

    # ---- 主循环 ----
    def loop(self):
        while not self.stop.is_set():
            try:
                op, payload = self.read_frame()
            except (IOError, socket.timeout, ssl.SSLError) as e:
                self.log("连接中断：%r" % e)
                return
            except Exception as e:
                self.log("收帧异常：%r" % e)
                return
            if op == 0x1:                       # 文本帧
                try:
                    msg = json.loads(payload.decode("utf-8"))
                except Exception:
                    continue
                try:
                    self.on_message(msg)
                except Exception as e:
                    self.log("处理消息出错：%r" % e)
            elif op == 0x9:                     # ping
                try:
                    self.send_frame(0xA, payload)
                except Exception:
                    return
            elif op == 0x8:                     # 关闭
                self.log("服务端要求关闭连接")
                return

    def close(self):
        self.stop.set()
        try:
            self.send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


# ==================== QQ 机器人 ====================
def _http(url, data=None, headers=None, timeout=20):
    """统一的 HTTP 请求，绕开本机代理残留"""
    import urllib.request
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with opener.open(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


class QQBot(object):
    def __init__(self, appid, secret, on_message, intents=BASE_INTENTS,
                 sandbox=False, on_log=None):
        self.appid = str(appid)
        self.secret = secret
        self.on_message = on_message          # func(group_openid, sender, text, ts)
        self.intents = intents
        self.sandbox = sandbox
        self.log = on_log or (lambda *a: None)
        self.token = ""
        self.token_at = 0
        self.seq = None
        self.ws = None
        self.hb = None
        # 运行状态：给本机看板顶部的状态条 / GET /api/qqstatus 读。
        # 「点刷新也没见到新消息」这类问题，看一眼 state 就明白了：
        #   接收中 = 通的（那问题在界面没显示）；断开重连中/令牌失败 = 通道断了。
        self.status = {
            "state": "未启动",        # 未启动 / 已取令牌 / 连接中 / 接收中 / 断开重连中 / 鉴权被拒 / 令牌失败
            "ok": False,              # 是否处于正常接收状态
            "last_msg_ts": 0,         # 最后收到群消息的时间
            "last_msg_from": "",
            "last_msg_text": "",
            "msg_count": 0,
            "err": "",
            "since": int(time.time()),
        }

    # ---- 1. 取令牌 ----
    def fetch_token(self):
        body = json.dumps({"appId": self.appid,
                           "clientSecret": self.secret}).encode("utf-8")
        st, txt = _http(TOKEN_URL, data=body,
                        headers={"Content-Type": "application/json"})
        d = json.loads(txt)
        if "access_token" not in d:
            # 11298 = 调用方 IP 不在白名单；100016 = AppID/Secret 不对
            self.status.update(state="令牌失败", ok=False, err=txt[:200])
            raise RuntimeError("取令牌失败（HTTP %s）：%s" % (st, txt[:300]))
        self.token = d["access_token"]
        self.token_at = time.time()
        self.status.update(state="已取令牌", ok=False, err="")
        return self.token

    # ---- 2. 取网关 ----
    def fetch_gateway(self):
        url = API_BASE + "/gateway"
        st, txt = _http(url, headers={"Authorization": "QQBot " + self.token})
        d = json.loads(txt)
        if "url" not in d:
            raise RuntimeError("取网关失败（HTTP %s）：%s" % (st, txt[:300]))
        return d["url"]

    # ---- 3. 鉴权 ----
    def identify(self):
        payload = {
            "op": 2,
            "d": {
                "token": "QQBot " + self.token,
                "intents": self.intents,
                "shard": [0, 1],
                "properties": {"$os": "windows",
                               "$browser": "shezhang1",
                               "$device": "shezhang1"}
            }
        }
        self.ws.send_text(json.dumps(payload))

    # ---- 4. 心跳 ----
    def heartbeat_loop(self, interval):
        while not self.ws.stop.is_set():
            time.sleep(max(1.0, interval))
            try:
                self.ws.send_text(json.dumps({"op": 1, "d": self.seq}))
            except Exception:
                return

    # 高频但无信息量的事件，不打日志刷屏
    _muted_types = {"MESSAGE_REACTION", "GUILD_MEMBER_ADD", "GUILD_MEMBER_REMOVE"}

    # ---- 5. 收事件 ----
    def on_ws(self, msg):
        op = msg.get("op")
        d = msg.get("d")
        if op == 10:                                  # Hello
            interval = (d or {}).get("heartbeat_interval", 45000) / 1000.0
            self.log("收到 Hello，心跳周期 %.0f 秒" % interval)
            self.identify()
            self.hb = threading.Thread(target=self.heartbeat_loop,
                                       args=(interval,), daemon=True)
            self.hb.start()
            return

        if op == 0:                                   # Dispatch
            t = msg.get("t")
            self.seq = msg.get("s")
            if t == "READY":
                self.log("✓ 鉴权成功，开始接收群消息")
                self.status.update(state="接收中", ok=True, err="",
                                   since=int(time.time()))
            elif t and t.endswith("_MESSAGE_CREATE"):
                self._dispatch(d, t)
            else:
                # 其他事件也露个头——用来确认机器人到底被授权了哪些事件
                if t not in self._muted_types:
                    self.log("事件 %s（非消息类，忽略）" % t)
            return

        if op == 11:                                  # 心跳回执
            return
        if op == 7:                                   # 要求重连
            self.log("服务端要求重连")
            self.ws.stop.set()
            return
        if op == 9:                                   # 参数错误
            self.status.update(state="鉴权被拒", ok=False,
                               err="检查 AppID/Secret 与 intents")
            self.log("鉴权被拒（op=9），检查 AppID/Secret 和 intents：" + json.dumps(msg)[:300])
            self.ws.stop.set()

    def _dispatch(self, d, kind):
        d = d or {}
        gid = d.get("group_openid") or ("私聊" if kind == "C2C_MESSAGE_CREATE" else "?")
        # 先原样记一笔——排查"某个群的消息收不到"时靠这行
        self.log("← 事件 %s ｜ 群 openid=%s ｜ 原文=%r"
                 % (kind, gid, ((d.get("content") or "")[:80])))
        text = (d.get("content") or "").strip()
        if not text:
            # 兜底：从 msg_elements 里拼
            for el in (d.get("msg_elements") or []):
                tb = (el.get("text") or {})
                if tb.get("text"):
                    text += tb["text"]
            text = text.strip()
        if not text:
            return
        author = (d.get("author") or {}).get("username") or \
                 (d.get("author") or {}).get("id") or "?"
        group = d.get("group_openid") or ("私聊" if kind == "C2C_MESSAGE_CREATE" else "?")
        ts = int(time.time())
        stamp = d.get("timestamp")
        if stamp:
            try:
                from datetime import datetime
                ts = int(datetime.fromisoformat(
                    stamp.replace("Z", "+00:00")).timestamp())
            except Exception:
                pass
        self.status.update(last_msg_ts=int(time.time()), last_msg_from=author,
                           last_msg_text=text[:60],
                           msg_count=int(self.status.get("msg_count") or 0) + 1)
        self.on_message(group, author, text, ts)

    # ---- 主动发消息 ----
    def _api(self, method, path, body):
        st, txt = _http(API_BASE + path, data=json.dumps(body).encode("utf-8"),
                        headers={"Authorization": "QQBot " + self.token,
                                 "Content-Type": "application/json"})
        try:
            d = json.loads(txt)
        except Exception:
            d = {"raw": txt}
        return st, d

    def send_group_msg(self, group_openid, content, msg_id=None):
        """向指定群主动发一条文本。需要群里开了「允许机器人主动发言」。"""
        if not self.token:
            return False, "尚未取得令牌"
        self.msg_seq = (getattr(self, "msg_seq", 0) + 1) % 100000
        body = {"content": content[:1500], "msg_type": 0,
                "msg_seq": self.msg_seq}
        if msg_id:
            body["msg_id"] = msg_id
        st, d = self._api("POST", "/v2/groups/%s/messages" % group_openid, body)
        ok = st == 200 and "code" not in d
        return ok, json.dumps(d, ensure_ascii=False)[:200]

    def send_c2c_msg(self, user_openid, content, msg_id=None):
        """向指定用户发私聊消息。需要用户先跟机器人说过话。"""
        if not self.token:
            return False, "尚未取得令牌"
        self.msg_seq = (getattr(self, "msg_seq", 0) + 1) % 100000
        body = {"content": content[:1500], "msg_type": 0,
                "msg_seq": self.msg_seq}
        if msg_id:
            body["msg_id"] = msg_id
        st, d = self._api("POST", "/v2/users/%s/messages" % user_openid, body)
        ok = st == 200 and "code" not in d
        return ok, json.dumps(d, ensure_ascii=False)[:200]

    # ---- 主循环（带重连）----
    def run(self, stop_event):
        backoff = 3
        while not stop_event.is_set():
            try:
                self.fetch_token()
                self.log("已取到 access_token")
                gw = self.fetch_gateway()
                self.log("网关地址：%s" % gw[:60])
                # 超时必须大于心跳间隔（腾讯给的是 41 秒），否则两次心跳之间必然读超时
                # → 表现为每 20 秒无谓重连一次
                self.ws = WSClient(gw, self.on_ws, on_log=self.log, timeout=100)
                self.ws.connect()
                self.status.update(state="连接中", ok=False, err="")
                self.ws.loop()
                backoff = 3
            except RuntimeError as e:
                self.status.update(state=(self.status.get("state") or "启动失败"),
                                   ok=False)
                self.log("启动失败：%s" % e)
                stop_event.wait(30)        # 令牌/网关失败，慢点重试
                continue
            except Exception as e:
                self.log("异常：%r" % e)
            if stop_event.is_set():
                break
            self.status.update(state="断开重连中", ok=False)
            self.log("%d 秒后重连" % backoff)
            stop_event.wait(backoff)
            backoff = min(backoff * 2, 60)
