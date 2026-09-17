/* 后端接口封装：统一处理返回格式、错误、token 注入与登录态。 */
window.API = (() => {
  const BASE = "";

  function notify(...args) {
    window.dispatchEvent(new CustomEvent("api-error", { detail: args[0] }));
  }

  async function req(method, path, body, opts = {}) {
    const headers = {};
    if (window.Auth && window.Auth.token) headers["Authorization"] = "Bearer " + window.Auth.token;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const fetchOpts = { method, headers };
    if (body !== undefined) fetchOpts.body = JSON.stringify(body);
    let resp;
    try {
      resp = await fetch(BASE + path, fetchOpts);
    } catch (e) {
      throw new Error("网络请求失败，请检查后端服务是否运行");
    }
    const json = await resp.json().catch(() => ({ code: -1, msg: "响应解析失败", data: null }));
    if (json.code === 40101) {
      if (window.Auth) window.Auth.logout();
      notify({ code: 40101, msg: json.msg });
      if (opts.silent) throw new Error(json.msg || "登录已过期");
      return json.data;
    }
    if (json.code === 40301) {
      if (!opts.silent) notify({ code: 40301, msg: json.msg || "无权限执行此操作" });
      throw new Error(json.msg || "无权限");
    }
    if (json.code !== 0) throw new Error(json.msg || "请求失败");
    return json.data;
  }

  function qs(params) {
    const p = {};
    if (params) {
      for (const [k, v] of Object.entries(params)) {
        if (v !== undefined && v !== null && v !== "") p[k] = v;
      }
    }
    const s = new URLSearchParams(p).toString();
    return s ? "?" + s : "";
  }

  return {
    // 账号
    login: (username, password) => req("POST", "/api/auth/login", { username, password }),
    me: () => req("GET", "/api/auth/me"),
    changePassword: (password) => req("POST", "/api/auth/change_password", { password }),
    users: () => req("GET", "/api/auth/users"),
    createUser: (body) => req("POST", "/api/auth/users", body),
    resetPassword: (id, password) => req("POST", `/api/auth/users/${id}/password`, { password }),
    setUserRole: (id, role) => req("POST", `/api/auth/users/${id}/role`, { role }),
    setUserStatus: (id, status) => req("POST", `/api/auth/users/${id}/status`, { status }),
    deleteUser: (id) => req("DELETE", `/api/auth/users/${id}`),
    roles: () => req("GET", "/api/auth/roles"),
    saveRoles: (roles) => req("POST", "/api/auth/roles", { roles }),

    // 水循环业务
    realtime: () => req("GET", "/api/water/realtime"),
    history: (start, end) =>
      req("GET", `/api/water/history?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`),
    stats: (start, end) =>
      req("GET", `/api/water/stats?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`),
    pump: (action) => req("POST", "/api/water/pump", { action }),
    heater: (action) => req("POST", "/api/water/heater", { action }),
    ingest: (data) => req("POST", "/api/water/ingest", data),
    // 定量浇水（设备固件侧达到目标自动关泵）
    pumpTargetGet: () => req("GET", "/api/water/pump/target"),
    pumpTargetSet: (liters) => req("POST", "/api/water/pump/target", { liters }),
    // 累计水量清零（破坏性操作）
    volumeReset: () => req("POST", "/api/water/volume/reset"),
    // 双水槽容积（用于把水位%换算成估算水量）
    tankSet: (tank, capacity) => req("POST", "/api/water/tank", { tank, capacity }),
    // 设备链路信息（IP/RSSI/运行时长/支持通道）
    device: () => req("GET", "/api/water/device"),
    setTarget: (temp) => req("POST", "/api/water/target", { temp }),
    pidMode: (enabled) => req("POST", "/api/water/pid/mode", { enabled }),
    pidGet: () => req("GET", "/api/water/pid"),
    pidSet: (p) => req("POST", "/api/water/pid", p),
    setPeriod: (period) => req("POST", "/api/water/period", { period }),
    alarmConfigGet: () => req("GET", "/api/water/alarm/config"),
    alarmConfigSet: (cfg) => req("POST", "/api/water/alarm/config", cfg),
    alarms: (params) => req("GET", "/api/water/alarms" + qs(params)),
    alarmStats: () => req("GET", "/api/water/alarm/stats"),
    logs: (params) => req("GET", "/api/water/logs" + qs(params)),
    judgeStatus: () => req("GET", "/api/water/judge/status"),
    judgeEnable: (enabled) => req("POST", "/api/water/judge/enable", { enabled: enabled ? 1 : 0 }),
    system: () => req("GET", "/api/water/system"),
    // 仪表盘布局（全局共享）与自定义卡片代理（白名单 GET 代发）
    dashboardLayoutGet: () => req("GET", "/api/water/dashboard/layout"),
    dashboardLayoutSave: (layout) => req("POST", "/api/water/dashboard/layout", { layout }),
    dashboardLayoutReset: () => req("POST", "/api/water/dashboard/layout/reset"),
    dashboardProxy: (url, log) => req("GET", "/api/water/dashboard/proxy?url=" + encodeURIComponent(url) + (log ? "&log=1" : "")),
  };
})();