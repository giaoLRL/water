/* 双水槽循环回路可视化组件。
 *
 * 把「储水槽 → 水泵 → 加热槽 → 回流」这个物理闭环画成一张整体 SVG：
 * 两个罐体（水位 / 波浪 / 气泡 / 刻度）+ 连接管路 + 管路上的水泵节点 + 水流粒子。
 * 相比"两个并排卡片"，这样能一眼看出这是一个循环系统而不是两个孤立容器。
 *
 * 注意：SVG 内的 id（渐变、裁剪）必须唯一，同页多实例时否则会互相串位，
 * 因此统一带 uid 后缀。
 *
 * 水位不做任何模拟：无数据一律显示 0，并用角标标明原因（「无传感器」/「离线」）。
 */
let waterLoopUid = 0;

window.WaterLoopViz = {
  name: "WaterLoopViz",
  props: {
    storage: { type: Object, default: () => ({}) },
    heater: { type: Object, default: () => ({}) },
    flowing: { type: Boolean, default: false },   // 是否有水流（决定管路动画）
    pumpOn: { type: Boolean, default: false },
    flowText: { type: String, default: "--" },
    online: { type: Boolean, default: false },    // 设备是否在线（离线时水位恒为 0 并标注）
  },
  data() {
    return {
      uid: 0,
      // 罐体几何（坐标系 viewBox 0 0 620 470）
      geo: { top: 60, bottom: 410, w: 200, h: 350 },
      tanks: [
        { key: "storage", label: "储水槽", x: 30 },
        { key: "heater", label: "加热槽", x: 390 },
      ],
      // 气泡固定分布（相对罐体左沿的偏移）+ 各自动画时长
      bubbles: [
        { dx: 34, r: 3, delay: 0.0, dur: 4.6 },
        { dx: 72, r: 2, delay: 1.1, dur: 5.4 },
        { dx: 106, r: 4, delay: 2.3, dur: 4.2 },
        { dx: 140, r: 2.5, delay: 0.6, dur: 5.0 },
        { dx: 56, r: 2, delay: 3.1, dur: 5.8 },
        { dx: 122, r: 3, delay: 1.7, dur: 4.4 },
      ],
      marks: [0, 25, 50, 75, 100],
    };
  },
  created() {
    waterLoopUid += 1;
    this.uid = waterLoopUid;
  },
  computed: {
    gradId() { return `loopGrad${this.uid}`; },
    bodyClipId() { return `loopBody${this.uid}`; },
    waterClipIds() {
      const m = {};
      this.tanks.forEach((t) => { m[t.key] = `loopWater${this.uid}_${t.key}`; });
      return m;
    },
    pumpText() { return this.pumpOn ? "水泵运行" : "水泵停止"; },
    showFlow() { return this.flowing ? this.flowText : "0.00"; },
  },
  methods: {
    info(key) { return (key === "storage" ? this.storage : this.heater) || {}; },
    pct(key) {
      const p = Number(this.info(key).percent);
      return isNaN(p) ? 0 : Math.max(0, Math.min(100, p));
    },
    /* 水位角标：说明该水位为何不是有效实测值（无模拟数据，无数据一律显示 0）
       未接入传感器 → 「无传感器」；已接入但设备离线 → 「离线」；正常实测 → 不显示 */
    unavailable(key) { return this.info(key).source !== "device"; },
    tagOf(key) {
      if (this.unavailable(key)) return "无传感器";
      return this.online ? "" : "离线";
    },
    waterHeight(key) { return this.geo.h * this.pct(key) / 100; },
    waterTop(key) { return this.geo.bottom - this.waterHeight(key); },
    markY(p) { return this.geo.bottom - this.geo.h * p / 100; },
    /* 波浪路径：从罐体左沿起 8 个 40px 周期(总宽 320)，再闭合到罐底。
       CSS 水平平移 80px(=2 个周期)后无缝循环，衔接无痕。 */
    wavePath(x0, key, offset) {
      const y = this.waterTop(key) + offset;
      let d = `M ${x0} ${y} q 20 -7 40 0`;
      for (let i = 0; i < 7; i += 1) d += " t 40 0";
      return `${d} V ${this.geo.bottom} H ${x0} Z`;
    },
  },
  template: `
  <div class="loop-wrap">
    <svg viewBox="0 0 620 470" class="loop-svg" preserveAspectRatio="xMidYMid meet"
         role="img" aria-label="双水槽循环回路示意">
      <defs>
        <linearGradient :id="gradId" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#7dd3fc" stop-opacity="0.95"/>
          <stop offset="100%" stop-color="#0369a1" stop-opacity="0.95"/>
        </linearGradient>
        <clipPath :id="bodyClipId">
          <rect v-for="t in tanks" :key="t.key" :x="t.x" :y="geo.top"
                :width="geo.w" :height="geo.h" rx="14"/>
        </clipPath>
        <clipPath v-for="t in tanks" :key="'wc' + t.key" :id="waterClipIds[t.key]">
          <rect :x="t.x" :y="waterTop(t.key)" :width="geo.w" :height="waterHeight(t.key)"/>
        </clipPath>
      </defs>

      <!-- 循环管路：出水（储水槽→加热槽）与回流（加热槽→储水槽） -->
      <g class="loop-pipes" :class="{ flowing: flowing }">
        <line class="loop-pipe-line" x1="230" y1="140" x2="390" y2="140"/>
        <line class="loop-pipe-line" x1="390" y1="330" x2="230" y2="330"/>
        <circle v-for="i in 4" :key="'o' + i" class="loop-particle out"
                cx="238" cy="140" r="3.4" :style="{ animationDelay: (i - 1) * 0.42 + 's' }"/>
        <circle v-for="i in 4" :key="'b' + i" class="loop-particle back"
                cx="382" cy="330" r="3.4" :style="{ animationDelay: (i - 1) * 0.42 + 's' }"/>
      </g>

      <!-- 罐体底板与外框 -->
      <g v-for="t in tanks" :key="'body' + t.key">
        <text class="loop-tank-label" :x="t.x + geo.w / 2" y="42" text-anchor="middle">{{ t.label }}</text>
        <rect class="tank-body" :x="t.x" :y="geo.top" :width="geo.w" :height="geo.h" rx="14"/>
      </g>

      <!-- 水体 + 气泡 + 波浪（整体裁剪在罐体内） -->
      <g :clip-path="'url(#' + bodyClipId + ')'">
        <g v-for="t in tanks" :key="'w' + t.key">
          <rect :x="t.x" :y="waterTop(t.key)" :width="geo.w" :height="waterHeight(t.key)"
                :fill="'url(#' + gradId + ')'"/>
          <g :clip-path="'url(#' + waterClipIds[t.key] + ')'">
            <circle v-for="(b, i) in bubbles" :key="i" class="tank-bubble"
                    :cx="t.x + b.dx" cy="404" :r="b.r"
                    :style="{ animationDuration: b.dur + 's', animationDelay: b.delay + 's' }"/>
          </g>
          <path class="tank-wave w1" :d="wavePath(t.x, t.key, 0)"/>
          <path class="tank-wave w2" :d="wavePath(t.x, t.key, 7)"/>
        </g>
      </g>

      <!-- 外框与刻度（刻度画在罐体外侧，保持罐内干净） -->
      <g v-for="t in tanks" :key="'frame' + t.key">
        <rect class="tank-frame" :x="t.x" :y="geo.top" :width="geo.w" :height="geo.h" rx="14"/>
        <g class="tank-scale">
          <line v-for="p in marks" :key="p"
                :x1="t.key === 'storage' ? t.x - 11 : t.x + geo.w"
                :y1="markY(p)"
                :x2="t.key === 'storage' ? t.x : t.x + geo.w + 11"
                :y2="markY(p)"/>
        </g>
      </g>

      <!-- 水位百分比与副标题 -->
      <g v-for="t in tanks" :key="'txt' + t.key">
        <text class="tank-pct" :x="t.x + geo.w / 2" y="242" text-anchor="middle">{{ pct(t.key).toFixed(0) }}%</text>
        <text v-if="tagOf(t.key)" class="tank-tag" :x="t.x + geo.w / 2" y="270" text-anchor="middle">{{ tagOf(t.key) }}</text>
        <text class="loop-tank-sub" :x="t.x + geo.w / 2" y="440" text-anchor="middle">{{ info(t.key).volume ?? '--' }} L · 水深 {{ info(t.key).height_cm ?? '--' }} cm</text>
      </g>

      <!-- 水泵节点（画在出水管路上） -->
      <g class="loop-pump" :class="{ on: pumpOn }">
        <circle class="pump-ring" cx="310" cy="140" r="26"/>
        <g class="impeller">
          <path class="pump-blade" d="M310 124 L319 140 L301 140 Z"/>
          <path class="pump-blade" d="M324 149 L308 153 L312 135 Z"/>
          <path class="pump-blade" d="M296 149 L303 133 L317 153 Z"/>
        </g>
        <circle class="pump-hub" cx="310" cy="140" r="4.5"/>
      </g>
      <text class="loop-pump-label" x="310" y="192" text-anchor="middle">{{ pumpText }}</text>
      <text class="loop-flow-label" x="310" y="106" text-anchor="middle">{{ showFlow }} L/min</text>
    </svg>
  </div>
  `,
};
