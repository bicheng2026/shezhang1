# 蛇杖一号 · 手机当主机（安卓 Termux）部署手册

> 日期：2026-10-02　作者：毕成
> 前置：云端方案已放弃；目标 = **手机常年开机 → 群消息一条不丢 → 电脑彻底不用开机**

> 🔴 **2026-10-02 晚更新：这份手册降级成「可选备份方案」了。**
> 主公改了两条硬约束（**一分钱不花 + 手机电脑不整天在线**），群消息改走
> **QQ Webhook → Cloudflare Workers 免费档**（永远在线、0 元、本机完全不参与），
> 见 `bridge/零成本零在线方案.md`。
> 这份 Termux 手册现在只在两种场合还用得上：
> ① 想让**公告**自动更新（Webhook 只负责群消息，公告还得有台机器跑 `crawl`）；
> ② 群消息接收端出问题时的**备份接收通道**。
> 另外：Webhook 切过去后，这份手册里跑的 WebSocket 长连接会失效（二选一，不是 bug）。

---

## 0. 一句话结论

**能，而且不用重写核心代码。** 三个已核实的底层事实：

1. `qqbot.py` 是**纯标准库**实现（首页就写着 "不装任何第三方包"），自带退避重连（`qqbot.py:391` 主循环 / `:416` 「N 秒后重连」）→ Termux 的 Python 直接能跑。
2. Windows 专属 API（`winreg` / `webbrowser`）**全在函数内部**，且 `bridge.py:1864` 有 `os.name != "nt"` 守卫、`bridge.py:1871` 才 `import winreg` → 手机上 import 整个世界都不会炸。
3. 看板在手机上是 `127.0.0.1:8890`，**手机自己的浏览器就能访问**，不需要任何公网服务器。

所以这次改动 = 加一层「安卓外壳」（装包 + 保活 + 自启），**bridge.py 一个字没动**。

---

## 1. 改完之后链路长这样

```
QQ群 ──实时WS──▶ 手机（Termux 常驻）             电脑（想开才开）
                 ├─ 机器人收班群消息                 ├─ 抓学校公告
                 ├─ 抓公告（连校园网WiFi时）          ├─ 看板（119寸？不，就是浏览器）
                 ├─ 本地库 phone.db / bridge.db     └─ 群消息靠手机那边，这里不收
                 └─ 看板 127.0.0.1:8890
                          │
                          └── 手机浏览器打开 → 班群消息 + 公告 全在这
```

**你要开机的是手机，电脑变成"想用才开"的备机。**

> ⚠️ **同一时间只能有一台机器连 QQ 机器人。** 同一个 AppID 在手机和电脑上各连一次，腾讯那边会把后连的挤掉（或分半），两边都会漏消息。
> → 手册第 5 步：电脑上把 `qq_official.enabled` 关掉，电脑只管抓公告 + 当看板。

---

## 2. 你要做的（前 2 步在应用商店，3-6 步在 Termux 里敲）

### 第 1 步：装 Termux（手机）

- **别用应用宝/华为应用市场那个阉割版**，去 **F-Droid**（官网 `f-droid.org`）装真版 Termux。
- 顺手装 **Termux:Boot**（开机自启用）、**Termux:Wake-lock**（防熄屏睡死）—— 都在 F-Droid 里搜名字就有。
- 装完打开 Termux，会显示 `$` 提示符，敲 `ls` 回车有目录列表就是成了。

### 第 2 步：装 F-Droid 版 Termux（只针对华为/小米的坑）

华为 EMUI 和小米 MIUI 对 Termux 杀后台特别狠，第 6 步会专门讲怎么放行。**先别急着配，跑完第 3 步再看还杀不杀。**

### 第 3 步：把代码弄到手机（Termux 里）

打开 Termux，一行一行敲：

```bash
pkg update -y && pkg upgrade -y
pkg install -y python termux-tools termux-wake-lock termux-api ca-certificates openssl git
termux-setup-storage
```

- `termux-setup-storage` 会弹一个权限框（同时会显示存储访问提示），**选允许**。授权后手机存储会出现在 `~/storage/shared`。
- 然后：

```bash
cd ~
git clone --depth 1 https://github.com/bicheng2026/shezhang1.git _src
mkdir -p ~/shezhang
cp -r ~/shezhang/_src/bridge/. ~/shezhang/
rm -rf ~/shezhang/_src
cd ~/shezhang
ls
```

`ls` 里能看到 `bridge.py`、`qqbot.py`、`config.json` 就对了。

> 如果你的网络下 `git clone` 拉不动/太慢 → 走备用路：电脑上把 `E:\白求恩一号\蛇杖一号\bridge` 整个文件夹拖进手机存储（数据线或微信文件传输助手都行），再在 Termux 里
> `cp -r ~/storage/shared/你放的位置/bridge/. ~/shezhang/` 把路径换成你自己的。

### 第 4 步：跑一键安装（其实第 3 步已经装完包了，这步只做配置和自启）

```bash
cd ~/shezhang
python - <<'PY'
import json
p="config.json"; cfg=json.load(open(p,encoding="utf-8"))
cfg["open_browser"]=False      # 手机上没有桌面浏览器可弹，关掉免得每次报错刷日志
cfg["sync_on_start"]=True      # 启动时抓一次公告
cfg["local_timer_enabled"]=False
cfg["phone_mode"]=True
json.dump(cfg,open(p,"w",encoding="utf-8"),ensure_ascii=False,indent=2)
print("config 已切到手机模式")
PY
```

### 第 5 步：电脑端关掉机器人（避免两边抢连接）

在你电脑上打开 `E:\白求恩一号\蛇杖一号\bridge\config.json`，把 `qq_official` 下改成：

```json
"qq_official": {
    "enabled": false,
    ...
}
```

**电脑从此只抓公告 + 当看板，群消息一律由手机收。** 需要临时让电脑收（比如手机坏了）时再改回 `true`。

### 第 6 步：保活（这一步决定它能不能真 24h 跑）

**6.1 先启动试跑**（这个窗口先别关，观察 5 分钟）：

```bash
cd ~/shezhang
bash start_phone.sh
```

看到类似：

```
QQ 官方机器人监听已启动（intents=33554433）
桥梁已启动 → http://127.0.0.1:8890/
```

用**手机自带的浏览器**（Chrome/系统浏览器，别用微信内置）打开 `http://127.0.0.1:8890/`，能看到看板就成了。

> 更省事的第二条路：手机 Chrome 打开 <https://bicheng2026.github.io/shezhang1/> → 进「信息全览」→ 页面会自己探测 `127.0.0.1:8890` → 探到就显示「班群机器人：接收中」+ 班群消息。这条走的是 https 页面请求 localhost（混合内容豁免），**同时在微信里点链接也能直接跳到这个页面**。两种你都试一下，哪个开得出来用哪个。

**6.2 开机自启**（跑一次就够）：

```bash
bash start_phone.sh boot
```

然后去系统设置放行（**各品牌不一样，这条必须手动点，脚本代替不了**）：

| 品牌 | 路径 |
|---|---|
| 华为 / 荣耀 | 设置 → 应用 → 应用启动管理 → Termux → 关掉「智能限制受限」，打开「允许自启动 / 允许关联启动 / 允许后台活动」 |
| 小米 / Redmi | 设置 → 应用设置 → 应用管理 → Termux → 省电策略选「无限制」；再进安全中心 → 后台管理 → 允许后台运行 |
| OPPO / vivo | 设置 → 应用管理 → Termux → 电池 → 允许后台高耗电 / 开启「允许自启动」 |
| 三星 | 设置 → 电池 → 后台使用限制 → 关闭「休眠正在使用的应用」 |

**6.3 关掉电池优化**（这一步最容易漏）：

设置 → 应用 → Termux → 电池 → **电池优化 → 不允许/无限制**。
（小米叫「耗电优化 → 不允许」，华为在「应用 → 权限 → 电池 → 忽略电池优化」里。）

---

## 3. 以后怎么查它活没活

Termux 里敲：

```bash
bash start_phone.sh status
```

会打印三块：进程在不在、机器人通道状态（**「接收中」= 通的**，这是正常的；「断开重连中」说明网抖了正在连，也会自己恢复）、日志尾部 12 行。

看日志：`tail -f ~/shezhang/phone.log`（Ctrl+C 退出跟随）。

---

## 4. 已经备好的文件

| 文件 | 干嘛的 |
|---|---|
| `bridge/phone/install_termux.sh` | 一键装包 + 搬代码 + 切手机模式 |
| `bridge/phone/start_phone.sh` | 守护启动 / `stop` / `status` / `boot` 四个动作 |

---

## 5. 五个真会踩到的坑

1. **屏幕一直亮着费电。**
   `termux-wake-lock` 保的是 CPU 不睡，**屏幕会自动熄屏**，所以不用手动亮屏。但整晚亮屏收消息，一天大概多掉 8–15% 电。解决办法：用的时候插电，或者把这台手机长期插着充电（智能插座限充 60% 能护电池）。

2. **校园网只在连 WiFi 时抓得到「内网张贴」。**
   手机断 WiFi 走 4G 时，那条源抓不到（403），其余 6 个源照抓。回校园网后下次启动自动补上。

3. **微信里点开的链接，一定是用系统浏览器打开才看得到班群消息。**
   微信内置浏览器（X5 内核）会拦 localhost，属正常现象——点右上角「···」→「在浏览器打开」。

4. **`git clone` 慢或失败** → 走第 3 步的备用路（数据线拷文件夹），不治也就一次的事。

5. **手机重启后没起来** → 说明第 6.2 的自启动没放行。进系统设置翻一遍 Termux 的「自启动 / 后台运行」，打开，然后重启一次手机验证。

---

## 6. 验收清单（跑完逐条打勾）

- [ ] `bash start_phone.sh status` 里机器人状态是 **接收中**
- [ ] 手机浏览器打开 `http://127.0.0.1:8890/` 能看到看板
- [ ] 往班群里发一条消息，2 分钟内看板上出现（**实测这一步，别光看日志**）
- [ ] 手机熄屏 10 分钟后，再发一条，仍收得到（验唤醒锁）
- [ ] 重启手机，起来后 2 分钟内自动连上（验自启）
- [ ] 电脑上 bridge 启动后，日志里没有再出现「机器人监听」→ 说明电脑已不抢连接
