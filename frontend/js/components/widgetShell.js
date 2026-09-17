/* 仪表盘通用卡片外壳：按卡片配置渲染，配合 gridstack 网格实现可编辑仪表盘。
 *
 * 数据来源两种：
 *   realtime —— 复用全局 2s 轮询的 /api/water/realtime 快照（零额外请求）；
 *   custom   —— 自定义 GET 接口，经后端 /api/water/dashboard/proxy 白名单代发，
 *               按卡片自己的 period(秒，≥2) 轮询。
 * 无数据一律显示 --，不做任何模拟；超阈值时数值标红。
 * builtin 特殊卡（quant/pid/recent/reset）逻辑自包含：
 *   自行按权限拉取所需数据（PID 参数 / 最近操作），直接调用后端控制接口，
 *   与旧版固定操作区同一后端接口与权限点（操作由后端写入日志）。
 * 控制开关卡支持自定义指令：配置 cmd:{on,off} 后经后端代理 GET 下发（log=1 写审计日志）。
 */
window.WidgetShell = {
  name: "WidgetShell",
  props: {
    widget: { type: Object, required: true },
    realtime: { type: Object, required: true },
    thresholds: { type: Object, default: () => ({}) },
    editing: { type: Boolean, default: false },
    online: { type: Boolean, default: false },
  },
  emits: ["remove", "config"],
  data() {
    return {
      customJson: null, customErr: "", series: [], busy: false,
      // ---- 内置操作卡状态 ----
      targetInput: 0, targetBusy: false, resetBusy: false,
      pid: { target_temp: 42, pid_enabled: false, kp: 16, ki: 0.3, kd: 25 }, pidBusy: false,
      recentLogs: [],
    };
  },
  computed: {
    w() { return this.widget; },
    val() {
      const s = this.w.source || {};
      if (s.kind === "custom") return window.Widgets.resolvePath(this.customJson, s.path);
      return window.Widgets.resolvePath(this.realtime, s.path);
    },
    text() {
      // 水槽卡：val 是整个水槽对象，展示的是其中的百分比
      if (this.w.type === "tank") return window.Widgets.fmtVal(this.tankPercent, this.w.decimals);
      return window.Widgets.fmtVal(this.val, this.w.decimals);
    },
    /* 数值范围：自定义卡用自配 min/max，内置卡用服务端阈值键 */
    range() {
      const t = this.w.thresholds || {};
      let min = t.min, max = t.max;
      if (t.minKey) min = this.thresholds[t.minKey];
      if (t.maxKey) max = this.thresholds[t.maxKey];
      return { min, max };
    },
    bad() {
      if (this.text === "--") return false;
      const v = Number(this.val);
      if (isNaN(v)) return false;
      const { min, max } = this.range;
      const num = (x) => (x === null || x === undefined || x === "" ? null : Number(x));
      const lo = num(min), hi = num(max);
      return (hi !== null && v > hi) || (lo !== null && v < lo);
    },
    footText() {
      const t = this.w.thresholds || {};
      const hasKeys = t.minKey || t.maxKey;
      const hasLiteral = t.min !== null && t.min !== undefined && t.min !== ""
        || t.max !== null && t.max !== undefined && t.max !== "";
      if (hasKeys || hasLiteral) {
        const num = (x) => (x === null || x === undefined || x === "" ? "--" : x);
        const lo = hasKeys ? (this.thresholds[t.minKey] ?? "--") : num(t.min);
        const hi = hasKeys ? (this.thresholds[t.maxKey] ?? "--") : num(t.max);
        return `阈值 ${lo} ~ ${hi} ${this.w.unit || ""}`;
      }
      return this.w.foot || "";
    },
    stateText() {
      const v = this.val;
      return v === "on" ? "已开启" : v === "off" ? "已关闭" : "未知";
    },
    stateClass() {
      const v = this.val;
      return v === "on" ? "ok" : v === "off" ? "off" : "";
    },
    chartId() { return this.w.domId || ("wg-chart-" + this.w.id); },
    hasChart() { return this.w.type === "spark" || this.w.type === "line"; },
    // ---- 水槽可视化卡（对象源或数值源两种形态） ----
    tankObj() { return (this.w.type === "tank" && this.val && typeof this.val === "object") ? this.val : {}; },
    tankPercent() {
      // 对象源取 percent；数值源（自定义接口返回百分比数字）直接用 val
      const p = this.w.type === "tank" && typeof this.val === "number"
        ? this.val : Number(this.tankObj.percent);
      return isNaN(p) ? 0 : Math.max(0, Math.min(100, p));
    },
    tankTag() {
      // 无传感器优先于离线：储水槽没接传感器，无论设备在不在线都标「无传感器」
      if (typeof this.val === "number") return "自定义";       // 数值源
      const s = this.tankObj.source;
      if (s === "none") return "无传感器";
      if (!this.online) return "离线";
      return "实测";
    },
    tankVolumeText() {
      if (typeof this.val === "number") return "";             // 数值源无水量信息
      const v = this.tankObj.volume, c = this.tankObj.capacity;
      if (v == null && c == null) return "";
      const f = (x) => (x == null || isNaN(Number(x)) ? "--" : Number(x).toFixed(1));
      return `${f(v)} / ${f(c)} L`;
    },
    // ---- 控制开关卡 ----
    ctlOn() { return this.val === "on"; },
    canCtl() { return window.Auth ? window.Auth.has("ctrl_light") : false; },
    ctlDisabled() { return this.busy || !this.online || !this.canCtl; },
    // ---- 定量浇水卡 ----
    targetProgress() { return window.Widgets.targetProgress(this.realtime); },
    // ---- 恒温闭环卡 ----
    pidSupported() { return !!this.realtime.pid_supported; },
    // ---- 内置「采集设备」卡 ----
    device() { return this.realtime.device || {}; },
    features() { return this.realtime.features || {}; },
    ipText() { return this.device.ip || "--"; },
    rssiText() { return this.device.rssi == null ? "--" : `${this.device.rssi} dBm`; },
    uptimeText() {
      const ms = this.device.uptime_ms;
      if (ms == null) return "--";
      const s = Math.floor(ms / 1000), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
      return h > 0 ? `${h} 小时 ${m} 分` : `${m} 分 ${s % 60} 秒`;
    },
    missingChannels() {
      const f = this.features, names = [];
      if (!f.temperature) names.push("水温");
      if (!f.pressure) names.push("水压");
      if (!f.heater) names.push("加热模块");
      return names;
    },
  },
  watch: {
    /* realtime 每 2s 整体刷新：每次到达都推一个趋势点（值不变也推，保证曲线连续）；
       定量输入框只在未聚焦时同步，避免覆盖正在输入的内容 */
    realtime() { this.onTick(); this.syncTargetInput(); },
    "w.type"() { this.$nextTick(() => this.renderChart()); },
  },
  mounted() {
    this.onTick();
    if ((this.w.source || {}).kind === "custom") this.startPoll();
    this.initOps();
  },
  beforeUnmount() {
    if (this._timer) clearInterval(this._timer);
    if (this._recentT) clearInterval(this._recentT);
  },
  methods: {
    perm(p) { return window.Auth ? window.Auth.has(p) : false; },
    /* 内置操作卡数据拉取：pid 参数一次拉取；最近操作 10s 轮询 */
    initOps() {
      const b = this.w.builtin;
      if (b === "pid" && this.perm("cfg_alarm")) {
        API.pidGet().then((d) => Object.assign(this.pid, d)).catch(() => { /* silent */ });
      }
      if (b === "recent" && this.perm("view_log")) {
        this.fetchRecent();
        this._recentT = setInterval(() => this.fetchRecent(), 10000);
      }
      if (b === "quant") this.syncTargetInput();
    },
    /* 定量输入框只在用户未聚焦时同步，避免覆盖正在输入的内容 */
    syncTargetInput() {
      const el = this.$refs.targetInput;
      if (el && document.activeElement === el) return;
      const v = Number(this.realtime.pump_target || 0);
      if (this.targetInput !== v) this.targetInput = v;
    },
    /* 最近操作（最近 4 条日志） */
    async fetchRecent() {
      try {
        const d = await API.logs({ page: 1, page_size: 4 });
        this.recentLogs = d.items || [];
      } catch (e) { /* silent */ }
    },
    /* 日志展示（与操作日志页共用 Widgets 纯函数） */
    actionLabel(a) { return window.Widgets.actionLabel(a); },
    isSystemLog(l) { return window.Widgets.isSystemLog(l); },
    operatorText(l) { return window.Widgets.operatorText(l); },
    onTick() {
      const v = this.val;
      const n = (v === null || v === undefined || v === "" || isNaN(Number(v))) ? 0 : Number(v);
      this.series.push(n);
      if (this.series.length > 60) this.series.shift();
      if (this.hasChart) this.renderChart();
    },
    /* 自定义接口轮询（经后端白名单代理，仅 GET） */
    async pollCustom() {
      try {
        const d = await API.dashboardProxy(this.w.source.url);
        this.customJson = d.json;
        this.customErr = "";
        this.onTick();
      } catch (e) { this.customErr = e.message || "请求失败"; }
    },
    startPoll() {
      const period = Math.max(2, Number(this.w.source.period) || 5) * 1000;
      this.pollCustom();
      this._timer = setInterval(() => this.pollCustom(), period);
    },
    renderChart() {
      if (!this.hasChart) return;
      const data = this.series;
      const color = this.w.color || "#38bdf8";
      window.Charts.init(this.chartId, {
        grid: { left: 2, right: 2, top: 5, bottom: 2 },
        xAxis: { type: "category", show: false, data: data.map((_, i) => i) },
        yAxis: { type: "value", show: false, min: "dataMin", max: "dataMax", scale: true },
        series: [{
          type: "line", data, showSymbol: false, smooth: true,
          lineStyle: { width: 1.5, color }, areaStyle: { color, opacity: 0.12 },
        }],
      });
    },
    /* 控制开关卡：下发水泵/加热指令。
       配置了自定义指令 cmd:{on,off} → 经后端代理 GET 下发（log=1 写审计日志）；
       未配置 → 内置通道 API.pump/heater（后端权限与日志不变）。 */
    async toggleCtl(e) {
      const action = e.target.checked ? "on" : "off";
      if (!this.online) { e.target.checked = this.ctlOn; alert("设备离线，无法控制"); return; }
      if (!this.canCtl) { e.target.checked = this.ctlOn; alert("无控制权限"); return; }
      this.busy = true;
      try {
        const cmdUrl = (this.w.cmd || {})[action === "on" ? "on" : "off"];
        if (cmdUrl) {
          await API.dashboardProxy(cmdUrl, true);   // 代理 GET 下发 + 审计日志
        } else {
          const ctl = this.w.ctl === "heater" ? "heater" : "pump";
          const d = ctl === "pump" ? await API.pump(action) : await API.heater(action);
          Object.assign(this.realtime, d);
        }
      } catch (err) {
        e.target.checked = this.ctlOn;
        alert(err.message);
      } finally { this.busy = false; }
    },
    /* ---------- 内置操作卡动作（与旧固定操作区同一接口） ---------- */
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
    /* 累计水量清零（破坏性，二次确认） */
    async resetVolume() {
      if (!confirm("确定清零累计水量？\n\n该操作会清除设备上的累计值，不可恢复。")) return;
      this.resetBusy = true;
      try {
        const d = await API.volumeReset();
        Object.assign(this.realtime, d);
        alert("累计水量已清零");
      } catch (e) { alert(e.message); }
      finally { this.resetBusy = false; }
    },
  },
  template: `
  <div class="kpi-card wg-card" :class="{ editing }">
    <div class="wg-grip" v-if="editing" title="按住拖动调整位置">⠿</div>
    <div class="wg-tools" v-if="editing">
      <button class="wg-btn" @click="$emit('config', w)" title="配置卡片">✎</button>
      <button class="wg-btn danger" @click="$emit('remove', w)" title="删除卡片">✕</button>
    </div>

    <!-- 内置：采集设备链路卡 -->
    <template v-if="w.type === 'builtin' && w.builtin === 'device'">
      <div class="kpi-label">{{ w.title }}<span class="dot" :class="online ? 'green' : 'red'"></span></div>
      <div class="kpi-kv"><span>地址</span><b>{{ ipText }}</b></div>
      <div class="kpi-kv"><span>信号</span><b>{{ rssiText }}</b></div>
      <div class="kpi-kv"><span>运行</span><b>{{ uptimeText }}</b></div>
      <div class="kpi-foot kpi-warn" v-if="missingChannels.length"
           :title="'当前固件未提供：' + missingChannels.join('、') + '。升级固件后在 backend/config.py 的 DEVICE_FEATURES 中开启对应通道。'">
        未提供通道：{{ missingChannels.join('、') }}
      </div>
    </template>

    <!-- 内置：定量浇水卡 -->
    <template v-else-if="w.type === 'builtin' && w.builtin === 'quant'">
      <div class="op-card">
        <div class="op-title">{{ w.title }}</div>
        <div class="op-input">
          <input ref="targetInput" id="pump-target-input" type="number"
                 v-model.number="targetInput" min="0" step="0.1">
          <span class="op-unit">L</span>
          <button class="btn-primary" :disabled="targetBusy || !online || !canCtl" @click="saveTarget">设定</button>
          <button class="btn-ghost" :disabled="targetBusy || !online || !canCtl"
                  @click="targetInput = 0; saveTarget()">取消</button>
        </div>
        <div class="target-progress" v-if="targetProgress">
          <div class="tp-head">
            <span>已注入 {{ targetProgress.done.toFixed(3) }} / {{ targetProgress.target.toFixed(2) }} L</span>
            <b>{{ targetProgress.percent.toFixed(1) }}%</b>
          </div>
          <div class="tp-bar"><span :style="{ width: targetProgress.percent + '%' }"></span></div>
        </div>
        <div class="op-foot">达到目标由设备固件自动关泵；完成后设备会清零累计水量并取消目标</div>
        <div class="kpi-foot kpi-warn" v-if="!canCtl">无控制权限（需「设备控制」权限）</div>
        <div class="kpi-foot kpi-warn" v-else-if="!online">设备离线，操作不可用</div>
      </div>
    </template>

    <!-- 内置：恒温闭环卡 -->
    <template v-else-if="w.type === 'builtin' && w.builtin === 'pid'">
      <div class="op-card">
        <div class="op-title">{{ w.title }}
          <label class="toggle" :class="{ on: pid.pid_enabled }" style="float:right;">
            <input type="checkbox" :checked="pid.pid_enabled"
                   :disabled="pidBusy || !online || !perm('cfg_alarm')" @change="togglePid">
            <span class="toggle-track"><span class="toggle-thumb"></span></span>
          </label>
        </div>
        <div class="op-input">
          <input type="number" v-model.number="pid.target_temp" min="0" max="90" step="0.5" style="width:80px;">
          <span class="op-unit">℃ 目标</span>
          <button class="btn-primary" :disabled="pidBusy || !online || !perm('cfg_alarm')" @click="savePidTarget">设定</button>
        </div>
        <div class="op-input" style="margin-top:8px;gap:6px;">
          <input type="number" v-model.number="pid.kp" min="0" step="0.1" style="width:56px;" title="比例系数 Kp">
          <input type="number" v-model.number="pid.ki" min="0" step="0.1" style="width:56px;" title="积分系数 Ki">
          <input type="number" v-model.number="pid.kd" min="0" step="0.1" style="width:56px;" title="微分系数 Kd">
          <button class="btn-ghost" :disabled="pidBusy || !online || !perm('cfg_alarm')" @click="savePidParams">存参数</button>
        </div>
        <div class="op-foot">{{ pid.pid_enabled ? '闭环运行中：按加热槽水温自动开关加热' : '闭环已关闭；开启后按加热槽水温自动开关加热(目标±1℃)' }}</div>
        <div class="kpi-foot kpi-warn" v-if="!perm('cfg_alarm')">无配置权限（需「告警阈值」权限）</div>
        <div class="kpi-foot kpi-warn" v-else-if="!online">设备离线，操作不可用</div>
      </div>
    </template>

    <!-- 内置：最近操作卡 -->
    <template v-else-if="w.type === 'builtin' && w.builtin === 'recent'">
      <div class="op-card">
        <div class="op-title">{{ w.title }}</div>
        <ul class="op-log">
          <li v-for="(l, i) in recentLogs" :key="i">
            <span class="op-time">{{ (l.ts || '').slice(11, 16) }}</span>
            <span class="op-who" :class="{ sys: isSystemLog(l) }">{{ operatorText(l) }}</span>
            <span class="op-what">{{ actionLabel(l.action) }}</span>
          </li>
          <li v-if="!recentLogs.length" class="op-empty">暂无记录</li>
        </ul>
      </div>
    </template>

    <!-- 内置：清零累计卡（破坏性，二次确认） -->
    <template v-else-if="w.type === 'builtin' && w.builtin === 'reset'">
      <div class="op-card">
        <div class="op-title">{{ w.title }}</div>
        <button class="btn-ghost danger" :disabled="resetBusy || !online || !canCtl" @click="resetVolume">清零累计水量</button>
        <div class="op-foot">清除设备上的累计值，不可恢复，执行前二次确认</div>
        <div class="kpi-foot kpi-warn" v-if="!canCtl">无控制权限（需「设备控制」权限）</div>
      </div>
    </template>

    <!-- 状态卡（开/关） -->
    <template v-else-if="w.type === 'state'">
      <div class="kpi-label">{{ w.title }}</div>
      <div class="kpi-state" :class="stateClass">{{ stateText }}</div>
      <div class="kpi-foot" v-if="footText">{{ footText }}</div>
    </template>

    <!-- 水槽可视化卡（迷你罐体 + 水位百分比） -->
    <template v-else-if="w.type === 'tank'">
      <div class="kpi-label">{{ w.title }}<span class="wg-tank-tag">{{ tankTag }}</span></div>
      <div class="wg-tank-row">
        <div class="wg-tank">
          <div class="wg-tank-water" :style="{ height: tankPercent + '%', background: (w.color || '#38bdf8') }"></div>
        </div>
        <div class="wg-tank-info">
          <div class="kpi-value">{{ text }}<span class="kpi-unit">{{ w.unit || '%' }}</span></div>
          <div class="kpi-kv" v-if="tankVolumeText"><span>水量</span><b>{{ tankVolumeText }}</b></div>
          <div class="kpi-foot" v-if="footText">{{ footText }}</div>
        </div>
      </div>
    </template>

    <!-- 控制开关卡（真实下发指令，需设备控制权限；与右栏操作区一致不做二次确认） -->
    <template v-else-if="w.type === 'control'">
      <div class="kpi-label">{{ w.title }}<span class="dot" :class="online ? 'green' : 'red'"></span></div>
      <div class="wg-ctl">
        <label class="toggle" :class="{ on: ctlOn }">
          <input type="checkbox" :checked="ctlOn" :disabled="ctlDisabled" @change="toggleCtl">
          <span class="toggle-track"><span class="toggle-thumb"></span></span>
        </label>
        <span class="kpi-state" :class="stateClass">{{ stateText }}</span>
      </div>
      <div class="kpi-foot" v-if="footText">{{ footText }}</div>
      <div class="kpi-foot kpi-warn" v-if="!canCtl">无控制权限（需「设备控制」权限）</div>
      <div class="kpi-foot kpi-warn" v-else-if="!online">设备离线，控制不可用</div>
    </template>

    <!-- 数值 / 迷你曲线 / 趋势曲线 -->
    <template v-else>
      <div class="kpi-label">{{ w.title }}</div>
      <div class="kpi-value" :class="{ 'wg-bad': bad }">{{ text }}<span class="kpi-unit">{{ w.unit }}</span></div>
      <div v-if="hasChart" class="wg-chart" :id="chartId"></div>
      <div class="kpi-foot" v-if="footText">{{ footText }}</div>
      <div class="kpi-foot kpi-warn" v-if="customErr" :title="customErr">自定义源请求失败</div>
    </template>
  </div>
  `,
};
