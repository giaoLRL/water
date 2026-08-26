/* 根应用：灯杆列表 → 灯杆详情 两级导航 + 全局轮询 */
const { createApp, reactive } = Vue;

const SVG_ATTRS = 'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"';
const ICONS = {
  logo: `<svg ${SVG_ATTRS} width="18" height="18"><path d="M8 3v18M8 6h10l-4 3 4 3H8"/><path d="M4 21h18"/></svg>`,
  bell: `<svg ${SVG_ATTRS} width="14" height="14"><path d="M12 3a6 6 0 0 1 6 6c0 4.2 1.5 5.7 2 6H4c.5-.3 2-1.8 2-6a6 6 0 0 1 6-6Z"/><path d="M10 19a2.2 2.2 0 0 0 4 0"/></svg>`,
};

const app = createApp({
  components: {
    lampList: window.ViewLampList,
    lampDetail: window.ViewLampDetail,
  },
  setup() {
    const state = reactive({
      view: "list",
      currentLampId: "",
      lamps: [],
      devices: [],
      activeAlarmCount: 0,
      serverTime: "",
    });

    async function pollLamps() {
      try {
        const d = await API.lamps();
        state.lamps = d.lampposts || [];
        state.activeAlarmCount = state.lamps.reduce((s, l) => s + (l.alarm_count || 0), 0);
      } catch (e) { /* silent */ }
    }

    async function pollSystem() {
      try {
        const d = await API.system();
        state.serverTime = d.server_time || "";
        state.devices = d.devices || [];
      } catch (e) { /* silent */ }
    }

    function openDetail(id) {
      state.currentLampId = id;
      state.view = "detail";
      window.scrollTo(0, 0);
    }
    function backToList() {
      state.view = "list";
      state.currentLampId = "";
      pollLamps();
    }

    pollLamps();
    pollSystem();
    setInterval(pollLamps, 2000);
    setInterval(pollSystem, 5000);

    return { state, ICONS, openDetail, backToList };
  },
  template: `
  <div class="app-root">
    <header>
      <div class="brand">
        <div class="logo" v-html="ICONS.logo"></div>
        <div>
          <h1>基于物联网的分布式智慧灯杆监控系统</h1>
          <div class="subtitle">多灯杆环境监测 · 视频监控 · 人员智能识别</div>
        </div>
      </div>
      <div class="status-chips">
        <span class="chip"><span class="dot green"></span>在线灯杆 {{ state.lamps.length }}</span>
        <span class="chip bell"><span v-html="ICONS.bell"></span><span class="badge" v-if="state.activeAlarmCount">{{ state.activeAlarmCount }}</span></span>
        <span class="chip">活跃告警 {{ state.activeAlarmCount }}</span>
        <span class="chip">{{ state.serverTime || "--" }}</span>
      </div>
    </header>

    <main class="main">
      <lampList v-if="state.view === 'list'" :lamps="state.lamps" :devices="state.devices" @open="openDetail"></lampList>
      <lampDetail v-else :lamp-id="state.currentLampId" @back="backToList"></lampDetail>
    </main>
  </div>
  `,
});

app.mount("#app");