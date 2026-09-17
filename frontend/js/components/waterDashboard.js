/* 综合面板：全卡片网格（可编辑仪表盘）+ 历史、统计、告警、日志。
 *
 * 实时监控页整体卡片化（gridstack 网格，12 列）：
 *   卡片 = 配置 JSON（widgets.js 定义结构），布局存后端 config 表，全局共享；
 *   展示卡（数值/曲线/状态/水槽/回路）与操作卡（水泵/加热/定量/PID/最近操作/清零）
 *   全部进网格，「编辑卡片」可拖动/缩放/删除/配置，「+ 添加卡片」按设备能力与权限列出目录；
 *   自定义接口卡经后端白名单代理代发，仅 GET。
 * 数据来源为现场 ESP32 采集端（纯真实模式），前端按 realtime.features 展示设备实际通道：
 *   支持 —— 瞬时流量、累计水量、水泵、定量目标、水温×2、水压、加热模块、光照、加热槽液位
 *   未接入 —— 储水槽液位（无传感器，水位恒显示 0 并标注「无传感器」）
 * 无数据一律显示 0 或 --，不做任何模拟。
 */

window.ViewWaterDash = {
  name: "WaterDashView",
  components: { WidgetShell: window.WidgetShell },
  props: { realtime: { type: Object, required: true } },
  data() {
    return {
      tab: "monitor",
      thresholds: {},
      deviceInfo: {},
      // 历史
      histPoints: [],
      histRangeKey: "1h",
      histType: "all",
      customStart: "",
      customEnd: "",
      // 统计/告警/日志
      stats: {},
      alarms: [],
      alarmTotal: 0, alarmPage: 1, alarmPageSize: 10,
      logs: [],
      logTotal: 0, logPage: 1, logPageSize: 10,
      logCategory: "",
      tick: 0,                // 2s 定时器计数，用于低频刷新
      // 可编辑卡片仪表盘
      widgets: [],
      editing: false,
      layoutReady: false,
      dlg: { show: false, mode: "add", tab: "builtin", target: null, form: {}, previewText: "", previewRaw: "", previewBusy: false },
      timer: null,
    };
  },
  computed: {
    online() { return !!this.realtime.sensor_online; },
    features() { return this.realtime.features || {}; },
    device() { return this.realtime.device || {}; },
    totalText() {
      const v = this.realtime.total_liters;
      return v === null || v === undefined ? "--" : Number(v).toFixed(3);
    },
    alarmPages() { return Math.max(1, Math.ceil(this.alarmTotal / this.alarmPageSize)); },
    logPages() { return Math.max(1, Math.ceil(this.logTotal / this.logPageSize)); },
    /* 内置卡片目录：按设备能力(feature)+权限(perm)+实时标志(flag)过滤，标记已添加；
       multi 卡（通用单水槽）不判重，可重复添加 */
    catalogList() {
      const present = new Set(this.widgets.map((w) => w.catalogKey || w.builtin).filter(Boolean));
      return window.Widgets.CATALOG
        .filter((c) => (!c.feature || this.features[c.feature])
                    && (!c.perm || this.perm(c.perm))
                    && (!c.flag || !!this.realtime[c.flag]))
        .map((c) => Object.assign({}, c, { added: !c.multi && present.has(c.key) }));
    },
  },
  watch: {
    /* 未保存过自定义布局时，默认布局随设备能力到达而重建 */
    features: {
      handler(n, o) {
        if (this._layoutFromServer || !this.layoutReady) return;
        if (JSON.stringify(n) === JSON.stringify(o)) return;
        this.widgets = window.Widgets.defaultLayout(this.features);
        this.rebuildGrid();
      },
      deep: true,
    },
  },
  mounted() {
    this.loadAll();
    this.refreshDevice();
    this.loadLayout();
    // 2 秒兜底刷新：兼顾隐藏 tab 切回时的低频数据
    this.timer = setInterval(() => {
      this.tick += 1;
      this.refreshDevice();
    }, 2000);
  },
  beforeUnmount() {
    if (this.timer) clearInterval(this.timer);
    if (this._saveT) clearTimeout(this._saveT);
    if (this._grid) { try { this._grid.destroy(false); } catch (e) { /* ignore */ } this._grid = null; }
  },
  methods: {
    perm(p) { return window.Auth ? window.Auth.has(p) : false; },
    fmt(s) {
      const d = new Date(s), p = (n) => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    },
    rangeEnd() { return this.fmt(new Date()); },
    rangeStart(key) {
      const hours = { "1h": 1, "6h": 6, "24h": 24 }[key] || 1;
      return this.fmt(new Date(Date.now() - hours * 3600 * 1000));
    },
    /* 目标输入框已随定量卡迁入 widgetShell（自包含焦点保护） */
    async loadAll() {
      try {
        const cfg = await API.alarmConfigGet();
        this.thresholds = cfg.thresholds || {};
      } catch (e) { /* silent */ }
      this.fetchHistory("1h");
      this.fetchStats("1h");
      this.fetchAlarms(1);
      this.fetchLogs(1);
    },
    async refreshDevice() {
      try {
        const d = await API.device();
        this.deviceInfo = d.device || {};
        if (d.features && this.realtime && !this.realtime.features) this.realtime.features = d.features;
      } catch (e) { /* silent */ }
    },
    // ---------- 历史 ----------
    async fetchHistory(key, start, end) {
      this.histRangeKey = key || this.histRangeKey;
      let s, e;
      if (key === "custom" && start && end) {
        s = String(start).replace("T", " ");
        e = String(end).replace("T", " ");
      } else { s = this.rangeStart(this.histRangeKey); e = this.rangeEnd(); }
      try {
        const d = await API.history(s, e);
        this.histPoints = d.points || [];
        this.renderChart();
      } catch (err) { /* silent */ }
    },
    queryCustom() {
      if (!this.customStart || !this.customEnd) { alert("请选择自定义时间范围的开始和结束时间"); return; }
      this.fetchHistory("custom", this.customStart, this.customEnd);
    },
    pickHistType(t) { this.histType = t; this.renderChart(); },
    renderChart() {
      const el = document.getElementById("water-hist-chart");
      if (!el || !window.echarts) return;
      const pts = this.histPoints || [];
      const times = pts.map((p) => p.ts);
      if (this.histType !== "all") {
        const def = {
          flow: ["瞬时流量", "flow_rate", "#38bdf8", "L/min", 0],
          total: ["累计水量", "total_flow", "#2dd4bf", "L", 0],
          stemp: ["储水槽温度", "storage_temp", "#fb923c", "℃", 1],
          htemp: ["加热槽温度", "heater_temp", "#f87171", "℃", 1],
          pressure: ["水压", "pressure", "#a78bfa", "kPa", 1],
          light: ["光照", "light", "#facc15", "lx", 1],
        }[this.histType] || ["瞬时流量", "flow_rate", "#38bdf8", "L/min", 0];
        window.Charts.init("water-hist-chart", {
          ...window.Charts.baseOption(times, def[3]),
          series: [window.Charts.lineSeries(def[0], pts.map((p) => p[def[1]]), def[2])],
        });
        return;
      }
      window.Charts.init("water-hist-chart", {
        tooltip: { trigger: "axis", backgroundColor: "#1a222d", borderColor: "#2a3442", textStyle: { color: "#d7e0ea" } },
        legend: { top: 0, textStyle: { color: "#7d8b99" } },
        grid: { left: 56, right: 60, top: 36, bottom: 60 },
        xAxis: { type: "category", data: times, boundaryGap: false, ...window.Charts.axisStyle },
        yAxis: [
          { type: "value", name: "流量 L/min", ...window.Charts.axisStyle, nameTextStyle: { color: "#7d8b99" } },
          { type: "value", name: "水量 L", nameTextStyle: { color: "#7d8b99" }, axisLabel: { color: "#7d8b99" }, splitLine: { show: false }, axisLine: { lineStyle: { color: "#2a3442" } } },
        ],
        dataZoom: [
          { type: "inside" },
          { type: "slider", height: 16, bottom: 8, borderColor: "#262f3b", backgroundColor: "#151b24", fillerColor: "rgba(45,212,191,0.14)", handleStyle: { color: "#2dd4bf" }, textStyle: { color: "#7d8b99" } },
        ],
        series: [
          window.Charts.lineSeries("瞬时流量", pts.map((p) => p.flow_rate), "#38bdf8", 0),
          window.Charts.lineSeries("累计水量", pts.map((p) => p.total_flow), "#2dd4bf", 1),
        ],
      });
    },
    // ---------- 统计 ----------
    async fetchStats(key) {
      const s = this.rangeStart(key || this.histRangeKey);
      const e = this.rangeEnd();
      try { this.stats = await API.stats(s, e); } catch (err) { /* silent */ }
      this.renderStatsChart();
    },
    renderStatsChart() {
      const el = document.getElementById("water-stats-chart");
      if (!el || !window.echarts) return;
      const d = this.stats || {};
      window.Charts.init("water-stats-chart", {
        ...window.Charts.barOption(["平均流量", "最高流量", "最低流量"], "L/min"),
        series: [window.Charts.barSeries("流量", [
          d.avg_flow || 0, d.max_flow || 0, d.min_flow || 0,
        ], "#38bdf8")],
      });
    },
    // ---------- 告警 ----------
    async fetchAlarms(page) {
      if (page) this.alarmPage = page;
      try {
        const d = await API.alarms({ page: this.alarmPage, page_size: this.alarmPageSize, status: "active" });
        this.alarms = d.items || [];
        this.alarmTotal = d.total || 0;
      } catch (e) { /* silent */ }
    },
    async fetchLogs(page) {
      if (page) this.logPage = page;
      try {
        const d = await API.logs({
          page: this.logPage,
          page_size: this.logPageSize,
          category: this.logCategory,
        });
        this.logs = d.items || [];
        this.logTotal = d.total || 0;
      } catch (e) { /* silent */ }
    },
    /* 日志展示（与卡片共用 Widgets 纯函数；操作日志页表格仍在用） */
    isSystemLog(l) { return window.Widgets.isSystemLog(l); },
    operatorText(l) { return window.Widgets.operatorText(l); },
    actionLabel(a) { return window.Widgets.actionLabel(a); },
    typeName(t) {
      return { flow_rate: "水流量", storage_temp: "储水槽温度", heater_temp: "加热槽温度", pressure: "水压", light: "光照" }[t] || t;
    },
    // ---------- 可编辑卡片仪表盘 ----------
    /* 读取布局：已保存过用服务端的（全局共享），否则按设备能力生成默认布局。
       按 builtin 去重，防止历史/手编布局出现两张同类特殊卡（如双回路卡 SVG id 冲突）。 */
    async loadLayout() {
      let saved = null;
      try { saved = (await API.dashboardLayoutGet()).layout; } catch (e) { /* silent */ }
      this._layoutFromServer = !!(saved && Array.isArray(saved.widgets) && saved.widgets.length);
      let widgets = this._layoutFromServer ? saved.widgets : window.Widgets.defaultLayout(this.features);
      const seen = new Set();
      widgets = widgets.filter((w) => {
        const k = w.builtin ? "b:" + w.builtin : "id:" + w.id;
        if (seen.has(k)) return false;
        seen.add(k);
        return true;
      });
      this.widgets = widgets;
      this.layoutReady = true;
      this.rebuildGrid();
    },
    initGrid() {
      const el = document.getElementById("dash-grid");
      if (!el || !window.GridStack) return;
      const g = window.GridStack.init({
        column: 12, cellHeight: 36, margin: 6, float: false,
        handle: ".wg-grip", resizable: { handles: "se" },
        disableDrag: !this.editing, disableResize: !this.editing,
      }, el);
      g.on("change", () => this.syncFromGrid());
      this._grid = g;
    },
    /* 结构变化（增/删/改配置）后重建网格；拖拽换位走 change 事件无需重建 */
    rebuildGrid() {
      this.$nextTick(() => {
        if (this._grid) { try { this._grid.destroy(false); } catch (e) { /* ignore */ } this._grid = null; }
        this.initGrid();
      });
    },
    syncFromGrid() {
      if (!this._grid || !this.editing) return;
      (this._grid.save(false) || []).forEach((it) => {
        const w = this.widgets.find((x) => x.id === String(it.id));
        if (w) w.grid = { x: it.x, y: it.y, w: it.w, h: it.h };
      });
      this.scheduleSave();
    },
    /* 序列化卡片：只保留配置字段，剔除运行时字段 */
    serialize(w) {
      const o = { id: w.id, type: w.type, title: w.title, unit: w.unit, decimals: w.decimals,
                  color: w.color, foot: w.foot, builtin: w.builtin, catalogKey: w.catalogKey,
                  domId: w.domId, tank: w.tank, ctl: w.ctl, cmd: w.cmd,
                  source: w.source, thresholds: w.thresholds, grid: w.grid };
      Object.keys(o).forEach((k) => o[k] === undefined && delete o[k]);
      return o;
    },
    scheduleSave() {
      if (!this.perm("cfg_system")) return;
      if (this._saveT) clearTimeout(this._saveT);
      this._saveT = setTimeout(() => this.saveLayout(), 400);
    },
    async saveLayout() {
      try {
        const widgets = this.widgets.map((w) => this.serialize(w));
        await API.dashboardLayoutSave({ version: 1, widgets });
        this._layoutFromServer = true;
      } catch (e) { /* 保存失败不打断编辑 */ }
    },
    toggleEdit() {
      this.editing = !this.editing;
      if (this._grid) {
        this._grid.enableMove(this.editing);
        this._grid.enableResize(this.editing);
      }
      if (!this.editing) this.saveLayout();
    },
    async resetLayout() {
      if (!confirm("恢复为默认布局？已添加的自定义卡片与排版将被清除。")) return;
      try { await API.dashboardLayoutReset(); } catch (e) { /* silent */ }
      this._layoutFromServer = false;
      this.widgets = window.Widgets.defaultLayout(this.features);
      this.editing = false;
      if (this._grid) { this._grid.enableMove(false); this._grid.enableResize(false); }
      this.rebuildGrid();
    },
    bottomY() {
      return this.widgets.reduce((m, w) => Math.max(m, ((w.grid || {}).y || 0) + ((w.grid || {}).h || 2)), 0);
    },
    /* 添加卡片（内置目录 / 自定义接口共用入口） */
    addWidget(cfg) {
      const w = JSON.parse(JSON.stringify(cfg));
      delete w.feature;
      delete w.added;
      delete w.multi;
      if (!w.id) w.id = window.Widgets.uid();
      w.grid = Object.assign({ w: 6, h: 2 }, w.grid, { x: 0, y: this.bottomY() });
      this.widgets.push(w);
      this.dlg.show = false;
      this.rebuildGrid();
      this.scheduleSave();
    },
    addFromCatalog(c) {
      const w = window.Widgets.fromCatalog(c);
      this.addWidget(w);
    },
    removeWidget(w) {
      if (!confirm(`删除卡片「${w.title}」？`)) return;
      this.widgets = this.widgets.filter((x) => x.id !== w.id);
      this.rebuildGrid();
      this.scheduleSave();
    },
    typeLabel(t) { return (window.Widgets.TYPES || {})[t] || t; },
    /* 这五种卡型的数据接口可自由编辑（URL 留空 = 本机实时快照） */
    isSourceEditable(t) { return ["value", "spark", "line", "state", "tank", "control"].includes(t); },
    openAdd() {
      this.dlg = { show: true, mode: "add", tab: "builtin", target: null,
                   form: this.blankCustomForm(), previewText: "", previewRaw: "", previewBusy: false };
    },
    blankCustomForm() {
      return { title: "", url: "", period: 5, path: "", unit: "", decimals: 1,
               type: "value", color: "#38bdf8", min: "", max: "", cmdOn: "", cmdOff: "" };
    },
    /* 自定义接口预览：走代理实取一次，验证 URL 与取值路径 */
    async previewCustom() {
      const f = this.dlg.form;
      const url = (f.url || "").trim();
      if (!url) {
        // 无 URL：直接从实时快照预览
        const v = window.Widgets.resolvePath(this.realtime, (f.path || "").trim());
        this.dlg.previewText = v === undefined ? "（实时快照中该路径未取到值）" : JSON.stringify(v);
        this.dlg.previewRaw = "（本机实时快照）";
        return;
      }
      this.dlg.previewBusy = true;
      try {
        const d = await API.dashboardProxy(url);
        const v = window.Widgets.resolvePath(d.json, f.path);
        this.dlg.previewText = v === undefined ? "（该路径未取到值，请检查路径写法）" : JSON.stringify(v);
        this.dlg.previewRaw = JSON.stringify(d.json ?? d.text).slice(0, 300);
      } catch (e) {
        this.dlg.previewText = "请求失败：" + (e.message || e);
        this.dlg.previewRaw = "";
      } finally { this.dlg.previewBusy = false; }
    },
    confirmCustom() {
      const f = this.dlg.form;
      if (!f.title.trim() || !(f.path || "").trim()) { alert("名称、取值路径必填（接口 URL 留空则读本机实时快照）"); return; }
      const cmdOn = (f.cmdOn || "").trim(), cmdOff = (f.cmdOff || "").trim();
      if (f.type === "control" && ((cmdOn ? 1 : 0) + (cmdOff ? 1 : 0)) === 1) {
        alert("开/关指令 URL 需成对填写，或都留空使用内置通道"); return;
      }
      const hasTh = (f.min !== "" && f.min != null) || (f.max !== "" && f.max != null);
      const url = (f.url || "").trim();
      const path = f.path.trim();
      const grids = { value: { w: 6, h: 2 }, spark: { w: 6, h: 3 }, line: { w: 6, h: 4 },
                      state: { w: 4, h: 2 }, tank: { w: 3, h: 4 }, control: { w: 4, h: 2 } };
      this.addWidget({
        catalogKey: "custom:" + f.type + ":" + url + "#" + path,
        type: f.type || "value",
        title: f.title.trim(),
        unit: f.unit || "", decimals: Number(f.decimals) || 0, color: f.color || "#38bdf8",
        source: url ? { kind: "custom", url, path, period: Math.max(2, Number(f.period) || 5) }
                    : { kind: "realtime", path },
        cmd: f.type === "control" && cmdOn ? { on: cmdOn, off: cmdOff } : undefined,
        thresholds: hasTh ? { min: f.min === "" ? null : Number(f.min), max: f.max === "" ? null : Number(f.max) } : null,
        grid: grids[f.type] || { w: 6, h: 2 },
      });
    },
    /* 卡片配置对话框：接口 URL 留空 = 本机实时快照，填了 = 自定义接口 */
    openConfig(w) {
      const t = w.thresholds || {};
      const src = w.source || {};
      this.dlg = { show: true, mode: "config", tab: "cfg", target: w.id, previewText: "", previewRaw: "", previewBusy: false,
                   form: { title: w.title, unit: w.unit || "", decimals: w.decimals ?? 1,
                           color: w.color || "#38bdf8", type: w.type, foot: w.foot || "",
                           min: t.min ?? "", max: t.max ?? "",
                           url: src.kind === "custom" ? src.url : "",
                           path: src.path || "",
                           period: src.period || 5,
                           cmdOn: (w.cmd || {}).on || "", cmdOff: (w.cmd || {}).off || "" } };
    },
    saveConfig() {
      const w = this.widgets.find((x) => x.id === this.dlg.target);
      if (!w) { this.dlg.show = false; return; }
      const f = this.dlg.form;
      const cmdOn = (f.cmdOn || "").trim(), cmdOff = (f.cmdOff || "").trim();
      if (w.type === "control" && ((cmdOn ? 1 : 0) + (cmdOff ? 1 : 0)) === 1) {
        alert("开/关指令 URL 需成对填写，或都留空使用内置通道"); return;
      }
      w.title = (f.title || "").trim() || w.title;
      if (this.isSourceEditable(w.type)) {
        // 数据接口：URL 留空 → 实时快照字段；非空 → 自定义接口轮询
        const url = (f.url || "").trim();
        const path = (f.path || "").trim() || (w.source || {}).path || "";
        if (w.type === "control") {
          w.color = f.color;
          if (cmdOn && cmdOff) w.cmd = { on: cmdOn, off: cmdOff }; else delete w.cmd;
        } else {
          w.type = f.type;
          w.unit = f.unit;
          w.decimals = Number(f.decimals) || 0;
          w.color = f.color;
        }
        if (url) w.source = { kind: "custom", url, path, period: Math.max(2, Number(f.period) || 5) };
        else w.source = { kind: "realtime", path };
        w._v = (w._v || 0) + 1;   // 触发组件重建，以新参数重启轮询
      }
      w.foot = f.foot;
      const keyed = w.thresholds && (w.thresholds.minKey || w.thresholds.maxKey);
      if (!keyed && ["value", "spark", "line"].includes(w.type)) {
        const has = (f.min !== "" && f.min != null) || (f.max !== "" && f.max != null);
        w.thresholds = has ? { min: f.min === "" ? null : Number(f.min), max: f.max === "" ? null : Number(f.max) } : null;
      }
      this.dlg.show = false;
      this.rebuildGrid();
      this.scheduleSave();
    },
  },
  template: `
  <div class="view-page">
    <div class="detail-head">
      <h2>水循环综合监控</h2>
      <span class="desc">瞬时流量 · 累计水量 · 水温 · 水压 · 定量浇水 · 恒温闭环</span>
      <span class="lamp-dot" :class="online ? 'on' : 'off'"></span>
      <span class="desc">{{ online ? '设备在线' : '设备离线' }}</span>
    </div>

    <div v-if="!online" class="offline-bar">
      <span class="ob-dot"></span>采集设备离线（{{ device.url || deviceInfo.url || '--' }}）{{ realtime.last_error ? '：' + realtime.last_error : '' }}
    </div>

    <div class="tabs">
      <span class="tab" :class="{ active: tab === 'monitor' }" @click="tab='monitor'">实时监控</span>
      <span class="tab" :class="{ active: tab === 'history' }" @click="tab='history'; $nextTick(()=>renderChart())">历史数据</span>
      <span class="tab" :class="{ active: tab === 'stats' }" @click="tab='stats'; $nextTick(()=>renderStatsChart())">数据统计</span>
      <span class="tab" v-if="perm('view_alarm')" :class="{ active: tab === 'alarm' }" @click="tab='alarm'; fetchAlarms(1)">告警记录</span>
      <span class="tab" v-if="perm('view_log')" :class="{ active: tab === 'logs' }" @click="tab='logs'; fetchLogs(1)">操作日志</span>
    </div>

    <!-- 实时监控：全卡片网格（回路图/操作卡均已卡片化，可自由增删拖拽） -->
    <div v-show="tab === 'monitor'" class="grid-zone">
      <div class="dash-editbar" v-if="perm('cfg_system')">
        <button class="btn-ghost" @click="toggleEdit">{{ editing ? '完成' : '编辑卡片' }}</button>
        <template v-if="editing">
          <button class="btn-ghost" @click="openAdd">+ 添加卡片</button>
          <button class="btn-ghost" @click="resetLayout">恢复默认</button>
        </template>
      </div>
      <div class="grid-stack" id="dash-grid">
        <div v-for="w in widgets" :key="w.id + '-' + (w._v || 0)" class="grid-stack-item"
             :gs-id="w.id" :gs-x="w.grid.x" :gs-y="w.grid.y" :gs-w="w.grid.w" :gs-h="w.grid.h"
             :gs-min-w="2" :gs-min-h="1">
          <div class="grid-stack-item-content">
            <WidgetShell :widget="w" :realtime="realtime" :thresholds="thresholds"
                         :editing="editing" :online="online"
                         @remove="removeWidget" @config="openConfig"/>
          </div>
        </div>
      </div>
    </div>

    <!-- 历史数据 -->
    <div v-show="tab === 'history'">
      <div class="section">
        <h3>历史曲线</h3>
        <div class="tabs">
          <span class="tab" :class="{ active: histRangeKey==='1h' }" @click="fetchHistory('1h')">近1小时</span>
          <span class="tab" :class="{ active: histRangeKey==='6h' }" @click="fetchHistory('6h')">近6小时</span>
          <span class="tab" :class="{ active: histRangeKey==='24h' }" @click="fetchHistory('24h')">近24小时</span>
          <span class="tab" :class="{ active: histRangeKey==='custom' }" @click="fetchHistory('custom')">自定义</span>
        </div>
        <div v-if="histRangeKey==='custom'" class="custom-range">
          <input type="datetime-local" v-model="customStart"><span>至</span>
          <input type="datetime-local" v-model="customEnd">
          <button class="btn-ghost" @click="queryCustom">查询</button>
        </div>
        <div class="tabs">
          <span class="tab" :class="{ active: histType==='all' }" @click="pickHistType('all')">全部</span>
          <span class="tab" :class="{ active: histType==='flow' }" @click="pickHistType('flow')">瞬时流量</span>
          <span class="tab" :class="{ active: histType==='total' }" @click="pickHistType('total')">累计水量</span>
          <span class="tab" v-if="features.temperature" :class="{ active: histType==='stemp' }" @click="pickHistType('stemp')">储水槽温度</span>
          <span class="tab" v-if="features.temperature" :class="{ active: histType==='htemp' }" @click="pickHistType('htemp')">加热槽温度</span>
          <span class="tab" v-if="features.pressure" :class="{ active: histType==='pressure' }" @click="pickHistType('pressure')">水压</span>
          <span class="tab" v-if="features.light" :class="{ active: histType==='light' }" @click="pickHistType('light')">光照</span>
        </div>
        <div class="chart" id="water-hist-chart"></div>
      </div>
    </div>

    <!-- 数据统计 -->
    <div v-show="tab === 'stats'">
      <div class="section">
        <h3>数据统计 <span class="desc">区间流量指标汇总（任务六·数据统计分析）</span></h3>
        <div class="chart small" id="water-stats-chart"></div>
      </div>
      <div class="grid-4" style="margin-bottom:14px;">
        <div class="metric"><div class="label">平均流量</div><div class="value">{{ stats.avg_flow ?? '--' }}<span class="unit">L/min</span></div></div>
        <div class="metric"><div class="label">最高流量</div><div class="value">{{ stats.max_flow ?? '--' }}<span class="unit">L/min</span></div></div>
        <div class="metric"><div class="label">最低流量</div><div class="value">{{ stats.min_flow ?? '--' }}<span class="unit">L/min</span></div></div>
        <div class="metric"><div class="label">区间用水量</div><div class="value">{{ stats.volume_used ?? '--' }}<span class="unit">L</span></div></div>
      </div>
      <div class="grid-4" v-if="features.temperature || features.pressure" style="margin-bottom:14px;">
        <div class="metric" v-if="features.temperature"><div class="label">储水槽均温</div><div class="value">{{ stats.avg_storage_temp ?? '--' }}<span class="unit">℃</span></div></div>
        <div class="metric" v-if="features.temperature"><div class="label">储水槽峰值</div><div class="value">{{ stats.max_storage_temp ?? '--' }}<span class="unit">℃</span></div></div>
        <div class="metric" v-if="features.temperature"><div class="label">加热槽均温</div><div class="value">{{ stats.avg_heater_temp ?? '--' }}<span class="unit">℃</span></div></div>
        <div class="metric" v-if="features.temperature"><div class="label">加热槽峰值</div><div class="value">{{ stats.max_heater_temp ?? '--' }}<span class="unit">℃</span></div></div>
      </div>
      <div class="grid-4" v-if="features.pressure" style="margin-bottom:14px;">
        <div class="metric"><div class="label">水压最高</div><div class="value">{{ stats.max_pressure ?? '--' }}<span class="unit">kPa</span></div></div>
        <div class="metric"><div class="label">水压最低</div><div class="value">{{ stats.min_pressure ?? '--' }}<span class="unit">kPa</span></div></div>
        <div class="metric" v-if="features.light"><div class="label">光照最高</div><div class="value">{{ stats.max_light ?? '--' }}<span class="unit">lx</span></div></div>
        <div class="metric" v-if="features.light"><div class="label">光照最低</div><div class="value">{{ stats.min_light ?? '--' }}<span class="unit">lx</span></div></div>
      </div>
      <div class="grid-4">
        <div class="metric"><div class="label">区间起始累计</div><div class="value">{{ stats.start_total ?? '--' }}<span class="unit">L</span></div></div>
        <div class="metric"><div class="label">区间结束累计</div><div class="value">{{ stats.end_total ?? '--' }}<span class="unit">L</span></div></div>
        <div class="metric"><div class="label">当前累计水量</div><div class="value">{{ totalText }}<span class="unit">L</span></div></div>
        <div class="metric"><div class="label">采样点数</div><div class="value">{{ stats.samples ?? 0 }}<span class="unit">条</span></div></div>
      </div>
      <div class="note">区间用水量 = 区间结束累计水量 − 区间起始累计水量；期间若执行过清零，该值按 0 计。</div>
    </div>

    <!-- 告警记录 -->
    <div v-show="tab === 'alarm'">
      <div class="section">
        <h3>告警记录 <span class="desc">当前活跃 {{ alarmTotal }} 条</span></h3>
        <div style="overflow-x:auto;">
          <table>
            <thead><tr><th>时间</th><th>类型</th><th>数值</th><th>阈值</th><th>方向</th><th>状态</th></tr></thead>
            <tbody>
              <tr v-for="(a,i) in alarms" :key="i">
                <td>{{ a.ts }}</td><td>{{ typeName(a.type) }}</td><td>{{ a.value }}</td><td>{{ a.threshold }}</td>
                <td>{{ a.direction==='above' ? '超上限' : '低于下限' }}</td>
                <td><span class="badge" :class="a.status==='active' ? 'danger' : 'ok'">{{ a.status==='active' ? '告警中' : '已恢复' }}</span></td>
              </tr>
              <tr v-if="!alarms.length"><td colspan="6" style="text-align:center;color:#6b7a90;">暂无告警记录</td></tr>
            </tbody>
          </table>
        </div>
        <div class="pager">
          <span>每页</span>
          <select v-model.number="alarmPageSize" @change="fetchAlarms(1)">
            <option :value="10">10</option><option :value="20">20</option><option :value="50">50</option>
          </select>
          <span>共 {{ alarmTotal }} 条 · 第 {{ alarmPage }} / {{ alarmPages }} 页</span>
          <button class="btn-ghost" :disabled="alarmPage<=1" @click="fetchAlarms(alarmPage-1)">上一页</button>
          <button class="btn-ghost" :disabled="alarmPage>=alarmPages" @click="fetchAlarms(alarmPage+1)">下一页</button>
        </div>
      </div>
    </div>

    <!-- 操作日志 -->
    <div v-show="tab === 'logs'">
      <div class="section">
        <h3>操作日志
          <span class="desc">共 {{ logTotal }} 条 · 记录设备控制、系统配置与账号管理</span>
        </h3>
        <div class="alarm-rule" style="margin-bottom:10px;">
          <label class="rule-item"><span>分类</span>
            <select v-model="logCategory" @change="fetchLogs(1)">
              <option value="">全部</option>
              <option value="device">设备控制</option>
              <option value="config">系统配置</option>
              <option value="account">账号管理</option>
            </select>
          </label>
          <span class="desc" style="margin-left:auto;">
            系统自动动作（恒温闭环 / 判定服务）无操作账号，标注为「系统」
          </span>
        </div>
        <div style="overflow-x:auto;">
          <table>
            <thead><tr><th>时间</th><th>操作人</th><th>指令</th><th>结果</th><th>说明</th></tr></thead>
            <tbody>
              <tr v-for="(l,i) in logs" :key="i">
                <td>{{ l.ts }}</td>
                <td>
                  <span v-if="isSystemLog(l)" class="badge warn">{{ operatorText(l) }}</span>
                  <span v-else-if="l.operator">{{ l.operator }}</span>
                  <span v-else style="color:var(--text-dim);" title="历史日志未记录操作人">—</span>
                </td>
                <td>{{ actionLabel(l.action) }}</td>
                <td><span class="badge" :class="l.result==='success' ? 'ok' : 'fail'">{{ l.result==='success' ? '成功':'失败' }}</span></td>
                <td>{{ l.detail || '' }}</td>
              </tr>
              <tr v-if="!logs.length"><td colspan="5" style="text-align:center;color:#6b7a90;">暂无操作日志</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- 添加卡片 / 卡片配置 对话框 -->
    <div class="modal-overlay" v-if="dlg.show" @click.self="dlg.show=false">
      <div class="modal wg-modal">
        <div class="modal-head">
          <h3>{{ dlg.mode === 'add' ? '添加卡片' : '卡片配置' }}</h3>
          <button class="close" @click="dlg.show=false">×</button>
        </div>

        <div class="tabs" v-if="dlg.mode==='add'">
          <span class="tab" :class="{active:dlg.tab==='builtin'}" @click="dlg.tab='builtin'">内置卡片</span>
          <span class="tab" :class="{active:dlg.tab==='custom'}" @click="dlg.tab='custom'">自定义接口</span>
        </div>

        <!-- 内置卡片目录 -->
        <div v-if="dlg.mode==='add' && dlg.tab==='builtin'" class="wg-catalog">
          <div v-for="c in catalogList" :key="c.key" class="wg-cat-item">
            <div class="wg-cat-name">
              <b>{{ c.title }}</b>
              <span class="wg-cat-type">{{ typeLabel(c.type) }}<template v-if="c.unit"> · {{ c.unit }}</template></span>
            </div>
            <button class="btn-ghost" :disabled="c.added" @click="addFromCatalog(c)">{{ c.added ? '已添加' : '添加' }}</button>
          </div>
          <div v-if="!catalogList.length" class="wg-empty">当前设备能力下无可添加的内置卡片</div>
        </div>

        <!-- 自定义接口卡片：全部卡型可添加 -->
        <div v-if="dlg.mode==='add' && dlg.tab==='custom'" class="wg-form">
          <div class="wg-row"><span>卡片名称</span><input v-model="dlg.form.title" placeholder="如：环境温度"></div>
          <div class="wg-row"><span>接口 URL</span><input v-model="dlg.form.url" placeholder="留空读本机实时快照；或填完整 GET 接口"></div>
          <div class="wg-row"><span>取值路径</span><input v-model="dlg.form.path" placeholder="如 value / data.temperature / tank.tanks.heater"></div>
          <div class="wg-row">
            <span>显示类型</span>
            <select v-model="dlg.form.type">
              <option value="value">数值卡</option>
              <option value="spark">数值+迷你曲线</option>
              <option value="line">趋势曲线</option>
              <option value="state">状态卡</option>
              <option value="tank">单水槽</option>
              <option value="control">控制开关</option>
            </select>
            <span style="width:auto;">轮询</span>
            <input type="number" v-model.number="dlg.form.period" min="2" max="300" style="max-width:64px;">
            <span style="width:auto;">秒</span>
          </div>
          <div class="wg-row" v-if="['value','spark','line','tank'].includes(dlg.form.type)">
            <span>单位</span><input v-model="dlg.form.unit" style="max-width:90px;">
            <span style="width:auto;">小数位</span>
            <input type="number" v-model.number="dlg.form.decimals" min="0" max="4" style="max-width:64px;">
            <span style="width:auto;">颜色</span>
            <input type="color" v-model="dlg.form.color" class="wg-color">
          </div>
          <div class="wg-row" v-if="dlg.form.type === 'control'">
            <span>开指令</span><input v-model="dlg.form.cmdOn" placeholder="开 = GET URL，留空用内置水泵通道">
          </div>
          <div class="wg-row" v-if="dlg.form.type === 'control'">
            <span>关指令</span><input v-model="dlg.form.cmdOff" placeholder="关 = GET URL，留空用内置水泵通道">
          </div>
          <div class="wg-row" v-if="['value','spark','line'].includes(dlg.form.type)">
            <span>告警阈值</span>
            <input type="number" v-model="dlg.form.min" placeholder="下限(可空)">
            <span style="width:auto;">~</span>
            <input type="number" v-model="dlg.form.max" placeholder="上限(可空)">
          </div>
          <div class="wg-row">
            <button class="btn-ghost" :disabled="dlg.previewBusy" @click="previewCustom">{{ dlg.previewBusy ? '请求中…' : '预览取值' }}</button>
            <span class="wg-preview" v-if="dlg.previewText">{{ dlg.previewText }}</span>
          </div>
          <div class="wg-raw" v-if="dlg.previewRaw" :title="dlg.previewRaw">原始返回：{{ dlg.previewRaw }}…</div>
          <div class="wg-note">接口 URL 留空读本机实时快照；填了则经后端代理 GET 代发（仅白名单主机，轮询 ≥2 秒）。控制开关卡的指令 URL 同样走代理并写入操作日志。</div>
          <div class="wg-actions">
            <button class="btn-primary" @click="confirmCustom">添加卡片</button>
          </div>
        </div>

        <!-- 已有卡片配置：接口 URL 留空读本机实时快照，填了走自定义接口 -->
        <div v-if="dlg.mode==='config'" class="wg-form">
          <div class="wg-row"><span>卡片名称</span><input v-model="dlg.form.title"></div>
          <template v-if="isSourceEditable(dlg.form.type)">
            <div class="wg-row">
              <span>显示类型</span>
              <select v-model="dlg.form.type">
                <option value="value">数值卡</option>
                <option value="spark">数值+迷你曲线</option>
                <option value="line">趋势曲线</option>
                <option value="state" v-if="dlg.form.type==='state'">状态卡</option>
                <option value="tank" v-if="dlg.form.type==='tank'">单水槽</option>
                <option value="control" v-if="dlg.form.type==='control'">控制开关</option>
              </select>
              <span style="width:auto;">颜色</span>
              <input type="color" v-model="dlg.form.color" class="wg-color">
            </div>
            <div class="wg-row" v-if="dlg.form.type !== 'control'">
              <span>单位</span><input v-model="dlg.form.unit" style="max-width:90px;">
              <span style="width:auto;">小数位</span>
              <input type="number" v-model.number="dlg.form.decimals" min="0" max="4" style="max-width:64px;">
            </div>
            <div class="wg-row">
              <span>接口 URL</span>
              <input v-model="dlg.form.url" placeholder="留空读本机实时快照；或填完整 GET 接口地址">
            </div>
            <div class="wg-row">
              <span>取值路径</span><input v-model="dlg.form.path" placeholder="如 flow_rate 或 data.value">
              <span style="width:auto;">轮询</span>
              <input type="number" v-model.number="dlg.form.period" min="2" max="300" style="max-width:60px;">
              <span style="width:auto;">秒</span>
            </div>
            <template v-if="dlg.form.type === 'control'">
              <div class="wg-row"><span>开指令</span><input v-model="dlg.form.cmdOn" placeholder="开 = GET URL，留空用内置通道"></div>
              <div class="wg-row"><span>关指令</span><input v-model="dlg.form.cmdOff" placeholder="关 = GET URL，留空用内置通道"></div>
              <div class="wg-note">指令经后端代理 GET 下发（仅白名单主机）并写入操作日志；两条都留空则使用内置水泵/加热通道。</div>
            </template>
          </template>
          <div class="wg-row" v-if="['value','spark','line'].includes(dlg.form.type) && !((widgets.find(x=>x.id===dlg.target)||{}).thresholds||{}).minKey">
            <span>告警阈值</span>
            <input type="number" v-model="dlg.form.min" placeholder="下限(可空)">
            <span style="width:auto;">~</span>
            <input type="number" v-model="dlg.form.max" placeholder="上限(可空)">
          </div>
          <div class="wg-row" v-else-if="['value','spark','line'].includes(dlg.form.type)">
            <span>告警阈值</span>
            <span class="wg-note" style="flex:1;">由「系统配置 → 告警阈值」统一维护，此处仅展示</span>
          </div>
          <div class="wg-row"><span>底部说明</span><input v-model="dlg.form.foot" placeholder="自定义底部说明文字(可空)"></div>
          <div class="wg-actions">
            <button class="btn-primary" @click="saveConfig">保存</button>
          </div>
        </div>
      </div>
    </div>
  </div>
  `,
};
