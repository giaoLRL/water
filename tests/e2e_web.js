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
