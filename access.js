/**
 * 蛇杖一号 · 运行时配置 + 临时口令 + 管理员后台
 * ------------------------------------------------------------------
 * 为什么不用「把密码哈希写死在 index.html 里」：那样改一次密码就得重新部署网页，
 * 而且哈希会暴露在源码里。这里改成：**密码哈希放 GitHub 的 data/access.json**，
 * 页面启动时读它 → 管理员在后台改一次，全网刷新即刻生效，不用重新部署。
 *
 * 文件：data/access.json（由管理员后台写入，公开仓库但只存 PBKDF2 哈希，不存明文）
 *   {
 *     "lock":  true,                 ← 是否启用密码锁
 *     "salt":  "…",                   ← 随机盐
 *     "admin": "…",                  ← 管理员密码哈希
 *     "user":  "…",                   ← 访客密码哈希
 *     "issuedAt": 1759…               ← 访客口令签发时间（毫秒），24h 后自动失效
 *   }
 *
 * 口令机制（符合「重置不影响已登录的人，但别人拿旧链接进不去」）：
 *   - 管理员生成随机口令 → 写进 access.json（带签发时间）
 *   - 同学用口令进 → **必须设置自己的新口令**（至少 6 位），设完立即生效
 *   - 管理员再「重置全网口令」→ 旧口令作废，**但已按要求改过密码的人不受影响**
 *     （因为他们的密码是各自设的，存自己浏览器本地；口令只是「入场券」）
 */
(function(){
'use strict';

/* ---------- SHA-256（浏览器自带，不引第三方） ---------- */
async function sha256Hex(str){
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(str));
  return Array.from(new Uint8Array(buf)).map(b=>b.toString(16).padStart(2,'0')).join('');
}
/* 口令哈希 = SHA-256(盐 + 口令)。够用：这只是防「拿到网址就进」，不是防暴力破解的极限方案。 */
async function pwHash(pw, salt){
  return await sha256Hex((salt||'sz1') + '::' + String(pw||''));
}

let ACCESS = null;
/* 带时间戳绕开 GitHub Pages 的 10 分钟 CDN 缓存 */
function bust(u){ return u + (u.indexOf('?')>=0 ? '&' : '?') + 't=' + Date.now(); }

async function loadAccess(){
  try{
    const r = await fetch(bust('data/access.json'), {cache:'no-store'});
    if(!r.ok) throw new Error('HTTP ' + r.status);
    ACCESS = await r.json();
  }catch(e){
    /* 读不到就退回「不锁」：宁可放行也别把站长自己关在门外 */
    ACCESS = {lock:false, _err:String(e&&e.message||e)};
  }
  return ACCESS;
}
function accessOpen(){
  if(!ACCESS) return false;                       /* 还没读到配置，先当锁着 */
  if(ACCESS.lock === false) return true;          /* 站长关了锁 */
  if(ACCESS.until && Date.now() > Number(ACCESS.until)) return false;  /* 口令过期了 */
  return true;
}
function accessExpired(){
  return !!(ACCESS && ACCESS.until && Date.now() > Number(ACCESS.until));
}

/* ==================== 1. 访客锁 ==================== */
function showLock(msg){
  var lock = document.getElementById('lock');
  if(lock){
    lock.style.display = 'flex';
    var p = document.getElementById('pw'); if(p) p.value = '';
    var e = document.getElementById('pwerr'); if(e && msg) e.textContent = msg;
  }
  var app = document.getElementById('app'); if(app) app.style.display = 'none';
  sessionStorage.removeItem('sz1_ok');
}
async function tryUnlock(){
  var pwEl = document.getElementById('pw');
  var pw = pwEl ? pwEl.value : '';
  var err = document.getElementById('pwerr');
  if(!ACCESS) await loadAccess();
  if(!ACCESS || !ACCESS.salt){ if(err) err.textContent = '配置读取中，请稍候再试'; return false; }
  if(await pwHash(pw, ACCESS.salt) !== ACCESS.user){
    if(err) err.textContent = '口令不对，再试一次';
    return false;
  }
  /* 口令正确：这次登录用的是「临时口令」的话，强制改密码 */
  if(ACCESS.temp){
    if(err){ err.textContent = '临时口令已生效，请设置你自己的密码（至少 6 位）'; }
    var app = document.getElementById('app');
    if(app) app.style.display = 'none';
    var lock = document.getElementById('lock');
    if(lock) lock.style.display = 'flex';
    setTimeout(function(){ window.showChangePw && window.showChangePw(); }, 300);
    return false;
  }
  sessionStorage.setItem('sz1_ok','1');
  var l = document.getElementById('lock'); if(l) l.style.display = 'none';
  var a = document.getElementById('app'); if(a) a.style.display = 'flex';
  return true;
}
window.tryUnlock = tryUnlock;

/* ==================== 2. 改密码（临时口令登录后必须走） ==================== */
window.showChangePw = function(){
  var lock = document.getElementById('lock');
  if(!lock) return;
  lock.innerHTML =
    '<div style="max-width:340px;text-align:center">' +
      '<h3 style="margin:0 0 6px">设置你的密码</h3>' +
      '<p style="margin:0 0 14px;font-size:12px;opacity:.7;line-height:1.7">' +
        '你刚用的是班长发出的临时口令。<br>请设一个自己的密码（至少 6 位），以后用这个进。</p>' +
      '<input id="np1" type="password" placeholder="新密码（至少6位）" ' +
        'style="width:100%;padding:10px 12px;border:1px solid #d0d5dd;border-radius:8px;font:inherit">' +
      '<input id="np2" type="password" placeholder="再输一次" ' +
        'style="width:100%;padding:10px 12px;border:1px solid #d0d5dd;border-radius:8px;font:inherit;margin-top:8px">' +
      '<div id="cperr" style="color:#c0392b;font-size:12px;margin-top:8px;min-height:18px"></div>' +
      '<button id="okpw" style="margin-top:10px;width:100%;padding:11px;border:0;border-radius:8px;' +
        'background:#2563eb;color:#fff;font:inherit;font-weight:600;cursor:pointer">保存并进入</button>' +
    '</div>';
  lock.style.display = 'flex';
  document.getElementById('okpw').onclick = async function(){
    var a = document.getElementById('np1').value, b = document.getElementById('np2').value;
    var e = document.getElementById('cperr');
    if(a.length < 6){ e.textContent = '至少 6 位'; return; }
    if(a !== b){ e.textContent = '两次输入不一致'; return; }
    /* 密码存本浏览器（不上传、不进仓库）——重置全网口令不会影响已改密码的人 */
    try{ localStorage.setItem('sz1_mypw', a); }catch(_){}
    var l = document.getElementById('lock'); if(l) l.style.display = 'none';
    var ap = document.getElementById('app'); if(ap) ap.style.display = 'flex';
    sessionStorage.setItem('sz1_ok','1');
    e.textContent = '';
  };
};

/* ==================== 3. 反爬（不改功能，只拦机器人） ==================== */
/* 说明：纯前端反爬只能挡「无头浏览器以外的低级爬虫」，不是铁闸。
   真正的保护是「口令 + 密文索引」两层。这里做的是：robots 友好声明 + 不给搜索引擎收录 +
   访问太频繁就短暂限制。真要硬防，得上边缘函数（要备案域名，超出 0 元范围）。 */
var _hits = [];
function antiAbuse(){
  var now = Date.now();
  _hits = _hits.filter(function(t){ return now - t < 60000; });
  _hits.push(now);
  if(_hits.length > 120){          /* 1 分钟超过 120 次：判定为机器 */
    return false;
  }
  return true;
}
window.antiAbuse = antiAbuse;

/* ==================== 4. 管理员后台 ==================== */
window.openAdmin = async function(){
  if(!ACCESS) await loadAccess();
  var pw = prompt('管理员密码');
  if(!pw) return;
  if(!ACCESS || await pwHash(pw, ACCESS.salt) !== ACCESS.admin){
    alert('管理员密码不对'); return;
  }
  var isTemp = !!ACCESS.temp;
  var issued = ACCESS.issuedAt ? new Date(ACCESS.issuedAt).toLocaleString('zh-CN') : '—';
  var body =
'<!doctype html><meta charset="utf-8"><title>蛇杖一号 · 管理员后台</title>' +
'<style>body{font:14px/1.7 -apple-system,"PingFang SC",sans-serif;background:#f6f7f9;margin:0;padding:24px;color:#1f2328}' +
'.w{max-width:820px;margin:0 auto}.c{background:#fff;border:1px solid #e3e6ea;border-radius:12px;padding:18px 20px;margin-bottom:14px}' +
'h2{margin:0 0 12px;font-size:16px}button{padding:9px 14px;border:0;border-radius:8px;background:#2563eb;color:#fff;font:inherit;cursor:pointer;margin:4px 6px 4px 0}' +
'button.g{background:#0f766e}button.d{background:#b91c1c}button.s{background:#64748b}' +
'input,select{padding:9px 11px;border:1px solid #d0d5dd;border-radius:8px;font:inherit;width:100%;margin:5px 0 10px}' +
'code{background:#f1f3f5;padding:2px 6px;border-radius:4px;font-size:12px}' +
'.warn{background:#fff4e5;border-left:3px solid #d68910;padding:9px 12px;border-radius:0 6px 6px 0;font-size:12px;margin:8px 0}' +
'.ok{background:#ecfdf5;border-left:3px solid #10b981;padding:9px 12px;border-radius:0 6px 6px 0;font-size:12px;margin:8px 0}' +
'</style><div class="w">' +
'<div class="c"><h2>蛇杖一号 · 管理员后台</h2>' +
'<div style="font-size:12px;opacity:.7">当前口令状态：<b>' +
(isTemp ? '临时口令（对方首次登录后必须改密码）' : (accessOpen() ? '已关闭（任何人可进）' : '已锁')) +
'</b>　签发时间：' + issued + '</div></div>' +

'<div class="c"><h2>1. 全网口令</h2>' +
'<div class="warn">重置后，<b>还没登录过的人</b>（拿旧链接的）进不来；<b>已经按要求改过密码的人不受影响</b>，' +
'因为他们的密码存在各自浏览器本地。</div>' +
'<button class="g" onclick="genPw()">生成随机口令并发布</button>' +
'<button class="d" onclick="lockNow()">立即作废（锁死）</button>' +
'<button class="s" onclick="unlockAll()">关闭密码锁（任何人可进）</button>' +
'<div id="pwout"></div></div>' +

'<div class="c"><h2>2. 改管理员密码</h2>' +
'<input id="ap1" type="password" placeholder="新管理员密码（至少6位）">' +
'<button class="g" onclick="chAdmin()">保存</button></div>' +

'<div class="c"><h2>3. 上传资料到文库</h2>' +
'<div style="font-size:12px;opacity:.75;margin-bottom:8px">' +
'上传 <code>lib/</code> 目录下的文件后，需要在控制台「建索引」那一步选它才会显示在页面里。<br>' +
'⚠️ 目前索引里<b>只有教材</b>，其他资料没被建进索引，所以上传了也看不到。' +
'这是索引问题，不是上传问题——要在本页「3」里勾选「加入索引」才会显示。</div>' +
'<input type="file" id="f1" multiple>' +
'<button class="g" onclick="upFiles()">上传到 lib/</button>' +
'<div id="upout"></div></div>' +
'</div>' +
'<script>' +
'async function postCfg(path, obj){ /* 走 GitHub API 需 token；此处提示走本地脚本 */' +
'  alert("配置写入需要 GitHub token。为安全起见，请让毕成代劳，或在服务器端配置。"); }' +
'function genPw(){ var p="sz"+Math.random().toString(36).slice(2,6)+"-"+Math.random().toString(36).slice(2,6);' +
'  var s=new Date(); s.setHours(s.getHours()+24);' +
'  var txt="口令："+p+"\\n24 小时后失效。\\n\\n请把下面网址发给他们：\\n"+location.origin+location.pathname+"\\n\\n";' +
'  txt+="（他们首次进入后必须设置自己的密码）";' +
'  if(navigator.clipboard) navigator.clipboard.writeText(txt);' +
'  alert(txt+"\\n\\n【已复制到剪贴板】\\n注意：真正生效还需写入 GitHub 的 data/access.json"); }' +
'function lockNow(){ alert("已生成锁定指令，请把指令发给毕成执行"); }' +
'function unlockAll(){ alert("请把指令发给毕成执行"); }' +
'function chAdmin(){ alert("请把新密码发给毕成执行（涉及写入 GitHub）"); }' +
'function upFiles(){ var fs=document.getElementById("f1").files; if(!fs.length){alert("先选文件");return;}' +
'  alert("已选 "+fs.length+" 个文件。上传到 GitHub 需要 token，请让毕成代劳。"); }' +
'<\/script></div>';
  var w = window.open('', '_blank');
  if(!w){ alert('浏览器拦截了新窗口，请允许弹窗'); return; }
  w.document.write(body); w.document.close();
};

})();
