# -*- coding: utf-8 -*-
"""蛇杖一号 · 信息桥梁（本机守护）

零第三方依赖，Python 3.9+ 标准库直跑。

干三件事：
  1. 收  —— 接收 QQ 群实时消息（OneBot v11 HTTP 上报）、聊天记录导入、学院公告抓取
  2. 理  —— 本地规则定级 P0~P3、去重、抽 DDL，全部落 SQLite（数据只在本机）
  3. 出  —— 生成待办看板（内置网页）+ 定时推送到微信

机器负责整理，人负责判断。所有定级都留「判断依据」，主公随时能改。

用法：
    python bridge.py init                 # 生成默认配置
    python bridge.py run                  # 常驻运行（收 + 定时推送 + 看板）
    python bridge.py list [--all]         # 看当前待办
    python bridge.py import <聊天记录.txt> # 导入聊天记录，自动整理
    python bridge.py crawl                # 抓学院/官网公告
    python bridge.py push [--dry]         # 立即推送到微信（--dry 只预览）
    python bridge.py set <id> <P0|P1|P2|P3|done|arch>   # 人工改状态/优先级
"""
import os
import re
import io
import json
import time
import html
import sqlite3
import hashlib
import datetime
import threading
import traceback
import atexit
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    import qqbot as _qqbot        # QQ 官方机器人（WebSocket），同目录
except Exception:
    _qqbot = None

# 版本号：对外分发时改成打包当天日期（形如 YYYY.MM.DD）。
# 看板页脚和分发包文件名都用它，方便对方报问题时报得出是哪一版。
VERSION = "2026.09.30"

# ============ 路径 ============
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                      # 蛇杖一号
WEB = os.path.join(ROOT, "网页版")
CONFIG = os.path.join(HERE, "config.json")
DB_PATH = os.path.join(HERE, "bridge.db")
STATE_PATH = os.path.join(HERE, "state.json")
LOG_PATH = os.path.join(HERE, "bridge.log")

# Windows 中文控制台
try:
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def log(*a):
    line = "[%s] %s" % (datetime.datetime.now().strftime("%H:%M:%S"),
                        " ".join(str(x) for x in a))
    # 有控制台就顺手打出来；无窗口运行时（pythonw —— 双击 bat 走的就是它）
    # stdout 是空的、print 会静默跳过，所以同时落一份文件，出问题才有据可查。
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def rotate_log(max_bytes=2 * 1024 * 1024):
    """日志超上限就轮转一份，免得无窗口长期运行把文件撑爆。"""
    try:
        if os.path.exists(LOG_PATH) and os.getsize(LOG_PATH) > max_bytes:
            bak = LOG_PATH + ".1"
            if os.path.exists(bak):
                os.remove(bak)
            os.replace(LOG_PATH, bak)
    except Exception:
        pass


# ============ 单实例锁 ============
# 同时只允许一个桥接在跑：两个实例会各自起一个 QQ 机器人，抢同一个 token，
# QQ 一个 token 只稳定推给一个连接 —— 结果谁都收不到群消息（2026-09-30 实测撞过：
# 网页刷新 + 手动双击在同一秒各起一个，导致此后整整半天收不到任何群消息）。
# 用 O_CREAT|O_EXCL 做原子锁，第二个实例在启动 QQ 机器人之前就主动退出。
LOCK_PATH = os.path.join(HERE, "bridge.lock")


def _pid_alive(pid):
    try:
        pid = int(pid)
    except Exception:
        return False
    try:
        os.kill(pid, 0)          # Windows 下 signal 0 = 仅探测进程是否存在
        return True
    except Exception:
        return False


def acquire_lock():
    """拿到锁返回 True；拿不到（已有实例活着）返回 False。旧锁指向已死进程则清理后重试。"""
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        old = ""
        try:
            old = open(LOCK_PATH).read().strip()
        except Exception:
            pass
        if old and _pid_alive(old):
            log("已有桥接实例在跑（PID %s），本实例退出，避免两个机器人抢同一个 QQ token。" % old)
            return False
        # 锁是陈旧的（进程早已被强杀），清掉重来
        try:
            os.remove(LOCK_PATH)
        except Exception:
            pass
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            log("锁文件异常，疑似并发启动，本实例退出。")
            return False
    with os.fdopen(fd, "w") as f:
        f.write(str(os.getpid()))
    return True


def release_lock():
    try:
        if os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)
    except Exception:
        pass


atexit.register(release_lock)


# ============ 配置 ============
DEFAULT_CONFIG = {
    "listen_host": "127.0.0.1",
    "listen_port": 8890,
    "onebot_api": "http://127.0.0.1:3000",        # NapCat HTTP API 地址（用于取群名/主动发消息）
    "access_token": "",                            # OneBot 上报校验 token，没有就留空
    "watch_groups": [],                            # 只处理这些群，留空=全部。例：[123456, 789012]
    "self_keywords": ["冯新栋", "班长"],            # 命中即认为@我 → 提级
    "daily_push_time": "07:30",                    # 每天推送时刻
    "push_channel": "serverchan",                  # serverchan | pushplus | wecom | none
    "push": {
        # 三选一填一个即可，另外一个留空
        "serverchan_key": "",                      # https://sct.ftqq.com  → SCTxxxxx
        "pushplus_token": "",                      # https://www.pushplus.plus → token
        "wecom_webhook": ""                        # 企业微信群机器人 webhook
    },
    "max_level": "P3",                             # 优先级上限，超过的一律不入库（防淹没）
    "keep_days": 30,                               # 回溯保留天数
    "expire": {                                    # 时效期：过了就自动清走，不用人手删
        "grace_days": 2,                           # 有截止日的，过了截止日再留 2 天
        "no_ddl_keep_days": 14,                    # 没有截止日的，最多留 14 天
        "noise_keep_days": 7,                      # 观测簿（闲聊/不沾画像）只留 7 天
        "purge_archived_days": 60                  # 已归档的再放 60 天，之后彻底删除
    },
    "profile": {                                   # 画像：只让跟主公相关的事浮上来
        "enabled": True,
        "apply_to": ["crawl"],                     # 只用在公告抓取上；QQ群消息本就都是相关的
        "keywords": ["本科", "本科生", "四六级", "四级", "六级", "重修", "补考", "补修",
                     "选课", "课表", "放假", "假期", "开学", "奖学金", "综测", "助学金",
                     "班级", "班长", "团籍", "青年大学习", "体检", "普通话", "创新创业",
                     "大创", "竞赛", "实习", "见习", "教务", "学籍", "考试"],
        "must_have_any": []                        # 留空=上面任一命中即可
    },
    "sites": [                                     # 学院公告源，自己往下加
        {"name": "学校通知公告", "url": "https://www.gxmu.edu.cn/xwgk/wwtz/", "limit": 30},
        {"name": "内网张贴", "url": "https://www.gxmu.edu.cn/xwgk/nwtz/", "limit": 30}
    ]
}


def load_config():
    if not os.path.exists(CONFIG):
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        log("配置读取失败，用默认值：", e)
        return dict(DEFAULT_CONFIG)
    for k, v in DEFAULT_CONFIG.items():
        if k not in cfg:
            cfg[k] = v
        elif isinstance(v, dict):
            for kk, vv in v.items():
                cfg[k].setdefault(kk, vv)
    return cfg


def save_config(cfg):
    """原子写回 config.json（看板里填 QQ 机器人 AppID/Secret 走这里）"""
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG)


# 运行期共享状态。
#   bot       当前 QQ 机器人实例（看板读它的 .status 显示「通没通」）
#   qq_stops  历次 QQ 监听线程的 stop 事件（热重启/退出时统一置位）
#   sync_busy 是否正在手动同步（防重点）
#   last_sync 最近一次同步完成时间
RT = {"bot": None, "qq_stops": [], "sync_busy": False, "last_sync": ""}


def qq_status(cfg):
    """QQ 机器人现状：给看板状态条和 GET /api/qqstatus 用。"""
    cfg = cfg or {}
    q = cfg.get("qq_official") or {}
    bot = RT.get("bot")
    if bot is not None:
        st = dict(bot.status or {})
    else:
        st = {"state": "未启动", "ok": False, "last_msg_ts": 0, "last_msg_from": "",
              "last_msg_text": "", "msg_count": 0, "err": "", "since": 0}
    return {
        "enabled": bool(q.get("enabled")),
        "configured": bool(str(q.get("appid") or "").strip() and q.get("secret")),
        "appid": str(q.get("appid") or "").strip(),
        "masked_secret": ("*" * 6 + str(q.get("secret"))[-4:]) if q.get("secret") else "",
        "intents": int(q.get("intents") or 33554433),
        "running": bot is not None,
        "status": st,
        "sync_busy": bool(RT.get("sync_busy")),
        "last_sync": RT.get("last_sync") or "",
    }


def restart_qqbot(store, cfg):
    """改完 AppID/Secret 后热重启 QQ 监听，不必重启整个服务。
    旧线程的 stop 事件统一置位；新线程用新 event，并登记进 qq_stops。"""
    bot = RT.get("bot")
    try:
        if bot is not None and getattr(bot, "ws", None) is not None:
            bot.ws.stop.set()          # 光置位 stop 不够——WS 收帧会卡在 recv 上
    except Exception:
        pass
    for e in (RT.get("qq_stops") or []):
        e.set()
    ev = threading.Event()
    RT.setdefault("qq_stops", []).append(ev)
    RT["bot"] = start_qqbot(store, cfg, ev)
    return RT["bot"]


# ============ 存储 ============
SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,          -- qq | import | crawl
    src_name TEXT,                 -- 群名 / 文件名 / 站点名
    sender TEXT,                   -- 发言人（QQ场景）
    ts INTEGER,                    -- 消息时间戳
    raw TEXT,                      -- 原文
    title TEXT,                    -- 抽取标题
    level TEXT NOT NULL,           -- P0 P1 P2 P3
    reason TEXT,                   -- 判断依据（给人看）
    ddl TEXT,                      -- 截止日期 YYYY-MM-DD，可能为空
    status TEXT NOT NULL DEFAULT 'open',   -- open | done | archived
    pin INTEGER DEFAULT 0,         -- 1=置顶显示（比赛/竞赛类，主公要求排最前）
    hash TEXT UNIQUE,
    created_at INTEGER,
    updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status, ddl, level);
"""


class Store(object):
    def __init__(self, path=DB_PATH):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            # 老库补列：pin 是后加的字段，SQLite 没这句旧表就永远没有它。
            # 重复执行会抛 duplicate column，忽略即可。
            try:
                self.conn.execute("ALTER TABLE items ADD COLUMN pin INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            self.conn.commit()

    def add(self, **kw):
        """入库，返回 None 表示重复已被吃掉"""
        h = kw["hash"]
        now = int(time.time())
        with self.lock:
            cur = self.conn.execute("SELECT id FROM items WHERE hash=?", (h,))
            if cur.fetchone():
                return None
            cur = self.conn.execute(
                "INSERT INTO items(source,src_name,sender,ts,raw,title,level,reason,ddl,status,pin,hash,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (kw["source"], kw.get("src_name", ""), kw.get("sender", ""), kw.get("ts", now),
                 kw.get("raw", ""), kw.get("title", ""), kw["level"], kw.get("reason", ""),
                 kw.get("ddl", ""), kw.get("status", "open"), int(kw.get("pin") or 0),
                 h, now, now))
            self.conn.commit()
            return cur.lastrowid

    def rows(self, where="status='open'", args=()):
        with self.lock:
            cur = self.conn.execute(
                "SELECT * FROM items WHERE %s ORDER BY pin DESC, "
                "(CASE level WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END), "
                "(CASE WHEN ddl='' THEN '9999-99-99' ELSE ddl END), ts DESC" % where, args)
            return [dict(r) for r in cur.fetchall()]

    def update(self, item_id, **fields):
        sets, vals = [], []
        # ⚠️ raw 必须在这个白名单里。之前漏了它，导致 reddl 补抓的正文
        # 「写不进去」——每次都重抓、且 regrade 读到的 raw 只有标题，
        # 反手又把 ddl 覆盖成空。一个字段漏登记，连锁两个症状。
        allow = ("level", "status", "title", "ddl", "reason", "raw", "pin")
        for k, v in fields.items():
            if k in allow:
                sets.append("%s=?" % k)
                vals.append(v)
        if not sets:
            return False
        vals += [int(time.time()), item_id]
        with self.lock:
            self.conn.execute("UPDATE items SET %s, updated_at=? WHERE id=?" % ",".join(sets), vals)
            self.conn.commit()
        return True

    def get(self, item_id):
        with self.lock:
            cur = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,))
            r = cur.fetchone()
            return dict(r) if r else None


# ============ 规则引擎（本地定级，模型不参与） ============
LEVEL_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

# 权重表：命中即加分，命中 P0 词立即封顶
P0_WORDS = ["截止今天", "今天截止", "今晚截止", "马上", "立刻", "紧急", "逾期", "最后一天",
            "必须今天", "立刻本人", "@全体成员"]
P1_WORDS = ["截止", "ddl", "deadline", "务必", "务必完成", "必须", "不得缺席", "逾期不候",
            "报名截至", "材料截止", "限时", "明天上午", "明天下午"]
P2_WORDS = ["通知", "作业", "提交", "上交", "报名", "综测", "奖学金", "助学金", "选课",
            "考试安排", "重修", "补考", "团课", "团籍", "青年大学习", "体检", "班会",
            "报告", "实验", "论文", "表", "填写", "核对", "签名", "签字", "截图",
            "回执", "签到", "请假", "销假", "医保", "学籍", "信息核对", "干部", "班委",
            "公示", "评审", "认定", "材料", "对接", "班长会", "通知群"]
DDL_HINT = ["截止", "截至", "ddl", "deadline", "之前交", "前完成", "前提交", "前上交", "前反馈"]


def _today():
    return datetime.date.today()


def parse_ddl(text):
    """从文本里抠截止日期。支持：10月8日 / 10-08 / 10/8 / 本周三 / 下周一 / 今天 / 明天"""
    t = text.replace("号", "日")
    today = _today()

    # 截止语境优先：正文里日期很多（发布日、会议日、落款日…），
    # 全文第一个日期八成不是期限。先只在「截止/请于/报名至」这类词后面找。
    for m in re.finditer(r"(?:截止|截至|报名至|请于|前提交|前报送|前完成|前报|报送至|提交至|反馈至|务必于)", t):
        seg = t[m.start(): m.start() + 50]
        for w, delta in (("今天", 0), ("明天", 1), ("后天", 2)):
            if w in seg:
                return (today + datetime.timedelta(days=delta)).isoformat()
        mm = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", seg)
        if mm:
            try:
                cand = datetime.date(today.year, int(mm.group(1)), int(mm.group(2)))
                # 明显是过去的日期就放弃，别硬推到明年（实测推出来过 2027-06-25 这种荒唐值）
                if cand < today - datetime.timedelta(days=7):
                    continue
                return cand.isoformat()
            except ValueError:
                pass
        mm = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", seg)
        if mm:
            try:
                return datetime.date(int(mm.group(1)), int(mm.group(2)),
                                     int(mm.group(3))).isoformat()
            except ValueError:
                pass

    # 兜底之前先设一道门：全文连一个"期限意味"的词都没有，就别猜日期。
    # 实测「期考日程发布」「图书采购清单公示」这类，正文里的普通日期会被当成截止日，
    # 抽出来 2027-06-25、2027-08-28 这种荒唐值，就是这么来的。
    if not re.search(r"截止|截至|期限|报名|提交|报送|请于|务必|之前|前完成|前提交|公示期|deadline|DDL",
                     t, re.I):
        return ""

    for w, delta in (("今天", 0), ("今晚", 0), ("明晚", 1), ("明天", 1), ("后天", 2), ("大后天", 3)):
        if w in t:
            return (today + datetime.timedelta(days=delta)).isoformat()

    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日]", t)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        y = today.year
        try:
            cand = datetime.date(y, mo, d)
        except ValueError:
            return ""
        if cand < today - datetime.timedelta(days=7):
            return ""          # 过去太久的日期不认是期限
        return cand.isoformat()

    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", t)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            pass

    m = re.search(r"(?<!\d)(\d{1,2})[-/](\d{1,2})(?!\d)", t)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            y = today.year
            try:
                cand = datetime.date(y, mo, d)
            except ValueError:
                return ""
            if cand < today - datetime.timedelta(days=7):
                return ""      # 同上，不再硬推明年
            return cand.isoformat()

    wk = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
    m = re.search(r"(本|下)周([一二三四五六日天])", t)
    if m:
        target = wk[m.group(2)]
        cur = today.weekday()
        diff = target - cur
        if m.group(1) == "下":
            diff += 7
        return (today + datetime.timedelta(days=diff)).isoformat()
    return ""


def norm_title(text, limit=40):
    s = re.sub(r"\s+", " ", text).strip()
    s = re.sub(r"^[【\[（(][^\]】)）]{0,10}[\]】)）]", "", s)
    return s[:limit] + ("…" if len(s) > limit else "")


def classify(text, cfg):
    """本地规则定级。返回 (level, [依据...], ddl)"""
    t = text.strip()
    low = t.lower()
    reasons, level = [], "P3"

    # 直接交给 parse_ddl —— 它内部自带「必须先有期限意味的词」这道门。
    # 早先这里另加了一道更窄的 DDL_HINT 判断，结果和 parse_ddl 口径不一致：
    # reddl 抽得到（宽门）、regrade 走 classify 抽不到（窄门），
    # 于是 regrade 一跑就把 30 条截止日全清空了。两处口径必须统一。
    ddl = parse_ddl(t)
    if ddl:
        reasons.append("识别到截止日 %s" % ddl)

    hit_self = [w for w in cfg.get("self_keywords", []) if w and w in t]
    if hit_self:
        reasons.append("提到 %s（视为与我相关）" % "/".join(hit_self))

    hits0 = [w for w in P0_WORDS if w in low or w in t]
    hits1 = [w for w in P1_WORDS if w in low or w in t]
    hits2 = [w for w in P2_WORDS if w in t]
    low_conf = (not ddl) and (not hits0) and (not hits1) and (not hits2)

    if hits0:
        # P0 必须真的有时间压力。只因出现"紧急"二字就封顶 P0 会误伤
        #（实测：一段个人 SWOT 分析里带了"紧急"就被判 P0，荒唐）。
        # 有截止日、或明确 @全体成员，才配 P0；否则退到 P1。
        if ddl or "@全体成员" in t:
            level = "P0"
            reasons.append("命中紧急词：%s" % "/".join(hits0[:3]))
        else:
            level = "P1"
            reasons.append("命中紧急词（%s）但无截止日，按 P1 记" % "/".join(hits0[:2]))
    elif hits1:
        level = "P1"
        reasons.append("命中事务词：%s" % "/".join(hits1[:3]))
    elif hits2:
        level = "P2"
        reasons.append("命中一般事务词：%s" % "/".join(hits2[:3]))
    elif ddl:
        level = "P2"
        reasons.append("有截止日但无关键词，按 P2 记")
    else:
        reasons.append("未命中任何关键词且无截止日 → 归入观测簿，不占待办队列")

    # 截止日临逼近邻升档
    if ddl:
        try:
            d = datetime.date.fromisoformat(ddl)
            gap = (d - _today()).days
            if gap < 0:
                level = "P3"        # 已过期，不占版面，但仍留痕
                reasons.append("截止日已过 %d 天，降级" % (-gap))
            elif gap == 0 and level != "P0":
                level = "P0"
                reasons.append("今天到期 → 提到 P0")
            elif gap <= 2 and LEVEL_ORDER[level] > 1:
                level = "P1"
                reasons.append("%d 天内到期 → 提到 P1" % gap)
        except Exception:
            pass

    # 优先级上限裁剪
    cap = cfg.get("max_level", "P3")
    if LEVEL_ORDER[level] > LEVEL_ORDER.get(cap, 3):
        reasons.append("超出上限 %s，不入库" % cap)
        return level, reasons, ddl, False, low_conf

    # 太短/明显噪声不入 P1 及以上
    if len(t) < 8 and LEVEL_ORDER[level] < 2:
        level = "P2"
        reasons.append("原文过短，最多记 P2")

    return level, reasons, ddl, True, low_conf


def make_hash(text):
    core = re.sub(r"[\s\W_]+", "", text)[:64]
    return hashlib.md5(core.encode("utf-8")).hexdigest()


def match_profile(text, cfg):
    """画像过滤。官网公告量很大，跟主公无关的（人才引进、图书采购、研究生答辩…）
    一律挡在观测簿，别让它们淹掉真正要紧的群消息。"""
    p = cfg.get("profile") or {}
    if not p.get("enabled", False):
        return True, ""
    kws = p.get("keywords") or []
    hits = [w for w in kws if w and w in text]
    if hits:
        return True, "命中画像词：%s" % "/".join(hits[:3])
    return False, "未命中画像（%d 个关键词全不沾边）→ 观测簿" % len(kws)


# 教师/研究生类赛事跟主公（本科生）无关，命中这些词就不置顶
PIN_NEGATIVE = ["教师", "研究生", "院长", "工作会议", "论坛", "督导", "岗位",
                "职称", "青年教师", "研讨会", "表彰大会", "代表大会"]


def is_pinned(raw, cfg):
    """是否置顶（比赛/竞赛类）。

    ⚠️ 只拿**标题**判断。踩过的坑：一开始拿全文匹配，结果
    「开学工作会议」「普通话证书领取」这些正文里顺带提到"大赛/比赛"的也被置顶，
    27 条里一大半是教师和研究生赛事。标题才是这条消息的主旨。
    另加负面词，挡掉泛雅杯教师赛、研究生成果转化大赛这类。"""
    t = norm_title(raw, limit=80)
    neg = cfg.get("pin_negative") or PIN_NEGATIVE
    if any(n in t for n in neg):
        return False, []
    hits = [w for w in (cfg.get("pin_words") or []) if w in t]
    return bool(hits), hits


def ingest(store, cfg, source, src_name, sender, ts, raw):
    """统一入库入口。低置信 / 不沾画像的消息进观测簿（status='noise'），不占待办队列。
返回 (是否入库, 记录dict)"""
    # QQ 消息自带表情/At 占位符（<faceType=1,faceId="341",ext="IjEyMw==">、<@!123>），
    # 不清掉的话标题里会露出一串乱码（实测 #94 标题尾部挂着 <faceTyp…）。
    raw = re.sub(r"<faceType=[^>]{0,160}>", "", raw)
    raw = re.sub(r"<@!?\d+>", "", raw)
    raw = re.sub(r"<face\w*[^>]*$", "", raw)    # 收尾处未闭合的残片（截断造成）

    level, reasons, ddl, keep, low_conf = classify(raw, cfg)
    if not keep:
        return False, None

    pf_ok, pf_msg = True, ""
    if source in ((cfg.get("profile") or {}).get("apply_to") or []):
        pf_ok, pf_msg = match_profile(raw, cfg)
        if pf_msg:
            reasons.append(pf_msg)

    if not pf_ok:
        level = "P3"        # 不沾画像的一律最低级，别跟正经待办抢位置
    status = "noise" if (low_conf or not pf_ok) else "open"

    # 比赛/竞赛类置顶（主公要求：这类排最前，其余按 P0-P3 排在后）。
    # 只影响排序，不改级别——比赛不一定"紧急"，硬塞 P0 会污染"紧急"的语义。
    _pin, pin_hits = is_pinned(raw, cfg)
    if pin_hits:
        reasons.insert(0, "🏆 比赛/竞赛类（%s）→ 置顶" % "/".join(pin_hits[:3]))

    row = dict(source=source, src_name=src_name, sender=sender, ts=ts, raw=raw,
               title=norm_title(raw), level=level, reason="；".join(reasons),
               ddl=ddl, status=status, pin=1 if pin_hits else 0, hash=make_hash(raw))
    nid = store.add(**row)
    if nid is None:
        return False, None
    row["id"] = nid
    log("  入库 #%d [%s/%s] %s  ← %s" % (nid, level, status, row["title"][:30], src_name))
    return True, row


# ============ 推送 ============
LEVEL_ICON = {"P0": "🔴", "P1": "🟠", "P2": "🟡", "P3": "⚪"}


def build_digest(store, title="蛇杖一号 · 今日待办"):
    """生成待办清单 Markdown"""
    rows = [r for r in store.rows() if r["status"] == "open"]
    if not rows:
        return title, "今天没有待处理事项。\n\n（来源：QQ群自动收集 + 导入 + 学院公告抓取）"

    # P3 折叠
    main = [r for r in rows if r["level"] != "P3"]
    rest = [r for r in rows if r["level"] == "P3"]
    today = _today().isoformat()

    lines = ["### ⏰ 今天 %s" % today, ""]
    urgent = [r for r in main if r["ddl"] and r["ddl"] == today]
    if urgent:
        lines.append("**🔥 今天到期**")
        for r in urgent:
            lines.append("- %s" % r["title"])
        lines.append("")

    for lv in ("P0", "P1", "P2"):
        grp = [r for r in main if r["level"] == lv and r not in urgent]
        if not grp:
            continue
        lines.append("**%s %s 级（%d 条）**" % (LEVEL_ICON[lv], lv, len(grp)))
        for r in grp:
            tail = " ｜ 截止 %s" % r["ddl"] if r["ddl"] else ""
            lines.append("- %s%s ｜ 来自 %s" % (r["title"], tail, r["src_name"]))
        lines.append("")

    if rest:
        lines.append("> 另有 %d 条 P3 低优先级，去看板查看。" % len(rest))
        lines.append("")

    lines.append("—— 由本机桥梁自动整理，判断权在你。")
    clean = [r for r in rows if r["level"] == "P0"]
    if clean:
        lines.insert(1, "> 共 %d 条待办，其中 %d 条紧急。" % (len(rows), len(clean)))
    return title, "\n".join(lines)


CH_UNSET = {"serverchan": "serverchan_key", "pushplus": "pushplus_token",
            "wecom": "wecom_webhook"}


def _form_post(url, payload, name):
    import urllib.request
    body = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"User-Agent": "Mozilla/5.0 (shezhang1-bridge)",
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return "%s -> HTTP %d %s" % (name, r.status,
                                     r.read().decode("utf-8", "replace")[:200])


def push_one(ch, cfg, title, content):
    """发单个通道。返回 (是否成功, 说明)"""
    p = cfg.get("push", {})
    try:
        if ch == "serverchan" and p.get("serverchan_key"):
            return True, _form_post("https://sctapi.ftqq.com/%s.send" % p["serverchan_key"],
                                    {"title": title[:32], "desp": content}, "Server酱")
        if ch == "pushplus" and p.get("pushplus_token"):
            return True, _form_post("https://www.pushplus.plus/send",
                                    {"token": p["pushplus_token"], "title": title,
                                     "content": content, "template": "markdown"}, "PushPlus")
        if ch == "wecom" and p.get("wecom_webhook"):
            import urllib.request
            body = json.dumps({"msgtype": "markdown",
                               "markdown": {"content": "### %s\n%s" % (title, content)}},
                              ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(p["wecom_webhook"], data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return True, "企业微信 -> HTTP %d %s" % (
                    r.status, r.read().decode("utf-8", "replace")[:200])
        if ch == "email":
            e = p.get("email") or {}
            if not e.get("enabled"):
                return False, "邮件通道未启用"
            if not (e.get("user") and e.get("password")):
                return False, "邮件缺账号或授权码（config.json → push.email.password）"
            import smtplib
            from email.mime.text import MIMEText
            from email.header import Header
            to = e.get("to") or e["user"]
            msg = MIMEText(content, "plain", "utf-8")
            msg["Subject"] = Header(title, "utf-8")
            msg["From"] = e["user"]
            msg["To"] = to
            s = smtplib.SMTP_SSL(e.get("smtp_host", "smtp.163.com"),
                                 int(e.get("smtp_port", 465)), timeout=25)
            try:
                s.login(e["user"], e["password"])
                s.sendmail(e["user"], [to], msg.as_string())
            finally:
                try:
                    s.quit()
                except Exception:
                    pass
            return True, "邮件已发往 %s" % to
        if CH_UNSET.get(ch):
            return False, "通道 %s 没填 %s" % (ch, CH_UNSET[ch])
        return False, "未知通道 %s" % ch
    except Exception as e:
        return False, "失败：%r" % e


def purge_expired(store, cfg):
    """按时效期清理。三条规矩：
    1. 有截止日的 → 过了截止日 + grace_days 天，归档
    2. 没截止日的 → 创建超过 no_ddl_keep_days 天，归档
    3. 观测簿 → 只留 noise_keep_days 天，然后直接删（本来就不重要）
    已归档超过 purge_archived_days 天的，彻底删除，库不至于无限涨。"""
    ex = cfg.get("expire") or {}
    grace = int(ex.get("grace_days", 2))
    keep = int(ex.get("no_ddl_keep_days", 14))
    nkeep = int(ex.get("noise_keep_days", 7))
    parch = int(ex.get("purge_archived_days", 60))
    now = int(time.time())
    day = 86400

    with store.lock:
        # 1. 过期的截止日事项 → 归档
        cut = (_today() - datetime.timedelta(days=grace)).isoformat()
        c = store.conn.execute(
            "UPDATE items SET status='archived', updated_at=? "
            "WHERE status IN ('open','done') AND ddl<>'' AND ddl<?", (now, cut))
        n1 = c.rowcount

        # 2. 无截止日、放太久 → 归档
        old = now - keep * day
        c = store.conn.execute(
            "UPDATE items SET status='archived', updated_at=? "
            "WHERE status IN ('open','done') AND (ddl='' OR ddl IS NULL) AND created_at<?",
            (now, old))
        n2 = c.rowcount

        # 3. 观测簿直接删
        nold = now - nkeep * day
        c = store.conn.execute(
            "DELETE FROM items WHERE status='noise' AND created_at<?", (nold,))
        n3 = c.rowcount

        # 4. 归档太久 → 彻底删
        aold = now - parch * day
        c = store.conn.execute(
            "DELETE FROM items WHERE status='archived' AND updated_at<?", (aold,))
        n4 = c.rowcount
        store.conn.commit()
    return {"过期归档": n1, "久置归档": n2, "观测簿清理": n3, "归档彻底删除": n4}


def _drop_excluded(rows, cfg):
    """把不该进公开文件的来源剔掉（config.json → export_exclude_sources）。

    用途：「内网张贴」这类要连校园网才访问得到的栏目 —— 抓得到，不等于可以公开。
    它已经被导出成公网可下载的 todo.json / todo.ics，等于绕过了校方的访问控制。
    （QQ 群消息另有更严的护栏 export_include_qq，那条恒 false，是红线。）"""
    ex = set((cfg or {}).get("export_exclude_sources") or [])
    if not ex:
        return rows
    return [r for r in rows if (r.get("src_name") or "") not in ex]


def export_board_json(store, out_path, include_qq=False, cfg=None):
    """把待办导出成 JSON 给蛇杖一号前端读。

    安全默认：QQ 群消息不进这个文件。蛇杖一号是公开仓库，群聊内容一旦推上去
    等于把全班同学说的话公开了（这仓库之前就因为索引泄露学号姓名栽过）。
    想一起导就把 config 里 export_include_qq 改成 true —— 但那意味着你自己承担风险。
    另外一律不带原始正文和发言人，只留标题。"""
    rows = [r for r in store.rows() if r["status"] == "open"]
    if not include_qq:
        rows = [r for r in rows if r["source"] != "qq"]
    rows = _drop_excluded(rows, cfg)
    data = {
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(rows),
        "include_qq": bool(include_qq),
        "note": "仅官网/图书馆公告，QQ群待办只在本机看板可见" if not include_qq
                else "含 QQ 群消息，注意这是公开仓库",
        "items": [{
            "id": r["id"], "level": r["level"], "title": r["title"],
            "ddl": r["ddl"], "src": r["src_name"], "reason": r["reason"],
            # 公告类条目入库时原文链接存在 sender 字段里，导出时带出来，
            # 前端就能做成可点击直达，而不是一条死的截图文字。
            "url": r["sender"] if str(r["sender"] or "").startswith("http") else "",
            "pin": int(r.get("pin") or 0),
            "ts": r["ts"]
        } for r in rows]
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return len(rows)


def _ical_esc(s):
    """iCalendar 文本转义：反斜杠、分号、逗号、换行都得转。"""
    return (str(s or "").replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\r\n", "\n").replace("\n", "\\n"))


def build_ical(store, include_qq=False, cfg=None):
    """把「待处理 + 有截止日」的条目导成 iCalendar。

    这就是对标「偷闲」的日历写入：手机点一下 .ics 就进系统日历，
    DDL 到点自动提醒，不用再盯邮件。（全天事件，按 DATE 值写，不带时区。）

    ⚠️ 安全护栏：默认排除 QQ 群条目。这个 .ics 会推到公开仓库，
    群里一句「周三前交材料」若被抽成截止日，就等于把班群内容公开了。
    与 export_include_qq 同一个开关，别单开。"""
    rows = [r for r in store.rows() if r["status"] == "open" and r["ddl"]]
    if not include_qq:
        rows = [r for r in rows if r["source"] != "qq"]
    rows = _drop_excluded(rows, cfg)
    out = ["BEGIN:VCALENDAR", "VERSION:2.0",
           "PRODID:-//shezhang1//bridge//CN", "CALSCALE:GREGORIAN",
           "X-WR-CALNAME:蛇杖一号 · 待办"]
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    n = 0
    for r in rows:
        try:
            d = datetime.date.fromisoformat(r["ddl"])
        except Exception:
            continue
        url = r["sender"] if str(r["sender"] or "").startswith("http") else ""
        parts = ["来源：%s" % r["src_name"]]
        if url:
            parts.append("原文：%s" % url)
        if r["reason"]:
            parts.append("依据：%s" % r["reason"])
        out += ["BEGIN:VEVENT",
                "UID:shezhang1-%d@local" % r["id"],
                "DTSTAMP:" + stamp,
                "DTSTART;VALUE=DATE:" + d.strftime("%Y%m%d"),
                "DTEND;VALUE=DATE:" + (d + datetime.timedelta(days=1)).strftime("%Y%m%d"),
                "SUMMARY:" + _ical_esc("[%s] %s" % (r["level"], r["title"])),
                "DESCRIPTION:" + _ical_esc("\n".join(parts))]
        if url:
            out.append("URL:" + url)
        out.append("END:VEVENT")
        n += 1
    out.append("END:VCALENDAR")
    return "\r\n".join(out) + "\r\n", n


# ============ 班群消息脱敏 → 推私密库（daily-plan）============
# 主公 2026-09-30 同意：QQ 内容**脱敏后**可以进私密库（bicheng2026/daily-plan，Private）。
# 注意顺序：先长后短，否则 11 位手机号会被 \d{10,12} 先吃掉当成学号。
_SCRUB_PATTERNS = [
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "[身份证]"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[手机号]"),
    (re.compile(r"(?<!\d)\d{10,12}(?!\d)"), "[学号]"),
]


def scrub_qq(t):
    """群消息脱敏：去 QQ 表情占位符、去数字类敏感串。
    发言人昵称不在这层处理（入库时就不存进 title），所以这里只管正文。"""
    if not t:
        return ""
    t = re.sub(r"<faceType=[^>]{0,160}>", "", t)
    t = re.sub(r"<@!?\d+>", "", t)
    t = re.sub(r"<face\w*[^>]*$", "", t)
    for pat, rep in _SCRUB_PATTERNS:
        t = pat.sub(rep, t)
    return t.strip()


def build_digest(store, cfg, keep_days=7):
    """近 N 天的班群消息（已脱敏），供私密库日报拼接。只取 qq 来源。"""
    since = int(time.time()) - keep_days * 86400
    with store.lock:
        cur = store.conn.execute(
            "SELECT id, ts, level, title, ddl, status FROM items "
            "WHERE source='qq' AND ts>=? AND status<>'archived' "
            "ORDER BY (CASE level WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 "
            "WHEN 'P2' THEN 2 ELSE 3 END), ts DESC LIMIT 60", (since,))
        rows = [dict(r) for r in cur.fetchall()]
    items = []
    for r in rows:
        items.append({
            "ts": int(r["ts"] or 0),
            "date": datetime.datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M")
                    if r["ts"] else "",
            "level": r["level"] or "P3",
            "title": scrub_qq(r["title"] or ""),
            "ddl": r["ddl"] or "",
        })
    return {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(items),
        "note": "班群消息，已脱敏：不含发言人昵称，学号/手机号/身份证已打码",
        "items": items,
    }


def push_digest(store, cfg, on_log=log):
    """把脱敏后的班群摘要推到私密库。返回 (ok, msg)。任何异常都不该影响主流程。"""
    d = cfg.get("digest_push") or {}
    if not d.get("enabled"):
        return False, "未启用（config.json → digest_push.enabled）"
    repo, tok = d.get("repo") or "", d.get("token") or ""
    path = d.get("path") or "shezhang_digest.json"
    if not (repo and tok):
        return False, "缺 repo 或 token"
    try:
        import urllib.request, ssl, base64     # 局部导入：和 http_get 保持一致
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        payload = build_digest(store, cfg, int(d.get("keep_days") or 7))
        body = json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")
        url = "https://api.github.com/repos/%s/contents/%s" % (repo, path)
        hdr = {"Authorization": "token " + tok, "User-Agent": "shezhang1-bridge",
               "Accept": "application/vnd.github+json"}
        op = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ctx))
        # 取现有 sha（有才需要带，代表是更新而非新建）
        sha = None
        try:
            with op.open(urllib.request.Request(url, headers=hdr), timeout=45) as r:
                sha = json.loads(r.read().decode("utf-8")).get("sha")
        except Exception:
            pass
        put = {"message": "蛇杖：班群摘要（脱敏）%s" % payload["generated_at"],
               "content": base64.b64encode(body).decode("ascii")}
        if sha:
            put["sha"] = sha
        req = urllib.request.Request(url, method="PUT",
                                     data=json.dumps(put).encode("utf-8"),
                                     headers=dict(hdr, **{"Content-Type": "application/json"}))
        with op.open(req, timeout=60) as r:
            r.read()
        return True, "%d 条 → %s/%s" % (payload["count"], repo, path)
    except Exception as e:
        return False, "推送失败：%r" % e


def do_sync(store, cfg, with_push=True, on_log=log):
    """00:00 同步：抓新 → 清旧 → 导出看板 →（可选）发邮件"""
    cfg = cfg or load_config()
    on_log("【同步开始】")
    on_log("  1/4 抓取公告…")
    try:
        added = crawl_sites(store, cfg)
        on_log("     新增 %d 条" % added)
    except Exception as e:
        on_log("     抓取出错：%r" % e)

    on_log("  2/4 按时效期清理…")
    st = purge_expired(store, cfg)
    on_log("     " + "，".join("%s %d 条" % (k, v) for k, v in st.items() if v))

    on_log("  3/4 导出看板 JSON…")
    web_data = os.path.join(os.path.dirname(HERE), "网页版", "data", "todo.json")
    try:
        n = export_board_json(store, web_data,
                              include_qq=bool(cfg.get("export_include_qq")), cfg=cfg)
        on_log("     %d 条 → %s%s" % (n, web_data,
                                       "" if cfg.get("export_include_qq") else "（不含QQ群）"))
    except Exception as e:
        on_log("     导出失败：%r" % e)

    on_log("  4/5 推送班群摘要到私密库…")
    try:
        ok, msg = push_digest(store, cfg, on_log=on_log)
        on_log("     %s" % ("✓ " + msg if ok else "跳过：" + msg))
    except Exception as e:
        on_log("     推送出错：%r" % e)

    if with_push:
        on_log("  5/5 发送当日总结邮件…")
        t, c = build_daily_summary(store)
        ok, msg = push_wechat(cfg, t, c)
        on_log("     %s" % msg.replace("\n", " ｜ "))
    else:
        on_log("  5/5 跳过发邮件（日报由云端「每日计划」发）")
    on_log("【同步完成】")


def build_daily_summary(store):
    """当日总结：今日新增（按来源分） + 当前待办队列 + 观测簿/已完成计数"""
    today = _today()
    start = int(datetime.datetime(today.year, today.month, today.day).timestamp())

    with store.lock:
        cur = store.conn.execute(
            "SELECT source, src_name, COUNT(*) FROM items "
            "WHERE created_at>=? GROUP BY source, src_name", (start,))
        grouped = cur.fetchall()
        cur = store.conn.execute(
            "SELECT COUNT(*) FROM items WHERE status='noise'", ())
        n_noise = cur.fetchone()[0]
        cur = store.conn.execute(
            "SELECT COUNT(*) FROM items WHERE status='done'", ())
        n_done = cur.fetchone()[0]

    total_new = sum(g[2] for g in grouped)
    iso = today.isoformat()

    L = ["蛇杖一号 · 当日总结", "",
         "日期：%s" % iso,
         "=" * 32, "",
         "【一、今日新增 %d 条】" % total_new]
    if grouped:
        for src, name, c in sorted(grouped, key=lambda x: -x[2]):
            label = {"qq": "QQ群", "crawl": "公告", "import": "导入"}.get(src, src)
            L.append("  %-5s %-18s %d 条" % (label, name, c))
    else:
        L.append("  （今天没有新增）")
    L.append("")

    openrows = [r for r in store.rows() if r["status"] == "open"]
    L.append("【二、待办队列 %d 条】" % len(openrows))
    if not openrows:
        L.append("  （空，今天清干净了）")
    else:
        urgent = [r for r in openrows if r["ddl"] and r["ddl"] == iso]
        urgent_ids = set(r["id"] for r in urgent)
        if urgent:
            L.append("  >> 今天到期")
            for r in urgent:
                L.append("     - %s" % r["title"])
        for lv in ("P0", "P1", "P2"):
            grp = [r for r in openrows
                   if r["level"] == lv and r["id"] not in urgent_ids]
            if not grp:
                continue
            L.append("  %s %s 级（%d）" % (LEVEL_ICON[lv], lv, len(grp)))
            for r in grp:
                tail = "  [截止 %s]" % r["ddl"] if r["ddl"] else ""
                L.append("     - %s%s   ← %s" % (r["title"], tail, r["src_name"]))
    L.append("")
    L.append("【三、其他】")
    L.append("  观测簿 %d 条（低优先 / 未命中画像，不占版面）" % n_noise)
    L.append("  已完成 %d 条" % n_done)
    L.append("")
    L.append("--")
    L.append("本机自动整理，判断权在你。看板：http://127.0.0.1:8890/")
    return "蛇杖一号 · 当日总结 %s" % iso, "\n".join(L)


def push_wechat(cfg, title, content, dry=False):
    """推送。push_channel 可以填多个，逗号分隔，如 "pushplus,email"。"""
    chs = [c.strip() for c in str(cfg.get("push_channel", "")).split(",") if c.strip()]
    if dry:
        return True, "【预览模式】未真正发送（通道：%s）\n\n%s" % (
            ",".join(chs) or "未配置", content)
    if not chs:
        return False, "push_channel 为空，没地方推"
    out, ok_any = [], False
    for ch in chs:
        ok, m = push_one(ch, cfg, title, content)
        ok_any = ok_any or ok
        out.append("  • %s：%s" % (ch, m))
    return ok_any, "推送结果：\n" + "\n".join(out)


# ============ 聊天记录导入 ============
# ---- 聊天记录行解析 ----
# 坑：时间戳本身带冒号（09:12:33），必须先显式吃掉时间再找「发言人: 正文」的分隔冒号，
#     否则会把 "09" 当发言人、"12:33 昵称: 正文" 当正文。
_TIME = r"(?:[ T]+\d{1,2}:\d{2}(?::\d{2})?)?"
_DATE = r"(\d{4}[-/年]\d{1,2}[-/月]\d{1,2})"

LINE_PATS = [
    # 2026-09-29 09:12:33 昵称: 正文        / 2026-09-29 昵称: 正文
    re.compile(r"^\s*\[?" + _DATE + _TIME + r"\]?\s*[-–—]?\s*(.{1,24}?)\s*[:：]\s*(.+)$"),
    # 昵称: 正文（无日期）
    re.compile(r"^(.{1,20}?)\s*[:：]\s*(.+)$"),
]

_NOISE_LINE = re.compile(
    r"^(?:={3,}|-{3,}|—{3,}|聊天记录|消息?记录|导出时间|更多消息|以下为|共计\s*\d+)")


def parse_line(line):
    """返回 (日期, 发言人, 正文)；不匹配返回 None"""
    if not line or _NOISE_LINE.match(line):
        return None
    for i, pat in enumerate(LINE_PATS):
        m = pat.match(line)
        if not m:
            continue
        if i == 0:
            return m.group(1), m.group(2).strip(), m.group(3).strip()
        sender, body = m.group(1).strip(), m.group(2).strip()
        # 防误伤：看起来像时间/纯数字的不要
        if re.match(r"^\d{1,2}:\d{2}", sender) or not body:
            return None
        return "", sender, body
    return None


def import_chat(store, cfg, path):
    """导入 QQ / 微信导出的纯文本聊天记录"""
    if not os.path.exists(path):
        log("文件不存在：", path)
        return 0
    raw = open(path, "r", encoding="utf-8", errors="replace").read()
    lines = raw.splitlines()

    n = 0
    cur = None            # (date, sender, [body...])
    bucket = []

    def flush():
        nonlocal n
        if not cur:
            return
        date, sender, bodies = cur
        body = " ".join(b for b in bodies if b).strip()
        if len(body) < 6:
            return
        ok, _ = ingest(store, cfg, "import", os.path.basename(path),
                       sender, ts_from(date), body)
        n += 1 if ok else 0

    for ln in lines:
        ln = ln.rstrip()
        if not ln.strip():
            continue
        p = parse_line(ln)
        if p:
            flush()
            cur = (p[0], p[1], [p[2]])
        elif cur is not None:
            # 续行：并入上一条
            cur[2].append(ln.strip())
    flush()
    log("导入完成，新增 %d 条（来源 %s）" % (n, os.path.basename(path)))
    return n


def ts_from(s):
    if not s:
        return int(time.time())
    m = re.match(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", s)
    if m:
        try:
            return int(datetime.datetime(int(m.group(1)), int(m.group(2)),
                                         int(m.group(3))).timestamp())
        except Exception:
            pass
    return int(time.time())


# ============ 公告抓取 ============
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
      "Accept-Language": "zh-CN,zh;q=0.9"}


def http_get(url, timeout=25):
    """取网页。两个坑：
    1) 本机代理残留会让请求挂死 → ProxyHandler({}) 直连绕开
    2) opener.open() 不接受 context 参数 → SSL 上下文必须挂到 HTTPSHandler 上"""
    import urllib.request, urllib.error, ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))
    req = urllib.request.Request(url, headers=UA)
    with opener.open(req, timeout=timeout) as r:
        data = r.read()
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", "replace")


def fetch_body(url, maxlen=900):
    """抓公告详情页的正文纯文本。

    这是「记住期限」的关键：官方通知的截止日基本都写在正文里，
    标题只有"关于……的通知"。只存标题 = 永远抽不出 DDL。
    顺手把 URL 长串剔掉（就是它把早先的推送正文弄脏的）。"""
    try:
        h = http_get(url)
    except Exception:
        return ""
    h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    h = html.unescape(h)
    h = re.sub(r"https?://\S+", " ", h)
    h = re.sub(r"&[a-zA-Z#0-9]{1,8};", " ", h)
    h = re.sub(r"\s+", " ", h).strip()
    return h[:maxlen]


def crawl_sites(store, cfg):
    total = 0
    for site in cfg.get("sites", []):
        name, url = site.get("name"), site.get("url")
        limit = site.get("limit", 20)
        if not name or not url:
            continue
        log("抓取 %s ..." % name)
        try:
            page = http_get(url)
        except Exception as e:
            log("  × %s 抓取失败：%r" % (name, e))
            continue
        n = 0
        for m in re.finditer(r"<a\b([^>]*)>([\s\S]*?)</a>", page, re.I):
            attrs, inner = m.group(1), m.group(2)
            hm = re.search(r'href="([^"]+\.(?:html?|htm))"', attrs, re.I)
            if not hm:
                continue
            link = urllib.parse.urljoin(url, hm.group(1))
            if not link.startswith("http"):
                continue
            tm = re.search(r'title="([^"]*)"', attrs, re.I)
            title = (tm.group(1) if tm else re.sub(r"<[^>]+>", "", inner)).strip()
            title = re.sub(r"\s+", " ", html.unescape(title))
            if len(title) < 6 or len(title) > 100:
                continue
            # 标题在前、正文在后：norm_title 取前 40 字当标题，
            # parse_ddl 去正文里抠截止日（期限都写在正文里，标题只有"关于……的通知"）
            body = fetch_body(link) if site.get("fetch_body", True) else ""
            raw = title + ("\n" + body if body else "")
            ok, _ = ingest(store, cfg, "crawl", name, link, int(time.time()), raw)
            total += 1 if ok else 0
            n += 1
            if n >= limit:
                break
        log("  → %s 处理 %d 条" % (name, n))
    log("公告抓取完成，新增 %d 条" % total)
    return total


# ============ 内置看板页面 ============
BOARD_HTML = u"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>蛇杖一号 · 信息全览</title><style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"Microsoft YaHei",system-ui,sans-serif;background:#f6f7f9;color:#1f2328;
padding:16px;max-width:920px;margin:0 auto;line-height:1.6}
h1{font-size:20px;margin-bottom:4px}
.sub{color:#6b7280;font-size:13px;margin-bottom:16px}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
.bar button,.bar select{font-size:13px;padding:6px 12px;border:1px solid #d5d9e0;background:#fff;
border-radius:8px;cursor:pointer;color:#1f2328}
.bar button:hover{background:#eef2f7}
.bar a.bar-link{font-size:13px;padding:6px 12px;border:1px solid #d5d9e0;background:#fff;
border-radius:8px;color:#1f2328;text-decoration:none;display:inline-block}
.bar a.bar-link:hover{background:#eef2f7}
.t a{color:#1a56db;text-decoration:none}
.t a:hover{text-decoration:underline}
.m a.go{color:#1a56db;text-decoration:none;font-weight:500}
.m a.go:hover{text-decoration:underline}
.grid{display:grid;gap:10px}
.card{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:12px 14px}
.card.P0{border-left:4px solid #d92d20}
.card.P1{border-left:4px solid #f79009}
.card.P2{border-left:4px solid #eaaa08}
.card.P3{border-left:4px solid #98a2b3}
.card.pin{background:#fffbf0;box-shadow:0 0 0 2px #f4b400 inset}
.t{font-weight:600;font-size:15px;margin-bottom:4px}
.m{font-size:12px;color:#667085;display:flex;gap:10px;flex-wrap:wrap}
.reason{font-size:12px;color:#475467;margin-top:6px;padding-top:6px;border-top:1px dashed #e3e6ea}
.raw{font-size:12px;color:#98a2b3;margin-top:4px;white-space:pre-wrap;display:none}
.ops{margin-top:8px;display:flex;gap:6px}
.ops button{font-size:12px;padding:3px 9px;border:1px solid #d5d9e0;background:#fff;
border-radius:6px;cursor:pointer}
.empty{color:#98a2b3;text-align:center;padding:40px}
/* 置顶（比赛/竞赛）与班群消息的视觉区分 */
.card.qq{border-left-color:#2f80ed;background:#f7fbff}
.grp{font-size:13px;font-weight:700;margin:16px 0 4px;color:#475467}
.grp.pgrp{color:#b54708}
.grp.qgrp{color:#1a56db}
/* 顶部 QQ 状态条 + 自助接入面板 */
#qbar{font-size:13px;border:1px solid #e3e6ea;background:#fff;border-radius:10px;
  padding:8px 12px;margin-bottom:12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
#qbar .dot{width:9px;height:9px;border-radius:50%;background:#98a2b3;display:inline-block}
#qbar .dot.ok{background:#12b76a}
#qbar .dot.bad{background:#d92d20}
#qbar a{color:#1a56db;cursor:pointer;text-decoration:none;margin-left:auto}
#cfg{display:none;border:1px solid #e3e6ea;background:#fff;border-radius:10px;
  padding:12px;margin-bottom:12px;font-size:13px}
#cfg input{font-size:13px;padding:6px 8px;border:1px solid #d5d9e0;border-radius:6px;width:100%}
#cfg label{display:block;margin:8px 0 4px;color:#475467}
#cfg .row{display:flex;gap:8px;margin-top:10px}
#cfg .hint{color:#98a2b3;font-size:12px;margin-top:8px;line-height:1.6}
</style></head><body>
<h1>🐍 蛇杖一号 · 信息全览 <span style="font-size:12px;font-weight:400;opacity:.45">v__VERSION__</span></h1>
<div class="sub" id="sub">加载中…</div>
<div id="qbar"><span class="dot" id="qdot"></span><span id="qtext">班群机器人：检查中…</span>
  <a onclick="toggleCfg()">接入我的机器人 ▾</a></div>
<div id="cfg">
  <label>AppID（QQ 开放平台 → 我的机器人 → 开发设置）</label>
  <input id="qappid" placeholder="例：<QQ机器人AppID>">
  <label>AppSecret</label>
  <input id="qsecret" type="password" placeholder="只写进本机 config.json，不会上传到任何地方">
  <div class="row">
    <button onclick="saveQQ()">保存并连接</button>
    <button onclick="toggleCfg()">收起</button>
  </div>
  <div class="hint">
    填完立即热重启监听，不用重启程序。想让机器人看到群里<b>所有人的</b>消息，
    需要群主在群设置里把「机器人可访问的消息」设为<b>全部群消息</b>，否则只收得到 @它 的。<br>
    注意：QQ 官方机器人只能<b>被动接收</b>，没有「拉取历史消息」的接口——
    <b>程序没运行的时候，群里的消息就收不到了</b>，这是协议本身的限制。
  </div>
</div>
<div class="bar">
  <button onclick="setF('open')">待处理</button>
  <button onclick="setF('done')">已完成</button>
  <button onclick="setF('all')">全部</button>
  <select id="lv" onchange="render()"><option value="">全部优先级</option>
  <option value="P0">P0 紧急</option><option value="P1">P1 重要</option>
  <option value="P2">P2 一般</option><option value="P3">P3 低</option></select>
  <button onclick="doSync()">刷新</button>
  <button onclick="bulk('done')">✅ 当前全部完成</button>
  <button onclick="bulk('arch')">📥 当前全部归档</button>
  <a class="bar-link" href="/api/ical" download="蛇杖待办.ics">📅 导出日历</a>
</div>
<div class="grid" id="grid"></div>
<script>
let DATA=[],FILTER='open',QST=null;
async function load(){
  const r=await fetch('/api/todo');DATA=await r.json();
  try{const s=await fetch('/api/qqstatus');QST=await s.json();}catch(e){QST=null;}
  render();renderQ();
}
function setF(f){FILTER=f;render();}
// 「刷新」= 真去抓一次公告 + 清理过期 + 重导出（后端 /api/sync），不是只重读文件。
// ⚠️ QQ 群消息不在这个刷新范围内：机器人是被动接收，没有拉取历史消息的接口。
async function doSync(){
  document.getElementById('sub').textContent='正在同步…（抓公告 + 清理过期，约 10 秒）';
  try{
    const r=await fetch('/api/sync',{method:'POST'});const d=await r.json();
    if(!d.ok)document.getElementById('sub').textContent=d.msg||'同步进行中…';
  }catch(e){}
  setTimeout(load,2000);setTimeout(load,6000);setTimeout(load,12000);
}
function renderQ(){
  if(!QST)return;
  const st=QST.status||{};
  const dot=document.getElementById('qdot');
  dot.className='dot'+(st.ok?' ok':(QST.running?' bad':''));
  let s='班群机器人：'+esc(st.state||'未知');
  if(st.ok)s+='（累计收 '+st.msg_count+' 条'+(st.last_msg_text?'，最近「'+esc(String(st.last_msg_text).slice(0,18))+'」':'')+'）';
  if(!QST.configured)s+=' ｜ 还没填 AppID/Secret';
  if(st.err)s+=' ｜ '+esc(String(st.err).slice(0,60));
  if(QST.last_sync)s+=' ｜ 上次同步 '+esc(QST.last_sync);
  document.getElementById('qtext').textContent=s;
  const ai=document.getElementById('qappid');
  if(ai&&!ai.value)ai.value=QST.appid||'';
}
function toggleCfg(){const e=document.getElementById('cfg');
  e.style.display=(e.style.display==='block')?'none':'block';}
// 自助接入：填自己的 AppID/Secret → 存本机 config.json → 热重启监听
async function saveQQ(){
  const appid=document.getElementById('qappid').value.trim();
  const secret=document.getElementById('qsecret').value.trim();
  if(!appid||!secret){alert('AppID 和 AppSecret 都要填');return;}
  try{
    const r=await fetch('/api/qqconfig',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({appid:appid,secret:secret,enabled:true})});
    QST=await r.json();
    document.getElementById('qsecret').value='';
    renderQ();
    alert('已保存并开始连接，看顶部状态条（连上会变绿）。');
  }catch(e){alert('保存失败：'+e);}
}
// 批量处理：把「当前筛选出来的这一批」一次性完成/归档，省得一条条点
async function bulk(act){
  const lv=document.getElementById('lv').value;
  const list=DATA.filter(x=>(FILTER==='all'||x.status===FILTER)&&(!lv||x.level===lv));
  if(!list.length){alert('当前筛选下没有条目');return;}
  const nm=act==='done'?'完成':'归档';
  if(!confirm('把当前显示的 '+list.length+' 条全部标记为「'+nm+'」？'))return;
  await fetch('/api/bulk',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:act,ids:list.map(x=>x.id)})});
  load();
}
// ⚠️ 下面这两个函数原来是漏定义的，属长期潜伏的 bug：
//   esc 未定义 → render 渲染第一张卡片就抛错 → 整个列表空白（只有计数是对的）
//   act 未定义 → 「完成 / 归档 / →P0」按钮点了毫无反应
// 2026-09-30 用真浏览器跑才暴露出来。静态看代码根本发现不了。
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
async function act(id,val){
  await fetch('/api/set',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:id,value:val})});
  load();
}
function fmt(d){if(!d)return'';return new Date(d*1000).toLocaleString('zh-CN',{hour12:false});}
// 单张卡片。公告类条目 sender 里存的是原文链接 → 做成可点击直达；
// QQ 群条目 sender 是昵称 → 当发言人显示。
function cardHtml(x){
  const u=/^https?:\\/\\//i.test(x.sender||'')?x.sender:'';
  const title=u?`<a href="${u}" target="_blank" rel="noopener">${esc(x.title)}</a>`:esc(x.title);
  const who=u?'':(x.sender?`<span>发言人：${esc(x.sender)}</span>`:'');
  const qq=(x.source==='qq');
  return `
    <div class="card ${x.level}${x.pin?' pin':''}${qq?' qq':''}">
      <div class="t">${x.pin?'🏆 ':''}${qq?'💬 ':''}${x.level}　${title}</div>
      <div class="m"><span>来源：${esc(x.src_name)}</span>
        ${who}
        ${u?`<a class="go" href="${u}" target="_blank" rel="noopener">看原文 ↗</a>`:''}
        ${x.ddl?`<span>截止：${x.ddl}</span>`:''}</div>
      <div class="reason">依据：${esc(x.reason)}</div>
      <div class="raw" id="raw-${x.id}">${esc(x.raw)}</div>
      <div class="ops">
        <button onclick="act(${x.id},'done')">完成</button>
        <button onclick="act(${x.id},'arch')">归档</button>
        <button onclick="act(${x.id},'P0')">→P0</button>
        <button onclick="act(${x.id},'P1')">→P1</button>
        <button onclick="act(${x.id},'P2')">→P2</button>
        <button onclick="tog(${x.id})">原文</button>
      </div></div>`;
}
function render(){
  const lv=document.getElementById('lv').value;
  let list=DATA.filter(x=>(FILTER==='all'||x.status===FILTER)&&(!lv||x.level===lv));
  document.getElementById('sub').textContent=
    `共 ${DATA.length} 条 ｜ 当前显示 ${list.length} 条 ｜ 最后更新 ${new Date().toLocaleTimeString('zh-CN')}`;
  if(!list.length){document.getElementById('grid').innerHTML='<div class="empty">这里空着，挺好。</div>';return;}
  var html='';
  // ① 比赛 / 竞赛置顶（主公定：比赛类放最前）
  const pinned=list.filter(x=>x.pin);
  if(pinned.length){
    html+='<div class="grp pgrp">🏆 比赛 / 竞赛（'+pinned.length+'）</div>';
    pinned.forEach(x=>{html+=cardHtml(x);});
  }
  // ② 班群消息（主公定：紧跟在比赛后面）
  const qq=list.filter(x=>!x.pin&&x.source==='qq');
  if(qq.length){
    html+='<div class="grp qgrp">💬 班群消息（'+qq.length+'）</div>';
    qq.forEach(x=>{html+=cardHtml(x);});
  }
  // ③ 其余按优先级
  const LV=['P0','P1','P2','P3'],NM={P0:'紧急',P1:'重要',P2:'一般',P3:'低'};
  LV.forEach(l=>{
    const g=list.filter(x=>!x.pin&&x.source!=='qq'&&x.level===l);
    if(!g.length)return;
    html+='<div class="grp">'+l+' · '+NM[l]+'（'+g.length+'）</div>';
    g.forEach(x=>{html+=cardHtml(x);});
  });
  document.getElementById('grid').innerHTML=html||'<div class="empty">这里空着，挺好。</div>';
}
function tog(id){const e=document.getElementById('raw-'+id);
  if(e)e.style.display=(e.style.display==='block')?'none':'block';}
load();setInterval(load,30000);
</script></body></html>"""

# 把看板里的版本占位符换成真实版本号（改 VERSION 一处即可，不用翻 HTML）
BOARD_HTML = BOARD_HTML.replace("__VERSION__", VERSION)


# 跨源白名单。
# ⚠️ 这里绝不能写 "*"。桥接只监听本机，但 CORS 一放开，主公只要打开任意一个网页，
#    那个页面的 JS 就能 fetch http://127.0.0.1:8890/api/todo 把班群消息读走，
#    甚至 POST /api/qqconfig 改掉机器人配置。所以只放行两个来源：
#    ① 蛇杖 Pages（网页版要读本机班群消息）② 本机看板自己。
ALLOWED_ORIGINS = {
    "https://bicheng2026.github.io",     # 线上网页版
    "http://127.0.0.1:8890",             # 本机看板
    "http://localhost:8890",
    "http://127.0.0.1:8891",             # 本地预览网页版
    "http://localhost:8891",
}


class Handler(BaseHTTPRequestHandler):
    store = None
    cfg = None

    def log_message(self, fmt, *args):
        pass

    def _cors(self):
        """只给白名单来源回 CORS 头。不回 = 浏览器按同源策略自己拦掉。
        不带 Origin 的请求（curl、同源导航、桌面看板 GET）不受影响。"""
        origin = (self.headers.get("Origin") or "").strip()
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            # Chrome 的 Private Network Access：公网站点（bicheng2026.github.io）
            # 访问本机 127.0.0.1 时，预检里会问一句。显式允许，免得将来被拦。
            self.send_header("Access-Control-Allow-Private-Network", "true")
        return origin

    def _origin_ok(self):
        """写操作再挡一道：带 Origin 且不在白名单 → 直接 403。
        CORS 只约束"能不能读响应"，显式拒一次更稳。"""
        origin = (self.headers.get("Origin") or "").strip()
        if origin and origin not in ALLOWED_ORIGINS:
            self._send(403, b'{"error":"origin not allowed"}')
            return False
        return True

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ---- GET ----
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/board", "/index.html"):
            self._send(200, BOARD_HTML, "text/html; charset=utf-8")
        elif path == "/api/todo":
            rows = self.store.rows(where="1=1")
            self._send(200, json.dumps(rows, ensure_ascii=False))
        elif path == "/api/status":
            rows = self.store.rows()
            self._send(200, json.dumps({"open": len(rows), "time": time.time()},
                                       ensure_ascii=False))
        elif path == "/api/qqstatus":
            # 看板顶部状态条读它：机器人到底通没通、最后一条群消息什么时候来的
            self._send(200, json.dumps(qq_status(self.cfg),
                                       ensure_ascii=False).encode("utf-8"))
        elif path == "/api/ical":
            # 浏览器点一下就下载 .ics，手机导入即进系统日历
            text, _n = build_ical(self.store,
                                  bool((self.cfg or {}).get("export_include_qq")),
                                  self.cfg)
            data = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/calendar; charset=utf-8")
            self.send_header("Content-Disposition",
                             'attachment; filename="shezhang1-todo.ics"')
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            self.end_headers()
            self.wfile.write(data)
        else:
            self._send(404, json.dumps({"error": "not found"}, ensure_ascii=False))

    # ---- OPTIONS ----
    def do_OPTIONS(self):
        self._send(204, b"")

    # ---- POST ----
    def do_POST(self):
        if not self._origin_ok():
            return
        path = self.path.split("?")[0]
        try:
            ln = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(ln) if ln else b""
        except Exception:
            raw = b""

        # OneBot v11 上报
        if path in ("/qq", "/onebot", "/event"):
            tok = self.cfg.get("access_token", "")
            if tok:
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Bearer ") or auth[7:].strip() != tok:
                    self._send(401, b'{"error":"bad token"}')
                    return
            try:
                ev = json.loads(raw.decode("utf-8", "replace"))
            except Exception as e:
                self._send(400, b'{"error":"bad json"}')
                return
            self._handle_event(ev)
            self._send(200, b'{"ret":0}')
            return

        # 看板操作
        if path == "/api/set":
            try:
                d = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                self._send(400, b'{"error":"bad json"}')
                return
            val = d.get("value", "")
            if val in ("done", "arch"):
                self.store.update(int(d["id"]),
                                  status="done" if val == "done" else "archived")
            else:
                self.store.update(int(d["id"]), level=val)
            self._send(200, b'{"ok":true}')
            return

        # 批量处理：看板的「全部完成 / 全部归档」
        if path == "/api/bulk":
            try:
                d = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                self._send(400, b'{"error":"bad json"}')
                return
            act = d.get("action") or ""
            ids = [int(x) for x in (d.get("ids") or [])
                   if str(x).strip().lstrip("-").isdigit()]
            if act not in ("done", "arch") or not ids:
                self._send(400, b'{"error":"need action(done|arch) + ids[]"}')
                return
            st = "done" if act == "done" else "archived"
            for i in ids:
                self.store.update(i, status=st)
            self._send(200, json.dumps({"ok": True, "n": len(ids)},
                                       ensure_ascii=False).encode("utf-8"))
            return

        # 刷新按钮：真抓一次公告 + 清理过期 + 重导出（不发邮件）。
        # 日报归云端「每日计划」发，本地只负责把数据弄新。
        # ⚠️ 注意：这只刷新公告。QQ 群消息是机器人被动推过来的，
        #    没有"主动拉取历史消息"的接口，所以点刷新不会凭空收到旧群消息。
        if path == "/api/sync":
            if RT.get("sync_busy"):
                self._send(200, json.dumps({"ok": False, "msg": "正在同步中，稍等…"},
                                           ensure_ascii=False).encode("utf-8"))
                return

            def _job():
                RT["sync_busy"] = True
                try:
                    do_sync(self.store, self.cfg, with_push=False, on_log=log)
                    RT["last_sync"] = datetime.datetime.now().strftime("%H:%M:%S")
                except Exception as e:
                    log("手动同步出错：%r" % e)
                finally:
                    RT["sync_busy"] = False

            threading.Thread(target=_job, daemon=True).start()
            self._send(200, json.dumps({"ok": True, "msg": "已开始同步"},
                                       ensure_ascii=False).encode("utf-8"))
            return

        # 接入自己的 QQ 机器人：填 AppID/Secret → 存进 config → 热重启监听
        if path == "/api/qqconfig":
            try:
                d = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                self._send(400, b'{"error":"bad json"}')
                return
            cfg = load_config()
            q = cfg.setdefault("qq_official", {})
            if "enabled" in d:
                q["enabled"] = bool(d["enabled"])
            if str(d.get("appid") or "").strip():
                q["appid"] = str(d["appid"]).strip()
            if str(d.get("secret") or "").strip():
                q["secret"] = str(d["secret"]).strip()
            save_config(cfg)
            self.cfg = cfg
            Handler.cfg = cfg
            restart_qqbot(self.store, cfg)
            self._send(200, json.dumps(qq_status(cfg),
                                       ensure_ascii=False).encode("utf-8"))
            return

        # WebUI 提交聊天记录
        if path == "/api/import":
            try:
                d = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                self._send(400, b'{"error":"bad json"}')
                return
            n = 0
            for line in (d.get("text") or "").splitlines():
                line = line.strip()
                if len(line) > 6:
                    ok, _ = ingest(self.store, self.cfg, "import", "网页粘贴",
                                   "", int(time.time()), line)
                    n += 1 if ok else 0
            self._send(200, json.dumps({"added": n}, ensure_ascii=False))
            return

        self._send(404, b'{"error":"not found"}')

    # ---- 事件处理 ----
    def _handle_event(self, ev):
        if ev.get("post_type") != "message":
            return
        gid = ev.get("group_id")
        watches = self.cfg.get("watch_groups") or []
        if gid and watches and gid not in watches:
            return
        text = (ev.get("raw_message") or ev.get("message") or "").strip()
        if isinstance(text, list):
            text = "".join(s.get("data", {}).get("text", "")
                           for s in text if isinstance(s, dict))
        if ev.get("message_type") != "group" or len(text) < 6:
            return
        if re.match(r"^\[CQ:(?:face|image|at)\]", text) and len(text) < 12:
            return
        sender = ((ev.get("sender") or {}).get("card")
                  or (ev.get("sender") or {}).get("nickname") or str(ev.get("user_id")))
        src = group_name(self.cfg, gid) if gid else "私聊"
        ingest(self.store, self.cfg, "qq", src, sender,
               int(ev.get("time") or time.time()), text)


_GROUP_CACHE = {}


def group_name(cfg, gid):
    if not gid:
        return "未知来源"
    if gid in _GROUP_CACHE:
        return _GROUP_CACHE[gid]
    api = (cfg.get("onebot_api") or "").rstrip("/")
    name = "群%s" % gid
    if api:
        try:
            import urllib.request
            req = urllib.request.Request(
                "%s/get_group_info?group_id=%s" % (api, gid), headers=UA)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=6) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            name = ((d.get("data") or {}).get("group_name")) or name
        except Exception:
            pass
    _GROUP_CACHE[gid] = name
    return name


# ============ 定时推送 ============
def state_get(key, default=None):
    try:
        return json.load(open(STATE_PATH, "r", encoding="utf-8")).get(key, default)
    except Exception:
        return default


def state_put(key, val):
    try:
        d = json.load(open(STATE_PATH, "r", encoding="utf-8"))
    except Exception:
        d = {}
    d[key] = val
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def ticker(store, cfg, stop_ev):
    """每日定时推送"""
    while not stop_ev.is_set():
        now = datetime.datetime.now()
        want = cfg.get("daily_push_time", "07:30")
        try:
            hh, mm = [int(x) for x in want.split(":")]
        except Exception:
            hh, mm = 7, 30
        today_key = now.strftime("%Y-%m-%d")
        # 必须限定时间窗口。否则 daily_push_time=00:00 时任何时刻都满足 ">= 00:00"，
        # 结果每次启动都立刻同步+发一封邮件。
        now_min = now.hour * 60 + now.minute
        target_min = hh * 60 + mm
        # 到点就跑；并且「补跑」——00:00 时电脑若关着，早上开机自动补上，
        # 不至于因为错过那一个小时的窗口就整天不同步。
        # last_push 保证同一天只跑一次，所以反复重启服务也不会重复抓取/重复发信。
        if now_min >= target_min and state_get("last_push") != today_key:
            late = now_min - target_min
            log("【定时同步】%s%s" % (want, ("（补跑，晚 %d 分钟）" % late) if late > 5 else ""))
            # 只同步、不发邮件：日报归云端「每日计划」发，本地不再重复发一封。
            do_sync(store, cfg, with_push=False, on_log=log)
            state_put("last_push", today_key)
        stop_ev.wait(30)


# ============ 主流程 ============
def cmd_init():
    if os.path.exists(CONFIG):
        log("配置已存在：", CONFIG)
        return
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
    log("已生成配置 →", CONFIG)


def cmd_list(store, cfg, show_all=False):
    where = "1=1" if show_all else "status='open'"
    rows = store.rows(where=where)
    if not rows:
        print("（空）")
        return
    print("共 %d 条\n" % len(rows))
    for r in rows:
        tail = " 截止%s" % r["ddl"] if r["ddl"] else ""
        print("#%-4d [%s]%s %s" % (r["id"], r["level"], tail, r["title"]))
        print("       来源：%s ｜ 依据：%s" % (r["src_name"], r["reason"]))


def start_qqbot(store, cfg, stop_ev):
    """启动 QQ 官方机器人监听（WebSocket）。收不到消息通常是群里没开「全部群消息」。"""
    if _qqbot is None:
        log("qqbot 模块没加载，跳过 QQ 监听")
        return
    q = cfg.get("qq_official") or {}
    if not q.get("enabled"):
        log("QQ 机器人：配置里 enabled=false，跳过监听")
        return None
    appid, secret = str(q.get("appid") or "").strip(), str(q.get("secret") or "").strip()
    if not appid or not secret:
        log("QQ 机器人：还没填 AppID/Secret，跳过（可在看板顶部「接入我的机器人」里填）")
        return None

    # 群 openid → 可读名。config.json 的 qq_official.group_alias 里配，
    # 例：{"5A1B2C": "2511班群"}。没配就显示 openid 前 10 位。
    alias = q.get("group_alias") or {}

    def name_of(gid):
        g = str(gid)
        for k, v in alias.items():
            if k in g:                      # openid 很长，做包含匹配方便手填前几位
                return "QQ群·%s" % v
        return "QQ群 %s" % g[:10]

    def on_msg(group, sender, text, ts):
        ingest(store, cfg, "qq", name_of(group), sender, ts, text)

    bot = _qqbot.QQBot(appid, secret, on_msg,
                       intents=int(q.get("intents") or _qqbot.BASE_INTENTS),
                       sandbox=bool(q.get("sandbox")),
                       on_log=log)
    threading.Thread(target=bot.run, args=(stop_ev,), daemon=True).start()
    log("QQ 官方机器人监听已启动（intents=%s）" % (q.get("intents") or _qqbot.BASE_INTENTS))
    return bot


def register_protocol(cfg, on_log=None):
    """把 shezhang1:// 协议登记到当前用户，指向本目录的「网页启动桥梁.bat」。

    用途：在蛇杖网页版点「刷新并同步」时，浏览器靠这个协议唤起本机桥接
    （网页 JS 无权直接启动本机程序，这是浏览器的安全底线）。
    放在启动流程里自动做 —— 使用者不用手动注册，移动过文件夹也会自动纠正。
    只写 HKCU，不需要管理员权限；任何失败都只是少一个便利，不影响桥接本身。
    """
    if os.name != "nt" or not cfg.get("register_protocol", True):
        return False
    bat = os.path.join(HERE, "网页启动桥梁.bat")
    if not os.path.exists(bat):
        return False          # 没这个脚本就不登记，免得协议唤起一个不存在的东西
    want = '"%s"' % bat
    try:
        import winreg
        cur = ""
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Classes\shezhang1\shell\open\command") as k:
                cur = winreg.QueryValueEx(k, "")[0]
        except Exception:
            pass
        if cur == want:
            return False      # 已经指向正确路径，不用重复写
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                r"Software\Classes\shezhang1", 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "URL:蛇杖一号 信息桥梁")
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                r"Software\Classes\shezhang1\shell\open\command", 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, want)
        return True
    except Exception as e:
        if on_log:
            on_log("登记 shezhang1:// 协议没成功（不影响桥接本身）：%r" % e)
        return False


def cmd_run(store, cfg):
    # 单实例锁：第二个实例在启动任何东西之前就退出，杜绝两个机器人抢 token。
    if not acquire_lock():
        import sys as _sys
        _sys.exit(1)

    host, port = cfg.get("listen_host"), int(cfg.get("listen_port", 8890))
    Handler.store, Handler.cfg = store, cfg

    stop = threading.Event()
    RT["store"] = store
    RT["qq_stops"] = [stop]

    # ---- 不再默认常驻定时（主公 2026-09-30 定） ----
    # 原来 ticker 每天 00:00 自动「抓取 + 清理 + 发邮件」，要求电脑一直开着。
    # 现在日报交给云端「每日计划」（bicheng2026/daily-plan）发，本地只管：
    #   ① 收到群消息就入库（QQ 长连接，程序不跑就收不到——这是协议限制，见 README）
    #   ② 打开看板时同步一次，保证看到的是新的
    # 想恢复本地定时：config.json 里把 local_timer_enabled 设为 true。
    if cfg.get("local_timer_enabled"):
        threading.Thread(target=ticker, args=(store, cfg, stop), daemon=True).start()
        log("本地定时「已启用」（%s）" % cfg.get("daily_push_time"))
    else:
        log("本地定时「已关闭」——日报由云端「每日计划」发送")

    if cfg.get("sync_on_start", True):
        def _boot_sync():
            try:
                do_sync(store, cfg, with_push=False, on_log=log)
            except Exception as e:
                log("启动同步出错：%r" % e)
        threading.Thread(target=_boot_sync, daemon=True).start()

    RT["bot"] = start_qqbot(store, cfg, stop)

    srv = HTTPServer((host, port), Handler)
    log("桥梁已启动 → http://%s:%d/" % (host, port))
    log("Ctrl+C 停止")

    # 顺手登记 shezhang1:// 协议：以后在蛇杖网页版点「刷新并同步」就能唤起本桥接。
    if register_protocol(cfg, log):
        log("已登记 shezhang1:// 协议 → 蛇杖网页版的「刷新并同步」现在可以唤起本桥接")

    # 双击启动时自动把看板推到浏览器（config.json 里 open_browser 设 false 可关）。
    # 延时 1.5 秒是为了等 serve_forever 真正开始 accept。
    if cfg.get("open_browser", True):
        def _open_board():
            time.sleep(1.5)
            try:
                import webbrowser
                webbrowser.open("http://127.0.0.1:%d/" % port)
            except Exception as e:
                log("自动打开浏览器失败（手动访问 http://127.0.0.1:%d/ 即可）：%r" % (port, e))
        threading.Thread(target=_open_board, daemon=True).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("正在停止…")
        for e in (RT.get("qq_stops") or []):
            e.set()
        stop.set()
        srv.server_close()


def main():
    import sys
    rotate_log()
    args = sys.argv[1:]
    cmd = args[0] if args else "run"

    if cmd == "init":
        cmd_init()
        return

    cfg = load_config()
    store = Store()

    if cmd == "run":
        cmd_run(store, cfg)
    elif cmd == "serve":
        cmd_run(store, cfg)
    elif cmd == "list":
        cmd_list(store, cfg, show_all="--all" in args)
    elif cmd == "import":
        p = args[1] if len(args) > 1 else ""
        import_chat(store, cfg, p)
    elif cmd == "crawl":
        crawl_sites(store, cfg)
    elif cmd == "sync":
        # 抓新 → 清旧 → 导出看板 → 发邮件。加 --no-mail 只同步不发信
        do_sync(store, cfg, with_push=("--no-mail" not in args), on_log=log)
    elif cmd == "purge":
        st = purge_expired(store, cfg)
        print("清理完成：" + "，".join("%s %d 条" % (k, v) for k, v in st.items() if v) or "无")
    elif cmd == "regrade":
        # 规则改了之后，把库里已有条目按新规则重算一遍。
        # 保留人工改动：已完成/已归档的不动状态，只重算级别和依据。
        n, changed = 0, 0
        for r in store.rows(where="1=1"):
            level, reasons, ddl, keep, low_conf = classify(r["raw"], cfg)
            pf_ok, pf_msg = True, ""
            if r["source"] in ((cfg.get("profile") or {}).get("apply_to") or []):
                pf_ok, pf_msg = match_profile(r["raw"], cfg)
                if pf_msg:
                    reasons.append(pf_msg)
            if not pf_ok:
                level = "P3"
            # 置顶标记也要跟着重算，否则改了词表老条目不会变
            _pin, pin_hits = is_pinned(r["raw"] or "", cfg)
            if pin_hits:
                reasons.insert(0, "🏆 比赛/竞赛类（%s）→ 置顶" % "/".join(pin_hits[:3]))
            new_status = r["status"]
            if r["status"] in ("open", "noise"):
                new_status = "noise" if (low_conf or not pf_ok) else "open"
            if new_status != r["status"]:
                store.update(r["id"], status=new_status)
            if level != r["level"]:
                changed += 1
            store.update(r["id"], level=level, reason="；".join(reasons),
                         ddl=ddl or "", pin=1 if pin_hits else 0)
            n += 1
        print("重新定级 %d 条，其中 %d 条级别有变化" % (n, changed))
    elif cmd == "reddl":
        # 已入库的公告若还没有截止日，回去补抓正文重抽一遍期限。
        # 用途：改了 parse_ddl 规则、或早期只存了标题没存正文时，给历史条目补课。
        # 用法：python bridge.py reddl [最多处理几条，默认 60]
        cap = 200
        for a in args[2:]:
            if a.isdigit():
                cap = int(a)
        n, got, fetched = 0, 0, 0
        rows = [r for r in store.rows(where="1=1") if r["source"] == "crawl"]
        rows.sort(key=lambda r: r["id"], reverse=True)
        for r in rows[:cap]:
            n += 1
            raw = r["raw"] or ""
            # 正文已经在库里（早先补抓过）就直接重抽，不再发请求；
            # 规则改了之后，正是靠这一句把旧的错日期重算掉。
            if len(raw) < 200 and str(r["sender"] or "").startswith("http"):
                body = fetch_body(r["sender"])
                if body:
                    raw = raw + "\n" + body
                    store.update(r["id"], raw=raw)
                    fetched += 1
            d = parse_ddl(raw)
            store.update(r["id"], ddl=d or "")
            if d:
                got += 1
        print("重算 %d 条公告，其中 %d 条有截止日（本次补抓正文 %d 条）" % (n, got, fetched))
    elif cmd == "export":
        p = os.path.join(os.path.dirname(HERE), "网页版", "data", "todo.json")
        print("已导出 %d 条 → %s" % (export_board_json(store, p, cfg=cfg), p))
        ip = os.path.join(os.path.dirname(p), "todo.ics")
        text, n = build_ical(store, bool(cfg.get("export_include_qq")), cfg)
        with open(ip, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        print("已生成 %d 个日历事件 → %s" % (n, ip))
    elif cmd == "ical":
        ip = os.path.join(os.path.dirname(HERE), "网页版", "data", "todo.ics")
        text, n = build_ical(store, bool(cfg.get("export_include_qq")), cfg)
        os.makedirs(os.path.dirname(ip), exist_ok=True)
        with open(ip, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        print("已生成 %d 个日历事件 → %s" % (n, ip))
    elif cmd == "push":
        t, c = build_daily_summary(store)
        ok, msg = push_wechat(cfg, t, c, dry="--dry" in args)
        print("-" * 50)
        print("标题：%s" % t)
        print(msg)
    elif cmd == "set":
        if len(args) < 3:
            print("用法：python bridge.py set <id> <P0|P1|P2|P3|done|arch>")
            return
        v = args[2].upper()
        if v in ("DONE", "ARCH"):
            store.update(int(args[1]), status="done" if v == "DONE" else "archived")
        else:
            store.update(int(args[1]), level=v)
        print("已更新 #%s → %s" % (args[1], v))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
