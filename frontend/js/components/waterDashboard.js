/* 综合面板：三栏驾驶舱（KPI / 双水槽循环回路 / 操作）+ 历史、统计、告警、日志。
 *
 * 数据来源为现场 ESP32 采集端（纯真实模式），前端按 realtime.features 展示设备实际通道：
 *   支持 —— 瞬时流量、累计水量、水泵、定量目标、水温×2、水压、加热模块、光照、加热槽液位
 *   未接入 —— 储水槽液位（无传感器，水位恒显示 0 并标注「无传感器」）
 * 无数据一律显示 0 或 --，不做任何模拟。
 */

window.ViewWaterDash = {
  name: "WaterDashView",
  components: { LoopViz: window.WaterLoopViz },
  props: { realtime: { type: Object, required: true } },
  data() {
    return {
      tab: "monitor",
      thresholds: {},
      pumpBusy: false,
      heaterBusy: false,
      targetInput: 0,
      targetBusy: false,
      resetBusy: false,
      deviceInfo: {},
      // 恒温闭环(PID)
      pid: { target_temp: 42, pid_enabled: false, kp: 16, ki: 0.3, kd: 25 },
      pidBusy: false,
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
      moreOpen: false,        // 「更多操作」折叠区
      recentLogs: [],         // 驾驶舱右侧「最近操作」
      tick: 0,                // 2s 定时器计数，用于低频刷新
      // 实时趋势缓冲
      trend: { flow: [], total: [] },
      timer: null,
    };
  },
  computed: {
    online() { return !!this.realtime.sensor_online; },
    features() { return this.realtime.features || {}; },
    device() { return this.realtime.device || {}; },
    pumpOn() { return this.realtime.pump_state === "on"; },
    pumpUnknown() { return this.realtime.pump_state === "unknown"; },
    heaterOn() { return this.realtime.heater_state === "on"; },
    heaterUnknown() { return this.realtime.heater_state !== "on" && this.realtime.heater_state !== "off"; },
    pidSupported() { return !!this.realtime.pid_supported; },
    // 设备不支持的通道（用于界面提示）
    missingChannels() {
      const f = this.features;
      const names = [];
      if (!f.temperature) names.push("水温");
      if (!f.pressure) names.push("水压");
      if (!f.heater) names.push("加热模块");
      return names;
    },
    flowText() {
      const v = this.realtime.flow_rate;
      return v === null || v === undefined ? "--" : Number(v).toFixed(2);
    },
    totalText() {
      const v = this.realtime.total_liters;
      return v === null || v === undefined ? "--" : Number(v).toFixed(3);
    },
    storageTempText() {
      const v = this.realtime.storage_temp;
      return v === null || v === undefined ? "--" : Number(v).toFixed(1);
    },
    heaterTempText() {
      const v = this.realtime.heater_temp;
      return v === null || v === undefined ? "--" : Number(v).toFixed(1);
    },
    pressureText() {
      const v = this.realtime.pressure;
      return v === null || v === undefined ? "--" : Number(v).toFixed(1);
    },
    lightText() {
      const v = this.realtime.light;
      return v === null || v === undefined ? "--" : Number(v).toFixed(1);
    },
    targetText() {
      const v = Number(this.realtime.pump_target || 0);
      return v > 0 ? `${v.toFixed(2)} L` : "未设置";
    },
    uptimeText() {
      const ms = this.device.uptime_ms != null ? this.device.uptime_ms : this.deviceInfo.uptime_ms;
      if (ms == null) return "--";
      const s = Math.floor(ms / 1000);
      const h = Math.floor(s / 3600);
      const m = Math.floor((s % 3600) / 60);
      return h > 0 ? `${h} 小时 ${m} 分` : `${m} 分 ${s % 60} 秒`;
    },
    rssiText() {
      const v = this.device.rssi != null ? this.device.rssi : this.deviceInfo.rssi;
      return v == null ? "--" : `${v} dBm`;
    },
    ipText() {
      return this.device.ip || this.deviceInfo.ip || this.deviceInfo.url || "--";
    },
    alarmPages() { return Math.max(1, Math.ceil(this.alarmTotal / this.alarmPageSize)); },
    logPages() { return Math.max(1, Math.ceil(this.logTotal / this.logPageSize)); },

    // ---------- 双水槽水位 ----------
    tankUnavailable() { return !!(this.realtime.tank || {}).any_unavailable; },
    flowActive() { return Number(this.realtime.flow_rate || 0) > 0.1; },
    // 定量浇水进度：本次已注入量 / 目标水量
    targetProgress() {
      const target = Number(this.realtime.pump_target || 0);
      if (!(target > 0)) return null;
      const total = Number(this.realtime.total_liters || 0);
      const base = Number(this.realtime.target_baseline ?? 0);
      // 设备完成一次定量后会把累计值清零，此时回退为按当前累计值计算
      const done = total >= base ? total - base : total;
      return {
        target,
        done: Math.max(0, done),
        percent: Math.max(0, Math.min(100, (done / target) * 100)),
      };
    },
  },
  watch: {
    realtime: {
      handler() {
        this.pushTrend();
        this.renderGauges();
        this.renderSparks();
        this.syncTargetInput();
      },
      deep: true,
    },
  },
  mounted() {
    this.syncTargetInput();
    this.loadAll();
    this.refreshDevice();
    // 2 秒兜底刷新：兼顾隐藏 tab 切回时的图表重绘
    this.timer = setInterval(() => {
      this.tick += 1;
      this.pushTrend();
      this.refreshGauges();
      this.refreshSparks();
      this.refreshDevice();
      // 「最近操作」无需 2s 一刷，10s 一次即可
      if (this.tick % 5 === 0) this.fetchRecentLogs();
    }, 2000);
  },
  beforeUnmount() {
    if (this.timer) clearInterval(this.timer);
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
    /* 目标输入框只在用户未聚焦时同步，避免覆盖正在输入的内容 */
    syncTargetInput() {
      const el = document.getElementById("pump-target-input");
      if (el && document.activeElement === el) return;
      const v = Number(this.realtime.pump_target || 0);
      if (this.targetInput !== v) this.targetInput = v;
    },
    pushTrend() {
      const push = (arr, v) => { arr.push(v === null || v === undefined ? 0 : Number(v)); if (arr.length > 60) arr.shift(); };
      push(this.trend.flow, this.realtime.flow_rate);
      push(this.trend.total, this.realtime.total_liters);
    },
    async loadAll() {
      try {
        const cfg = await API.alarmConfigGet();
        this.thresholds = cfg.thresholds || {};
      } catch (e) { /* silent */ }
      if (this.perm("cfg_alarm")) {
        try { this.pid = Object.assign(this.pid, await API.pidGet()); } catch (e) { /* silent */ }
      }
      this.fetchHistory("1h");
      this.fetchStats("1h");
      this.fetchAlarms(1);
      this.fetchLogs(1);
      this.fetchRecentLogs();
    },
    async refreshDevice() {
      try {
        const d = await API.device();
        this.deviceInfo = d.device || {};
        if (d.features && this.realtime && !this.realtime.features) this.realtime.features = d.features;
      } catch (e) { /* silent */ }
    },
    // ---------- 水泵控制 ----------
    async onPump(e) {
      const action = e.target.checked ? "on" : "off";
      if (!this.online) { e.target.checked = this.pumpOn; alert("设备离线，无法控制水泵"); return; }
      this.pumpBusy = true;
      try {
        const d = await API.pump(action);
        Object.assign(this.realtime, d);
      } catch (err) {
        e.target.checked = this.pumpOn;
        alert(err.message);
      } finally { this.pumpBusy = false; }
    },
    // ---------- 加热模块控制 ----------
    async onHeater(e) {
      const action = e.target.checked ? "on" : "off";
      if (!this.online) { e.target.checked = this.heaterOn; alert("设备离线，无法控制加热模块"); return; }
      this.heaterBusy = true;
      try {
        const d = await API.heater(action);
        Object.assign(this.realtime, d);
      } catch (err) {
        e.target.checked = this.heaterOn;
        alert(err.message);
      } finally { this.heaterBusy = false; }
    },
    // ---------- 恒温闭环(PID) ----------
    async savePidTarget() {
      const v = Number(this.pid.target_temp);
      if (isNaN(v) || v < 0 || v > 90) { alert("请输入 0~90 之间的目标温度(℃)"); return; }
      this.pidBusy = true;
      try {
        const d = await API.setTarget(v);
        this.pid.target_temp = d.target_temp;
        alert(`目标温度已设为 ${d.target_temp} ℃`);
      } catch (e) { alert(e.message); }
      finally { this.pidBusy = false; }
    },
    async togglePid(e) {
      this.pidBusy = true;
      try {
        const d = await API.pidMode(e.target.checked ? 1 : 0);
        this.pid.pid_enabled = d.pid_enabled;
      } catch (err) {
        e.target.checked = this.pid.pid_enabled;
        alert(err.message);
      } finally { this.pidBusy = false; }
    },
    async savePidParams() {
      const p = { kp: Number(this.pid.kp), ki: Number(this.pid.ki), kd: Number(this.pid.kd) };
      for (const [k, v] of Object.entries(p)) {
        if (isNaN(v) || v < 0) { alert(`请填写有效的 PID 参数 ${k}`); return; }
      }
      this.pidBusy = true;
      try { await API.pidSet(p); alert("PID 参数已保存"); }
      catch (e) { alert(e.message); }
      finally { this.pidBusy = false; }
    },
    // ---------- 定量浇水 ----------
    async saveTarget() {
      const v = parseFloat(this.targetInput);
      if (isNaN(v) || v < 0) { alert("请输入有效的目标水量(L)，填 0 表示取消定量"); return; }
      this.targetBusy = true;
      try {
        const d = await API.pumpTargetSet(v);
        Object.assign(this.realtime, d);
        alert(v > 0 ? `已设定定量浇水 ${v} L，达到后设备自动关泵` : "已取消定量浇水");
      } catch (e) { alert(e.message); }
      finally { this.targetBusy = false; }
    },
    // ---------- 累计水量清零（破坏性） ----------
    async resetVolume() {
      if (!confirm("确定清零累计水量？\n\n该操作会清除设备上的累计值，不可恢复。")) return;
      this.resetBusy = true;
      try {
        const d = await API.volumeReset();
        Object.assign(this.realtime, d);
        this.fetchHistory(this.histRangeKey);
        this.fetchStats(this.histRangeKey);
        alert("累计水量已清零");
      } catch (e) { alert(e.message); }
      finally { this.resetBusy = false; }
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
    /* 取某个水槽的水位信息。模板里带参数调用，因此必须放 methods——
       Vue 的 computed 不接受参数，写成 computed 会导致整个视图渲染失败。 */
    tankOf(key) { return ((this.realtime.tank || {}).tanks || {})[key] || {}; },
    async fetchRecentLogs() {
      try {
        const d = await API.logs({ page: 1, page_size: 4 });
        this.recentLogs = d.items || [];
      } catch (e) { /* silent */ }
    },
    /* 系统自动动作没有操作账号，用来源(source)区分 */
    isSystemLog(l) { return l.source === "auto" || l.source === "judge"; },
    operatorText(l) {
      if (l.source === "auto") return "系统 · 恒温闭环";
      if (l.source === "judge") return "系统 · 判定服务";
      return l.operator || "—";
    },
    actionLabel(a) {
      return {
        "config.alarm": "修改告警阈值",
        "config.period": "修改采集周期",
        "config.tank": "修改水槽容积",
        "account.create": "创建账号",
        "account.password": "重置密码",
        "account.role": "修改角色",
        "account.status": "启用/禁用账号",
        "account.delete": "删除账号",
        "account.roles": "保存权限矩阵",
      }[a] || a;
    },
    typeName(t) {
      return { flow_rate: "水流量", storage_temp: "储水槽温度", heater_temp: "加热槽温度", pressure: "水压", light: "光照" }[t] || t;
    },
    // ---------- 仪表盘与迷你曲线 ----------
    renderGauges() {
      const el = document.getElementById("gauge-flow");
      if (!el) return;
      const v = Number(this.realtime.flow_rate || 0);
      window.Charts.init("gauge-flow", window.Charts.gaugeOption(Math.min(v, 20), 0, 20, "L/min", "#38bdf8"), false);
    },
    refreshGauges() {
      const v = Number(this.realtime.flow_rate || 0);
      window.Charts.set("gauge-flow", window.Charts.gaugeOption(Math.min(v, 20), 0, 20, "L/min", "#38bdf8"));
    },
    renderSparks() {
      const defs = [
        ["spark-flow", "flow", "#38bdf8"],
        ["spark-total", "total", "#2dd4bf"],
      ];
      defs.forEach(([id, key, color]) => {
        const data = this.trend[key];
        if (!Array.isArray(data) || data.length < 2) return;
        window.Charts.init(id, {
          grid: { left: 2, right: 2, top: 5, bottom: 2 },
          xAxis: { type: "category", show: false, data: data.map((_, i) => i) },
          yAxis: { type: "value", show: false, min: "dataMin", max: "dataMax" },
          series: [{ type: "line", data, showSymbol: false, smooth: true, lineStyle: { width: 1.5, color }, areaStyle: { color, opacity: 0.12 } }],
        });
      });
    },
    refreshSparks() {
      const defs = [["spark-flow", "flow"], ["spark-total", "total"]];
      defs.forEach(([id, key]) => {
        const data = this.trend[key];
        if (Array.isArray(data) && data.length >= 2) window.Charts.set(id, { series: [{ data }] });
      });
    },
  },
  template: `
  <div class="view-page">
    <div class="detail-head">
      <h2>水循环综合监控</h2>
      <span class="desc">瞬时流量 · 累计水量 · 水温 · 水压 · 水泵控制 · 定量浇水 · 恒温闭环</span>
      <span class="lamp-dot" :class="online ? 'on' : 'off'"></span>
      <span class="desc">{{ online ? '设备在线' : '设备离线' }}</span>
      <div class="detail-controls" v-if="perm('ctrl_light') && features.pump">
        <label class="toggle" :class="{ on: pumpOn }">
          <input type="checkbox" :checked="pumpOn" :disabled="pumpBusy || !online" @change="onPump">
          <span class="toggle-track"><span class="toggle-thumb"></span></span>
        </label>
        <span class="toggle-state">{{ pumpUnknown ? '水泵状态未知' : (pumpOn ? '水泵已开' : '水泵已关') }}</span>
      </div>
      <span v-else-if="!features.pump" class="toggle-state" style="color:var(--text-dim);">该设备不支持水泵控制</span>
      <span v-else class="toggle-state" style="color:var(--text-dim);">无控制权限</span>
      <div class="detail-controls" v-if="perm('ctrl_light') && features.heater">
        <label class="toggle" :class="{ on: heaterOn }">
          <input type="checkbox" :checked="heaterOn" :disabled="heaterBusy || !online" @change="onHeater">
          <span class="toggle-track"><span class="toggle-thumb"></span></span>
        </label>
        <span class="toggle-state">{{ heaterUnknown ? '加热状态未知' : (heaterOn ? '加热已开' : '加热已关') }}</span>
      </div>
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

    <!-- 实时监控：三栏驾驶舱 -->
    <div v-show="tab === 'monitor'" class="cockpit">
      <!-- 左栏：关键指标 -->
      <aside class="ck-kpi">
        <div class="kpi-card">
          <div class="kpi-label">瞬时流量</div>
          <div class="kpi-value">{{ flowText }}<span class="kpi-unit">L/min</span></div>
          <div class="kpi-spark" id="spark-flow"></div>
        </div>

        <div class="kpi-card">
          <div class="kpi-label">累计水量</div>
          <div class="kpi-value">{{ totalText }}<span class="kpi-unit">L</span></div>
          <div class="kpi-spark" id="spark-total"></div>
        </div>

        <!-- 新增通道小卡：双列紧凑区，控制左栏高度保证首屏零滚动 -->
        <!-- 水泵状态不设 KPI 卡：顶部开关文字与回路图水泵节点已充分表达 -->
        <div class="kpi-duo">
          <div class="kpi-card" v-if="features.temperature">
            <div class="kpi-label">储水槽水温</div>
            <div class="kpi-value">{{ storageTempText }}<span class="kpi-unit">℃</span></div>
            <div class="kpi-foot">阈值 {{ thresholds.storage_temp_min ?? '--' }} ~ {{ thresholds.storage_temp_max ?? '--' }} ℃</div>
          </div>

          <div class="kpi-card" v-if="features.temperature">
            <div class="kpi-label">加热槽水温</div>
            <div class="kpi-value">{{ heaterTempText }}<span class="kpi-unit">℃</span></div>
            <div class="kpi-foot">阈值 {{ thresholds.heater_temp_min ?? '--' }} ~ {{ thresholds.heater_temp_max ?? '--' }} ℃</div>
          </div>

          <div class="kpi-card" v-if="features.pressure">
            <div class="kpi-label">水压</div>
            <div class="kpi-value">{{ pressureText }}<span class="kpi-unit">kPa</span></div>
            <div class="kpi-foot">阈值 {{ thresholds.pressure_min ?? '--' }} ~ {{ thresholds.pressure_max ?? '--' }} kPa</div>
          </div>

          <div class="kpi-card kpi-wide" v-if="features.light">
            <div class="kpi-label">光照</div>
            <div class="kpi-value">{{ lightText }}<span class="kpi-unit">lx</span></div>
            <div class="kpi-foot">阈值 {{ thresholds.light_min ?? '--' }} ~ {{ thresholds.light_max ?? '--' }} lx</div>
          </div>
        </div>

        <div class="kpi-card">
          <div class="kpi-label">采集设备<span class="dot" :class="online ? 'green' : 'red'"></span></div>
          <div class="kpi-kv"><span>地址</span><b>{{ ipText }}</b></div>
          <div class="kpi-kv"><span>信号</span><b>{{ rssiText }}</b></div>
          <div class="kpi-kv"><span>运行</span><b>{{ uptimeText }}</b></div>
          <div class="kpi-foot kpi-warn" v-if="missingChannels.length"
               :title="'当前固件未提供：' + missingChannels.join('、') + '。升级固件后在 backend/config.py 的 DEVICE_FEATURES 中开启对应通道。'">
            未提供通道：{{ missingChannels.join('、') }}
          </div>
        </div>
      </aside>

      <!-- 中栏：双水槽循环回路（视觉主体） -->
      <section class="ck-loop">
        <LoopViz :storage="tankOf('storage')" :heater="tankOf('heater')"
                 :flowing="flowActive" :pump-on="pumpOn" :flow-text="flowText" :online="online"/>
        <div class="loop-caption">
          <span class="badge" :class="(online && !tankUnavailable) ? 'ok' : 'warn'">{{
            !online ? '设备离线' : (tankUnavailable ? '液位不完整' : '液位实测') }}</span>
          <span class="desc">{{ tankUnavailable
            ? '储水槽未接液位传感器，水位显示 0；加热槽为超声波实测。无数据一律显示 0，不做模拟'
            : '两槽水位来自传感器实测值' }}</span>
        </div>
      </section>

      <!-- 右栏：操作 -->
      <aside class="ck-ops">
        <div class="op-card" v-if="pidSupported && perm('cfg_alarm')">
          <div class="op-title">恒温闭环 (PID)
            <label class="toggle" :class="{ on: pid.pid_enabled }" style="float:right;">
              <input type="checkbox" :checked="pid.pid_enabled" :disabled="pidBusy || !online" @change="togglePid">
              <span class="toggle-track"><span class="toggle-thumb"></span></span>
            </label>
          </div>
          <div class="op-input">
            <input type="number" v-model.number="pid.target_temp" min="0" max="90" step="0.5" style="width:80px;">
            <span class="op-unit">℃ 目标</span>
            <button class="btn-primary" :disabled="pidBusy || !online" @click="savePidTarget">设定</button>
          </div>
          <div class="op-input" style="margin-top:8px;gap:6px;">
            <input type="number" v-model.number="pid.kp" min="0" step="0.1" style="width:56px;" title="比例系数 Kp">
            <input type="number" v-model.number="pid.ki" min="0" step="0.1" style="width:56px;" title="积分系数 Ki">
            <input type="number" v-model.number="pid.kd" min="0" step="0.1" style="width:56px;" title="微分系数 Kd">
            <button class="btn-ghost" :disabled="pidBusy || !online" @click="savePidParams">存参数</button>
          </div>
          <div class="op-foot">{{ pid.pid_enabled ? '闭环运行中：按加热槽水温自动开关加热' : '闭环已关闭；开启后按加热槽水温自动开关加热(目标±1℃)' }}</div>
        </div>

        <div class="op-card" v-if="perm('ctrl_light') && features.pump_target">
          <div class="op-title">定量浇水</div>
          <div class="op-input">
            <input id="pump-target-input" type="number" v-model.number="targetInput" min="0" step="0.1">
            <span class="op-unit">L</span>
            <button class="btn-primary" :disabled="targetBusy || !online" @click="saveTarget">设定</button>
            <button class="btn-ghost" :disabled="targetBusy || !online" @click="targetInput = 0; saveTarget()">取消</button>
          </div>
          <div class="target-progress" v-if="targetProgress">
            <div class="tp-head">
              <span>已注入 {{ targetProgress.done.toFixed(3) }} / {{ targetProgress.target.toFixed(2) }} L</span>
              <b>{{ targetProgress.percent.toFixed(1) }}%</b>
            </div>
            <div class="tp-bar"><span :style="{ width: targetProgress.percent + '%' }"></span></div>
          </div>
          <div class="op-foot">达到目标由设备固件自动关泵；完成后设备会清零累计水量并取消目标</div>
        </div>

        <div class="op-card" v-if="perm('view_log')">
          <div class="op-title">最近操作</div>
          <ul class="op-log">
            <li v-for="(l, i) in recentLogs" :key="i">
              <span class="op-time">{{ (l.ts || '').slice(11, 16) }}</span>
              <span class="op-who" :class="{ sys: isSystemLog(l) }">{{ operatorText(l) }}</span>
              <span class="op-what">{{ actionLabel(l.action) }}</span>
            </li>
            <li v-if="!recentLogs.length" class="op-empty">暂无记录</li>
          </ul>
        </div>

        <div class="op-card op-more" v-if="perm('ctrl_light')">
          <button class="op-more-btn" @click="moreOpen = !moreOpen">
            更多操作<span class="op-caret" :class="{ open: moreOpen }">▾</span>
          </button>
          <div v-show="moreOpen" class="op-more-body">
            <button class="btn-ghost danger" :disabled="resetBusy || !online || !features.volume_reset"
                    @click="resetVolume">清零累计水量</button>
            <div class="op-foot">清除设备上的累计值，不可恢复，执行前二次确认</div>
          </div>
        </div>
      </aside>
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
  </div>
  `,
};
