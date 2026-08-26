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
      return this.lamps.filter((l) => l.online).length;
    },
    lightOnCount() {
      return this.lamps.filter((l) => l.light_state === "on").length;
    },
    alarmTotal() {
      return this.lamps.reduce((s, l) => s + (l.alarm_count || 0), 0);
    },
    overview() {
      return [
        { ico: "lamp", label: "灯杆总数", num: this.lamps.length, cls: "" },
        { ico: "signal", label: "在线灯杆", num: this.onlineCount, cls: "green" },
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
      },
      deep: true,
    },
  },
  mounted() {
    this.drawLampMap();
    this.drawAlarmPie();
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
    fmtLux(v) {
      if (v == null) return "--";
      if (v >= 1000) return (v / 1000).toFixed(1) + "k";
      return Math.round(v);
    },
    statusText(s) {
      return { online: "在线", offline: "离线", sim: "模拟", detecting: "检测中" }[s] || s;
    },
    statusCls(s) {
      return { online: "running", offline: "offline", sim: "sim", detecting: "detecting" }[s] || "";
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
            return `${d.name}<br/>温度 ${d.temperature}℃ · 湿度 ${d.humidity}%<br/>光照 ${this.fmtLux(d.luminance)} lx<br/>灯光 ${d.on ? "开启" : "关闭"}`;
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
      this.alarmItems = Object.keys(agg).map((n) => ({ name: n, value: agg[n] }));
      if (!document.getElementById("lamp-alarm-pie") || !window.echarts) return;
      window.Charts.init("lamp-alarm-pie", window.Charts.pieOption(this.alarmItems));
    },
  },
  template: `
    <div class="view-page">
      <div class="view-title">
        <h2>智慧灯杆导航</h2>
        <span class="desc">共 {{ lamps.length }} 个灯杆 · 点击卡片或地图点位进入详细信息</span>
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
          <h3>灯杆分布图</h3>
          <div class="chart map-chart" id="lamp-map-chart"></div>
        </div>
        <div class="section" style="margin-bottom:0;">
          <h3>活跃告警类型分布</h3>
          <div class="chart map-chart" id="lamp-alarm-pie" v-show="alarmItems.length"></div>
          <div class="empty-chart" v-if="!alarmItems.length">暂无活跃告警</div>
        </div>
      </div>

      <div class="section" style="margin-bottom:14px;">
        <h3>设备在线状态 <span class="desc">按接口返回情况实时检测</span></h3>
        <div class="device-status">
          <div class="status-item" v-for="d in devices" :key="d.type + d.name" :class="statusCls(d.status)">
            <span class="state-dot"></span>
            <div style="flex:1;">
              <div class="name">{{ d.name }} <span class="detail">{{ d.detail }}</span></div>
              <div class="state">{{ statusText(d.status) }}</div>
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
              <div class="l-value">{{ (l.temperature || 0).toFixed(1) }}<span class="l-unit">℃</span></div>
            </div>
            <div class="lamp-metric">
              <div class="l-label">空气湿度</div>
              <div class="l-value">{{ (l.humidity || 0).toFixed(1) }}<span class="l-unit">%</span></div>
            </div>
            <div class="lamp-metric">
              <div class="l-label">光照强度</div>
              <div class="l-value">{{ fmtLux(l.luminance) }}<span class="l-unit">lx</span></div>
            </div>
          </div>
          <div class="lamp-card-foot">
            <span class="lamp-light" :class="{ on: l.light_state === 'on' }">
              灯光 {{ l.light_state === 'on' ? '开启' : '关闭' }}
            </span>
            <span class="lamp-video">{{ l.video_source === 'rtsp' ? '实时视频' : '模拟画面' }}</span>
            <span class="lamp-video" :class="{ real: l.sensor_source === 'esp32' }">{{ l.sensor_source === 'esp32' ? '真实温湿度' : '模拟温湿度' }}</span>
            <span class="lamp-alarm" v-if="l.alarm_count">
              <b>{{ l.alarm_count }}</b> 告警
            </span>
            <span class="lamp-alarm none" v-else>无告警</span>
          </div>
        </div>
      </div>
      <div class="section" v-if="!lamps.length">
        <div style="text-align:center;color:#7d8b99;padding:20px;">暂无灯杆数据</div>
      </div>
    </div>
  `,
};