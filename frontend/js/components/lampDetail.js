/* 灯杆详情视图：实时监控 + 历史数据 + 告警 + 人员监测 + 操作日志。 */
window.ViewLampDetail = {
  name: "LampDetailView",
  props: {
    lampId: { type: String, required: true },
    initialTab: { type: String, default: "monitor" },
  },
  emits: ["back"],
  data() {
    return {
      tab: "monitor",
      lamp: {},
      thresholds: {},
      // 历史
      histPoints: [],
      histRangeKey: "1h",
      histType: "all",
      stats: {},
      customStart: "",
      customEnd: "",
      // 实时趋势（仪表盘迷你曲线累积缓冲）
      trend: { temperature: [], humidity: [], luminance: [], smoke: [] },
      // 告警
      alarms: [],
      alarmTotal: 0, alarmPage: 1, alarmPageSize: 10,
      alarmKeyword: "", alarmStart: "", alarmEnd: "",
      alarmDetail: null,     // 单条告警详情（含异常快照图）
      // 人员监测
      detections: [],
      detTotal: 0, detPage: 1, detPageSize: 10,
      detKeyword: "", detStart: "", detEnd: "",
      curDetect: null,       // 持续自动识别最新结果
      ruleEnabled: 1,        // 人数告警规则：是否启用
      ruleMin: 3,            // 人数告警阈值
      detailImages: null,
      detailInfo: null,
      // 日志
      logs: [],
      logTotal: 0, logPage: 1, logPageSize: 10,
      logKeyword: "", logStart: "", logEnd: "",
      // 灯光滑块（请求中禁用，防连点）
      lightBusy: false,
      // 历史明细（前端分页）
      histPage: 1, histPageSize: 10,
      timer: null,
    };
  },
  computed: {
    videoUrl() {
      return API.videoUrl(this.lampId);
    },
    detectVideoUrl() {
      return API.detectVideoUrl(this.lampId);
    },
    sensorTag() {
      if (this.lamp.sensor_source === "esp32") {
        return this.lamp.sensor_online === false ? "传感器离线" : "真实温湿度(ESP32)";
      }
      return "无真实传感器";
    },
    luxTag() {
      if (this.lamp.sensor_source === "esp32") {
        return this.lamp.light_online === false ? "光照离线" : "真实光照(GY-302)";
      }
      return "无真实传感器";
    },
    smokeTag() {
      if (this.lamp.sensor_source === "esp32") {
        return this.lamp.smoke_online === false ? "烟雾离线" : "烟雾浓度(MQ-2)";
      }
      return "无真实传感器";
    },
    alarmPages() { return Math.max(1, Math.ceil(this.alarmTotal / this.alarmPageSize)); },
    detPages() { return Math.max(1, Math.ceil(this.detTotal / this.detPageSize)); },
    logPages() { return Math.max(1, Math.ceil(this.logTotal / this.logPageSize)); },
    histRows() {
      const all = this.histPoints.slice(-200);
      const start = (this.histPage - 1) * this.histPageSize;
      return all.slice(start, start + this.histPageSize).reverse();
    },
    histPageTotal() {
      return Math.max(1, Math.ceil(Math.min(this.histPoints.length, 200) / this.histPageSize));
    },
  },
  mounted() {
    this.tab = this.initialTab || "monitor";
    this.loadAll();
    this.timer = setInterval(() => {
      this.fetchLamp();
      this.fetchDetectCurrent();
    }, 2000);
  },
  beforeUnmount() {
    if (this.timer) clearInterval(this.timer);
  },
  methods: {
    fmt(s) {
      const d = new Date(s);
      const p = (n) => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    },
    fmtSmoke(v) {
      if (v == null) return "--";
      return Math.round(v);
    },
    rangeEnd() { return this.fmt(new Date()); },
    rangeStart(key) {
      const hours = { "1h": 1, "6h": 6, "24h": 24 }[key] || 1;
      return this.fmt(new Date(Date.now() - hours * 3600 * 1000));
    },
    async loadAll() {
      this.fetchLamp();
      this.fetchHistory("1h");
      this.fetchStats("1h");
      this.fetchAlarms();
      this.fetchDetections();
      this.fetchLogs(1);
      this.fetchDetectCurrent();
      try {
        this.thresholds = (await API.alarmConfigGet()).thresholds || {};
        this.applyRuleFromThresholds();
      } catch (e) { /* silent */ }
    },
    async fetchLamp() {
      try {
        this.lamp = await API.lamp(this.lampId);
        const t = this.trend;
        const push = (arr, v) => { arr.push(v); if (arr.length > 60) arr.shift(); };
        push(t.temperature, this.lamp.temperature || 0);
        push(t.humidity, this.lamp.humidity || 0);
        push(t.luminance, this.lamp.luminance || 0);
        push(t.smoke, this.lamp.smoke || 0);
        this.renderSparks();
        this.renderGauges();
      } catch (e) { /* silent */ }
    },
    async fetchHistory(key, start, end) {
      this.histRangeKey = key || this.histRangeKey;
      let s, e;
      if (key === "custom" && start && end) {
        s = String(start).replace("T", " ");
        e = String(end).replace("T", " ");
      } else {
        s = this.rangeStart(this.histRangeKey);
        e = this.rangeEnd();
      }
      try {
        const d = await API.history(this.lampId, s, e);
        this.histPoints = d.points || [];
        if (!this.trend.temperature.length && this.histPoints.length) {
          this.trend.temperature = this.histPoints.map((p) => p.temperature).slice(-60);
          this.trend.humidity = this.histPoints.map((p) => p.humidity).slice(-60);
          this.trend.luminance = this.histPoints.map((p) => p.luminance).slice(-60);
          this.trend.smoke = this.histPoints.map((p) => p.smoke).slice(-60);
        }
        this.renderChart();
      } catch (err) { /* silent */ }
    },
    async fetchStats(key) {
      const s = this.rangeStart(key || this.histRangeKey);
      const e = this.rangeEnd();
      try {
        this.stats = await API.stats(this.lampId, s, e);
      } catch (err) { /* silent */ }
    },
    dateFilter(start, end) {
      // 日历筛选：把日期转成当天起止时间
      const r = {};
      if (start) r.start = start + " 00:00:00";
      if (end) r.end = end + " 23:59:59";
      return r;
    },
    async fetchAlarms(page) {
      if (page) this.alarmPage = page;
      if (this.alarmPage < 1) this.alarmPage = 1;
      try {
        const d = await API.alarms(this.lampId, {
          page: this.alarmPage, page_size: this.alarmPageSize,
          keyword: this.alarmKeyword || undefined,
          ...this.dateFilter(this.alarmStart, this.alarmEnd),
        });
        this.alarms = d.items || [];
        this.alarmTotal = d.total || 0;
        this.renderAlarmChart();
      } catch (e) { /* silent */ }
    },
    resetAlarmFilter() {
      this.alarmKeyword = ""; this.alarmStart = ""; this.alarmEnd = "";
      this.fetchAlarms(1);
    },
    async fetchDetections(page) {
      if (page) this.detPage = page;
      if (this.detPage < 1) this.detPage = 1;
      try {
        const d = await API.detections(this.lampId, {
          page: this.detPage, page_size: this.detPageSize,
          keyword: this.detKeyword || undefined,
          ...this.dateFilter(this.detStart, this.detEnd),
        });
        this.detections = d.items || [];
        this.detTotal = d.total || 0;
        this.renderDetectionChart();
      } catch (e) { /* silent */ }
    },
    resetDetFilter() {
      this.detKeyword = ""; this.detStart = ""; this.detEnd = "";
      this.fetchDetections(1);
    },
    async fetchLogs(page) {
      if (page) this.logPage = page;
      if (this.logPage < 1) this.logPage = 1;
      try {
        const d = await API.logs(this.lampId, {
          page: this.logPage, page_size: this.logPageSize,
          keyword: this.logKeyword || undefined,
          ...this.dateFilter(this.logStart, this.logEnd),
        });
        this.logs = d.items || [];
        this.logTotal = d.total || 0;
      } catch (e) { /* silent */ }
    },
    resetLogFilter() {
      this.logKeyword = ""; this.logStart = ""; this.logEnd = "";
      this.fetchLogs(1);
    },
    async saveEnvRule() {
      const keys = ["temp_max", "temp_min", "humidity_max", "humidity_min", "luminance_max", "luminance_min"];
      const cfg = {};
      for (const k of keys) {
        const v = parseFloat(this.thresholds[k]);
        if (isNaN(v)) { alert(`请填写有效数值: ${k}`); return; }
        cfg[k] = v;
      }
      try {
        this.thresholds = (await API.alarmConfigSet(cfg)).thresholds || {};
        this.applyRuleFromThresholds();
        alert("环境参数阈值已保存");
      } catch (e) {
        alert(e.message);
      }
    },
    setTab(t) {
      this.tab = t;
      if (t === "alarm") this.fetchAlarms(1);
      if (t === "detections") this.fetchDetections(1);
      if (t === "logs") this.fetchLogs(1);
      if (t === "history") { this.histPage = 1; this.fetchHistory(this.histRangeKey); }
      setTimeout(() => window.Charts.resizeAll(), 120);
    },
    async doControl(action) {
      try {
        const d = await API.control(this.lampId, action);
        this.lamp = d;
      } catch (e) {
        alert(e.message);
      }
    },
    async onLightToggle(e) {
      // 滑块目标态：关→开、开→关；与当前态一致的无效操作直接忽略
      const target = e.target.checked ? "on" : "off";
      if (this.lamp.light_state === target) {
        e.target.checked = this.lamp.light_state === "on";
        return;
      }
      this.lightBusy = true;
      try {
        const d = await API.control(this.lampId, target);
        this.lamp = d;
      } catch (err) {
        e.target.checked = this.lamp.light_state === "on";
        alert(err.message);
      } finally {
        this.lightBusy = false;
      }
    },
    async fetchDetectCurrent() {
      try {
        this.curDetect = await API.detectCurrent(this.lampId);
      } catch (e) { /* silent */ }
    },
    applyRuleFromThresholds() {
      const min = parseFloat(this.thresholds.person_alert_min);
      if (!isNaN(min)) this.ruleMin = min;
      this.ruleEnabled = this.thresholds.person_alert_enabled ? 1 : 0;
    },
    async savePersonRule() {
      const min = parseFloat(this.ruleMin);
      if (isNaN(min) || min < 1) { alert("人数阈值必须是 >= 1 的数字"); return; }
      try {
        const cfg = { person_alert_enabled: this.ruleEnabled ? 1 : 0, person_alert_min: min };
        this.thresholds = (await API.alarmConfigSet(cfg)).thresholds || {};
        alert("人数告警规则已保存");
      } catch (e) {
        alert(e.message);
      }
    },
    async viewDetection(id) {
      try {
        const d = await API.detection(id);
        this.detailImages = { original: d.original_image, processed: d.processed_image };
        this.detailInfo = d;
      } catch (e) { /* silent */ }
    },
    async viewAlarm(id) {
      try {
        this.alarmDetail = await API.alarm(id);
      } catch (e) { /* silent */ }
    },
    closeAlarm() {
      this.alarmDetail = null;
    },
    closeDetail() {
      this.detailImages = null;
      this.detailInfo = null;
    },
    pickHistType(t) { this.histType = t; this.renderChart(); },
    renderChart() {
      const el = document.getElementById("detail-hist-chart");
      if (!el || !window.echarts) return;
      const pts = this.histPoints || [];
      const times = pts.map((p) => p.ts);

      if (this.histType === "all") {
        window.Charts.init("detail-hist-chart", {
          tooltip: { trigger: "axis", backgroundColor: "#1a222d", borderColor: "#2a3442", textStyle: { color: "#d7e0ea" } },
          legend: { top: 0, textStyle: { color: "#7d8b99" } },
          grid: { left: 56, right: 58, top: 36, bottom: 66 },
          xAxis: { type: "category", data: times, boundaryGap: false, ...window.Charts.axisStyle },
          yAxis: [
            { type: "value", name: "温度℃ / 湿度%", ...window.Charts.axisStyle, nameTextStyle: { color: "#7d8b99" } },
            { type: "value", name: "光照 lx", nameTextStyle: { color: "#7d8b99" }, axisLabel: { color: "#7d8b99" }, splitLine: { show: false }, axisLine: { lineStyle: { color: "#2a3442" } } },
          ],
          dataZoom: [
            { type: "inside" },
            { type: "slider", height: 16, bottom: 8, borderColor: "#262f3b", backgroundColor: "#151b24", fillerColor: "rgba(45,212,191,0.14)", handleStyle: { color: "#2dd4bf" }, textStyle: { color: "#7d8b99" } },
          ],
          series: [
            window.Charts.lineSeries("环境温度", pts.map((p) => p.temperature), "#2dd4bf", 0),
            window.Charts.lineSeries("空气湿度", pts.map((p) => p.humidity), "#38bdf8", 0),
            window.Charts.lineSeries("光照强度", pts.map((p) => p.luminance), "#fbbf24", 1),
          ],
        });
        return;
      }
      let data, name, color, unit;
      if (this.histType === "humidity") {
        data = pts.map((p) => p.humidity); name = "空气湿度"; color = "#38bdf8"; unit = "%";
      } else if (this.histType === "luminance") {
        data = pts.map((p) => p.luminance); name = "光照强度"; color = "#fbbf24"; unit = "lx";
      } else if (this.histType === "smoke") {
        data = pts.map((p) => p.smoke); name = "烟雾浓度"; color = "#f472b6"; unit = "AO";
      } else {
        data = pts.map((p) => p.temperature); name = "环境温度"; color = "#2dd4bf"; unit = "℃";
      }
      window.Charts.init("detail-hist-chart", {
        ...window.Charts.baseOption(times, unit),
        series: [window.Charts.lineSeries(name, data, color)],
      });
    },
    renderSparks() {
      const defs = [
        ["spark-temp", "temperature", "#2dd4bf"],
        ["spark-hum", "humidity", "#38bdf8"],
        ["spark-lux", "luminance", "#fbbf24"],
      ];
      defs.forEach(([id, key, color]) => {
        const data = this.trend[key];
        if (!Array.isArray(data) || data.length < 2) return;
        window.Charts.init(id, {
          grid: { left: 2, right: 2, top: 5, bottom: 2 },
          xAxis: { type: "category", show: false, data },
          yAxis: { type: "value", show: false, min: "dataMin", max: "dataMax" },
          series: [{
            type: "line", data, showSymbol: false, smooth: true,
            lineStyle: { width: 1.5, color },
            areaStyle: { color, opacity: 0.12 },
          }],
        });
      });
    },
    renderGauges() {
      const defs = [
        ["gauge-temp", this.lamp.temperature || 0, -10, 60, "℃", "#2dd4bf"],
        ["gauge-hum", this.lamp.humidity || 0, 0, 100, "%", "#38bdf8"],
        ["gauge-lux", Math.min(this.lamp.luminance || 0, 100000), 0, 100000, "lx", "#fbbf24"],
      ];
      defs.forEach(([id, v, min, max, unit, color]) => {
        if (!document.getElementById(id)) return;
        window.Charts.init(id, window.Charts.gaugeOption(v, min, max, unit, color), false);
      });
    },
    renderDetectionChart() {
      const els = document.getElementById("detect-stat-chart");
      if (!els) return;
      const rows = (this.detections || []).slice(0, 20).reverse();
      window.Charts.init("detect-stat-chart", {
        ...window.Charts.barOption(rows.map((d) => (d.ts || "").slice(11, 19)), "人数"),
        series: [window.Charts.barSeries("检出人数", rows.map((d) => d.person_count || 0), "#2dd4bf")],
      });
    },
    renderAlarmChart() {
      const els = document.getElementById("alarm-stat-chart");
      if (!els) return;
      const agg = {};
      (this.alarms || []).forEach((a) => {
        const name = this.typeName(a.type) || a.type;
        agg[name] = (agg[name] || 0) + 1;
      });
      const names = Object.keys(agg);
      window.Charts.init("alarm-stat-chart", {
        ...window.Charts.barOption(names, "次数"),
        series: [window.Charts.barSeries("告警次数", names.map((n) => agg[n]), "#f87171")],
      });
    },
    typeName(t) {
      return { temperature: "环境温度", humidity: "空气湿度", luminance: "光照强度", person: "人员数量", smoke: "烟雾浓度" }[t] || t;
    },
  },
  template: `
    <div class="view-page">
      <div class="detail-head">
        <button class="btn-ghost" @click="$emit('back')">← 返回列表</button>
        <h2>{{ lamp.name || '灯杆' + lampId }}</h2>
        <span class="desc">{{ lamp.location }}</span>
        <span class="lamp-dot" :class="lamp.light_state === 'on' ? 'on' : 'off'"></span>
        <div class="detail-controls">
          <!-- 灯光滑块：关→只能开，开→只能关 -->
          <label class="toggle" :class="{ on: lamp.light_state === 'on' }">
            <input type="checkbox" :checked="lamp.light_state === 'on'" :disabled="lightBusy" @change="onLightToggle">
            <span class="toggle-track"><span class="toggle-thumb"></span></span>
          </label>
          <span class="toggle-state">{{ lamp.light_state === 'on' ? '灯光已开' : '灯光已关' }}</span>
        </div>
      </div>

      <div class="tabs">
        <span class="tab" :class="{ active: tab === 'monitor' }" @click="setTab('monitor')">实时监控</span>
        <span class="tab" :class="{ active: tab === 'history' }" @click="setTab('history')">历史数据</span>
        <span class="tab" :class="{ active: tab === 'alarm' }" @click="setTab('alarm')">告警记录</span>
        <span class="tab" :class="{ active: tab === 'detections' }" @click="setTab('detections')">人员监测</span>
        <span class="tab" :class="{ active: tab === 'logs' }" @click="setTab('logs')">操作日志</span>
      </div>

      <!-- 实时监控 -->
      <div v-show="tab === 'monitor'">
        <div class="grid-4" style="margin-bottom:14px;">
          <div class="metric gauge-box">
            <div class="label">环境温度 <span class="desc">{{ sensorTag }} · 阈值 {{ thresholds.temp_min ?? '--' }}~{{ thresholds.temp_max ?? '--' }}℃</span></div>
            <div class="gauge" id="gauge-temp"></div>
            <div class="spark" id="spark-temp"></div>
          </div>
          <div class="metric gauge-box">
            <div class="label">空气湿度 <span class="desc">阈值 {{ thresholds.humidity_min ?? '--' }} ~ {{ thresholds.humidity_max ?? '--' }} %</span></div>
            <div class="gauge" id="gauge-hum"></div>
            <div class="spark" id="spark-hum"></div>
          </div>
          <div class="metric gauge-box">
            <div class="label">光照强度 <span class="desc">{{ luxTag }}</span></div>
            <div class="gauge" id="gauge-lux"></div>
            <div class="spark" id="spark-lux"></div>
          </div>
          <div class="metric gauge-box smoke-box">
            <div class="label">烟雾浓度 <span class="desc">{{ smokeTag }}</span></div>
            <div class="smoke-value" :class="lamp.smoke_alarm ? 'alarm' : 'ok'">
              <div class="smoke-num">{{ fmtSmoke(lamp.smoke) }}</div>
              <span class="smoke-state" :class="lamp.smoke_alarm ? 'alarm' : 'ok'">{{ lamp.smoke_alarm ? '报警！' : '正常' }}</span>
            </div>
          </div>
        </div>
        <div class="grid-2 detail-cols">
          <div class="section" style="margin-bottom:0;">
            <h3>视频监控 <span class="desc">{{ lamp.video_source === 'rtsp' ? '实时视频' : '模拟画面' }}</span></h3>
            <img class="video-frame" :src="videoUrl" alt="视频流">
          </div>
          <div class="section" style="margin-bottom:0;">
            <h3>人员智能监测 <span class="desc">持续自动识别</span></h3>
            <div class="detect-summary detect-live">
              <span class="detect-person-num">检出 <b :class="curDetect && curDetect.alarm_active ? 'alarm' : ''">{{ curDetect && curDetect.person_count != null ? curDetect.person_count : '--' }}</b> 人</span>
              <span>置信度 {{ curDetect && curDetect.max_confidence != null ? Number(curDetect.max_confidence).toFixed(2) : '--' }}</span>
              <span class="badge" :class="curDetect && curDetect.alarm_active ? 'danger' : 'ok'">{{ curDetect && curDetect.alarm_active ? '人流量告警' : '正常' }}</span>
              <span class="desc">{{ curDetect && curDetect.ts ? curDetect.ts : '识别启动中…' }}</span>
            </div>
            <img class="video-frame" :src="detectVideoUrl" alt="标注视频流">
            <div v-if="!curDetect || !curDetect.enabled" class="note">人数告警规则未启用或识别服务尚未返回，可在“告警记录”页配置人数阈值。</div>
          </div>
        </div>
      </div>

      <!-- 历史数据 -->
      <div v-show="tab === 'history'">
        <div class="section">
          <h3>历史曲线</h3>
          <div class="tabs">
            <span class="tab" :class="{ active: histRangeKey === '1h' }" @click="fetchHistory('1h')">近1小时</span>
            <span class="tab" :class="{ active: histRangeKey === '6h' }" @click="fetchHistory('6h')">近6小时</span>
            <span class="tab" :class="{ active: histRangeKey === '24h' }" @click="fetchHistory('24h')">近24小时</span>
            <span class="tab" :class="{ active: histRangeKey === 'custom' }" @click="fetchHistory('custom')">自定义</span>
          </div>
          <div v-if="histRangeKey === 'custom'" class="custom-range">
            <input type="datetime-local" v-model="customStart">
            <span>至</span>
            <input type="datetime-local" v-model="customEnd">
            <button class="btn-ghost" @click="fetchHistory('custom', customStart, customEnd)">查询</button>
          </div>
          <div class="tabs">
            <span class="tab" :class="{ active: histType === 'all' }" @click="pickHistType('all')">全部</span>
            <span class="tab" :class="{ active: histType === 'temperature' }" @click="pickHistType('temperature')">温度</span>
            <span class="tab" :class="{ active: histType === 'humidity' }" @click="pickHistType('humidity')">湿度</span>
            <span class="tab" :class="{ active: histType === 'luminance' }" @click="pickHistType('luminance')">光照</span>
            <span class="tab" :class="{ active: histType === 'smoke' }" @click="pickHistType('smoke')">烟雾</span>
          </div>
          <div class="chart" id="detail-hist-chart"></div>
        </div>
        <div class="grid-4" style="margin-bottom:14px;">
          <div class="metric"><div class="label">平均温度</div><div class="value">{{ stats.avg_temp ?? '--' }}<span class="unit">℃</span></div></div>
          <div class="metric"><div class="label">最高温度</div><div class="value">{{ stats.max_temp ?? '--' }}<span class="unit">℃</span></div></div>
          <div class="metric"><div class="label">平均湿度</div><div class="value">{{ stats.avg_humidity ?? '--' }}<span class="unit">%</span></div></div>
          <div class="metric"><div class="label">最高光照</div><div class="value">{{ stats.max_luminance ?? '--' }}<span class="unit">lx</span></div></div>
        </div>
        <div class="section">
          <div class="table-actions">
            <h3 style="margin-bottom:0;">数据明细</h3>
            <span class="desc">共 {{ histPoints.length }} 个采样点（展示最近 200 个）</span>
          </div>
          <div style="overflow-x:auto;">
            <table>
              <thead><tr><th>时间</th><th>温度 ℃</th><th>湿度 %</th><th>光照 lx</th><th>烟雾 AO</th><th>灯光</th></tr></thead>
              <tbody>
                <tr v-for="(p, i) in histRows" :key="i">
                  <td>{{ p.ts }}</td><td>{{ p.temperature }}</td><td>{{ p.humidity }}</td><td>{{ p.luminance }}</td>
                  <td>{{ p.smoke }}</td>
                  <td>{{ p.light_state === 'on' ? '开' : '关' }}</td>
                </tr>
                <tr v-if="!histPoints.length"><td colspan="6" style="text-align:center;color:#6b7a90;">暂无数据</td></tr>
              </tbody>
            </table>
          </div>
          <div class="pager">
            <span>每页</span>
            <select v-model.number="histPageSize" @change="histPage = 1">
              <option :value="10">10</option><option :value="20">20</option><option :value="50">50</option>
            </select>
            <span>第 {{ histPage }} / {{ histPageTotal }} 页</span>
            <button class="btn-ghost" :disabled="histPage <= 1" @click="histPage--">上一页</button>
            <button class="btn-ghost" :disabled="histPage >= histPageTotal" @click="histPage++">下一页</button>
          </div>
        </div>
      </div>

      <!-- 告警记录：告警配置 + 规则 + 记录列表（筛选/分页） -->
      <div v-show="tab === 'alarm'">
        <div class="section">
          <h3>环境参数告警阈值 <span class="desc">温度 / 湿度 / 光照上下限 · 超限触发告警</span></h3>
          <div class="alarm-rule">
            <label class="rule-item"><span>温度上限 ℃</span><input type="number" v-model.number="thresholds.temp_max"></label>
            <label class="rule-item"><span>温度下限 ℃</span><input type="number" v-model.number="thresholds.temp_min"></label>
            <label class="rule-item"><span>湿度上限 %</span><input type="number" v-model.number="thresholds.humidity_max"></label>
            <label class="rule-item"><span>湿度下限 %</span><input type="number" v-model.number="thresholds.humidity_min"></label>
            <label class="rule-item"><span>光照上限 lx</span><input type="number" v-model.number="thresholds.luminance_max"></label>
            <label class="rule-item"><span>光照下限 lx</span><input type="number" v-model.number="thresholds.luminance_min"></label>
            <button class="btn-primary" @click="saveEnvRule">保存环境阈值</button>
          </div>
        </div>
        <div class="section">
          <h3>人数告警规则 <span class="desc">识别到的人数达到阈值即告警</span></h3>
          <div class="alarm-rule">
            <label class="rule-item">
              <span>启用人数告警</span>
              <input type="checkbox" v-model="ruleEnabled" :true-value="1" :false-value="0">
            </label>
            <label class="rule-item">
              <span>人数告警阈值（人）</span>
              <input type="number" v-model.number="ruleMin" min="1" step="1">
            </label>
            <button class="btn-primary" @click="savePersonRule">保存规则</button>
          </div>
        </div>
        <div class="section">
          <h3>告警类型分布</h3>
          <div class="chart" id="alarm-stat-chart"></div>
        </div>
        <div class="section">
          <h3>告警记录 <span class="desc">共 {{ alarmTotal }} 条</span></h3>
          <div class="filter-bar">
            <input type="search" v-model="alarmKeyword" placeholder="搜索类型/描述" @keyup.enter="fetchAlarms(1)">
            <input type="date" v-model="alarmStart" title="开始日期">
            <span>至</span>
            <input type="date" v-model="alarmEnd" title="结束日期">
            <button class="btn-ghost" @click="fetchAlarms(1)">查询</button>
            <button class="btn-ghost" @click="resetAlarmFilter">重置</button>
          </div>
          <div style="overflow-x:auto;">
            <table>
              <thead><tr><th>时间</th><th>类型</th><th>数值</th><th>阈值</th><th>方向</th><th>状态</th><th>快照</th></tr></thead>
              <tbody>
                <tr v-for="(a, i) in alarms" :key="i">
                  <td>{{ a.ts }}</td>
                  <td>{{ typeName(a.type) }}</td>
                  <td>{{ a.value }}</td>
                  <td>{{ a.threshold }}</td>
                  <td>{{ a.direction === 'above' ? '超上限' : '低于下限' }}</td>
                  <td><span class="badge" :class="a.status === 'active' ? 'danger' : 'ok'">{{ a.status === 'active' ? '告警中' : '已恢复' }}</span></td>
                  <td><button class="btn-ghost" :disabled="!a.has_image" @click="viewAlarm(a.id)">{{ a.has_image ? '查看快照' : '无' }}</button></td>
                </tr>
                <tr v-if="!alarms.length"><td colspan="7" style="text-align:center;color:#6b7a90;">暂无告警记录</td></tr>
              </tbody>
            </table>
          </div>
          <div class="pager">
            <span>每页</span>
            <select v-model.number="alarmPageSize" @change="fetchAlarms(1)">
              <option :value="10">10</option><option :value="20">20</option><option :value="50">50</option><option :value="100">100</option>
            </select>
            <span>共 {{ alarmTotal }} 条 · 第 {{ alarmPage }} / {{ alarmPages }} 页</span>
            <button class="btn-ghost" :disabled="alarmPage <= 1" @click="fetchAlarms(alarmPage - 1)">上一页</button>
            <button class="btn-ghost" :disabled="alarmPage >= alarmPages" @click="fetchAlarms(alarmPage + 1)">下一页</button>
          </div>
        </div>
      </div>

      <!-- 人员监测记录（人数规则已移至告警记录页） -->
      <div v-show="tab === 'detections'">
        <div class="section">
          <h3>人员检出趋势</h3>
          <div class="chart" id="detect-stat-chart"></div>
        </div>
        <div class="section">
          <h3>人员监测记录 <span class="desc">共 {{ detTotal }} 条</span></h3>
          <div class="filter-bar">
            <input type="search" v-model="detKeyword" placeholder="搜索窗口" @keyup.enter="fetchDetections(1)">
            <input type="date" v-model="detStart" title="开始日期">
            <span>至</span>
            <input type="date" v-model="detEnd" title="结束日期">
            <button class="btn-ghost" @click="fetchDetections(1)">查询</button>
            <button class="btn-ghost" @click="resetDetFilter">重置</button>
          </div>
          <div style="overflow-x:auto;">
            <table>
              <thead><tr><th>时间</th><th>窗口</th><th>人数</th><th>最高置信度</th><th>操作</th></tr></thead>
              <tbody>
                <tr v-for="(d, i) in detections" :key="i">
                  <td>{{ d.ts }}</td>
                  <td>{{ d.lamp_id }}</td>
                  <td>{{ d.person_count }}</td>
                  <td>{{ d.max_confidence }}</td>
                  <td><button class="btn-ghost" @click="viewDetection(d.id)">查看</button></td>
                </tr>
                <tr v-if="!detections.length"><td colspan="5" style="text-align:center;color:#6b7a90;">暂无监测记录</td></tr>
              </tbody>
            </table>
          </div>
          <div class="pager">
            <span>每页</span>
            <select v-model.number="detPageSize" @change="fetchDetections(1)">
              <option :value="10">10</option><option :value="20">20</option><option :value="50">50</option><option :value="100">100</option>
            </select>
            <span>共 {{ detTotal }} 条 · 第 {{ detPage }} / {{ detPages }} 页</span>
            <button class="btn-ghost" :disabled="detPage <= 1" @click="fetchDetections(detPage - 1)">上一页</button>
            <button class="btn-ghost" :disabled="detPage >= detPages" @click="fetchDetections(detPage + 1)">下一页</button>
          </div>
        </div>
      </div>

      <!-- 操作日志（筛选/分页） -->
      <div v-show="tab === 'logs'">
        <div class="section">
          <h3>设备操作日志 <span class="desc">共 {{ logTotal }} 条</span></h3>
          <div class="filter-bar">
            <input type="search" v-model="logKeyword" placeholder="搜索窗口/指令/说明" @keyup.enter="fetchLogs(1)">
            <input type="date" v-model="logStart" title="开始日期">
            <span>至</span>
            <input type="date" v-model="logEnd" title="结束日期">
            <button class="btn-ghost" @click="fetchLogs(1)">查询</button>
            <button class="btn-ghost" @click="resetLogFilter">重置</button>
          </div>
          <div style="overflow-x:auto;">
            <table>
              <thead><tr><th>时间</th><th>窗口</th><th>指令</th><th>结果</th><th>说明</th></tr></thead>
              <tbody>
                <tr v-for="(l, i) in logs" :key="i">
                  <td>{{ l.ts }}</td>
                  <td>{{ l.lamp_id }}</td>
                  <td>{{ l.action === 'on' ? '开灯' : '关灯' }}</td>
                  <td><span class="badge" :class="l.result === 'success' ? 'ok' : 'fail'">{{ l.result === 'success' ? '成功' : '失败' }}</span></td>
                  <td>{{ l.detail || '' }}</td>
                </tr>
                <tr v-if="!logs.length"><td colspan="5" style="text-align:center;color:#6b7a90;">暂无操作日志</td></tr>
              </tbody>
            </table>
          </div>
          <div class="pager">
            <span>每页</span>
            <select v-model.number="logPageSize" @change="fetchLogs(1)">
              <option :value="10">10</option><option :value="20">20</option><option :value="50">50</option><option :value="100">100</option>
            </select>
            <span>共 {{ logTotal }} 条 · 第 {{ logPage }} / {{ logPages }} 页</span>
            <button class="btn-ghost" :disabled="logPage <= 1" @click="fetchLogs(logPage - 1)">上一页</button>
            <button class="btn-ghost" :disabled="logPage >= logPages" @click="fetchLogs(logPage + 1)">下一页</button>
          </div>
        </div>
      </div>

      <!-- 记录图片详情弹层 -->
      <div class="modal-overlay" v-if="detailImages" @click="closeDetail">
        <div class="modal" @click.stop>
          <div class="modal-head">
            <h3>监测记录详情</h3>
            <button class="close" @click="closeDetail">×</button>
          </div>
          <div v-if="detailInfo" class="detect-summary">
            <span>窗口 {{ detailInfo.lamp_id }}</span>
            <span>检测到 <b>{{ detailInfo.person_count }}</b> 人</span>
            <span>置信度 {{ detailInfo.max_confidence }}</span>
            <span class="desc">{{ detailInfo.ts }}</span>
          </div>
          <div class="detect-imgs">
            <div class="detect-img-box"><div class="detect-img-label">原始图像</div><img :src="detailImages.original" alt="原始图像"></div>
            <div class="detect-img-box"><div class="detect-img-label">标注图像</div><img :src="detailImages.processed" alt="标注图像"></div>
          </div>
        </div>
      </div>

      <!-- 告警快照弹层 -->
      <div class="modal-overlay" v-if="alarmDetail" @click="closeAlarm">
        <div class="modal" @click.stop>
          <div class="modal-head">
            <h3>异常快照 · {{ typeName(alarmDetail.type) }}</h3>
            <button class="close" @click="closeAlarm">×</button>
          </div>
          <div v-if="alarmDetail" class="detect-summary">
            <span>窗口 {{ alarmDetail.lamp_id }}</span>
            <span>数值 <b>{{ alarmDetail.value }}</b>（阈值 {{ alarmDetail.threshold }}）</span>
            <span>{{ alarmDetail.direction === 'above' ? '超上限' : '低于下限' }}</span>
            <span class="badge" :class="alarmDetail.status === 'active' ? 'danger' : 'ok'">{{ alarmDetail.status === 'active' ? '告警中' : '已恢复' }}</span>
            <span class="desc">{{ alarmDetail.ts }}</span>
          </div>
          <div v-if="alarmDetail && alarmDetail.image" class="detect-img-box">
            <div class="detect-img-label">异常截图（标记）</div>
            <img :src="alarmDetail.image" alt="异常快照">
          </div>
          <div v-else class="note">该告警无截图快照。</div>
        </div>
      </div>
    </div>
  `,
};