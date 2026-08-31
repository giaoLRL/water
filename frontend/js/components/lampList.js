/* 灯杆列表视图：顶部概览统计 + 灯杆分布图 + 告警分布 + 卡片式导航，点击进入详情。 */
window.ViewLampList = {
  name: "LampListView",
  props: {
    lamps: { type: Array, default: () => [] },
    devices: { type: Array, default: () => [] },
  },
  emits: ["open"],
  data() {
    return { alarmItems: [] };
  },
  computed: {
    onlineCount() {
      // 灯杆在线 = 全部已配置设备（温湿度/光照/烟雾/视频）在线
      return this.lamps.filter((l) => l.lamp_online).length;
    },
    lightOnCount() {
      return this.lamps.filter((l) => l.light_state === "on").length;
    },
    alarmTotal() {
      return this.lamps.reduce((s, l) => s + (l.alarm_count || 0), 0);
    },
    overview() {
      return [
        { ico: "lamp", label: "机房总数", num: this.lamps.length, cls: "" },
        { ico: "signal", label: "在线机房", num: this.onlineCount, cls: "green" },
        { ico: "bulb", label: "灯光开启", num: this.lightOnCount, cls: "amber" },
        { ico: "bell", label: "活跃告警", num: this.alarmTotal, cls: this.alarmTotal ? "red" : "" },
      ];
    },
    icons() {
      const a = 'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"';
      return {
        lamp: `<svg ${a}><path d="M8 3v18M8 6h10l-4 3 4 3H8"/><path d="M4 21h18"/></svg>`,
        signal: `<svg ${a}><path d="M4 20V11m5 9V7m5 13v-8m5 8V4"/></svg>`,
        bulb: `<svg ${a}><path d="M9 18h6m-5 3h4M12 3a6 6 0 0 0-3.5 10.9c.7.5 1.5 1.3 1.5 2.1h4c0-.8.8-1.6 1.5-2.1A6 6 0 0 0 12 3Z"/></svg>`,
        bell: `<svg ${a}><path d="M12 3a6 6 0 0 1 6 6c0 4.2 1.5 5.7 2 6H4c.5-.3 2-1.8 2-6a6 6 0 0 1 6-6Z"/><path d="M10 19a2.2 2.2 0 0 0 4 0"/></svg>`,
      };
    },
  },
  watch: {
    lamps: {
      handler() {
        this.drawLampMap();
        this.drawAlarmPie();
        this.drawAlarmTree();
      },
    },
  },
  mounted() {
    this.drawLampMap();
    this.drawAlarmPie();
    this.drawAlarmTree();
    this._onResize = () => window.Charts.resizeAll();
    window.addEventListener("resize", this._onResize);
  },
  beforeUnmount() {
    window.removeEventListener("resize", this._onResize);
  },
  methods: {
    open(id) {
      this.$emit("open", id);
    },
    perm(p) {
      return window.Auth ? window.Auth.has(p) : false;
    },
    fmtLux(v) {
      if (v == null) return "--";
      if (v >= 1000) return (v / 1000).toFixed(1) + "k";
      return Math.round(v);
    },
    fmtSmoke(v) {
      if (v == null) return "--";
      return Math.round(v);
    },
    statusText(s) {
      return { online: "在线", offline: "离线", sim: "无真实传感器", detecting: "检测中" }[s] || s;
    },
    statusCls(s) {
      return { online: "running", offline: "offline", sim: "sim", detecting: "detecting" }[s] || "";
    },
    /* 真实传感器（ESP32）对应指标是否明确离线：离线时页面应显示 "--" 而非 0 值假数据 */
    sensorOff(l, kind) {
      if (l.sensor_source !== "esp32") return false;
      if (kind === "temp_hum") return l.sensor_online === false;
      if (kind === "lux") return l.light_online === false;
      if (kind === "smoke") return l.smoke_online === false;
      return false;
    },
    /* 灯杆分布图：示意图坐标 + 实时状态着色，点击进入详情 */
    drawLampMap() {
      if (!document.getElementById("lamp-map-chart") || !window.echarts) return;
      const pos = { "01": [22, 32], "02": [74, 26], "03": [48, 68] };
      const data = this.lamps.map((l) => ({
        value: pos[l.id] || [50, 50],
        lampId: l.id,
        name: l.name || l.id,
        temperature: l.temperature,
        humidity: l.humidity,
        luminance: l.luminance,
        smoke: l.smoke,
        smoke_alarm: l.smoke_alarm,
        alarm: (l.alarm_count || 0) > 0,
        on: l.light_state === "on",
      }));
      const chart = window.Charts.init("lamp-map-chart", {
        tooltip: {
          backgroundColor: "#1a222d",
          borderColor: "#2a3442",
          textStyle: { color: "#d7e0ea" },
          formatter: (p) => {
            const d = p.data;
            return `${d.name}<br/>温度 ${d.temperature}℃ · 湿度 ${d.humidity}%<br/>光照 ${this.fmtLux(d.luminance)} lx<br/>烟雾 ${this.fmtSmoke(d.smoke)}${d.smoke_alarm ? "（报警）" : ""}<br/>灯光 ${d.on ? "开启" : "关闭"}`;
          },
        },
        grid: { left: 10, right: 10, top: 30, bottom: 14 },
        xAxis: { type: "value", min: 0, max: 100, show: false },
        yAxis: { type: "value", min: 0, max: 100, show: false },
        series: [{
          type: "scatter",
          symbolSize: 36,
          data,
          itemStyle: {
            color: (p) => (p.data.alarm ? "#f87171" : p.data.on ? "#34d399" : "#38bdf8"),
            borderColor: "#0d1117",
            borderWidth: 2,
          },
          label: {
            show: true,
            formatter: (p) => p.data.name,
            position: "bottom",
            color: "#d7e0ea",
            fontSize: 12,
            distance: 8,
          },
        }],
      });
      chart.off("click");
      chart.on("click", (p) => {
        if (p.data && p.data.lampId) this.open(p.data.lampId);
      });
    },
    /* 活跃告警类型分布（环形图） */
    drawAlarmPie() {
      const agg = {};
      (this.lamps || []).forEach((l) => {
        (l.active_alarms || []).forEach((a) => {
          const name = a.label || a.type || "未知";
          agg[name] = (agg[name] || 0) + 1;
        });
      });
      this.alarmItems = Object.keys(agg).map((n) => ({ name: n, value: agg[n] }))
        .sort((x, y) => this.typeOrder(x.name) - this.typeOrder(y.name));
      const el = document.getElementById("lamp-alarm-pie");
      if (!el || !window.echarts) return;
      const colors = this.alarmItems.map((it) => this.typeColor(it.name));
      const opt = window.Charts.pieOption(this.alarmItems, colors);
      // 百分比半径（不读运行时宽度，避免初始化错位），由容器高度决定圆的大小
      opt.series[0].radius = ["30%", "74%"];
      opt.series[0].center = ["50%", "46%"];
      window.Charts.init("lamp-alarm-pie", opt);
    },
    /* 告警类型固定排序与配色：保证树图与饼图颜色一一对应 */
    typeOrder(name) {
      const order = ["人员数量", "空气湿度", "环境温度", "光照强度", "烟雾浓度", "设备离线"];
      const i = order.indexOf(name);
      return i === -1 ? 99 : i;
    },
    typeColor(name) {
      const map = {
        "人员数量": "#f87171",
        "空气湿度": "#38bdf8",
        "环境温度": "#2dd4bf",
        "光照强度": "#fbbf24",
        "烟雾浓度": "#f472b6",
        "设备离线": "#a78bfa",
      };
      return map[name] || "#a78bfa";
    },
    /* 活跃告警矩形树图：类型 → 灯杆 二层铺满，无空白，颜色与饼图对应 */
    drawAlarmTree() {
      const el = document.getElementById("lamp-alarm-tree");
      if (!el || !window.echarts) return;
      // 两层矩形树：类型 → 各灯杆
      const byType = {};
      (this.lamps || []).forEach((l) => {
        (l.active_alarms || []).forEach((a) => {
          const t = a.label || a.type || "未知";
          if (!byType[t]) byType[t] = [];
          byType[t].push({ name: l.name || l.id, value: 1 });
        });
      });
      const names = Object.keys(byType).sort((x, y) => this.typeOrder(x) - this.typeOrder(y));
      const data = names.map((t) => ({ name: t, children: byType[t] }));
      if (!data.length) {
        // 全部告警恢复时清空旧图
        window.Charts.init("lamp-alarm-tree", { series: [] });
        return;
      }
      window.Charts.init("lamp-alarm-tree", {
        tooltip: { backgroundColor: "#1a222d", borderColor: "#2a3442", textStyle: { color: "#d7e0ea" } },
        series: [{
          type: "treemap", data,
          roam: false,
          breadcrumb: { show: false },
          label: { show: true, formatter: "{b}", color: "#d7e0ea", fontSize: 11 },
          upperLabel: { show: false },
          itemStyle: { borderColor: "#0f151e", borderWidth: 2 },
          levels: [
            { itemStyle: { borderColor: "#0f151e", borderWidth: 2, gapWidth: 3 } },
            { itemStyle: { borderColor: "#0f151e", borderWidth: 1, gapWidth: 2 },
              label: { show: true, formatter: "{b}", color: "#e6edf3", fontSize: 10 } },
          ],
        }],
        // 与外层类型顺序一致 → 颜色与饼图一一对应
        color: names.map((n) => this.typeColor(n)),
      });
    },
  },
  template: `
    <div class="view-page">
      <div class="view-title">
        <h2>智慧机房导航</h2>
        <span class="desc">共 {{ lamps.length }} 个机房 · 点击卡片或地图点位进入详细信息</span>
      </div>

      <div class="overview-grid">
        <div class="overview-card" v-for="o in overview" :key="o.label">
          <div class="o-ico" v-html="icons[o.ico]"></div>
          <div>
            <div class="o-num" :class="o.cls ? 'c-' + o.cls : ''">{{ o.num }}</div>
            <div class="o-label">{{ o.label }}</div>
          </div>
        </div>
      </div>

      <div class="grid-2" style="margin-bottom:14px;">
        <div class="section" style="margin-bottom:0;">
          <h3>机房分布图</h3>
          <div class="chart map-chart" id="lamp-map-chart"></div>
        </div>
        <div class="section" style="margin-bottom:0;">
          <h3>活跃告警类型分布与构成</h3>
          <div class="half-row">
            <div class="half">
              <div class="half-title">类型占比</div>
              <div class="chart alarm-pie" id="lamp-alarm-pie"></div>
              <div class="empty-chart" v-if="!alarmItems.length">暂无活跃告警</div>
            </div>
            <div class="half">
              <div class="half-title">类型 × 机房（占比占满）</div>
              <div class="chart alarm-tree" id="lamp-alarm-tree"></div>
            </div>
          </div>
        </div>
      </div>

      <div class="section" v-if="perm('view_device')" style="margin-bottom:14px;">
        <h3>设备在线状态 <span class="desc">按接口返回情况实时检测</span></h3>
        <div class="device-status">
          <div class="device-group" v-for="g in devices" :key="g.group">
            <div class="device-group-title">{{ g.group }}</div>
            <div class="device-group-items">
              <div class="status-item" v-for="d in g.items" :key="d.type + d.name" :class="statusCls(d.status)">
                <span class="state-dot"></span>
                <div style="flex:1;">
                  <div class="name">{{ d.name }} <span class="detail">{{ d.detail }}</span></div>
                  <div class="state">{{ statusText(d.status) }}</div>
                </div>
              </div>
            </div>
          </div>
          <div class="status-item" v-if="!devices.length"><div class="name" style="color:var(--text-dim);">设备状态加载中…</div></div>
        </div>
      </div>

      <div class="lamp-grid">
        <div class="lamp-card" v-for="l in lamps" :key="l.id" @click="open(l.id)">
          <div class="lamp-card-head">
            <span class="lamp-id">{{ l.name }}</span>
            <span class="lamp-loc">{{ l.location }}</span>
            <span class="lamp-dot" :class="l.light_state === 'on' ? 'on' : 'off'"></span>
          </div>
          <div class="lamp-metrics">
            <div class="lamp-metric">
              <div class="l-label">环境温度</div>
              <div class="l-value">{{ sensorOff(l, 'temp_hum') ? '--' : (l.temperature || 0).toFixed(1) }}<span class="l-unit">℃</span></div>
            </div>
            <div class="lamp-metric">
              <div class="l-label">空气湿度</div>
              <div class="l-value">{{ sensorOff(l, 'temp_hum') ? '--' : (l.humidity || 0).toFixed(1) }}<span class="l-unit">%</span></div>
            </div>
            <div class="lamp-metric">
              <div class="l-label">光照强度</div>
              <div class="l-value">{{ sensorOff(l, 'lux') ? '--' : fmtLux(l.luminance) }}<span class="l-unit">lx</span></div>
            </div>
            <div class="lamp-metric" :class="{ 'smoke-warn': l.smoke_alarm }">
              <div class="l-label">{{ l.smoke_alarm ? '烟雾报警！' : '烟雾浓度' }}</div>
              <div class="l-value">{{ sensorOff(l, 'smoke') ? '--' : fmtSmoke(l.smoke) }}</div>
            </div>
          </div>
          <div class="lamp-card-foot">
            <span class="lamp-light" :class="{ on: l.light_state === 'on' }">
              灯光 {{ l.light_state === 'on' ? '开启' : '关闭' }}
            </span>
            <span class="lamp-video">{{ l.video_source === 'rtsp' ? '实时视频' : '模拟画面' }}</span>
            <span class="lamp-video" :class="{ real: l.sensor_source === 'esp32' && l.sensor_online !== false, offline: l.sensor_source === 'esp32' && l.sensor_online === false }">{{ l.sensor_source === 'esp32' ? (l.sensor_online === false ? '温湿度离线' : '真实温湿度') : '无真实传感器' }}</span>
            <span class="lamp-alarm" v-if="l.alarm_count">
              <b>{{ l.alarm_count }}</b> 告警
            </span>
            <span class="lamp-alarm none" v-else>无告警</span>
          </div>
        </div>
      </div>
      <div class="section" v-if="!lamps.length">
        <div style="text-align:center;color:#7d8b99;padding:20px;">暂无机房数据</div>
      </div>
    </div>
  `,
};
