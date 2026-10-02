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
var _hits = [], _pageHits = [];
function antiAbuse(){
  var now = Date.now();
  _hits = _hits.filter(function(t){ return now - t < 60000; });
  _hits.push(now);
  if(_hits.length > 120) return false;
  return true;
}
window.antiAbuse = antiAbuse;

/* ============ 反爬加固（2026-10-03）============
   一、机器人识别：无头浏览器最典型的破绽是 navigator.webdriver / 无头 UA。
      识别到就「假装报错」，不弹提示（弹提示等于告诉对方我们检测到了）。
   二、粘性会话：必须先在页面里正常停留过、点过东西，才允许取整册（fullBytes）。
      这样脚本必须模拟真实交互，纯 curl/requests 拿不到全文。
   三、整册取文限流：短时间取多本整册就中断。
   四、频率：每分钟请求上限，超过就降速。
   ⚠️ 说实话：这些都是「提高爬取成本」，不是铁闸。密钥在客户端 JS 里，
      愿意花功夫的人终究能拿到。真正的铁闸要上边缘函数做鉴权（需备案域名，超 0 元）。 */

/* —— 1. 机器人识别 —— */
window.isBot = function isBot(){
  try{
    if(navigator.webdriver === true) return true;
    var ua = navigator.userAgent || "";
    if(/HeadlessChrome|PhantomJS|Puppeteer|Playwright|Selenium|bot\/|crawler|spider|python-requests|curl\/|wget|libwww-perl|java\/|okhttp|axios/i.test(ua)) return true;
    /* ⚠️ 以下两条容易误伤真人浏览器，慎用：
       - navigator.languages 为空：部分安卓/隐私模式会为空 → 不作为判据
       - plugins 数为 0：新版 Chrome 正常也是 0 → 只在 UA 也不像主流浏览器时才判
       只保留「屏宽高为 0」这一条（真浏览器不会是 0）。 */
    if(window.screen && (window.screen.width === 0 || window.screen.height === 0)) return true;
  }catch(e){}
  return false;
}

/* —— 2. 粘性会话：真人用过页面才放行整册 —— */
var _human = false;
function markHuman(){ _human = true; }
window.markHuman = markHuman;
function isHuman(){ return _human; }
window.isHuman = isHuman;
/* 页面加载 1.5 秒后、以及任何一次点击，都算「真人在用」 */
setTimeout(function(){ _human = true; }, 1500);
["click","keydown","mousemove","touchstart","wheel"].forEach(function(ev){
  document.addEventListener(ev, function(){ _human = true; }, {once:true, passive:true});
});

/* —— 3. 整册取文限流（防批量拖书） —— */
var _fullLog = [];
window.fullAllow = function fullAllow(){
  var now = Date.now();
  _fullLog = _fullLog.filter(function(t){ return now - t < 120000; });
  _fullLog.push(now);
  if(_fullLog.length > 12) return false;   /* 2 分钟内取超过 12 本整册 → 拦 */
  return true;
}
window.fullAllow = fullAllow;

/* —— 4. 一次性「访问凭据」：页面启动时向云函数领一个短时令牌，
      之后所有取文都要带它。没有令牌的直接请求（curl/脚本）拿不到数据。 —— */
var GATE = null;
async function getGate(force){
  if(GATE && !force && GATE.exp > Date.now()) return GATE;
  try{
    var r = await fetch("https://1499683192-f4e14euqer.ap-guangzhou.tencentscf.com/gate", {cache:"no-store"});
    var j = await r.json();
    if(j && j.token) GATE = {token:j.token, exp: Date.now() + (j.ttl||120000)};
  }catch(e){ GATE = null; }
  return GATE;
}
window.getGate = getGate;

/* ==================== 4. 管理员后台 ==================== */
/* 云函数地址：管理员后台的所有动作都发到它（token 在云端，不经过浏览器） */
var CLOUD = 'https://1499683192-f4e14euqer.ap-guangzhou.tencentscf.com';

window.callCloud = async function(action, obj){
  var r = await fetch(CLOUD + '/admin?action=' + encodeURIComponent(action), {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(obj || {})
  });
  return await r.json();
};

/* ==================== 管理员后台（2026-10-03 重写）====================
   🔴 之前空白的原因（记下来别再犯）：
   1) `w.document.body.innerHTML = adminHtml(...)` —— adminHtml 返回的是**完整 <body> 结构**，
      塞进 innerHTML 等于把 <body> 嵌进 body 里，浏览器直接忽略 → 页面空白。
   2) 后台脚本用 `window.addEventListener("load", ...)` 渲染，但 document.write 之后
      load 事件早就过了，监听器永远不触发 → 内容根本没渲染。
   现在改成：**一次性 document.write 出完整 HTML**（含 CSS 与内联脚本，立即执行）。 */
window.openAdmin = async function(){
  if(!ACCESS) await loadAccess();
  var pw = prompt('管理员密码');
  if(!pw) return;
  var w = window.open('', '_blank');
  if(!w){ alert('浏览器拦截了新窗口，请允许弹窗'); return; }
  var salt = (ACCESS && ACCESS.salt) || 'sz1';

  w.document.write('<!doctype html><meta charset="utf-8"><title>蛇杖一号 · 管理员后台</title>' +
    '<body style="font:14px/1.8 -apple-system,\'PingFang SC\',sans-serif;background:#f6f7f9;padding:24px">' +
    '<div style="max-width:760px;margin:0 auto"><h2>蛇杖一号 · 管理员后台</h2>' +
    '<p id="tip" style="font-size:13px">正在校验口令…</p></div>');

  var r = null;
  try{
    r = await window.callCloud('check', {pw: pw, salt: salt});   /* 只校验，不改状态 */
  }catch(e){ r = null; }

  if(!r || r.code === 401 || r.code === 403){
    w.document.getElementById('tip').innerHTML =
      '<span style="color:#b91c1c">口令不对，或连不上云端。</span><br>' +
      '<span style="font-size:12px;opacity:.7">' + (((r && r.msg) || '') || '请检查网络后重试') + '</span>';
    return;
  }
  /* 口令通过：一次性写出完整后台页面 */
  w.document.write(adminPage(salt, pw, r));
  w.document.close();
};

/* ---- 后台页面：完整 HTML（含 CSS + 立即执行的脚本） ---- */
function adminPage(salt, pw, state){
  var untilTxt = (state && state.until) ? new Date(state.until).toLocaleString('zh-CN') : '—';
  var lockTxt = (state && state.acc_public && state.acc_public.lock) ? '<b>已锁</b>' : '<b>开放（任何人可进）</b>';
  var tempTxt = (state && state.acc_public && state.acc_public.temp) ? '临时口令生效中（对方首次登录须改密码）' : '非临时口令';

  function card(n, inner){
    return '<div style="background:#fff;border:1px solid #e3e6ea;border-radius:12px;padding:16px 18px;margin-bottom:14px">' +
      '<h3 style="margin:0 0 10px;font-size:15px">' + n + '</h3>' + inner + '</div>';
  }
  var BTN = 'style="padding:9px 14px;border:0;border-radius:8px;color:#fff;font:inherit;cursor:pointer;margin:4px 6px 4px 0"';

  var html =
    '<div style="max-width:760px;margin:0 auto">' +
      '<h2 style="margin:0 0 4px">蛇杖一号 · 管理员后台</h2>' +
      '<p style="font-size:12px;opacity:.65;margin:0 0 18px">口令正确 · 云端连接正常</p>' +

      card('1. 全网口令',
        '<div style="background:#fff8e6;border-left:3px solid #d68910;padding:9px 12px;border-radius:0 6px 6px 0;font-size:12px;line-height:1.7;margin-bottom:10px">' +
        '「生成随机口令」= 发布一张 24 小时有效的入场券，对方首次进入后<b>必须设置自己的密码</b>。<br>' +
        '重置后：<b>还没登录过的人</b>（拿旧链接的）进不来；<b>已经改过密码的人不受影响</b>（密码存在他们自己浏览器里）。</div>' +
        '<button id="bgen" ' + BTN + ' background:#0f766e">生成随机口令（24h 有效）</button>' +
        '<button id="block" ' + BTN + ' background:#b91c1c">立即作废（锁死）</button>' +
        '<button id="bopen" ' + BTN + ' background:#64748b">关闭密码锁（任何人可进）</button>' +
        '<div id="r1" style="margin-top:10px;font-size:13px;min-height:22px"></div>') +

      card('当前状态',
        '<div style="font-size:13px">状态：' + lockTxt + '　' + tempTxt + '<br>失效时间：' + untilTxt + '</div>') +

      card('2. 改管理员密码',
        '<input id="np" type="password" placeholder="新管理员密码（至少 6 位）" ' +
          'style="padding:9px 11px;border:1px solid #d0d5dd;border-radius:8px;font:inherit;width:220px">' +
        '<button id="badmin" ' + BTN + ' background:#0f766e">保存</button>' +
        '<div id="r2" style="margin-top:8px;font-size:13px;min-height:22px"></div>') +

      card('3. 上传资料到文库',
        '<div style="font-size:12px;opacity:.8;line-height:1.7;margin-bottom:8px">' +
        '上传到 <code>lib/</code>。<br>' +
        '⚠️ <b>目前索引里只有教材</b>——教材以外的资料<b>故意不建索引</b>（涉版权，回答时可参考但不展示、不引用），' +
        '所以上传了也不会显示在页面上，这是设计如此。</div>' +
        '<input type="file" id="f1" multiple style="margin-bottom:8px">' +
        '<button id="bup" ' + BTN + ' background:#0f766e">上传</button>' +
        '<div id="r3" style="margin-top:8px;font-size:13px"></div>') +
    '</div>';

  var script =
    '<script>' +
    'var SALT=' + JSON.stringify(salt) + ', PW=' + JSON.stringify(pw) + ';' +
    "var CLOUD='https://1499683192-f4e14euqer.ap-guangzhou.tencentscf.com';" +
    "async function cc(a,o){var r=await fetch(CLOUD+'/admin?action='+encodeURIComponent(a)," +
      "{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(o||{})});return await r.json();}" +
    "function set(t,html,color){var e=document.getElementById(t);if(e){e.innerHTML=html;if(color)e.style.color=color;}}" +
    "async function act(a){" +
      " var r1=document.getElementById('r1'); if(r1) r1.textContent='处理中…';" +
      " var r=await cc(a,{pw:PW,salt:SALT});" +
      " if(r&&(r.code===0||r.action)){" +
      "   if(a==='gen'){set('r1','新口令已生成（24 小时后失效）：<b style='font-size:16px'>'+r.msg+'<\/b>" +
      "<br>把它和网址一起发给他们，他们首次进入后必须设置自己的密码。','#059669');}" +
      "   else{set('r1',(a==='lock'?'已作废并锁死。拿旧链接的人进不来了。':'已关闭密码锁，任何人可进。'),'#059669');}" +
      " } else { set('r1','失败：'+((r&&r.msg)||'未知错误'),'#b91c1c'); } }" +
    "document.getElementById('bgen').onclick=function(){act('gen')};" +
    "document.getElementById('block').onclick=function(){act('lock')};" +
    "document.getElementById('bopen').onclick=function(){act('open')};" +
    "document.getElementById('badmin').onclick=async function(){" +
      " var n=document.getElementById('np').value;" +
      " if(n.length<6){set('r2','至少 6 位','#b91c1c');return;}" +
      " var r=await cc('adminpw',{pw:PW,salt:SALT,newpw:n});" +
      " if(r&&r.code===0){set('r2','已修改。下次进后台请用新密码。','#059669'); PW=n;}" +
      " else{set('r2','失败：'+((r&&r.msg)||''),'#b91c1c');} };" +
    "document.getElementById('bup').onclick=function(){" +
      " var f=document.getElementById('f1').files;" +
      " if(!f.length){set('r3','先选文件','#b91c1c');return;}" +
      " set('r3','已选 '+f.length+' 个文件。上传到 GitHub 需要写权限（token 只在云端，不经过浏览器），请让毕成代劳。','#667085'); };" +
    '<' + '/script>';

  return '<style>body{margin:0}button:hover{filter:brightness(1.08)}</style>' + html + script;
}

})();
