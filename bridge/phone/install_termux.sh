#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
#  蛇杖一号 · 安卓手机主机 一键安装脚本
#  跑在 Termux 里：  bash install_termux.sh
#
#  作用：装 Python / 证书 / 唤醒锁，把桥接代码搬到手机，
#        配好开机自启与守护，最后把「手机模式」写进 config.json。
#  全程零第三方 Python 包（bridge 是纯标准库，Termux 自带）。
# ============================================================
set -e
HOME_DIR="$HOME"
TARGET="$HOME_DIR/shezhang"

say() { echo ""; echo "==> $*"; }

say "1/6 更新软件源"
pkg update -y >/dev/null 2>&1 || true
pkg upgrade -y >/dev/null 2>&1 || true

say "2/6 安装运行必需包"
# python  = 跑 bridge.py（Termux 提供的 Python 自带 sqlite3 / ssl / ssl 全部标准库）
# termux-tools = 给 termux-wake-lock / termux-setup-storage 用
# termux-wake-lock = 熄屏也保持 CPU 唤醒（收消息的关键）
# termux-api      = 可选，以后想用手机通知栏提醒会用到
pkg install -y python termux-tools termux-wake-lock termux-api ca-certificates openssl git >/dev/null 2>&1 || true

say "3/6 申请存储权限（会弹授权框，选「允许」）"
[ -d "$HOME_DIR/storage" ] || termux-setup-storage

say "4/6 准备目录 $TARGET"
rm -rf "$TARGET"
mkdir -p "$TARGET"

say "5/6 把桥接代码放到手机"
read -r -p "    代码来源：1=git clone(需仓库可访问)  2=从手机存储目录复制  [1] " SRC
SRC="${SRC:-1}"
if [ "$SRC" = "1" ]; then
  cd "$TARGET"
  git clone --depth 1 https://github.com/bicheng2026/shezhang1.git _src
  cp -r "$TARGET/_src/bridge/." "$TARGET/"
  rm -rf "$TARGET/_src"
else
  echo "    请把电脑上的 bridge 整个文件夹，用数据线拖到手机存储里（任意位置都行）。"
  read -r -p "    粘贴手机里 bridge 文件夹的完整路径（例：/storage/emulated/0/Download/bridge）：" SRC2
  cp -r "$SRC2"/. "$TARGET/"
fi
cd "$TARGET"

say "6/6 切「手机模式」配置"
python - <<'PY'
import json, io
p = "config.json"
cfg = json.load(open(p, encoding="utf-8"))
cfg["open_browser"] = False
# 手机当主机：群消息由这台机器收，电脑端靠 /api/sync 拉
cfg["sync_on_start"] = True
cfg["local_timer_enabled"] = False
cfg.setdefault("phone_mode", True)
json.dump(cfg, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("   config.json 已写入：open_browser=false, phone_mode=true")
PY

chmod +x start_phone.sh 2>/dev/null || true
say "安装完成。"
cat <<'EOM'

  接下来两步（系统设置，必须手动点，脚本代替不了）：
    ① 电池优化：设置 → 应用 → Termux → 电池 → 允许/无限制
    ② 自启动：  设置 → 应用 → Termux → 允许自启动 / 后台运行 / 不受限制
                 （华为看「应用启动管理」，小米看「省电策略→无限制」）

  然后执行：bash start_phone.sh
EOM
echo ""
