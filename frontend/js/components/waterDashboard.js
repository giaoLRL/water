/* 综合面板：实时监控(2路温度+流量+压力)、泵/加热控制、恒温PID、历史曲线、统计、告警、日志 */
window.ViewWaterDash = {
  name: "WaterDashView",
  props: { realtime: { type: Object, required: true } },
  data() {
    return {
      tab: "monitor",
      thresholds: {},
      target: 42,
      pidEnabled: 0,
      pumpBusy: false,
      heaterBusy: false,
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
      // 实时趋势缓冲
      trend: { storage: [], heater: [], flow: [], pressure: [] },
      timer: null,
    };
  },
  computed: {
    alarmPages() { return Math.max(1, Math.ceil(this.alarmTotal / this.alarmPageSize)); },
    logPages() { return Math.max(1, Math.ceil(this.logTotal / this.logPageSize)); },
  },
  watch: {
    realtime: {
      handler(d) {
        this.pushTrend();
        this.renderGauges();
        this.renderSparks();
      },
      deep: true,
    },
  },
  mounted() {
    this.target = this.realtime.target_temp ?? 42;
    this.pidEnabled = this.realtime.pid_enabled ? 1 : 0;
    this.loadAll();
    this.timer = setInterval(() => {
      this.pushTrend();
      this.refreshGauges();
      this.refreshSparks();
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
    pushTrend() {
      const r = this.realtime;
      const push = (arr, v) => { arr.push(v == null ? 0 : v); if (arr.length > 60) arr.shift(); };
      push(this.trend.storage, r.storage_temp);
      push(this.trend.heater, r.heater_temp);
      push(this.trend.flow, r.flow_rate);
      push(this.trend.pressure, r.pressure);
    },
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
    // ---------- 控制 ----------
    async onPump(e) {
      const target = e.target.checked ? "on" : "off";
      if (this.realtime.pump_state === target) { e.target.checked = this.realtime.pump_state === "on"; return; }
      this.pumpBusy = true;
      try {
        const d = await API.pump(target);
        Object.assign(this.realtime, d);
      } catch (err) {
        e.target.checked = this.realtime.pump_state === "on";
        alert(err.message);
      } finally { this.pumpBusy = false; }
    },
    async onHeater(e) {
      const target = e.target.checked ? "on" : "off";
      if (this.realtime.heater_state === target) { e.target.checked = this.realtime.heater_state === "on"; return; }
      this.heaterBusy = true;
      try {
        const d = await API.heater(target);
        Object.assign(this.realtime, d);
      } catch (err) {
        e.target.checked = this.realtime.heater_state === "on";
        alert(err.message);
      } finally { this.heaterBusy = false; }
    },
    async saveTarget() {
      const t = parseFloat(this.target);
      if (isNaN(t)) { alert("请输入有效的目标温度"); return; }
      try {
        const d = await API.setTarget(t);
        this.realtime.target_temp = d.target_temp;
        alert("目标温度已设定");
      } catch (e) { alert(e.message); }
    },
    async togglePid(e) {
      const target = e.target.checked ? 1 : 0;
      this.pidBusy = true;
      try {
        await API.pidMode(target);
        this.pidEnabled = target;
        this.realtime.pid_enabled = !!target;
      } catch (err) { e.target.checked = this.pidEnabled === 1; alert(err.message); }
      finally { this.pidBusy = false; }
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
          temperature: ["储水槽温度", "storage_temp", "#2dd4bf", "℃"],
          heater: ["加热槽温度", "heater_temp", "#f87171", "℃"],
          flow: ["水流量", "flow_rate", "#38bdf8", "L/min"],
          pressure: ["水压", "pressure", "#fbbf24", "kPa"],
        }[this.histType] || ["储水槽温度", "storage_temp", "#2dd4bf", "℃"];
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
          { type: "value", name: "温度℃", ...window.Charts.axisStyle, nameTextStyle: { color: "#7d8b99" } },
          { type: "value", name: "流量/压力", nameTextStyle: { color: "#7d8b99" }, axisLabel: { color: "#7d8b99" }, splitLine: { show: false }, axisLine: { lineStyle: { color: "#2a3442" } } },
        ],
        dataZoom: [
          { type: "inside" },
          { type: "slider", height: 16, bottom: 8, borderColor: "#262f3b", backgroundColor: "#151b24", fillerColor: "rgba(45,212,191,0.14)", handleStyle: { color: "#2dd4bf" }, textStyle: { color: "#7d8b99" } },
        ],
        series: [
          window.Charts.lineSeries("储水槽温度", pts.map((p) => p.storage_temp), "#2dd4bf", 0),
          window.Charts.lineSeries("加热槽温度", pts.map((p) => p.heater_temp), "#f87171", 0),
          window.Charts.lineSeries("水流量", pts.map((p) => p.flow_rate), "#38bdf8", 1),
          window.Charts.lineSeries("水压", pts.map((p) => p.pressure), "#fbbf24", 1),
        ],
      });
    },
    // ---------- 统计 ----------
    async fetchStats(key) {
      const s = this.rangeStart(key || this.histRangeKey);
      const e = this.rangeEnd();
      try { this.stats = await API.stats(s, e); } catch (err) { /* silent */ }
      this.renderAlarmChart();
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
        const d = await API.logs({ page: this.logPage, page_size: this.logPageSize });
        this.logs = d.items || [];
        this.logTotal = d.total || 0;
      } catch (e) { /* silent */ }
    },
    renderAlarmChart() {
      const el = document.getElementById("water-alarm-chart");
      if (!el || !window.echarts) return;
      let data;
      try { data = this.stats; } catch (e) { data = {}; }
      window.Charts.init("water-alarm-chart", {
        ...window.Charts.baseOption(["平均温度", "最高温度", "最低温度"], "℃"),
        series: [window.Charts.barSeries("数值", [
          data.avg_heater_temp || 0, data.max_heater_temp || 0, data.min_heater_temp || 0,
        ], "#2dd4bf")],
      });
    },
    typeName(t) {
      return { storage_temp: "储水槽温度", heater_temp: "加热槽温度", flow_rate: "水流量", pressure: "水压" }[t] || t;
    },
    // ---------- 仪表盘与迷你曲线 ----------
    renderGauges() {
      const r = this.realtime;
      const defs = [
        ["gauge-storage", r.storage_temp || 0, 0, 80, "℃", "#2dd4bf"],
        ["gauge-heater", r.heater_temp || 0, 0, 80, "℃", "#f87171"],
        ["gauge-flow", Math.min(r.flow_rate || 0, 20), 0, 20, "L/min", "#38bdf8"],
        ["gauge-pressure", Math.min(r.pressure || 0, 160), 0, 160, "kPa", "#fbbf24"],
      ];
      defs.forEach(([id, v, min, max, unit, color]) => {
        if (!document.getElementById(id)) return;
        window.Charts.init(id, window.Charts.gaugeOption(v, min, max, unit, color), false);
      });
    },
    refreshGauges() {
      const r = this.realtime;
      const defs = [
        ["gauge-storage", r.storage_temp || 0, 0, 80, "℃", "#2dd4bf"],
        ["gauge-heater", r.heater_temp || 0, 0, 80, "℃", "#f87171"],
        ["gauge-flow", Math.min(r.flow_rate || 0, 20), 0, 20, "L/min", "#38bdf8"],
        ["gauge-pressure", Math.min(r.pressure || 0, 160), 0, 160, "kPa", "#fbbf24"],
      ];
      defs.forEach(([id, v, min, max, unit, color]) => {
        window.Charts.set(id, window.Charts.gaugeOption(v, min, max, unit, color));
      });
    },
    renderSparks() {
      const defs = [
        ["spark-storage", "storage", "#2dd4bf"],
        ["spark-heater", "heater", "#f87171"],
        ["spark-flow", "flow", "#38bdf8"],
        ["spark-pressure", "pressure", "#fbbf24"],
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
      const defs = [["spark-storage", "storage"], ["spark-heater", "heater"], ["spark-flow", "flow"], ["spark-pressure", "pressure"]];
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
      <span class="desc">2 路温度 · 流量 · 压力 · 水泵 · 加热模块</span>
      <span class="lamp-dot on" style="margin-left:auto;"></span>
      <div class="detail-controls" v-if="perm('ctrl_light')">
        <label class="toggle" :class="{ on: realtime.pump_state === 'on' }">
          <input type="checkbox" :checked="realtime.pump_state === 'on'" :disabled="pumpBusy" @change="onPump">
          <span class="toggle-track"><span class="toggle-thumb"></span></span>
        </label>
        <span class="toggle-state">{{ realtime.pump_state === 'on' ? '水泵已开' : '水泵已关' }}</span>
        <label class="toggle" :class="{ on: realtime.heater_state === 'on' }">
          <input type="checkbox" :checked="realtime.heater_state === 'on'" :disabled="heaterBusy" @change="onHeater">
          <span class="toggle-track"><span class="toggle-thumb"></span></span>
        </label>
        <span class="toggle-state">{{ realtime.heater_state === 'on' ? '加热已开' : '加热已关' }}</span>
      </div>
      <span v-else class="toggle-state" style="color:var(--text-dim);">无控制权限</span>
    </div>

    <div class="tabs">
      <span class="tab" :class="{ active: tab === 'monitor' }" @click="tab='monitor'">实时监控</span>
      <span class="tab" :class="{ active: tab === 'history' }" @click="tab='history'; $nextTick(()=>renderChart())">历史数据</span>
      <span class="tab" :class="{ active: tab === 'stats' }" @click="tab='stats'; fetchStats('1h')">数据统计</span>
      <span class="tab" v-if="perm('view_alarm')" :class="{ active: tab === 'alarm' }" @click="tab='alarm'; fetchAlarms(1)">告警记录</span>
      <span class="tab" v-if="perm('view_log')" :class="{ active: tab === 'logs' }" @click="tab='logs'; fetchLogs(1)">操作日志</span>
    </div>

    <!-- 实时监控 -->
    <div v-show="tab === 'monitor'">
      <div class="metrics-grid" style="margin-bottom:14px;">
        <div class="metric gauge-box">
          <div class="label">储水槽温度 <span class="desc">阈值 {{ thresholds.storage_temp_min ?? '--' }}~{{ thresholds.storage_temp_max ?? '--' }}℃</span></div>
          <div class="gauge" id="gauge-storage"></div><div class="spark" id="spark-storage"></div>
        </div>
        <div class="metric gauge-box">
          <div class="label">加热槽温度 <span class="desc">阈值 {{ thresholds.heater_temp_min ?? '--' }}~{{ thresholds.heater_temp_max ?? '--' }}℃</span></div>
          <div class="gauge" id="gauge-heater"></div><div class="spark" id="spark-heater"></div>
        </div>
        <div class="metric gauge-box">
          <div class="label">管路水流量 <span class="desc">阈值 {{ thresholds.flow_min ?? '--' }}~{{ thresholds.flow_max ?? '--' }} L/min</span></div>
          <div class="gauge" id="gauge-flow"></div><div class="spark" id="spark-flow"></div>
        </div>
        <div class="metric gauge-box">
          <div class="label">管路水压 <span class="desc">阈值 {{ thresholds.pressure_min ?? '--' }}~{{ thresholds.pressure_max ?? '--' }} kPa</span></div>
          <div class="gauge" id="gauge-pressure"></div><div class="spark" id="spark-pressure"></div>
        </div>
      </div>

      <div class="section" v-if="perm('cfg_alarm')">
        <h3>本地恒温闭环控制 <span class="desc">PID · 目标温度 · 稳态±1℃（任务六）</span></h3>
        <div class="alarm-rule">
          <div class="rule-item"><span>目标温度</span><input type="number" v-model.number="target" min="0" max="90" style="width:90px;"> <span>℃</span></div>
          <button class="btn-ghost" @click="saveTarget">设定目标</button>
          <label class="rule-item"><span>启用恒温(PID)</span>
            <input type="checkbox" :checked="pidEnabled === 1" :disabled="pidBusy" @change="togglePid">
          </label>
          <span class="desc">当前 PID 输出占空比：{{ realtime.pid_duty ?? '--' }}% ｜ 目标 {{ realtime.target_temp ?? '--' }}℃</span>
        </div>
      </div>
      <div v-else class="section"><p class="desc">当前账号无恒温控制权限。</p></div>

      <div class="note">数据每 {{ realtime.period ? '~' : '' }}2 秒自动刷新。水泵开→管路有水流量；加热开→加热槽升温（模拟）。</div>
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
          <span class="tab" :class="{ active: histType==='temperature' }" @click="pickHistType('temperature')">储水槽温度</span>
          <span class="tab" :class="{ active: histType==='heater' }" @click="pickHistType('heater')">加热槽温度</span>
          <span class="tab" :class="{ active: histType==='flow' }" @click="pickHistType('flow')">水流量</span>
          <span class="tab" :class="{ active: histType==='pressure' }" @click="pickHistType('pressure')">水压</span>
        </div>
        <div class="chart" id="water-hist-chart"></div>
      </div>
    </div>

    <!-- 数据统计 -->
    <div v-show="tab === 'stats'">
      <div class="section">
        <h3>数据统计 <span class="desc">加热槽温度曲线汇总（任务六·数据统计分析）</span></h3>
        <div class="chart" id="water-alarm-chart"></div>
      </div>
      <div class="grid-4" style="margin-bottom:14px;">
        <div class="metric"><div class="label">平均储水槽温度</div><div class="value">{{ stats.avg_storage_temp ?? '--' }}<span class="unit">℃</span></div></div>
        <div class="metric"><div class="label">平均加热槽温度</div><div class="value">{{ stats.avg_heater_temp ?? '--' }}<span class="unit">℃</span></div></div>
        <div class="metric"><div class="label">最高/最低压力</div><div class="value">{{ stats.max_pressure ?? '--' }} / {{ stats.min_pressure ?? '--' }}<span class="unit">kPa</span></div></div>
        <div class="metric"><div class="label">累计水流量</div><div class="value">{{ stats.total_flow ?? '--' }}<span class="unit">L</span></div></div>
      </div>
      <div class="grid-4">
        <div class="metric"><div class="label">最高储水槽温度</div><div class="value">{{ stats.max_storage_temp ?? '--' }}<span class="unit">℃</span></div></div>
        <div class="metric"><div class="label">最低储水槽温度</div><div class="value">{{ stats.min_storage_temp ?? '--' }}<span class="unit">℃</span></div></div>
        <div class="metric"><div class="label">最高加热槽温度</div><div class="value">{{ stats.max_heater_temp ?? '--' }}<span class="unit">℃</span></div></div>
        <div class="metric"><div class="label">采样点数</div><div class="value">{{ histPoints.length }}<span class="unit">条</span></div></div>
      </div>
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
        <h3>设备操作日志 <span class="desc">共 {{ logTotal }} 条</span></h3>
        <div style="overflow-x:auto;">
          <table>
            <thead><tr><th>时间</th><th>指令</th><th>结果</th><th>说明</th></tr></thead>
            <tbody>
              <tr v-for="(l,i) in logs" :key="i">
                <td>{{ l.ts }}</td>
                <td>{{ l.action }}</td>
                <td><span class="badge" :class="l.result==='success' ? 'ok' : 'fail'">{{ l.result==='success' ? '成功':'失败' }}</span></td>
                <td>{{ l.detail || '' }}</td>
              </tr>
              <tr v-if="!logs.length"><td colspan="4" style="text-align:center;color:#6b7a90;">暂无操作日志</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
  `,
};