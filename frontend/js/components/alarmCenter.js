/* 全站告警中心：聚合所有机房的告警记录与类型分布，可查看快照、跳转到对应机房。 */
window.ViewAlarmCenter = {
  name: "AlarmCenterView",
  emits: ["back", "goto"],
  data() {
    return {
      alarms: [],
      total: 0, page: 1, pageSize: 10,
      keyword: "", start: "", end: "",
      stats: [],
      alarmDetail: null,
    };
  },
  computed: {
    pages() { return Math.max(1, Math.ceil(this.total / this.pageSize)); },
  },
  mounted() {
    this.load();
  },
  methods: {
    typeName(t) {
      return { temperature: "环境温度", humidity: "空气湿度", luminance: "光照强度", person: "人员数量", smoke: "烟雾浓度", device_offline: "设备离线" }[t] || t;
    },
    dateFilter() {
      const r = {};
      if (this.start) r.start = this.start + " 00:00:00";
      if (this.end) r.end = this.end + " 23:59:59";
      return r;
    },
    async load() {
      // 告警记录与类型统计并行加载：统计接口很快，不必等慢的告警列表完成后再出图
      this.fetchAlarms(this.page);
      this.fetchStats();
    },
    async fetchAlarms(page) {
      if (page) this.page = page;
      if (this.page < 1) this.page = 1;
      try {
        const d = await API.alarms(null, {
          page: this.page, page_size: this.pageSize,
          keyword: this.keyword || undefined,
          ...this.dateFilter(),
        });
        this.alarms = d.items || [];
        this.total = d.total || 0;
      } catch (e) { /* silent */ }
    },
    async fetchStats() {
      try {
        const d = await API.alarmStats(null);
        this.stats = d.stats || [];
        this.renderStatsChart();
      } catch (e) { /* silent */ }
    },
    reset() {
      this.keyword = ""; this.start = ""; this.end = "";
      this.fetchAlarms(1);
    },
    async viewAlarm(id) {
      try {
        this.alarmDetail = await API.alarm(id);
      } catch (e) { /* silent */ }
    },
    closeAlarm() { this.alarmDetail = null; },
    renderStatsChart() {
      const el = document.getElementById("alarm-center-chart");
      if (!el || !window.echarts) return;
      const names = this.stats.map((s) => this.typeName(s.type));
      const values = this.stats.map((s) => s.count);
      window.Charts.init("alarm-center-chart", {
        ...window.Charts.barOption(names, "次数"),
        series: [window.Charts.barSeries("告警次数", values, "#f87171")],
      });
    },
    goLamp(id) { this.$emit("goto", id); },
  },
  template: `
  <div class="view-page">
    <div class="detail-head">
      <button class="btn-ghost" @click="$emit('back')">← 返回</button>
      <h2>全站告警中心</h2>
      <span class="desc">聚合所有机房的告警记录，点击可查看快照或跳转机房</span>
    </div>

    <div class="section">
      <h3>告警类型分布（全库）</h3>
      <div class="chart" id="alarm-center-chart"></div>
    </div>

    <div class="section">
      <h3>告警记录 <span class="desc">共 {{ total }} 条</span></h3>
      <div class="filter-bar">
        <input type="search" v-model="keyword" placeholder="搜索类型/描述" @keyup.enter="fetchAlarms(1)">
        <input type="date" v-model="start" title="开始日期">
        <span>至</span>
        <input type="date" v-model="end" title="结束日期">
        <button class="btn-ghost" @click="fetchAlarms(1)">查询</button>
        <button class="btn-ghost" @click="reset">重置</button>
      </div>
      <div style="overflow-x:auto;">
        <table>
          <thead><tr><th>时间</th><th>机房</th><th>类型</th><th>数值</th><th>阈值</th><th>方向</th><th>状态</th><th>快照</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="(a, i) in alarms" :key="i">
              <td>{{ a.ts }}</td>
              <td>{{ a.lamp_id }}</td>
              <td>{{ typeName(a.type) }}</td>
              <td>{{ a.value }}</td>
              <td>{{ a.threshold }}</td>
              <td>{{ a.direction === 'above' ? '超上限' : '低于下限' }}</td>
              <td><span class="badge" :class="a.status === 'active' ? 'danger' : 'ok'">{{ a.status === 'active' ? '告警中' : '已恢复' }}</span></td>
              <td><button class="btn-ghost" :disabled="!a.has_image" @click="viewAlarm(a.id)">{{ a.has_image ? '查看快照' : '无' }}</button></td>
              <td><button class="btn-ghost" @click="goLamp(a.lamp_id)">前往机房</button></td>
            </tr>
            <tr v-if="!alarms.length"><td colspan="9" style="text-align:center;color:#6b7a90;">暂无告警记录</td></tr>
          </tbody>
        </table>
      </div>
      <div class="pager">
        <span>每页</span>
        <select v-model.number="pageSize" @change="fetchAlarms(1)">
          <option :value="10">10</option><option :value="20">20</option><option :value="50">50</option><option :value="100">100</option>
        </select>
        <span>共 {{ total }} 条 · 第 {{ page }} / {{ pages }} 页</span>
        <button class="btn-ghost" :disabled="page <= 1" @click="fetchAlarms(page - 1)">上一页</button>
        <button class="btn-ghost" :disabled="page >= pages" @click="fetchAlarms(page + 1)">下一页</button>
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
          <span>机房 {{ alarmDetail.lamp_id }}</span>
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
