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
  svgIds: () => Array.from(document.querySelectorAll(".loop-svg [id]")).map((e) => e.id),
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

    /* ================= 4. 实时监控页（三栏驾驶舱） ================= */
    console.log("== 4. 实时监控页（三栏驾驶舱 + 循环回路） ==");
    check("标题正确", (await cdp.eval("return __e2e.text('.detail-head h2');")) === "水循环综合监控");
    check("驾驶舱已渲染", await cdp.eval("return !!__e2e.q('.cockpit');"));
    check("三栏结构齐全(左KPI / 中回路 / 右操作)",
          await cdp.eval("return !!__e2e.q('.ck-kpi') && !!__e2e.q('.ck-loop') && !!__e2e.q('.ck-ops');"));
    const cols = await cdp.eval(
      "return ['.ck-kpi','.ck-loop','.ck-ops'].map(s => Math.round(__e2e.q(s).getBoundingClientRect().left));");
    check("三栏自左向右排列且不重叠", cols[0] < cols[1] && cols[1] < cols[2], JSON.stringify(cols));
    check("KPI 卡片 4 张", (await cdp.eval("return __e2e.count('.kpi-card');")) === 4,
          "实际 " + await cdp.eval("return __e2e.count('.kpi-card');"));
    check("操作卡 3 张(定量/最近操作/更多)", (await cdp.eval("return __e2e.count('.op-card');")) === 3);

    // —— 循环回路 ——
    check("回路 SVG 1 个（两槽合并为一张整体图）", (await cdp.eval("return __e2e.count('.loop-svg');")) === 1);
    const labels = await cdp.eval("return __e2e.qa('.loop-tank-label').map(e => e.textContent.trim());");
    check("两槽名称为 储水槽/加热槽", JSON.stringify(labels) === JSON.stringify(["储水槽", "加热槽"]), JSON.stringify(labels));
    check("罐体 2 个", (await cdp.eval("return __e2e.count('.tank-frame');")) === 2);
    check("波浪路径每槽 2 条(共 4)", (await cdp.eval("return __e2e.count('.tank-wave');")) === 4);
    check("气泡每槽 6 个(共 12)", (await cdp.eval("return __e2e.count('.tank-bubble');")) === 12);
    check("刻度每槽 5 档(共 10)", (await cdp.eval("return __e2e.count('.tank-scale line');")) === 10);
    check("循环管路 2 条(出水+回流)", (await cdp.eval("return __e2e.count('.loop-pipe-line');")) === 2);
    check("水流粒子 8 个", (await cdp.eval("return __e2e.count('.loop-particle');")) === 8);
    check("水泵节点 1 个", (await cdp.eval("return __e2e.count('.loop-pump');")) === 1);

    const ids = await cdp.eval("return __e2e.svgIds();");
    check("回路 SVG 内 id 不重复（两槽共用一张 SVG 也不会串位）",
          new Set(ids).size === ids.length, JSON.stringify(ids));
    check("含渐变 + 罐体裁剪 + 两槽水面裁剪共 4 个 id", ids.length === 4, JSON.stringify(ids));

    const pcts = await cdp.eval("return __e2e.qa('.tank-pct').map(e => e.textContent.trim());");
    check("水位百分比文本已渲染", pcts.length === 2 && pcts.every((t) => /^\d{1,3}%$/.test(t)), JSON.stringify(pcts));
    check("模拟水槽带「模拟」角标", (await cdp.eval("return __e2e.count('.tank-sim-tag');")) >= 1);
    const waterH = await cdp.eval(
      "return __e2e.qa('.loop-svg rect[fill^=\"url\"]').map(r => parseFloat(r.getAttribute('height')));");
    check("两槽水高均在 0~350 之间", waterH.length === 2 && waterH.every((h) => h >= 0 && h <= 350), JSON.stringify(waterH));

    const geo = await cdp.eval(`
      const fr = Array.from(document.querySelectorAll('.tank-frame')).map(e => e.getBoundingClientRect());
      const pump = __e2e.q('.loop-pump').getBoundingClientRect();
      const pc = Array.from(document.querySelectorAll('.tank-pct')).map(e => e.getBoundingClientRect());
      return {
        sideBySide: fr.length === 2 && fr[1].left > fr[0].right,
        pumpBetween: fr.length === 2 && pump.left > fr[0].right && pump.right < fr[1].left,
        pctInside: pc.length === 2 && pc.every((p, i) => {
          const cx = p.left + p.width / 2;
          return cx > fr[i].left && cx < fr[i].right;
        }),
        tankW: Math.round(fr[0] ? fr[0].width : 0), tankH: Math.round(fr[0] ? fr[0].height : 0),
      };`);
    check("两罐左右并排不重叠", geo.sideBySide, JSON.stringify(geo));
    check("水泵节点位于两罐之间（画在管路上）", geo.pumpBetween, JSON.stringify(geo));
    check("水位百分比居中于罐内", geo.pctInside, JSON.stringify(geo));
    check("罐体已实际布局(尺寸>100px)", geo.tankW > 100 && geo.tankH > 100, JSON.stringify(geo));

    // —— 首屏零滚动 ——
    const sc = await cdp.eval("return { s: document.documentElement.scrollHeight, v: innerHeight };");
    check("实时监控页首屏零滚动", sc.s <= sc.v + 2, JSON.stringify(sc));
    console.log(`  → 页面 ${sc.s}px / 视口 ${sc.v}px`);

    // —— 更多操作默认折叠（危险操作降权）——
    check("「更多操作」默认折叠", await cdp.eval(
      "const b=__e2e.q('.op-more-body'); return !!b && getComputedStyle(b).display === 'none';"));
    check("折叠时清零按钮不可见", await cdp.eval(
      "const b=__e2e.btnByText('清零累计水量'); return !!b && b.offsetParent === null;"));
    check("展开「更多操作」", await cdp.eval("return __e2e.clickText('.op-more-btn','更多操作');"));
    await sleep(600);
    check("展开后清零按钮可见", await cdp.eval(
      "const b=__e2e.btnByText('清零累计水量'); return !!b && b.offsetParent !== null;"));

    check("流量迷你趋势容器存在", await cdp.eval("return !!__e2e.q('#spark-flow');"));
    check("累计水量迷你趋势容器存在", await cdp.eval("return !!__e2e.q('#spark-total');"));
    check("水泵开关存在", await cdp.eval("return !!__e2e.q('.detail-controls .toggle input');"));

    console.log("== 4.1 控制件状态与后端一致性（本脚本不实际动作水泵） ==");
    const rtApi = (await api("GET", "/api/water/realtime")).data;
    const tog = await cdp.eval("const t=__e2e.q('.detail-controls .toggle input'); return t ? {checked:t.checked, disabled:t.disabled} : null;");
    check("水泵开关存在于控制区", !!tog, JSON.stringify(tog));
    if (tog) {
      check("水泵开关勾选态与后端 pump_state 一致",
            tog.checked === (rtApi.pump_state === "on"),
            JSON.stringify({ ui: tog.checked, api: rtApi.pump_state }));
      check("设备离线时水泵开关被禁用",
            rtApi.sensor_online ? true : tog.disabled === true,
            JSON.stringify({ online: rtApi.sensor_online, disabled: tog.disabled }));
    }
    check("后端实时数据含双水槽液位",
          typeof rtApi.tank.tanks.storage.percent === "number" && typeof rtApi.tank.tanks.heater.percent === "number",
          JSON.stringify(rtApi.tank.tanks));
    const uiPct = await cdp.eval("return __e2e.qa('.tank-pct').map(e=>parseFloat(e.textContent));");
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
    const resetBtn = await cdp.eval("const b=__e2e.btnByText('清零累计水量'); return b ? { found:true, disabled:b.disabled } : { found:false };");
    check("清零按钮存在", resetBtn.found, JSON.stringify(resetBtn));
    if (resetBtn.found && resetBtn.disabled) {
      // 设备离线时按钮按设计禁用（!online），此时无法触发确认框
      warn("清零按钮当前为禁用态（设备离线或通道不可用），跳过二次确认校验", JSON.stringify(resetBtn));
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
    check("已回到实时监控", await cdp.eval("return !!__e2e.q('.cockpit');"));

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
    const m = await cdp.eval("return { sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth, tanks: __e2e.count('.tank-frame') };");
    check("手机宽度下无横向溢出", m.sw <= m.cw + 2, JSON.stringify(m));
    check("手机宽度下罐体仍为 2 个", m.tanks === 2, JSON.stringify(m));
    check("手机宽度下驾驶舱退回单列",
          (await cdp.eval("return getComputedStyle(__e2e.q('.cockpit')).gridTemplateColumns.split(' ').length;")) === 1);
    check("手机宽度下 KPI 竖排为单列",
          (await cdp.eval("return getComputedStyle(__e2e.q('.ck-kpi')).flexDirection;")) === "column");
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
