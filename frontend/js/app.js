/* 根应用：登录 → 灯杆列表 → 灯杆详情 三级导航 + 全局轮询 + 权限路由 */
const { createApp, reactive, computed } = Vue;

const SVG_ATTRS = 'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"';
const ICONS = {
  logo: `<svg ${SVG_ATTRS} width="18" height="18"><path d="M8 3v18M8 6h10l-4 3 4 3H8"/><path d="M4 21h18"/></svg>`,
  bell: `<svg ${SVG_ATTRS} width="14" height="14"><path d="M12 3a6 6 0 0 1 6 6c0 4.2 1.5 5.7 2 6H4c.5-.3 2-1.8 2-6a6 6 0 0 1 6-6Z"/><path d="M10 19a2.2 2.2 0 0 0 4 0"/></svg>`,
  gear: `<svg ${SVG_ATTRS} width="14" height="14"><circle cx="12" cy="12" r="3.2"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.09a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h.09a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.09a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z"/></svg>`,
  user: `<svg ${SVG_ATTRS} width="14" height="14"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>`,
  logout: `<svg ${SVG_ATTRS} width="14" height="14"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>`,
};

const app = createApp({
  components: {
    loginView: window.ViewLogin,
    lampList: window.ViewLampList,
    lampDetail: window.ViewLampDetail,
    sysConfig: window.ViewSysConfig,
    alarmCenter: window.ViewAlarmCenter,
  },
  setup() {
    const state = reactive({
      view: "login",           // login / list / detail / config / alarmCenter
      currentLampId: "",
      detailTab: "monitor",    // 从告警中心跳转时指定详情页 tab
      lamps: [],
      devices: [],
      activeAlarmCount: 0,
      serverTime: "",
      authTip: "",             // 403 权限提示条
    });

    const perm = (p) => window.Auth.has(p);
    // 当前登录用户（reactive 跟随 Auth 状态变化）
    const authUser = computed(() => (window.Auth && window.Auth.user) || null);
    // 灯杆在线数：全部设备（温湿度/光照/烟雾/视频）在线才算
    const onlineCount = computed(() =>
      (state.lamps || []).filter((l) => l.lamp_online).length
    );

    function startPoll() {
      window.clearInterval(window.__pollLamps);
      window.clearInterval(window.__pollSystem);
      window.__pollLamps = setInterval(pollLamps, 2000);
      window.__pollSystem = setInterval(pollSystem, 5000);
      pollLamps();
      pollSystem();
    }
    function stopPoll() {
      window.clearInterval(window.__pollLamps);
      window.clearInterval(window.__pollSystem);
    }

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
      state.detailTab = "monitor";
      state.view = "detail";
      window.scrollTo(0, 0);
    }
    function openConfig() {
      state.view = "config";
      window.scrollTo(0, 0);
    }
    function openAlarmCenter() {
      state.view = "alarmCenter";
      window.scrollTo(0, 0);
    }
    function gotoLampAlarm(id) {
      // 全站告警中心"前往灯杆"：进入详情页并停到告警记录 tab
      state.currentLampId = id;
      state.detailTab = "alarm";
      state.view = "detail";
      window.scrollTo(0, 0);
    }
    function backToList() {
      state.view = "list";
      state.currentLampId = "";
      pollLamps();
      pollSystem();
    }
    function onLogged() {
      state.view = "list";
      state.authTip = "";
      startPoll();
    }
    function doLogout() {
      window.Auth.logout();
      stopPoll();
      state.view = "login";
      state.authTip = "";
    }

    // 登录态校验
    window.Auth.init().then(() => {
      if (window.Auth.user) {
        onLogged();
      } else {
        state.view = "login";
      }
    });

    // 全局监听：401 → 登出；403 → 顶部提示条
    window.addEventListener("api-error", (ev) => {
      const d = ev.detail || {};
      if (d.code === 40101) {
        stopPoll();
        state.view = "login";
        state.authTip = "";
      } else if (d.code === 40301) {
        state.authTip = d.msg || "无权限执行此操作";
        setTimeout(() => { state.authTip = ""; }, 4000);
      }
    });

    return {
      state, ICONS, perm, authUser, onlineCount,
      openDetail, openConfig, openAlarmCenter, gotoLampAlarm, backToList,
      onLogged, doLogout,
    };
  },
  template: `
  <div class="app-root" v-if="state.view !== 'login'">
    <header>
      <div class="brand">
        <div class="logo" v-html="ICONS.logo"></div>
        <div>
          <h1>基于物联网的分布式智慧灯杆监控系统</h1>
          <div class="subtitle">多灯杆环境监测 · 视频监控 · 人员智能识别</div>
        </div>
      </div>
      <div class="status-chips">
        <span class="chip clickable" v-if="perm('cfg_system')" @click="openConfig"><span v-html="ICONS.gear" style="vertical-align:-2px;"></span> 系统配置</span>
        <span class="chip" v-if="perm('view_device')"><span class="dot" :class="onlineCount ? 'green' : 'red'"></span>在线灯杆 {{ onlineCount }}</span>
        <span class="chip bell clickable" v-if="perm('view_alarm')" title="查看全站告警" @click="openAlarmCenter"><span v-html="ICONS.bell"></span><span class="badge" v-if="state.activeAlarmCount">{{ state.activeAlarmCount }}</span></span>
        <span class="chip" v-if="perm('view_alarm')">活跃告警 {{ state.activeAlarmCount }}</span>
        <span class="chip" v-if="perm('view_device')">{{ state.serverTime || "--" }}</span>
        <span class="chip user-chip"><span v-html="ICONS.user" style="vertical-align:-2px;"></span>
          {{ authUser ? authUser.username : '--' }}
          <em class="role-tag">{{ authUser ? authUser.role_label : '' }}</em>
        </span>
        <span class="chip clickable" title="退出登录" @click="doLogout"><span v-html="ICONS.logout" style="vertical-align:-2px;"></span> 退出</span>
      </div>
    </header>

    <div v-if="state.authTip" class="auth-tip">{{ state.authTip }}</div>

    <main class="main">
      <lampList v-if="state.view === 'list' && perm('view_monitor')" :lamps="state.lamps" :devices="state.devices" @open="openDetail"></lampList>
      <lampDetail v-else-if="state.view === 'detail' && perm('view_monitor')" :lamp-id="state.currentLampId" :initial-tab="state.detailTab" @back="backToList"></lampDetail>
      <sysConfig v-else-if="state.view === 'config' && perm('cfg_system')" @back="backToList"></sysConfig>
      <alarmCenter v-else-if="state.view === 'alarmCenter' && perm('view_alarm')" @back="backToList" @goto="gotoLampAlarm"></alarmCenter>
      <div v-else-if="state.view === 'list' || state.view === 'detail' || state.view === 'config' || state.view === 'alarmCenter'" class="view-page">
        <div class="no-perm">当前账号无权限访问该页面</div>
      </div>
    </main>
  </div>
  <loginView v-else @logged="onLogged"></loginView>
  `,
});

app.mount("#app");