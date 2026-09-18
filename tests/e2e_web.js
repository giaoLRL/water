#!/usr/bin/env node
/* 前端端到端测试（真实浏览器）：用 Chrome/Edge 无头模式 + CDP 驱动页面，
 * 逐项验证登录、双水槽水位动画、各标签页、图表、全部控制按钮、系统配置、
 * 账号管理、判定服务页、二次确认拦截、响应式布局，并收集控制台错误。
 *
 * 前置：后端已启动（backend/main.py），MySQL 可用。
 * 用法：
 *     node tests/e2e_web.js
 * 可选环境变量：
 *     E2E_BASE=http://127.0.0.1:8000   被测地址
 *     E2E_BROWSER=<可执行文件路径>       指定浏览器
 *     E2E_HEADED=1                     显示浏览器窗口（调试用）
 *
 * 安全说明：本脚本不会启动水泵、不会清零累计水量。
 * 点「清零累计水量」时只验证二次确认弹窗出现，然后选择“取消”，不执行清零。
 */
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const BASE = process.env.E2E_BASE || "http://127.0.0.1:8000";
const ADMIN = { username: "admin", password: "admin123" };
const DEBUG_PORT = Number(process.env.E2E_DEBUG_PORT || 9222);

let passed = 0;
let failed = 0;
let warned = 0;
const consoleErrors = [];
const dialogs = [];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function check(name, cond, detail = "") {
  if (cond) { passed += 1; console.log(`  [PASS] ${name}`); }
  else { failed += 1; console.log(`  [FAIL] ${name} ${detail}`); }
}

function warn(name, detail = "") {
  warned += 1;
  console.log(`  [WARN] ${name} ${detail}`);
}

function findBrowser() {
  const cands = [
    process.env.E2E_BROWSER,
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  ].filter(Boolean);
  return cands.find((c) => fs.existsSync(c)) || null;
}

async function waitForJsonList(timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/list`);
      const list = await res.json();
      const page = list.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
      if (page) return page;
    } catch (e) { /* 浏览器还没起来 */ }
    await sleep(250);
  }
  throw new Error("等待浏览器调试端口超时");
}

class CDP {
  constructor(ws) {
    this.ws = ws;
    this.seq = 0;
    this.pending = new Map();
    this.listeners = new Map();
    ws.addEventListener("message", (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.id !== undefined) {
        const slot = this.pending.get(msg.id);
        if (slot) {
          this.pending.delete(msg.id);
          if (msg.error) slot.reject(new Error(JSON.stringify(msg.error)));
          else slot.resolve(msg.result);
        }
      } else if (msg.method) {
        (this.listeners.get(msg.method) || []).forEach((fn) => fn(msg.params));
      }
    });
  }

  on(method, fn) {
    if (!this.listeners.has(method)) this.listeners.set(method, []);
    this.listeners.get(method).push(fn);
  }

  send(method, params = {}) {
    const id = ++this.seq;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`CDP 超时: ${method}`));
        }
      }, 30000);
    });
  }

  async eval(expression) {
    const res = await this.send("Runtime.evaluate", {
      expression: `(() => { ${expression} })()`,
      returnByValue: true,
      awaitPromise: true,
    });
    if (res.exceptionDetails) {
      const d = res.exceptionDetails;
      throw new Error("页面执行异常: " + (d.exception?.description || d.text));
    }
    return res.result.value;
  }
}

/* ---------- 页面内辅助函数（注入一次，供后续 eval 复用） ---------- */
const HELPERS = `
window.__e2e = {
  q: (s) => document.querySelector(s),
  qa: (s) => Array.from(document.querySelectorAll(s)),
  text: (s) => { const e = document.querySelector(s); return e ? e.textContent.trim() : null; },
  count: (s) => document.querySelectorAll(s).length,
  // Vue v-model 必须走原生 setter + input 事件
  setInput: (s, v) => {
    const el = document.querySelector(s);
    if (!el) return false;
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
    setter.call(el, String(v));
    el.dispatchEvent(new Event("input", { bubbles: true }));
    return true;
  },
  clickText: (sel, txt) => {
    const el = Array.from(document.querySelectorAll(sel))
      .find((e) => e.textContent.trim().includes(txt));
    if (!el) return false;
    el.click();
    return true;
  },
  // 表格：告警/日志两个表格都在 DOM 里(v-show)，必须只取当前可见的那个
  visibleTable: () => Array.from(document.querySelectorAll("table"))
    .find((t) => t.offsetParent !== null) || null,
  headers: (sel) => {
    const tables = Array.from(document.querySelectorAll(sel));
    const t = tables.find((x) => x.offsetParent !== null) || tables[0];
    return t ? Array.from(t.querySelectorAll("thead th")).map((e) => e.textContent.trim()) : [];
  },
  btnByText: (txt) => Array.from(document.querySelectorAll("button"))
    .find((b) => b.textContent.trim().includes(txt)) || null,
  // 按 label 文案定位配置项输入框
  setInputByLabel: (labelText, value) => {
    const item = Array.from(document.querySelectorAll(".rule-item"))
      .find((e) => e.textContent.includes(labelText));
    if (!item) return false;
    const el = item.querySelector("input");
    if (!el) return false;
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
    setter.call(el, String(value));
    el.dispatchEvent(new Event("input", { bubbles: true }));
    return true;
  },
  // 在包含 containerText 的配置块里点按钮。
  // 注意：配置页的「采集周期 / 双水槽容积 / 采集设备」共用一个 .section，
  // 因此必须先缩小到 .alarm-rule 再找按钮，否则会点到相邻块的按钮。
  clickBtnIn: (containerText, btnText) => {
    const rule = Array.from(document.querySelectorAll(".alarm-rule"))
      .find((r) => r.textContent.includes(containerText));
    const scope = rule || Array.from(document.querySelectorAll(".section"))
      .find((s) => s.textContent.includes(containerText));
    if (!scope) return false;
    const b = Array.from(scope.querySelectorAll("button"))
      .find((x) => x.textContent.trim().includes(btnText));
    if (!b) return false;
    b.click();
    return true;
  },
  // 在包含 rowText 的表格行里点按钮
  clickRowBtn: (rowText, btnText) => {
    const tr = Array.from(document.querySelectorAll("tr"))
      .find((t) => t.textContent.includes(rowText));
    if (!tr) return false;
    const b = Array.from(tr.querySelectorAll("button"))
      .find((x) => x.textContent.trim().includes(btnText));
    if (!b) return false;
    b.click();
    return true;
  },
  rowTexts: () => Array.from(document.querySelectorAll("tbody tr")).map((t) => t.textContent.trim()),
  hasCanvas: (s) => { const e = document.querySelector(s); return !!(e && e.querySelector("canvas")); },
  svgIds: () => Array.from(document.querySelectorAll("svg [id]")).map((e) => e.id),
};

// 控制台错误收集（在页面里挂钩子，兜住异步报错）
window.__e2eErrors = [];
const __origErr = console.error;
console.error = function (...a) { window.__e2eErrors.push(a.map(String).join(" ")); __origErr.apply(console, a); };
window.addEventListener("error", (e) => window.__e2eErrors.push("window.onerror: " + (e.message || "")));
window.addEventListener("unhandledrejection", (e) => window.__e2eErrors.push("unhandledrejection: " + (e.reason && e.reason.message || e.reason)));
`;

async function main() {
  const browser = findBrowser();
  if (!browser) throw new Error("未找到 Chrome/Edge，可用 E2E_BROWSER 指定");
  console.log(`浏览器: ${browser}\n被测地址: ${BASE}\n`);

  // 先用 HTTP 直接登录一次，供"界面操作后回读后端"的交叉校验使用
  let apiToken = "";
  async function api(method, p, body) {
    const res = await fetch(BASE + p, {
      method,
      headers: {
        "Content-Type": "application/json",
        ...(apiToken ? { Authorization: "Bearer " + apiToken } : {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return res.json();
  }
  const login = await api("POST", "/api/auth/login", ADMIN);
  apiToken = login?.data?.token || "";
  if (!apiToken) throw new Error("无法通过 API 登录，请确认后端与账号密码：" + JSON.stringify(login));

  // 布局快照与复位：保证卡片数量断言基于默认布局，全部结束后恢复原布局
  const layoutBefore = (await api("GET", "/api/water/dashboard/layout")).data?.layout ?? null;
  await api("POST", "/api/water/dashboard/layout/reset");
  // 前置检查：若另一个客户端（例如你自己打开的页面停在「编辑卡片」模式）在并发写布局，
  // "默认布局"前提会被破坏，这里显式提示，避免后续卡片数断言给出难以定位的失败。
  let layoutAfterReset = null;
  for (let i = 0; i < 4 && !layoutAfterReset; i += 1) {     // 采样 4 秒，捕捉并发写入
    await sleep(1000);
    layoutAfterReset = (await api("GET", "/api/water/dashboard/layout")).data?.layout ?? null;
  }
  if (layoutAfterReset) {
    console.log("  [WARN] 检测到其他客户端正在并发改写仪表盘布局（当前已保存 "
      + ((layoutAfterReset.widgets || []).length) + " 张卡）——通常是浏览器页面处于「编辑卡片」模式。");
    console.log("         请退出编辑模式或关闭该页面后重跑；本次仍继续，卡片数量相关断言可能失败。");
  }

  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "e2e-chrome-"));
  const args = [
    "--headless=new",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
    "--window-size=1440,900",
    `--remote-debugging-port=${DEBUG_PORT}`,
    `--user-data-dir=${userDataDir}`,
    "about:blank",
  ];
  if (process.env.E2E_HEADED === "1") {
    args.splice(0, 1);   // 去掉 headless
  }
  const child = spawn(browser, args, { stdio: "ignore" });

  let cdp;
  try {
    const target = await waitForJsonList();
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener("open", resolve);
      ws.addEventListener("error", () => reject(new Error("WebSocket 连接失败")));
      setTimeout(() => reject(new Error("WebSocket 连接超时")), 10000);
    });
    cdp = new CDP(ws);

    cdp.on("Runtime.consoleAPICalled", (p) => {
      if (p.type === "error") {
        consoleErrors.push((p.args || []).map((a) => a.value ?? a.description ?? "").join(" "));
      }
    });
    cdp.on("Runtime.exceptionThrown", (p) => {
      consoleErrors.push("未捕获异常: " + (p.exceptionDetails?.exception?.description || p.exceptionDetails?.text || ""));
    });
    cdp.on("Log.entryAdded", (p) => {
      if (p.entry && p.entry.level === "error") consoleErrors.push(p.entry.text);
    });
    // 弹窗处理：清零的确认框一律“取消”，避免误清现场累计水量
    cdp.on("Page.javascriptDialogOpening", async (p) => {
      dialogs.push({ type: p.type, message: p.message });
      const isReset = String(p.message || "").includes("清零");
      try {
        await cdp.send("Page.handleJavaScriptDialog", { accept: !isReset });
      } catch (e) { /* ignore */ }
    });

    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Log.enable");

    /* ================= 1. 登录页 ================= */
    console.log("== 1. 登录页 ==");
    await cdp.send("Page.navigate", { url: BASE + "/" });
    await sleep(2500);
    await cdp.eval(HELPERS + "; return true;");
    check("登录页渲染(#app 有内容)", await cdp.eval("return document.querySelector('#app').children.length > 0;"));
    check("登录卡片存在", await cdp.eval("return !!__e2e.q('.login-card');"));
    check("登录输入框 2 个", (await cdp.eval("return __e2e.count('.login-input');")) === 2);
    check("登录按钮存在", await cdp.eval("return !!__e2e.q('.login-btn');"));
    check("登录页 Canvas 背景已绘制", await cdp.eval("const c=__e2e.q('.login-canvas'); return !!(c && c.width>0);"));

    console.log("== 2. 登录校验 ==");
    await cdp.eval("__e2e.setInput('.login-input', 'admin'); return true;");
    await cdp.eval("__e2e.setInput('.login-input:nth-of-type(2)', 'wrongpass'); return true;");
    // 第二个输入框是按类选择器取，改用索引方式更稳
    await cdp.eval("const els=__e2e.qa('.login-input'); const s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set; s.call(els[1],'wrongpass'); els[1].dispatchEvent(new Event('input',{bubbles:true})); return true;");
    await cdp.eval("__e2e.q('.login-btn').click(); return true;");
    await sleep(1200);
    check("错误密码给出提示", await cdp.eval("return (__e2e.text('.config-msg')||'').length > 0;"),
          await cdp.eval("return __e2e.text('.config-msg');"));
    check("错误密码仍停留在登录页", await cdp.eval("return !!__e2e.q('.login-card');"));

    console.log("== 3. 正确登录 ==");
    await cdp.eval("const els=__e2e.qa('.login-input'); const s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set; s.call(els[0],'admin'); els[0].dispatchEvent(new Event('input',{bubbles:true})); s.call(els[1],'admin123'); els[1].dispatchEvent(new Event('input',{bubbles:true})); return true;");
    await cdp.eval("__e2e.q('.login-btn').click(); return true;");
    await sleep(3000);
    check("登录成功进入面板", await cdp.eval("return !!__e2e.q('.view-page');"));
    check("顶部显示当前用户", (await cdp.eval("return __e2e.text('.user-chip');") || "").includes("admin"));
    check("token 已写入 localStorage", await cdp.eval("return !!localStorage.getItem('iot_token');"));

    /* ================= 4. 实时监控页（全卡片网格） ================= */
    console.log("== 4. 实时监控页（全卡片网格 + 单水槽卡） ==");
    check("标题正确", (await cdp.eval("return __e2e.text('.detail-head h2');")) === "水循环综合监控");
    check("卡片网格已渲染", await cdp.eval("return !!__e2e.q('.grid-zone #dash-grid.grid-stack');"));
    // 默认布局卡数随固件能力动态变化：
    //   展示卡：流量+累计(基础2) + 温度×2 + 压力 + 光照 + 单水槽×2(恒2)
    //   操作卡：水泵开关 + 加热开关 + 定量浇水（默认布局不含权限类卡：PID/最近操作/清零/设备）
    const featE2e = (await api("GET", "/api/water/realtime")).data?.features || {};
    const kpiExpected = 2 + 2
        + (featE2e.temperature ? 2 : 0) + (featE2e.pressure ? 1 : 0) + (featE2e.light ? 1 : 0)
        + (featE2e.pump ? 1 : 0) + (featE2e.heater ? 1 : 0) + (featE2e.pump_target ? 1 : 0);
    check(`默认卡片 ${kpiExpected} 张(按固件能力)`, (await cdp.eval("return __e2e.count('.kpi-card');")) === kpiExpected,
          "实际 " + await cdp.eval("return __e2e.count('.kpi-card');"));
    check("定量浇水卡存在(默认布局唯一操作卡)", (await cdp.eval("return __e2e.count('.op-card');")) === 1);

    // —— 单水槽卡（通用卡，已无循环回路系统） ——
    check("单水槽卡 2 张", (await cdp.eval("return __e2e.count('.wg-tank');")) === 2);
    const tankNames = await cdp.eval(
      "return __e2e.qa('.wg-tank').map(e => e.closest('.kpi-card').querySelector('.kpi-label').textContent.trim());");
    check("两槽名称为 储水槽/加热槽",
          tankNames.some((t) => t.startsWith("储水槽")) && tankNames.some((t) => t.startsWith("加热槽")),
          JSON.stringify(tankNames));
    check("水位百分比文本已渲染", await cdp.eval(
      "const p=__e2e.qa('.wg-tank-info .kpi-value').map(e=>e.textContent.trim());"
      + "return p.length === 2 && p.every(t => /\\d/.test(t));"),
      await cdp.eval("return __e2e.qa('.wg-tank-info .kpi-value').map(e=>e.textContent.trim());"));
    // 未接传感器的槽位（储水槽）不带任何模拟值：水位显示 0 并标注「无传感器」
    const tankTags = await cdp.eval(
      "return __e2e.qa('.wg-tank-tag').map(e => e.textContent.trim());");
    check("未接传感器的水槽带「无传感器」角标", tankTags.includes("无传感器"), JSON.stringify(tankTags));
    check("无数据槽位水位显示 0%", await cdp.eval(
      "return __e2e.qa('.wg-tank-info .kpi-value').some(e => e.textContent.trim().startsWith('0'));"));
    const tankGeo = await cdp.eval(`
      const cards = Array.from(document.querySelectorAll('.kpi-card'))
        .filter(c => c.querySelector('.wg-tank')).map(c => c.getBoundingClientRect());
      return { n: cards.length, sideBySide: cards.length === 2 && cards[1].left > cards[0].right,
               h: Math.round(cards[0] ? cards[0].height : 0) };`);
    check("两水槽卡左右并排不重叠", tankGeo.sideBySide, JSON.stringify(tankGeo));
    check("水槽卡已实际布局(高度>100px)", tankGeo.h > 100, JSON.stringify(tankGeo));

    // —— 首屏零滚动 ——
    const sc = await cdp.eval("return { s: document.documentElement.scrollHeight, v: innerHeight };");
    check("实时监控页首屏零滚动", sc.s <= sc.v + 2, JSON.stringify(sc));
    console.log(`  → 页面 ${sc.s}px / 视口 ${sc.v}px`);

    // —— 危险操作降权：清零卡不在默认布局 ——
    check("清零卡不在默认布局", await cdp.eval(
      "const b=__e2e.btnByText('清零累计水量'); return !b || b.offsetParent === null;"));

    check("流量迷你趋势容器存在", await cdp.eval("return !!__e2e.q('#spark-flow');"));
    check("累计水量迷你趋势容器存在", await cdp.eval("return !!__e2e.q('#spark-total');"));
    const ctlExp = (featE2e.pump ? 1 : 0) + (featE2e.heater ? 1 : 0);
    check(`网格内控制卡开关 ${ctlExp} 个(水泵/加热)`, (await cdp.eval("return __e2e.count('.wg-ctl input');")) === ctlExp);

    console.log("== 4.2 可编辑卡片仪表盘（gridstack 网格 + 增删卡片） ==");
    check("网格容器已渲染", await cdp.eval("return !!__e2e.q('#dash-grid.grid-stack');"));
    check("编辑栏位于网格上方", await cdp.eval(
      "const b=__e2e.q('.dash-editbar'); const g=__e2e.q('#dash-grid');"
      + "return !!b && !!g && b.getBoundingClientRect().bottom <= g.getBoundingClientRect().top + 2;"));
    check("定量卡脚注未被裁切", await cdp.eval(
      "const card=[...document.querySelectorAll('.kpi-card')].find(c=>c.querySelector('.op-foot'));"
      + "if(!card) return false; const foot=card.querySelector('.op-foot');"
      + "return foot.getBoundingClientRect().bottom <= card.getBoundingClientRect().bottom + 2;"));
    check("网格项与卡片数一致", await cdp.eval(
      "return __e2e.count('.grid-stack-item') === __e2e.count('.kpi-card');"));
    // 进入编辑模式
    check("进入编辑模式", await cdp.eval("return __e2e.clickText('.dash-editbar .btn-ghost','编辑卡片');"));
    await sleep(400);
    check("编辑态每卡显示拖拽手柄与工具按钮", await cdp.eval(
      `return __e2e.count('.wg-grip') === ${kpiExpected} && __e2e.count('.wg-tools') === ${kpiExpected};`));
    // 添加内置卡片：加热槽水位
    check("打开添加卡片对话框", await cdp.eval("return __e2e.clickText('.dash-editbar .btn-ghost','+ 添加卡片');"));
    await sleep(400);
    check("目录弹出且含内置卡片", await cdp.eval(
      "return !!__e2e.q('.wg-modal') && __e2e.count('.wg-cat-item') > 0;"));
    await cdp.eval(`const it = [...document.querySelectorAll('.wg-cat-item')]
      .find(e => e.textContent.includes('加热槽水位'));
      it.querySelector('button').click(); return true;`);
    await sleep(600);
    check("添加内置卡后卡片 +1", (await cdp.eval("return __e2e.count('.kpi-card');")) === kpiExpected + 1,
          "实际 " + await cdp.eval("return __e2e.count('.kpi-card');"));
    // 删除该卡（确认框由 CDP 自动接受）
    await cdp.eval(`const item = [...document.querySelectorAll('.grid-stack-item')]
      .find(e => e.textContent.includes('加热槽水位'));
      item.querySelector('.wg-btn.danger').click(); return true;`);
    await sleep(600);
    check("删除后卡片数还原", (await cdp.eval("return __e2e.count('.kpi-card');")) === kpiExpected);
    // 自定义接口卡：经后端代理取 /api/water/realtime 的 data.flow_rate
    check("再次打开添加卡片对话框", await cdp.eval("return __e2e.clickText('.dash-editbar .btn-ghost','+ 添加卡片');"));
    await sleep(300);
    check("切到自定义接口页签", await cdp.eval("return __e2e.clickText('.wg-modal .tab','自定义接口');"));
    await sleep(300);
    await cdp.eval(`const set=(el,v)=>{const s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;s.call(el,v);el.dispatchEvent(new Event('input',{bubbles:true}));};
      const rows=[...document.querySelectorAll('.wg-modal .wg-form .wg-row')];
      set(rows[0].querySelector('input'),'E2E自定义卡');
      set(rows[1].querySelector('input'),'http://127.0.0.1:8000/api/water/realtime?token=${apiToken}');
      set(rows[2].querySelector('input'),'data.flow_rate');
      return true;`);
    await cdp.eval("return __e2e.clickText('.wg-modal button','预览取值');");
    await sleep(1500);
    check("自定义接口预览取到值", await cdp.eval(
      "const t=__e2e.text('.wg-preview')||''; return t.length>0 && !t.includes('失败') && !t.includes('未取到');"),
      await cdp.eval("return __e2e.text('.wg-preview');"));
    await cdp.eval("return __e2e.clickText('.wg-modal .btn-primary','添加卡片');");
    await sleep(2500);
    check("自定义卡已上屏", await cdp.eval(
      "return [...document.querySelectorAll('.kpi-card')].some(c=>c.textContent.includes('E2E自定义卡'));"));
    // 数值校验：设备离线时 flow_rate 无读数，卡片按纯真实模式显示 --（降级为 WARN，不算失败）
    const rtCus = (await api("GET", "/api/water/realtime")).data;
    const cusShow = await cdp.eval(
      `const c=[...document.querySelectorAll('.kpi-card')].find(c=>c.textContent.includes('E2E自定义卡'));
       return c && !c.querySelector('.kpi-value').textContent.includes('--');`);
    if (!rtCus.sensor_online && rtCus.flow_rate == null) {
      warn("自定义卡数值校验跳过（设备离线，flow_rate 无读数，卡片按设计显示 --）");
    } else {
      check("自定义卡显示数值", !!cusShow);
    }
    // 清理：删除自定义卡（保持在编辑模式，继续后面的用例）
    await cdp.eval(`const item=[...document.querySelectorAll('.grid-stack-item')]
      .find(e=>e.textContent.includes('E2E自定义卡'));
      item.querySelector('.wg-btn.danger').click(); return true;`);
    await sleep(600);
    check("清理后卡片数还原", (await cdp.eval("return __e2e.count('.kpi-card');")) === kpiExpected);

    // 通用单水槽卡：multi 不判重，可连加两个
    check("再次打开添加卡片对话框(单水槽)", await cdp.eval("return __e2e.clickText('.dash-editbar .btn-ghost','+ 添加卡片');"));
    await sleep(400);
    check("切回内置卡片页签", await cdp.eval("return __e2e.clickText('.wg-modal .tab','内置卡片');"));
    await sleep(300);
    await cdp.eval(`const it = [...document.querySelectorAll('.wg-cat-item')]
      .find(e => e.textContent.includes('单水槽'));
      it.querySelector('button').click(); return true;`);
    await sleep(600);
    check("单水槽卡已上屏(+1)", (await cdp.eval("return __e2e.count('.kpi-card');")) === kpiExpected + 1);
    // 目录按钮仍可点（未变「已添加」），再添加一个
    check("再次打开添加卡片对话框(单水槽×2)", await cdp.eval("return __e2e.clickText('.dash-editbar .btn-ghost','+ 添加卡片');"));
    await sleep(400);
    const tankBtn = await cdp.eval(`const it = [...document.querySelectorAll('.wg-cat-item')]
      .find(e => e.textContent.includes('单水槽'));
      return it && !it.querySelector('button').disabled;`);
    check("单水槽目录不判重(按钮仍可点)", tankBtn === true);
    await cdp.eval(`const it = [...document.querySelectorAll('.wg-cat-item')]
      .find(e => e.textContent.includes('单水槽'));
      it.querySelector('button').click(); return true;`);
    await sleep(600);
    check("单水槽卡可重复添加(+2)", (await cdp.eval("return __e2e.count('.kpi-card');")) === kpiExpected + 2);
    // 清理两张单水槽卡
    for (let i = 0; i < 2; i += 1) {
      await cdp.eval(`const item=[...document.querySelectorAll('.grid-stack-item')]
        .find(e=>e.textContent.includes('单水槽'));
        item.querySelector('.wg-btn.danger').click(); return true;`);
      await sleep(500);
    }
    check("单水槽卡已清理", (await cdp.eval("return __e2e.count('.kpi-card');")) === kpiExpected);

    // 自定义控制卡：状态源走本机 realtime，指令 URL 填只读安全地址（绝不点击开关）
    check("打开添加卡片对话框(控制卡)", await cdp.eval("return __e2e.clickText('.dash-editbar .btn-ghost','+ 添加卡片');"));
    await sleep(400);
    check("切到自定义接口页签(控制卡)", await cdp.eval("return __e2e.clickText('.wg-modal .tab','自定义接口');"));
    await sleep(300);
    await cdp.eval(`const set=(el,v)=>{const s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;s.call(el,v);el.dispatchEvent(new Event('input',{bubbles:true}));};
      const sel=document.querySelector('.wg-modal select');
      sel.value='control'; sel.dispatchEvent(new Event('change',{bubbles:true}));
      return true;`);
    await sleep(500);   // Vue 异步渲染：等「开/关指令」输入框出现
    await cdp.eval(`const set=(el,v)=>{const s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;s.call(el,v);el.dispatchEvent(new Event('input',{bubbles:true}));};
      const byPh=(ph)=>[...document.querySelectorAll('.wg-modal input')].find(i=>i.placeholder && i.placeholder.includes(ph));
      set(byPh('如：环境温度'),'E2E控制卡');
      set(byPh('如 value'),'pump_state');
      set(byPh('开 = GET URL'),'http://127.0.0.1:8000/api/water/realtime?token=${apiToken}');
      set(byPh('关 = GET URL'),'http://127.0.0.1:8000/api/water/realtime?token=${apiToken}');
      return true;`);
    await cdp.eval("return __e2e.clickText('.wg-modal .btn-primary','添加卡片');");
    await sleep(800);
    check("自定义控制卡已上屏", await cdp.eval(
      "const c=[...document.querySelectorAll('.kpi-card')].find(c=>c.textContent.includes('E2E控制卡'));"
      + "return !!c && !!c.querySelector('.wg-ctl input');"));
    // 清理：删除自定义控制卡并退出编辑模式
    await cdp.eval(`const item=[...document.querySelectorAll('.grid-stack-item')]
      .find(e=>e.textContent.includes('E2E控制卡'));
      item.querySelector('.wg-btn.danger').click(); return true;`);
    await sleep(600);
    check("控制卡清理后卡片数还原", (await cdp.eval("return __e2e.count('.kpi-card');")) === kpiExpected);
    check("退出编辑模式", await cdp.eval("return __e2e.clickText('.dash-editbar .btn-ghost','完成');"));
    await sleep(300);
    check("退出后手柄隐藏", (await cdp.eval("return __e2e.count('.wg-grip');")) === 0);

    console.log("== 4.1 控制卡状态与后端一致性（本脚本不实际动作水泵） ==");
    const rtApi = (await api("GET", "/api/water/realtime")).data;
    const ctls = await cdp.eval(
      "return __e2e.qa('.wg-ctl input').map(t => ({ checked: t.checked, disabled: t.disabled }));");
    check("网格内控制卡开关数量正确", ctls.length === ctlExp, JSON.stringify(ctls));
    if (ctls.length >= 1) {
      check("水泵开关卡勾选态与后端 pump_state 一致",
            ctls[0].checked === (rtApi.pump_state === "on"),
            JSON.stringify({ ui: ctls[0].checked, api: rtApi.pump_state }));
      check("设备离线时水泵开关被禁用",
            rtApi.sensor_online ? true : ctls[0].disabled === true,
            JSON.stringify({ online: rtApi.sensor_online, disabled: ctls[0].disabled }));
    }
    if (ctls.length >= 2) {
      check("加热开关卡勾选态与后端 heater_state 一致",
            ctls[1].checked === (rtApi.heater_state === "on"),
            JSON.stringify({ ui: ctls[1].checked, api: rtApi.heater_state }));
    }
    check("后端实时数据含双水槽液位",
          typeof rtApi.tank.tanks.storage.percent === "number" && typeof rtApi.tank.tanks.heater.percent === "number",
          JSON.stringify(rtApi.tank.tanks));
    const uiPct = await cdp.eval(
      "const names=['储水槽','加热槽'];"
      + "return __e2e.qa('.kpi-card').filter(c=>c.querySelector('.wg-tank'))"
      + ".sort((a,b)=>names.findIndex(n=>a.textContent.includes(n))-names.findIndex(n=>b.textContent.includes(n)))"
      + ".map(c=>parseFloat(c.querySelector('.wg-tank-info .kpi-value').textContent));");
    const apiPct = [rtApi.tank.tanks.storage.percent, rtApi.tank.tanks.heater.percent];
    check("界面水位百分比与后端一致(±1%)",
          uiPct.length === 2 && uiPct.every((v, i) => Math.abs(v - apiPct[i]) <= 1),
          JSON.stringify({ ui: uiPct, api: apiPct }));
    check("未设定量目标时进度条不显示",
          rtApi.pump_target > 0 ? true : (await cdp.eval("return __e2e.count('.target-progress') === 0;")),
          "pump_target=" + rtApi.pump_target);

    /* ================= 5. 各标签页 ================= */
    console.log("== 5. 标签页切换 ==");
    check("切到历史数据", await cdp.eval("return __e2e.clickText('.tab','历史数据');"));
    await sleep(1800);
    check("历史曲线已渲染 canvas", await cdp.eval("return __e2e.hasCanvas('#water-hist-chart');"));
    check("历史页有时间范围选项", await cdp.eval("return ['近1小时','近6小时','近24小时','自定义'].every(t=>document.body.textContent.includes(t));"));
    check("切换近24小时", await cdp.eval("return __e2e.clickText('.tab','近24小时');"));
    await sleep(1500);
    check("切换后图表仍在", await cdp.eval("return __e2e.hasCanvas('#water-hist-chart');"));
    check("切换累计水量曲线", await cdp.eval("return __e2e.clickText('.tab','累计水量');"));
    await sleep(800);

    check("切到数据统计", await cdp.eval("return __e2e.clickText('.tab','数据统计');"));
    await sleep(1800);
    check("统计图已渲染 canvas", await cdp.eval("return __e2e.hasCanvas('#water-stats-chart');"));
    check("统计含平均/最高/最低流量卡", await cdp.eval("return ['平均流量','最高流量','最低流量','区间用水量','采样点数'].every(t=>document.body.textContent.includes(t));"));

    check("切到告警记录", await cdp.eval("return __e2e.clickText('.tab','告警记录');"));
    await sleep(1200);
    const alarmHeaders = await cdp.eval("return __e2e.headers('table');");
    check("告警表头正确", JSON.stringify(alarmHeaders) === JSON.stringify(["时间", "类型", "数值", "阈值", "方向", "状态"]), JSON.stringify(alarmHeaders));

    check("切到操作日志", await cdp.eval("return __e2e.clickText('.tab','操作日志');"));
    await sleep(1200);
    const logHeaders = await cdp.eval("return __e2e.headers('table');");
    check("日志表头含操作人列",
          JSON.stringify(logHeaders) === JSON.stringify(["时间", "操作人", "指令", "结果", "说明"]),
          JSON.stringify(logHeaders));
    check("日志页有分页控件", await cdp.eval("return !!__e2e.q('.pager');"));
    check("日志页有分类筛选", await cdp.eval("return !!__e2e.q('.alarm-rule select');"));
    const opCells = await cdp.eval(
      "const t=__e2e.visibleTable(); if(!t) return [];"
      + "return Array.from(t.querySelectorAll('tbody tr')).slice(0,5)"
      + ".map(r => r.children[1] ? r.children[1].textContent.trim() : '');");
    check("操作人列有内容（账号名或系统标记）",
          opCells.length === 0 || opCells.every((c) => c.length > 0), JSON.stringify(opCells));
    check("日志分类筛选可选值完整",
          (await cdp.eval("const s=__e2e.q('.alarm-rule select'); return s ? Array.from(s.options).map(o=>o.textContent.trim()) : [];")).join(",")
          === "全部,设备控制,系统配置,账号管理", "");

    check("切回实时监控", await cdp.eval("return __e2e.clickText('.tab','实时监控');"));
    await sleep(1200);

    /* ================= 6. 累计水量清零的二次确认 ================= */
    console.log("== 6. 破坏性操作拦截 ==");
    // 清零卡不在默认布局（危险操作降权）：编辑模式下从目录临时添加
    check("进入编辑模式(添加清零卡)", await cdp.eval("return __e2e.clickText('.dash-editbar .btn-ghost','编辑卡片');"));
    await sleep(400);
    check("打开添加卡片对话框", await cdp.eval("return __e2e.clickText('.dash-editbar .btn-ghost','+ 添加卡片');"));
    await sleep(400);
    await cdp.eval(`const it = [...document.querySelectorAll('.wg-cat-item')]
      .find(e => e.textContent.includes('清零累计'));
      it.querySelector('button').click(); return true;`);
    await sleep(600);
    const resetBtn = await cdp.eval("const b=__e2e.btnByText('清零累计水量'); return b ? { found:true, disabled:b.disabled } : { found:false };");
    check("清零按钮存在", resetBtn.found, JSON.stringify(resetBtn));
    if (resetBtn.found && resetBtn.disabled) {
      // 设备离线/无权限时按钮按设计禁用，此时无法触发确认框
      warn("清零按钮当前为禁用态（设备离线或无权限），跳过二次确认校验", JSON.stringify(resetBtn));
    } else if (resetBtn.found) {
      const beforeDialogs = dialogs.length;
      await cdp.eval("return __e2e.clickText('button','清零累计水量');");
      await sleep(1500);
      const resetDialog = dialogs.slice(beforeDialogs).find((d) => d.type === "confirm");
      check("点清零会弹出二次确认", !!resetDialog, JSON.stringify(dialogs.slice(beforeDialogs)));
      check("确认框文案含风险提示", !!(resetDialog && resetDialog.message.includes("不可恢复")),
            resetDialog ? resetDialog.message : "");
      check("已选择取消(未执行清零)", !!resetDialog, "确认框已按取消处理");
    }
    // 清理：删除清零卡并退出编辑模式
    await cdp.eval(`const item = [...document.querySelectorAll('.grid-stack-item')]
      .find(e => e.textContent.includes('清零累计'));
      item.querySelector('.wg-btn.danger').click(); return true;`);
    await sleep(600);
    check("清零卡已删除并退出编辑", await cdp.eval("return __e2e.clickText('.dash-editbar .btn-ghost','完成');"));

    /* ================= 7. 系统配置页 ================= */
    console.log("== 7. 系统配置页 ==");
    check("进入系统配置", await cdp.eval("return __e2e.clickText('.chip','系统配置');"));
    await sleep(1500);
    check("配置页标题", (await cdp.eval("return __e2e.text('.detail-head h2');")) === "系统配置");
    check("告警阈值输入框存在", await cdp.eval("return !!__e2e.q('.rule-item input[type=number]');"));
    check("双水槽容积输入框 2 个", await cdp.eval("const a=__e2e.qa('.rule-item'); return a.some(e=>e.textContent.includes('储水槽(L)')) && a.some(e=>e.textContent.includes('加热槽(L)'));"));
    check("采集周期输入框存在", await cdp.eval("return document.body.textContent.includes('采集周期(秒)');"));
    check("设备信息区显示设备地址", await cdp.eval("return document.body.textContent.includes('192.168.31.100');"));

    console.log("== 7.1 告警阈值保存（改后回读并恢复） ==");
    const cfg0 = await api("GET", "/api/water/alarm/config");
    const originMax = cfg0.data.thresholds.flow_max;
    check("阈值输入框可写入", await cdp.eval(`return __e2e.setInputByLabel('水流量上限', ${originMax + 7});`));
    check("点击保存告警阈值", await cdp.eval("return __e2e.clickBtnIn('水流量上限', '保存告警阈值');"));
    await sleep(1500);
    const cfg1 = await api("GET", "/api/water/alarm/config");
    check("后端已收到新阈值", cfg1.data.thresholds.flow_max === originMax + 7,
          `期望 ${originMax + 7} 实际 ${cfg1.data.thresholds.flow_max}`);
    await cdp.eval(`return __e2e.setInputByLabel('水流量上限', ${originMax});`);
    await cdp.eval("return __e2e.clickBtnIn('水流量上限', '保存告警阈值');");
    await sleep(1200);
    check("阈值已恢复原值", (await api("GET", "/api/water/alarm/config")).data.thresholds.flow_max === originMax);

    console.log("== 7.2 双水槽容积保存 ==");
    const sys0 = await api("GET", "/api/water/system");
    const capS = sys0.data.tank_capacities.storage;
    const capH = sys0.data.tank_capacities.heater;
    check("写入储水槽容积", await cdp.eval(`return __e2e.setInputByLabel('储水槽(L)', ${capS + 50});`));
    check("写入加热槽容积", await cdp.eval(`return __e2e.setInputByLabel('加热槽(L)', ${capH + 60});`));
    check("点击保存水槽容积", await cdp.eval("return __e2e.clickBtnIn('加热槽(L)', '保存');"));
    await sleep(1800);
    const sys1 = await api("GET", "/api/water/system");
    check("储水槽容积已生效", sys1.data.tank_capacities.storage === capS + 50,
          JSON.stringify(sys1.data.tank_capacities));
    check("加热槽容积已生效", sys1.data.tank_capacities.heater === capH + 60,
          JSON.stringify(sys1.data.tank_capacities));
    // 恢复
    await cdp.eval(`return __e2e.setInputByLabel('储水槽(L)', ${capS});`);
    await cdp.eval(`return __e2e.setInputByLabel('加热槽(L)', ${capH});`);
    await cdp.eval("return __e2e.clickBtnIn('加热槽(L)', '保存');");
    await sleep(1500);
    const sys2 = await api("GET", "/api/water/system");
    check("水槽容积已恢复", sys2.data.tank_capacities.storage === capS && sys2.data.tank_capacities.heater === capH);

    console.log("== 7.3 采集周期保存 ==");
    const per0 = sys0.data.period;
    check("写入采集周期", await cdp.eval("return __e2e.setInputByLabel('采集周期(秒)', 2.0);"));
    check("点击保存采集周期", await cdp.eval("return __e2e.clickBtnIn('采集周期(秒)', '保存');"));
    await sleep(1500);
    check("采集周期已生效", (await api("GET", "/api/water/system")).data.period === 2.0);
    await cdp.eval(`return __e2e.setInputByLabel('采集周期(秒)', ${per0});`);
    await cdp.eval("return __e2e.clickBtnIn('采集周期(秒)', '保存');");
    await sleep(1500);
    check("采集周期已恢复", (await api("GET", "/api/water/system")).data.period === per0);

    console.log("== 7.3b 报警联动（卡片即来源） ==");
    check("联动编辑区已渲染", await cdp.eval("return document.body.textContent.includes('报警联动');"));
    check("执行器档案区块已移除", !(await cdp.eval("return document.body.textContent.includes('执行器档案');")));
    check("自定义通道声明区块已移除", !(await cdp.eval("return document.body.textContent.includes('自定义传感器通道');")));
    check("添加联动规则", await cdp.eval("return __e2e.clickText('button','+ 添加规则');"));
    await sleep(500);
    const srcOpts = await cdp.eval(`const r=document.querySelector('.link-rule');
      const s=r && r.querySelector('select[title="触发源卡片（实时监控页）"]');
      return s ? [...s.options].map(o=>o.textContent.trim()) : [];`);
    check("触发源下拉取自实时监控页卡片", srcOpts.length > 0 && srcOpts.some(t=>t.includes('流量')), JSON.stringify(srcOpts));
    const actOpts = await cdp.eval(`const r=document.querySelector('.link-rule');
      const s=r && [...r.querySelectorAll('select')].find(x=>[...x.options].some(o=>o.textContent.includes('+ 添加动作')));
      return s ? [...s.options].map(o=>o.textContent.trim()) : [];`);
    check("动作下拉取自实时监控页控制卡", actOpts.some(t=>t.includes('水泵')), JSON.stringify(actOpts));
    // 阈值 99999：本机流量不可能越限，规则绝不会真实触发（安全）
    await cdp.eval(`const set=(el,v)=>{const s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;s.call(el,v);el.dispatchEvent(new Event('input',{bubbles:true}));};
      const rule=document.querySelector('.link-rule');
      const name=rule.querySelector('input[placeholder*="规则名"]');
      set(name,'E2E联动');
      const th=[...rule.querySelectorAll('input[type=number]')].find(i=>i.closest('.rule-item') && i.closest('.rule-item').textContent.includes('阈值'));
      set(th,'99999');
      return true;`);
    check("保存报警联动", await cdp.eval("return __e2e.clickText('button','保存报警联动');"));
    await sleep(1200);
    const linksApi = (await api("GET", "/api/water/alarm/links")).data.links || [];
    check("规则已入后端(卡片触发源 + 卡片动作)",
          linksApi.length === 1 && linksApi[0].source_card === "b-flow"
          && Number(linksApi[0].threshold) === 99999
          && linksApi[0].source.kind === "realtime" && linksApi[0].source.path === "flow_rate"
          && linksApi[0].actions[0].card === "b-ctl_pump" && linksApi[0].actions[0].kind === "pump",
          JSON.stringify(linksApi));
    await api("POST", "/api/water/alarm/links", { links: [] });
    check("联动规则已恢复空", ((await api("GET", "/api/water/alarm/links")).data.links || []).length === 0);

    // 组合条件 + 持续判定：填「持续 5 秒」并加一条附加条件后保存（表单点击验证）
    check("添加联动规则(组合条件)", await cdp.eval("return __e2e.clickText('button','+ 添加规则');"));
    await sleep(500);
    const numBefore = await cdp.eval("return document.querySelector('.link-rule').querySelectorAll('input[type=number]').length;");
    await cdp.eval(`const set=(el,v)=>{const s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;s.call(el,v);el.dispatchEvent(new Event('input',{bubbles:true}));};
      const rule=document.querySelector('.link-rule');
      const hold=[...rule.querySelectorAll('input[type=number]')].find(i=>i.closest('.rule-item') && i.closest('.rule-item').textContent.includes('持续'));
      if(!hold) return false;
      set(hold,'5');
      const th=[...rule.querySelectorAll('input[type=number]')].find(i=>i.closest('.rule-item') && i.closest('.rule-item').textContent.includes('阈值'));
      if(th) set(th,'99999');
      return true;`);
    check("持续秒数输入框可填", await cdp.eval(
      "const h=[...document.querySelector('.link-rule').querySelectorAll('input[type=number]')]"
      + ".find(i=>i.closest('.rule-item')&&i.closest('.rule-item').textContent.includes('持续')); return !!h && h.value==='5';"));
    check("添加附加条件", await cdp.eval(`const r=document.querySelector('.link-rule');
      const sel=[...r.querySelectorAll('select')].find(x=>[...x.options].some(o=>o.textContent.includes('+ 添加条件')));
      if(!sel || sel.options.length < 2) return false;
      sel.value = sel.options[1].value; sel.dispatchEvent(new Event('change',{bubbles:true})); return true;`));
    await sleep(400);
    check("附加条件行已出现",
          (await cdp.eval("return document.querySelector('.link-rule').querySelectorAll('input[type=number]').length;")) === numBefore + 1,
          `before=${numBefore}`);
    check("保存组合条件联动", await cdp.eval("return __e2e.clickText('button','保存报警联动');"));
    await sleep(1200);
    const linksAdv = (await api("GET", "/api/water/alarm/links")).data.links || [];
    const withHold = linksAdv.find((l) => Number(l.hold) === 5 && (l.extra || []).length === 1);
    check("后端收到 hold 与附加条件",
          !!withHold,
          JSON.stringify(linksAdv).slice(0, 220));
    await api("POST", "/api/water/alarm/links", { links: [] });
    check("组合条件规则已清理", ((await api("GET", "/api/water/alarm/links")).data.links || []).length === 0);

    console.log("== 7.3c 自定义传感器通道（卡片即声明） ==");
    // 只加一张自定义接口卡（URL 指向本机只读接口），卡片即声明，后端自动轮询入库
    const declLayout = { version: 1, widgets: [
      { id: "e2e-decl", type: "value", title: "E2E声明卡", unit: "s", decimals: 2,
        source: { kind: "custom", url: `http://127.0.0.1:8000/api/water/realtime?token=${apiToken}`,
                  path: "data.period", period: 2 },
        grid: { x: 0, y: 0, w: 3, h: 2 } }] };
    const saveDecl = await api("POST", "/api/water/dashboard/layout", { layout: declLayout });
    check("测试布局保存成功", !!saveDecl && saveDecl.code === 0, JSON.stringify(saveDecl).slice(0, 160));
    let savedDecl = (await api("GET", "/api/water/dashboard/layout")).data.layout;
    if (!savedDecl || (savedDecl.widgets || []).length !== 1) {      // 偶发未生效时重试一次
      await api("POST", "/api/water/dashboard/layout", { layout: declLayout });
      await sleep(400);
      savedDecl = (await api("GET", "/api/water/dashboard/layout")).data.layout;
    }
    check("测试布局已生效（1 张声明卡）", !!savedDecl && (savedDecl.widgets || []).length === 1,
          JSON.stringify(((savedDecl || {}).widgets || []).map((w) => w.id)));
    await sleep(3200);
    const rtDecl = (await api("GET", "/api/water/realtime")).data;
    check("自定义卡自动成为后端通道（卡片即声明）",
          (rtDecl.custom_channels || []).some((c) => c.id === "e2e-decl"), JSON.stringify(rtDecl.custom_channels));
    check("该通道已轮询到读数", rtDecl.custom && rtDecl.custom["e2e-decl"] !== undefined, JSON.stringify(rtDecl.custom));
    // 配置页重新加载后，触发源下拉里应能看到这张卡片
    await cdp.send("Page.navigate", { url: BASE + "/" });
    await sleep(2600);
    await cdp.eval(HELPERS + "; return true;");   // 导航后重新注入页面内辅助函数
    check("重新进入系统配置", await cdp.eval("return __e2e.clickText('.chip','系统配置');"));
    await sleep(1500);
    check("添加联动规则（声明卡）", await cdp.eval("return __e2e.clickText('button','+ 添加规则');"));
    await sleep(500);
    const srcOpts2 = await cdp.eval(`const r=document.querySelector('.link-rule');
      const s=r && r.querySelector('select[title="触发源卡片（实时监控页）"]');
      return s ? [...s.options].map(o=>o.textContent.trim()) : [];`);
    check("触发源下拉含自定义接口卡", srcOpts2.indexOf("E2E声明卡") !== -1, JSON.stringify(srcOpts2));
    // 清理测试布局，并让页面回到默认布局（后续响应式检查依赖默认卡片）
    await api("POST", "/api/water/dashboard/layout/reset");
    await cdp.send("Page.navigate", { url: BASE + "/" });
    await sleep(2600);
    await cdp.eval(HELPERS + "; return true;");
    check("清理后重新进入系统配置", await cdp.eval("return __e2e.clickText('.chip','系统配置');"));
    await sleep(1500);
    check("测试布局已清理", (await api("GET", "/api/water/dashboard/layout")).data.layout === null);

    console.log("== 7.3d 外部设备接入 / 定时任务 / 通道标定 ==");
    const gwBefore = (await api("GET", "/api/water/gateway")).data.gateways;
    const tmBefore = (await api("GET", "/api/water/timers")).data.timers;
    const calBefore = (await api("GET", "/api/water/calibration")).data.calibration;
    check("外部设备接入区块已渲染", await cdp.eval("return document.body.textContent.includes('外部设备接入');"));
    check("定时任务区块已渲染", await cdp.eval("return document.body.textContent.includes('定时任务');"));
    check("通道标定区块已渲染", await cdp.eval("return document.body.textContent.includes('通道标定');"));
    check("添加外部设备", await cdp.eval("return __e2e.clickText('button','+ 添加外部设备');"));
    await sleep(400);
    check("外部设备行含接入方式选择", await cdp.eval("return document.body.textContent.includes('串口服务器(RTU over TCP)');"));
    check("保存外部设备", await cdp.eval("return __e2e.clickText('button','保存外部设备');"));
    await sleep(1200);
    check("外部设备已入后端", ((await api("GET", "/api/water/gateway")).data.gateways || []).length === 1,
          JSON.stringify((await api("GET", "/api/water/gateway")).data.gateways));
    check("添加定时任务", await cdp.eval("return __e2e.clickText('button','+ 添加定时任务');"));
    await sleep(400);
    check("保存定时任务", await cdp.eval("return __e2e.clickText('button','保存定时任务');"));
    await sleep(1200);
    check("定时任务已入后端", ((await api("GET", "/api/water/timers")).data.timers || []).length === 1);
    // 两点标定：原始 100→10、200→25 ⇒ scale=0.15 offset=-5
    await cdp.eval(`const set=(el,v)=>{const s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;s.call(el,v);el.dispatchEvent(new Event('input',{bubbles:true}));};
      const sel=[...document.querySelectorAll('select')].find(s=>[...s.options].some(o=>o.textContent.includes('储水槽水温')));
      if(sel){ sel.value='storage_temp'; sel.dispatchEvent(new Event('change',{bubbles:true})); }
      const ins=[...document.querySelectorAll('input[placeholder^="原始值"], input[placeholder^="工程值"]')];
      if(ins.length<4) return false;
      set(ins[0],'100'); set(ins[1],'10'); set(ins[2],'200'); set(ins[3],'25'); return true;`);
    check("点击计算系数", await cdp.eval("return __e2e.clickText('button','计算系数');"));
    await sleep(800);
    check("保存标定", await cdp.eval("return __e2e.clickText('button','保存标定');"));
    await sleep(1200);
    const calAfter = (await api("GET", "/api/water/calibration")).data.calibration;
    check("标定系数已生效(0.15 / -5)",
          Math.abs(Number(((calAfter.storage_temp || {}).scale ?? 1)) - 0.15) < 1e-6, JSON.stringify(calAfter));
    await api("POST", "/api/water/gateway", { gateways: gwBefore });
    await api("POST", "/api/water/timers", { timers: tmBefore });
    await api("POST", "/api/water/calibration", { calibration: calBefore });
    check("新增配置已还原",
          ((await api("GET", "/api/water/timers")).data.timers || []).length === tmBefore.length
          && ((await api("GET", "/api/water/gateway")).data.gateways || []).length === gwBefore.length);

    console.log("== 7.3e 判定服务报文模板 ==");
    const jtBefore = (await api("GET", "/api/water/judge/template")).data;
    check("进入判定服务", await cdp.eval("return __e2e.clickText('.chip','判定服务');"));
    await sleep(1500);
    check("报文模板区已渲染", await cdp.eval("return document.body.textContent.includes('报文模板');"));
    check("填入示例模板", await cdp.eval("return __e2e.clickText('button','填入示例');"));
    await sleep(300);
    check("模板含占位符", await cdp.eval("const t=document.querySelector('textarea'); return !!t && t.value.includes('{{device_id}}');"));
    await cdp.eval(`const i=[...document.querySelectorAll('input')].find(x=>(x.placeholder||'').includes('192.168'));
      if(i){const s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;s.call(i,'http://127.0.0.1:9100');i.dispatchEvent(new Event('input',{bubbles:true}));} return true;`);
    check("保存地址与模板", await cdp.eval("return __e2e.clickText('button','保存地址与模板');"));
    await sleep(1200);
    const jtAfter = (await api("GET", "/api/water/judge/template")).data;
    check("判定地址与模板已入后端",
          jtAfter.url === "http://127.0.0.1:9100" && !!(jtAfter.template || {}).report,
          JSON.stringify(jtAfter).slice(0, 160));
    await api("POST", "/api/water/judge/template", { url: jtBefore.url || "", template: jtBefore.template || {} });
    check("判定模板已还原", !((await api("GET", "/api/water/judge/template")).data.template || {}).report);
    check("返回面板", await cdp.eval("return __e2e.clickText('button','返回');"));
    await sleep(1200);

    console.log("== 7.3f 历史回放 ==");
    // 本节需要一个"已保存且含自定义通道"的布局，历史页才会出现自定义标签；结束时复位
    await api("POST", "/api/water/dashboard/layout", { layout: { version: 1, widgets: [
      { id: "e2e-tab-chan", type: "value", title: "E2E通道卡", unit: "s", decimals: 1,
        source: { kind: "custom", url: `http://127.0.0.1:8000/api/water/realtime?token=${apiToken}`,
                  path: "data.period", period: 2 },
        grid: { x: 0, y: 0, w: 3, h: 2 } }] } });
    await cdp.send("Page.navigate", { url: BASE + "/" });
    await sleep(2600);
    await cdp.eval(HELPERS + "; return true;");
    check("切到历史数据", await cdp.eval("return __e2e.clickText('.tab','历史数据');"));
    await sleep(1500);
    // 通道类型标签互斥：内置标签按 activeHistKey 高亮，点自定义通道后内置标签必须取消选中
    const activeTypeTabs = "return [...document.querySelectorAll('.hist-type-tabs .tab.active')].map(t=>t.textContent.trim());";
    check("初始仅一个通道标签选中", (await cdp.eval(activeTypeTabs)).length === 1,
          JSON.stringify(await cdp.eval(activeTypeTabs)));
    const customTab = await cdp.eval(
      "const t=[...document.querySelectorAll('.hist-type-tabs .tab')].find(x=>x.textContent.trim()==='E2E通道卡');"
      + "if(!t) return ''; t.click(); return t.textContent.trim();");
    await sleep(2000);
    const afterCustom = await cdp.eval(activeTypeTabs);
    check("点自定义通道标签后只有它选中（不再两组各亮一个）",
          !!customTab && afterCustom.length === 1 && afterCustom[0] === customTab,
          JSON.stringify({ customTab, afterCustom }));
    check("切回内置标签", await cdp.eval("return __e2e.clickText('.hist-type-tabs .tab','加热槽温度');"));
    await sleep(1800);
    const afterBuiltin = await cdp.eval(activeTypeTabs);
    check("切回内置标签后自定义标签已取消选中",
          afterBuiltin.length === 1 && afterBuiltin[0] === "加热槽温度", JSON.stringify(afterBuiltin));
    check("切回全部", await cdp.eval("return __e2e.clickText('.hist-type-tabs .tab','全部');"));
    await sleep(1500);
    check("全部标签选中唯一", (await cdp.eval(activeTypeTabs)).length === 1,
          JSON.stringify(await cdp.eval(activeTypeTabs)));
    check("历史含水泵/加热状态与定量目标标签", await cdp.eval(
      "const t=document.body.textContent; return t.includes('水泵状态') && t.includes('加热状态') && t.includes('定量目标');"));
    check("水泵状态曲线可切换", await cdp.eval("return __e2e.clickText('.tab','水泵状态');"));
    await sleep(1800);
    check("状态曲线已绘制", await cdp.eval(
      "const el=document.getElementById('water-hist-chart'); return !!el && !!el.querySelector('canvas');"));
    check("切回全部曲线", await cdp.eval("return __e2e.clickText('.tab','全部');"));
    await sleep(1200);
    check("切到近24小时", await cdp.eval("return __e2e.clickText('.tab','近24小时');"));
    await sleep(2500);
    check("长区间显示降采样提示", await cdp.eval(
      "const t=document.body.textContent; return t.includes('已降采样') && /区间共 \\d+ 个采样点/.test(t);"));
    check("回放工具条已渲染", await cdp.eval("return document.body.textContent.includes('历史回放');"));
    check("导出 CSV 按钮在位", await cdp.eval("return __e2e.clickText('button','导出 CSV') === true;"));
    await sleep(600);
    check("加载回放", await cdp.eval("return __e2e.clickText('button','加载回放');"));
    await sleep(2500);
    check("回放滑块与帧信息出现", await cdp.eval(
      "return document.querySelectorAll('input[type=range]').length > 0 && /\\d+ \\/ \\d+ 帧/.test(document.body.textContent);"));
    // 曲线回放游标（markLine）+ 视口跟随
    check("曲线出现回放游标", await cdp.eval(
      "const c=window.Charts.get('water-hist-chart'); const ml=c && c.getOption().series[0].markLine;"
      + "return !!ml && Array.isArray(ml.data) && ml.data.length===1;"));
    const cursorIdx0 = await cdp.eval(
      "const c=window.Charts.get('water-hist-chart'); return Number(c.getOption().series[0].markLine.data[0].xAxis);");
    check("加载回放后视口收敛到游标附近", await cdp.eval(
      "const c=window.Charts.get('water-hist-chart'); const z=(c.getOption().dataZoom||[])[0]||{};"
      + "const s=Number(z.startValue), e=Number(z.endValue), n=c.getOption().xAxis[0].data.length;"
      + "return isFinite(s) && isFinite(e) && e > s && (e - s) < n;"));
    // 生长式回放：主序列只画到游标，幽灵序列保留全长轮廓；图例不出现幽灵
    check("曲线为生长回放（主序列截至游标 + 幽灵线全长）", await cdp.eval(
      "const c=window.Charts.get('water-hist-chart'); const o=c.getOption();"
      + "const n=o.xAxis[0].data.length; return o.series.length >= 4"
      + " && o.series[0].data.length < n && o.series[1].data.length === n && /未回放/.test(o.series[1].name);"));
    check("图例不含幽灵序列", await cdp.eval(
      "const l=(window.Charts.get('water-hist-chart').getOption().legend[0]||{}).data||[];"
      + "return l.length === 2 && !l.some((x) => /未回放/.test(x));"));
    const growBefore = await cdp.eval("return window.Charts.get('water-hist-chart').getOption().series[0].data.length;");
    await cdp.eval("const r=document.querySelector('input[type=range]'); r.value=Math.floor(Number(r.max)/2);"
      + "r.dispatchEvent(new Event('input',{bubbles:true})); return true;");
    await sleep(2200);
    const growAfter = await cdp.eval("return window.Charts.get('water-hist-chart').getOption().series[0].data.length;");
    check("拖动回放时曲线随之生长", growAfter > growBefore, JSON.stringify({ growBefore, growAfter }));
    // 拖动滑块到最后一帧：游标前进，且视口跟随滚动
    await cdp.eval("const r=document.querySelector('input[type=range]'); r.value=r.max;"
      + "r.dispatchEvent(new Event('input',{bubbles:true})); return true;");
    await sleep(2500);
    const cursorIdx1 = await cdp.eval(
      "const c=window.Charts.get('water-hist-chart'); return Number(c.getOption().series[0].markLine.data[0].xAxis);");
    check("拖动回放滑块时曲线游标前进", cursorIdx1 > cursorIdx0,
          JSON.stringify({ cursorIdx0, cursorIdx1 }));
    check("游标移出窗口时视口跟随滚动", await cdp.eval(
      "const c=window.Charts.get('water-hist-chart'); const z=(c.getOption().dataZoom||[])[0]||{};"
      + "return Number(z.startValue) > 0;"));
    // 回到中间帧再播放（避免刚从末帧开始播、立即结束导致「暂停」按钮不存在）
    await cdp.eval("const r=document.querySelector('input[type=range]');"
      + "r.value=Math.floor(Number(r.max)/4); r.dispatchEvent(new Event('input',{bubbles:true})); return true;");
    await sleep(1600);
    check("播放回放", await cdp.eval("return __e2e.clickText('button','播放');"));
    await sleep(1800);
    check("回放帧已推进", await cdp.eval("return /[2-9]\\d* \\/ \\d+ 帧/.test(document.body.textContent);"));
    check("暂停回放", await cdp.eval("return __e2e.clickText('button','暂停');"));
    await sleep(400);
    check("切换数据统计", await cdp.eval("return __e2e.clickText('.tab','数据统计');"));
    await sleep(1800);
    check("统计显示有效读数点与加权平均", await cdp.eval(
      "const t=document.body.textContent; return t.includes('有效流量读数点') && t.includes('加权平均流量')"
      + " && t.includes('采样点数（含离线空行）');"));
    check("切回历史数据", await cdp.eval("return __e2e.clickText('.tab','历史数据');"));
    await sleep(1200);
    // 本节自建的布局复位，并刷新页面回到默认布局（后续 7.3g 与响应式检查依赖默认卡片）
    await api("POST", "/api/water/dashboard/layout/reset");
    await cdp.send("Page.navigate", { url: BASE + "/" });
    await sleep(2600);
    await cdp.eval(HELPERS + "; return true;");
    check("本节测试布局已复位并回到监控页", await cdp.eval("return !!__e2e.q('.kpi-card');"));

    console.log("== 7.3g 系统拓扑卡 / 一键场景卡 ==");
    check("返回实时监控", await cdp.eval(
      "return __e2e.clickText('.tab','实时监控') || __e2e.clickText('.chip','实时监控');"));
    await sleep(1500);
    const kpiBeforeNew = await cdp.eval("return __e2e.count('.kpi-card');");
    check("进入编辑模式(拓扑/场景卡)", await cdp.eval("return __e2e.clickText('.dash-editbar .btn-ghost','编辑卡片');"));
    await sleep(500);
    for (const label of ["系统拓扑", "一键场景"]) {
      await cdp.eval("return __e2e.clickText('.dash-editbar .btn-ghost','+ 添加卡片');");
      await sleep(400);
      await cdp.eval("return __e2e.clickText('.wg-modal .tab','内置卡片');");
      await sleep(300);
      const added = await cdp.eval(`const it=[...document.querySelectorAll('.wg-cat-item')].find(e=>e.textContent.includes('${label}'));
        if(!it) return false; const b=it.querySelector('button'); if(b.disabled) return false; b.click(); return true;`);
      check(`添加${label}卡`, added);
      await sleep(700);
    }
    check("拓扑卡 SVG 已渲染", await cdp.eval("return !!document.querySelector('svg.topo');"));
    check("拓扑卡管路与节点齐全", await cdp.eval(
      "return document.querySelectorAll('.topo-pipe').length === 2 && document.querySelectorAll('.topo-node').length === 3;"));
    check("场景卡两个按钮就位", await cdp.eval(
      "const c=[...document.querySelectorAll('.kpi-card')].find(x=>x.textContent.includes('一键场景'));"
      + "return !!c && c.textContent.includes('启动循环') && c.textContent.includes('急停全部');"));
    // 只点「急停全部」（关闭类指令），绝不点「启动循环」（避免开泵）
    await cdp.eval("const c=[...document.querySelectorAll('.kpi-card')].find(x=>x.textContent.includes('一键场景'));"
      + "const b=[...c.querySelectorAll('button')].find(x=>x.textContent.includes('急停')); b.click(); return true;");
    await sleep(3000);
    const devOnlineNow = (await api("GET", "/api/water/system")).data.device_online;
    const stopLogged = await (async () => {
      const items = (await api("GET", "/api/water/logs?page=1&page_size=10")).data.items || [];
      return items.some((l) => /pump_off|heater_off|device.custom/.test(l.action));
    })();
    if (devOnlineNow) {
      check("急停已写操作日志", stopLogged);
    } else {
      // 设备离线且默认布局无自定义执行器时不会产生控制日志（属设计内降级）
      warn("急停未产生控制日志（设备离线且无自定义执行器）", "");
    }
    check("场景卡显示执行结果", await cdp.eval(
      "const c=[...document.querySelectorAll('.kpi-card')].find(x=>x.textContent.includes('一键场景')); return /已急停/.test(c.textContent);"));
    for (const label of ["系统拓扑", "一键场景"]) {
      await cdp.eval(`const item=[...document.querySelectorAll('.grid-stack-item')].find(e=>e.textContent.includes('${label}'));
        if(item) item.querySelector('.wg-btn.danger').click(); return true;`);
      await sleep(700);
    }
    check("新增卡片已删除", (await cdp.eval("return __e2e.count('.kpi-card');")) === kpiBeforeNew);
    check("退出编辑模式(拓扑/场景卡)", await cdp.eval("return __e2e.clickText('.dash-editbar .btn-ghost','完成');"));
    await sleep(400);
    check("回到系统配置继续后续用例", await cdp.eval("return __e2e.clickText('.chip','系统配置');"));
    await sleep(1500);

    console.log("== 7.3h 恒温闭环多路回路（卡片即来源） ==");
    // 用"无害执行器"验证闭环：执行器卡的指令指向本机只读接口，不触碰真实加热继电器
    const roUrl = `http://127.0.0.1:8000/api/water/realtime?token=${apiToken}`;
    await api("POST", "/api/water/dashboard/layout", { layout: { version: 1, widgets: [
      { id: "e2e-pid-sensor", type: "value", title: "E2E温度源", unit: "℃", decimals: 1,
        source: { kind: "realtime", path: "heater_temp" }, grid: { x: 0, y: 0, w: 3, h: 2 } },
      { id: "e2e-pid-guard", type: "tank", title: "E2E液位", unit: "%", decimals: 1,
        source: { kind: "realtime", path: "tank.tanks.heater" }, grid: { x: 3, y: 0, w: 3, h: 2 } },
      { id: "e2e-pid-act", type: "control", title: "E2E加热执行器",
        cmd: { on: roUrl, off: roUrl }, source: { kind: "realtime", path: "pump_state" },
        grid: { x: 6, y: 0, w: 3, h: 2 } },
      { id: "e2e-pid-card", type: "builtin", builtin: "pid", title: "E2E闭环", catalogKey: "pid",
        pid: { sensor_card: "e2e-pid-sensor", actuator_card: "e2e-pid-act",
               guard_card: "e2e-pid-guard", guard_min: 0, target: 60,
               kp: 16, ki: 0.3, kd: 25, enabled: false },
        grid: { x: 0, y: 2, w: 4, h: 4 } }] } });
    await cdp.send("Page.navigate", { url: BASE + "/" });
    await sleep(2600);
    await cdp.eval(HELPERS + "; return true;");
    await sleep(2500);
    check("闭环卡显示温度来源与执行器名称", await cdp.eval(
      "const c=[...document.querySelectorAll('.kpi-card')].find(x=>x.textContent.includes('E2E闭环'));"
      + "return !!c && c.textContent.includes('E2E温度源') && c.textContent.includes('E2E加热执行器');"));
    check("闭环初始为关闭状态", await cdp.eval(
      "const c=[...document.querySelectorAll('.kpi-card')].find(x=>x.textContent.includes('E2E闭环'));"
      + "const t=c.querySelector('.toggle'); return !!t && !t.classList.contains('on');"));
    // 配置对话框：三个下拉应能选到卡片（来源/执行器/防干烧条件）
    check("进入编辑模式(闭环卡)", await cdp.eval("return __e2e.clickText('.dash-editbar .btn-ghost','编辑卡片');"));
    await sleep(500);
    await cdp.eval("const item=[...document.querySelectorAll('.grid-stack-item')].find(e=>e.textContent.includes('E2E闭环'));"
      + "const b=[...item.querySelectorAll('.wg-btn')].find(x=>(x.getAttribute('title')||'').includes('配置')); b.click(); return true;");
    await sleep(700);
    check("闭环配置对话框含来源/执行器/联锁三项", await cdp.eval(
      "const t=document.body.textContent; return t.includes('温度来源卡') && t.includes('加热执行器卡') && t.includes('防干烧条件卡');"));
    check("来源下拉列出候选卡片", await cdp.eval(
      "const sels=[...document.querySelectorAll('.wg-modal select')];"
      + "const s=sels.find(x=>[...x.options].some(o=>o.textContent.includes('E2E温度源'))); return !!s;"));
    // 改目标温度 → 保存 → 后端卡片配置应更新
    await cdp.eval("const row=[...document.querySelectorAll('.wg-modal .wg-row')].find(r=>r.textContent.includes('目标温度'));"
      + "const inp=row && row.querySelector('input[type=number]'); if(!inp) return false;"
      + "const s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
      + "s.call(inp,'55'); inp.dispatchEvent(new Event('input',{bubbles:true})); return true;");
    check("保存闭环卡配置", await cdp.eval("return __e2e.clickText('.wg-modal .btn-primary','保存');"));
    await sleep(1500);
    const pidLayoutAfter = (await api("GET", "/api/water/dashboard/layout")).data.layout;
    const pidCardAfter = (pidLayoutAfter.widgets || []).find((w) => w.id === "e2e-pid-card") || {};
    check("闭环卡配置已写回布局（目标=55）", Number((pidCardAfter.pid || {}).target) === 55,
          JSON.stringify(pidCardAfter.pid));
    check("退出编辑模式(闭环卡)", await cdp.eval("return __e2e.clickText('.dash-editbar .btn-ghost','完成');"));
    await sleep(800);
    // 开启闭环：执行器是自定义指令（无害 GET），应写出 pid_actuator_on 日志
    const logBeforePid = ((await api("GET", "/api/water/logs?page=1&page_size=5")).data.items || [])
      .map((l) => l.ts + "|" + l.action);
    await cdp.eval("const c=[...document.querySelectorAll('.kpi-card')].find(x=>x.textContent.includes('E2E闭环'));"
      + "c.querySelector('.wg-ctl input, .toggle input').click(); return true;");
    await sleep(3500);
    const pidLogs = ((await api("GET", "/api/water/logs?page=1&page_size=20")).data.items || [])
      .filter((l) => !logBeforePid.includes(l.ts + "|" + l.action));
    check("开启闭环后按门限下发自定义执行器", pidLogs.some((l) => l.action === "pid_actuator_on"),
          JSON.stringify(pidLogs.map((l) => l.action)));
    const loopsNow = (await api("GET", "/api/water/pid/loops")).data.loops || [];
    const thisLoop = loopsNow.find((l) => l.id === "e2e-pid-card") || {};
    check("闭环回路状态可供卡片显示（含来源/执行器/实测）",
          thisLoop.sensor_title === "E2E温度源" && thisLoop.actuator_title === "E2E加热执行器"
          && thisLoop.enabled === true, JSON.stringify(thisLoop));
    // 关掉闭环，避免测试结束后继续下发指令
    await cdp.eval("const c=[...document.querySelectorAll('.kpi-card')].find(x=>x.textContent.includes('E2E闭环'));"
      + "c.querySelector('.wg-ctl input, .toggle input').click(); return true;");
    await sleep(2000);
    check("闭环已关闭（测试收尾）",
          ((await api("GET", "/api/water/pid/loops")).data.loops || [])
            .some((l) => l.id === "e2e-pid-card" && l.enabled === false));
    // 本节结束：复位布局并回到配置页（后续用例依赖默认布局）
    await api("POST", "/api/water/dashboard/layout/reset");
    await cdp.send("Page.navigate", { url: BASE + "/" });
    await sleep(2600);
    await cdp.eval(HELPERS + "; return true;");
    check("闭环测试布局已复位并回到配置页",
          await cdp.eval("return __e2e.clickText('.chip','系统配置');"));
    await sleep(1200);

    console.log("== 7.3i 布局一键恢复上一次（并发保护配套） ==");
    // 布局全局共享一份；后端每次保存/重置前会把上一版存为「上一次布局」，
    // 这里验证：保存 A 再保存 B 后，界面上点一次「恢复上一次」能回到 A（再点回到 B）。
    const layoutA = { version: 1, widgets: [
      { id: "e2e-mark-a1", type: "value", title: "E2E标记A1", unit: "", decimals: 1,
        source: { kind: "realtime", path: "flow_rate" }, grid: { x: 0, y: 0, w: 2, h: 2 } },
      { id: "e2e-mark-a2", type: "value", title: "E2E标记A2", unit: "", decimals: 1,
        source: { kind: "realtime", path: "total_liters" }, grid: { x: 2, y: 0, w: 2, h: 2 } },
    ] };
    const layoutB = { version: 1, widgets: [
      { id: "e2e-mark-b1", type: "value", title: "E2E标记B1", unit: "", decimals: 1,
        source: { kind: "realtime", path: "flow_rate" }, grid: { x: 0, y: 0, w: 2, h: 2 } },
    ] };
    await api("POST", "/api/water/dashboard/layout", { layout: layoutA });
    await api("POST", "/api/water/dashboard/layout", { layout: layoutB });
    const revNow = (await api("GET", "/api/water/dashboard/layout")).data;
    check("保存两版布局后后端记录了可恢复的上一版",
          (revNow.layout.widgets || []).length === 1 && revNow.has_prev === true,
          JSON.stringify({ n: revNow.layout.widgets.length, has_prev: revNow.has_prev }));
    // 旧版本号保存必须被拒（防止停在旧布局的窗口把新布局整片覆盖）
    const stale = await api("POST", "/api/water/dashboard/layout",
      { layout: layoutA, base_rev: revNow.rev - 5 });
    check("版本号过期时后端拒绝保存（code=40009）", stale.code === 40009, JSON.stringify(stale).slice(0, 160));
    // 页面刷新后拿到最新版本号，进编辑模式点按钮
    await cdp.send("Page.navigate", { url: BASE + "/" });
    await sleep(2600);
    await cdp.eval(HELPERS + "; return true;");
    check("标记布局 B 已在页面生效（1 张卡）", (await cdp.eval("return __e2e.count('.kpi-card');")) === 1);
    check("进入编辑模式", await cdp.eval("return __e2e.clickText('button','编辑卡片');"));
    await sleep(600);
    check("编辑模式出现「恢复上一次」按钮",
          await cdp.eval("return __e2e.qa('button').some(b=>b.textContent.trim().includes('恢复上一次'));"));
    check("点击「恢复上一次」", await cdp.eval("return __e2e.clickText('button','恢复上一次');"));
    await sleep(2000);
    const backA = (await api("GET", "/api/water/dashboard/layout")).data.layout;
    check("一键恢复到上一版布局 A（2 张标记卡）",
          (backA.widgets || []).map((w) => w.id).join(",") === "e2e-mark-a1,e2e-mark-a2",
          JSON.stringify((backA.widgets || []).map((w) => w.id)));
    check("再点一次可切回布局 B（恢复前会备份当前版）",
          await cdp.eval("return __e2e.clickText('button','恢复上一次');"));
    await sleep(2000);
    const backB = (await api("GET", "/api/water/dashboard/layout")).data.layout;
    check("已切回布局 B（1 张标记卡）",
          (backB.widgets || []).map((w) => w.id).join(",") === "e2e-mark-b1",
          JSON.stringify((backB.widgets || []).map((w) => w.id)));
    const logActs = (await api("GET", "/api/water/logs?category=config&page_size=10")).data.items
      .map((i) => i.detail || "");
    check("恢复操作写入操作日志", logActs.some((d) => d.includes("恢复上一次")), JSON.stringify(logActs.slice(0, 4)));
    // 本节收尾：复位为默认布局并回到配置页（后续用例依赖默认布局）
    await api("POST", "/api/water/dashboard/layout/reset");
    await cdp.send("Page.navigate", { url: BASE + "/" });
    await sleep(2600);
    await cdp.eval(HELPERS + "; return true;");
    check("布局标记已清理并回到配置页",
          await cdp.eval("return __e2e.clickText('.chip','系统配置');"));
    await sleep(1200);

    console.log("== 7.4 账号管理 ==");
    check("切到账号管理标签", await cdp.eval("return __e2e.clickText('.tab','账号管理');"));
    await sleep(1500);
    check("账号表格已渲染", (await cdp.eval("return __e2e.headers('table');")).includes("用户名"));
    check("权限矩阵已渲染", await cdp.eval("return !!__e2e.q('.perm-matrix');"));
    const uname = "e2e" + Date.now().toString().slice(-6);
    await cdp.eval(`const els=__e2e.qa('.login-input'); const s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set; s.call(els[0],'${uname}'); els[0].dispatchEvent(new Event('input',{bubbles:true})); s.call(els[1],'e2epass123'); els[1].dispatchEvent(new Event('input',{bubbles:true})); return true;`);
    check("提交创建用户", await cdp.eval("return __e2e.clickBtnIn('账号管理', '创建用户');"));
    await sleep(1800);
    const users1 = await api("GET", "/api/auth/users");
    check("新用户已在后端创建", users1.data.users.some((u) => u.username === uname),
          JSON.stringify(users1.data.users.map((u) => u.username)));
    check("新用户已出现在表格", await cdp.eval(`return __e2e.rowTexts().some(t => t.includes('${uname}'));`));
    check("删除该用户", await cdp.eval(`return __e2e.clickRowBtn('${uname}', '删除');`));
    await sleep(1800);
    const users2 = await api("GET", "/api/auth/users");
    check("新用户已从后端删除", !users2.data.users.some((u) => u.username === uname),
          JSON.stringify(users2.data.users.map((u) => u.username)));
    check("表格中已无该用户", await cdp.eval(`return !__e2e.rowTexts().some(t => t.includes('${uname}'));`));

    /* ================= 8. 判定服务页 ================= */
    console.log("== 8. 判定服务页 ==");
    check("进入判定服务", await cdp.eval("return __e2e.clickText('.chip','判定服务');"));
    await sleep(1500);
    check("判定页标题", (await cdp.eval("return __e2e.text('.detail-head h2');") || "").includes("判定服务"));
    check("判定页有启用开关", await cdp.eval("return !!__e2e.q('input[type=checkbox]');"));
    check("判定页显示通信记录表", (await cdp.eval("return __e2e.headers('table');")).includes("动作"));
    check("返回面板", await cdp.eval("return __e2e.clickText('button','返回');"));
    await sleep(1200);
    check("已回到实时监控", await cdp.eval("return !!__e2e.q('.grid-zone');"));

    console.log("== 8.1 操作日志记录账号与来源（验证 7.4 产生的新日志） ==");
    check("切到操作日志", await cdp.eval("return __e2e.clickText('.tab','操作日志');"));
    await sleep(1500);
    const recent = await cdp.eval(
      "const t=__e2e.visibleTable(); if(!t) return [];"
      + "return Array.from(t.querySelectorAll('tbody tr')).slice(0,5).map(r =>"
      + " Array.from(r.children).map(c => c.textContent.trim()));");
    check("最新日志含刚创建/删除的账号记录",
          recent.some((r) => r.some((c) => c.includes('创建账号') || c.includes('删除账号'))),
          JSON.stringify(recent));
    check("这些日志的操作人为 admin",
          recent.filter((r) => r.some((c) => c.includes('创建账号') || c.includes('删除账号')))
                .every((r) => r[1] === 'admin'), JSON.stringify(recent));
    // 分类筛选：切到账号管理
    await cdp.eval("const s=__e2e.q('.alarm-rule select'); if(s){ s.value='account'; s.dispatchEvent(new Event('change',{bubbles:true})); } return true;");
    await sleep(1500);
    const acctActs = await cdp.eval(
      "const t=__e2e.visibleTable(); if(!t) return [];"
      + "return Array.from(t.querySelectorAll('tbody tr')).map(r => r.children[2] ? r.children[2].textContent.trim() : '');");
    check("账号分类筛选生效（只剩账号类动作）",
          acctActs.length > 0 && acctActs.every((a) => /账号|权限|密码/.test(a) || a === '暂无操作日志'),
          JSON.stringify(acctActs));
    const acctOps = await cdp.eval(
      "const t=__e2e.visibleTable(); if(!t) return [];"
      + "return Array.from(t.querySelectorAll('tbody tr')).map(r => r.children[1] ? r.children[1].textContent.trim() : '');");
    check("账号类日志操作人均为 admin",
          acctOps.every((o) => o === 'admin' || o === ''), JSON.stringify(acctOps));
    await cdp.eval("const s=__e2e.q('.alarm-rule select'); if(s){ s.value=''; s.dispatchEvent(new Event('change',{bubbles:true})); } return true;");
    await sleep(1000);

    /* ================= 9. 响应式 ================= */
    console.log("== 9. 响应式布局 ==");
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: 390, height: 844, deviceScaleFactor: 2, mobile: true,
    });
    await sleep(1500);
    const m = await cdp.eval("return { sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth, tanks: __e2e.count('.wg-tank') };");
    check("手机宽度下无横向溢出", m.sw <= m.cw + 2, JSON.stringify(m));
    check("手机宽度下罐体仍为 2 个", m.tanks === 2, JSON.stringify(m));
    check("手机宽度下网格仍渲染", await cdp.eval("return !!__e2e.q('#dash-grid.grid-stack');"));
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: 1440, height: 900, deviceScaleFactor: 1, mobile: false,
    });
    await sleep(1000);
    const d = await cdp.eval("return { sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth };");
    check("桌面宽度下无横向溢出", d.sw <= d.cw + 2, JSON.stringify(d));

    /* ================= 10. 退出登录 ================= */
    console.log("== 10. 退出登录 ==");
    check("点击退出", await cdp.eval("return __e2e.clickText('.chip','退出');"));
    await sleep(1500);
    check("回到登录页", await cdp.eval("return !!__e2e.q('.login-card');"));
    check("token 已清除", await cdp.eval("return !localStorage.getItem('iot_token');"));

    /* ================= 11. 控制台错误 ================= */
    console.log("== 11. 控制台 ==");
    const pageErrs = await cdp.eval("return window.__e2eErrors || [];");
    const allErrs = consoleErrors.concat(pageErrs).filter(Boolean);
    // 设备偶发超时导致的轮询失败不算前端缺陷
    const realErrs = allErrs.filter((e) => !/40101|40301|登录已过期|Failed to fetch|net::ERR/i.test(e));
    check("无前端 JS 报错", realErrs.length === 0, JSON.stringify(realErrs.slice(0, 5)));
    if (allErrs.length !== realErrs.length) {
      warn(`过滤掉 ${allErrs.length - realErrs.length} 条网络/鉴权噪声`, "");
    }
  } finally {
    // 恢复进入测试前的仪表盘布局
    try {
      if (layoutBefore) await api("POST", "/api/water/dashboard/layout", { layout: layoutBefore });
      else await api("POST", "/api/water/dashboard/layout/reset");
      // 还原后再核对一次卡片数：布局是全局共享的，若还有别的页面停在编辑模式，
      // 它可能在这之后又把旧布局写回去，这里至少要把情况说出来（可点「恢复上一次」回退）。
      const restored = (await api("GET", "/api/water/dashboard/layout")).data?.layout;
      const want = layoutBefore ? (layoutBefore.widgets || []).length : 0;
      const got = ((restored || {}).widgets || []).length;
      if (got !== want) {
        console.log(`  [WARN] 布局还原后卡片数不符（期望 ${want}，实际 ${got}）：`
          + "可能有其它页面正在并发写仪表盘布局；请在页面上点「恢复上一次」回退，"
          + "或先关闭/退出编辑模式的页面后重跑。");
      } else {
        console.log(`  布局已还原：${got} 张卡片`);
      }
    } catch (e) { /* ignore */ }
    try { if (cdp) await cdp.send("Browser.close"); } catch (e) { /* ignore */ }
    await sleep(500);
    try { child.kill(); } catch (e) { /* ignore */ }
    await sleep(300);
    try { fs.rmSync(userDataDir, { recursive: true, force: true }); } catch (e) { /* ignore */ }
  }

  console.log(`\n结果: ${passed} 通过, ${failed} 失败, ${warned} 提示`);
  if (dialogs.length) console.log(`弹窗记录: ${JSON.stringify(dialogs)}`);
  process.exit(failed ? 1 : 0);
}

main().catch((e) => {
  console.error("E2E 运行失败:", e && e.stack || e);
  process.exit(2);
});
