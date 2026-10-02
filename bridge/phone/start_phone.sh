#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
#  蛇杖一号 · 手机主机 启动/守护/状态
#  用法（Termux 里）：
#     bash start_phone.sh      守护方式启动（这个窗口别关，或直接按 Ctrl+C 只会停守护）
#     bash start_phone.sh stop 停掉桥接
#     bash start_phone.sh status 只看状态，不启动
#     bash start_phone.sh boot  装开机自启（只需跑一次）
# ============================================================
DIR="$HOME/shezhang"
LOG="$DIR/phone.log"
[ -d "$DIR" ] && cd "$DIR" || { echo "找不到 $DIR，先跑 install_termux.sh"; exit 1; }

case "$1" in
  stop)
    pkill -f "bridge.py run" && echo "已发出停止信号" || echo "没有在跑的 bridge 进程"
    exit 0
    ;;
  status)
    echo "---- 进程 ----"
    ps aux | grep "[b]ridge.py run" | head -5 || echo "（无进程）"
    echo "---- 机器人通道 ----"
    curl -s --max-time 5 http://127.0.0.1:8890/api/qqstatus || echo "（8890 没响应，服务可能没起）"
    echo ""
    echo "---- 日志尾部 ----"
    [ -f "$LOG" ] && tail -n 12 "$LOG" || echo "（暂无日志）"
    exit 0
    ;;
  boot)
    mkdir -p "$HOME/.termux/boot"
    cat > "$HOME/.termux/boot/start-bridge.sh" <<'BOOT'
#!/data/data/com.termux/files/usr/bin/bash
sleep 30              # 开机等网络就绪，给 30 秒
cd ~/shezhang
nohup bash start_phone.sh >> phone.log 2>&1 &
BOOT
    chmod +x "$HOME/.termux/boot/start-bridge.sh"
    echo "开机自启已写入 ~/.termux/boot/start-bridge.sh"
    echo "（还需要在系统设置里给 Termux 开「允许自启动 / 后台运行」才真正生效）"
    exit 0
    ;;
  *)
    ;;
esac

# ---------- 守护：进程被系统杀掉就 5 秒后拉起来 ----------
echo "蛇杖一号手机主机启动中…日志：$LOG"
echo "省电设置：这里【不】申请 CPU 唤醒锁。"
echo "          内核自己就能维持 TCP 连接，强制唤醒锁会让 CPU 整夜不睡，一晚多掉约 10% 电，纯浪费。"

# 通知栏弹窗提醒：主公 2026-10-02 明确「新消息不用弹窗」，默认关。
# 想开：  ALARM=1 bash start_phone.sh
ALARM="${ALARM:-0}"
if [ "$ALARM" != "1" ]; then
  echo "通知栏弹窗提醒：关闭（按主公要求，不打扰）"
else
  ALARM_DIR="$(dirname "$0")"
  if [ -f "$ALARM_DIR/phone_alarm.py" ]; then
    nohup python "$ALARM_DIR/phone_alarm.py" >> "$LOG" 2>&1 &
    echo "通知栏提醒已启动（每 2 分钟查一次本地库，收到新群消息弹一条）"
  fi
fi

trap 'pkill -f phone_alarm.py; pkill -f "bridge.py run"; pkill -f push_to_wx.py; echo "已停止" ' INT TERM

# ---- 小程序数据推送（每 5 分钟一次，推到微信云函数给小程序读）----
# 前提：手机上放好了 ../wx/push_to_wx.py，且 config.json 里 wx.cloud_url / wx.token 填了。
# 没填就静默跳过，不打扰主公；云函数 URL 化拿到之后自然生效。
WX_DIR="$(dirname "$0")"
if [ -f "$WX_DIR/push_to_wx.py" ]; then
  grep -q '"wx"' "$WX_DIR/config.json" 2>/dev/null && HASWX=1 || HASWX=0
  if [ "$HASWX" = "1" ]; then
    nohup bash -c 'while true; do python "'"$WX_DIR"'/push_to_wx.py" >> "'"$LOG"'" 2>&1; sleep 300; done' >> "$LOG" 2>&1 &
    echo "小程序数据推送已启动（每 5 分钟一次）"
  else
    echo "跳过小程序推送：config.json 里还没有 wx 段（等 cloud_url 填了会自动生效）"
  fi
fi

while true; do
  python bridge.py run >> "$LOG" 2>&1
  echo "[$(date +%H:%M:%S)] 进程退出，5 秒后自动拉起" >> "$LOG"
  sleep 5
done
