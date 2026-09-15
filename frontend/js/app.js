/* 根应用：登录 → 综合面板(实时/历史/统计/告警) + 判定服务页 + 系统配置页 */
const { createApp, reactive, computed } = Vue;

const ICONS = {
  drop: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="18" height="18" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2.7S6 9 6 14a6 6 0 0 0 12 0c0-5-6-11.3-6-11.3Z"/><path d="M9.5 15a2.5 2.5 0 0 0 2.5 2.5"/></svg>`,
  bell: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="14" height="14" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a6 6 0 0 1 6 6c0 4.2 1.5 5.7 2 6H4c.5-.3 2-1.8 2-6a6 6 0 0 1 6-6Z"/><path d="M10 19a2.2 2.2 0 0 0 4 0"/></svg>`,
  gear: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="14" height="14" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3.2"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.09a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h.09a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.09a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z"/></svg>`,
  judge: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="14" height="14" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 3 14h9l-1 8 10-12h-9l1-8Z"/></svg>`,
  user: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="14" height="14" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>`,
  logout: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="14" height="14" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>`,
};

const app = createApp({
  components: {
    loginView: window.ViewLogin,
    waterDash: window.ViewWaterDash,
    judgePanel: window.ViewJudgePanel,
    sysConfig: window.ViewSysConfig,
  },
  setup() {
    const state = reactive({
      view: "login",
      realtime: {},
      activeAlarmCount: 0,
      serverTime: "",
      authTip: "",
      alarmPopups: [],
    });

    const perm = (p) => window.Auth.has(p);
    const seenAlarmKeys = new Set();
    let alarmInitDone = false;
    const popTimers = {};
    const authUser = computed(() => (window.Auth && window.Auth.user) || null);

    function dismissAlarmPopup(key) {
      const t = popTimers[key];
      if (t) { clearTimeout(t); delete popTimers[key]; }
      state.alarmPopups = state.alarmPopups.filter((p) => p.key !== key);
    }

    function checkNewAlarms() {
      const keys = [];
      (state.realtime.active_alarms || []).forEach((a) => keys.push(a.type));
      if (!alarmInitDone) {
        keys.forEach((k) => seenAlarmKeys.add(k));
        alarmInitDone = true;
        return;
      }
      keys.forEach((k) => {
        if (seenAlarmKeys.has(k) || state.alarmPopups.some((p) => p.key === k)) return;
        seenAlarmKeys.add(k);
        const a = (state.realtime.active_alarms || []).find((x) => x.type === k);
        if (!a) return;
        if (state.alarmPopups.length >= 3) dismissAlarmPopup(state.alarmPopups[0].key);
        const pop = {
          key: k, typeLabel: a.label || a.type,
          detail: (a.message || `${a.label || a.type} ${a.value ?? ""}`).trim(),
        };
        state.alarmPopups.push(pop);
        popTimers[k] = setTimeout(() => dismissAlarmPopup(k), 6000);
      });
    }

    function startPoll() {
      clearInterval(window.__pollWater);
      clearInterval(window.__pollSystem);
      window.__pollWater = setInterval(pollRealtime, 2000);
      window.__pollSystem = setInterval(pollSystem, 5000);
      pollRealtime();
      pollSystem();
    }
    function stopPoll() {
      clearInterval(window.__pollWater);
      clearInterval(window.__pollSystem);
    }
    async function pollRealtime() {
      try {
        const d = await API.realtime();
        if (d) {
          state.realtime = d;
          state.activeAlarmCount = d.alarm_count || 0;
          checkNewAlarms();
        }
      } catch (e) { /* silent */ }
    }
    async function pollSystem() {
      try {
        const d = await API.system();
        state.serverTime = d.server_time || state.serverTime;
      } catch (e) { /* silent */ }
    }

    function openJudge() { state.view = "judge"; window.scrollTo(0, 0); }
    function openConfig() { state.view = "config"; window.scrollTo(0, 0); }
    function backHome() { state.view = "dashboard"; pollRealtime(); window.scrollTo(0, 0); }
    function onLogged() {
      state.view = "dashboard";
      state.authTip = "";
      startPoll();
    }
    function doLogout() {
      window.Auth.logout();
      stopPoll();
      Object.values(popTimers).forEach(clearTimeout);
      for (const k of Object.keys(popTimers)) delete popTimers[k];
      state.alarmPopups = [];
      seenAlarmKeys.clear();
      alarmInitDone = false;
      state.view = "login";
    }

    window.Auth.init().then(() => {
      if (window.Auth.user) onLogged();
      else state.view = "login";
    });

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
      state, ICONS, perm, authUser,
      openJudge, openConfig, backHome, onLogged, doLogout,
      dismissAlarmPopup,
    };
  },
  template: `
  <div class="app-root" v-if="state.view !== 'login'">
    <header>
      <div class="brand">
        <div class="logo"><span v-html="ICONS.drop"></span></div>
        <div>
          <h1>智能水循环监测与温控系统</h1>
          <div class="subtitle">水循环监测 · 流量计量 · 定量浇水 · 报警 · 数据统计</div>
        </div>
      </div>
      <div class="status-chips">
        <span class="chip clickable" v-if="perm('view_device')" @click="openJudge"><span v-html="ICONS.judge" style="vertical-align:-2px;"></span> 判定服务</span>
        <span class="chip clickable" v-if="perm('cfg_system')" @click="openConfig"><span v-html="ICONS.gear" style="vertical-align:-2px;"></span> 系统配置</span>
        <span class="chip bell clickable" v-if="perm('view_alarm')"><span v-html="ICONS.bell"></span><span class="badge" v-if="state.activeAlarmCount">{{ state.activeAlarmCount }}</span></span>
        <span class="chip" v-if="perm('view_alarm')">活跃告警 {{ state.activeAlarmCount }}</span>
        <span class="chip" v-if="perm('view_device')">{{ state.serverTime || '--' }}</span>
        <span class="chip user-chip"><span v-html="ICONS.user" style="vertical-align:-2px;"></span>
          {{ authUser ? authUser.username : '--' }}
          <em class="role-tag">{{ authUser ? authUser.role_label : '' }}</em>
        </span>
        <span class="chip clickable" title="退出登录" @click="doLogout"><span v-html="ICONS.logout" style="vertical-align:-2px;"></span> 退出</span>
      </div>
    </header>

    <div v-if="state.authTip" class="auth-tip">{{ state.authTip }}</div>

    <div class="alarm-popups" v-if="state.alarmPopups.length">
      <div class="alarm-popup" v-for="p in state.alarmPopups" :key="p.key">
        <span class="ap-badge">告警</span>
        <div class="ap-body">
          <div class="ap-title">{{ p.typeLabel }}</div>
          <div class="ap-desc">{{ p.detail }}</div>
        </div>
        <span class="ap-close" title="关闭" @click.stop="dismissAlarmPopup(p.key)">×</span>
      </div>
    </div>

    <main class="main">
      <waterDash v-if="state.view === 'dashboard' && perm('view_monitor')" :realtime="state.realtime"></waterDash>
      <judgePanel v-else-if="state.view === 'judge' && perm('view_device')" @back="backHome"></judgePanel>
      <sysConfig v-else-if="state.view === 'config' && perm('cfg_system')" @back="backHome"></sysConfig>
      <div v-else-if="state.view === 'dashboard' || state.view === 'judge' || state.view === 'config'" class="view-page">
        <div class="no-perm">当前账号无权限访问该页面</div>
      </div>
    </main>
  </div>
  <loginView v-else @logged="onLogged"></loginView>
  `,
});

app.mount("#app");