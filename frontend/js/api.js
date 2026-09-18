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

  /* 文件下载：带 Authorization 拉取二进制并触发浏览器保存。
     后端权限不足时仍返回统一 JSON（HTTP 200），这里按 content-type 区分后抛错。 */
  async function download(path) {
    const headers = {};
    if (window.Auth && window.Auth.token) headers["Authorization"] = "Bearer " + window.Auth.token;
    let resp;
    try {
      resp = await fetch(BASE + path, { headers });
    } catch (e) {
      throw new Error("导出失败：网络请求错误");
    }
    const ctype = resp.headers.get("content-type") || "";
    if (ctype.indexOf("application/json") !== -1) {
      const j = await resp.json().catch(() => ({}));
      throw new Error(j.msg || "导出失败");
    }
    const blob = await resp.blob();
    const cd = resp.headers.get("content-disposition") || "";
    const m = /filename="?([^";]+)"?/.exec(cd);
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = m ? m[1] : "export.csv";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    // 后端通过响应头告知实际导出条数与区间总条数（超限时保留最新数据）
    return {
      rows: Number(resp.headers.get("x-exported-rows") || 0),
      total: Number(resp.headers.get("x-total-rows") || 0),
      filename: a.download,
    };
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
    /* limit = 图上最大点数：区间再长也覆盖到最新数据，超出时后端等距降采样 */
    history: (start, end, limit) =>
      req("GET", `/api/water/history?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`
        + (limit ? `&limit=${limit}` : "")),
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
    // 报警联动（v3 卡片即来源）：触发源与动作都指向实时监控页的卡片
    alarmLinksGet: () => req("GET", "/api/water/alarm/links"),
    alarmLinksSet: (links) => req("POST", "/api/water/alarm/links", { links }),
    // 自定义传感器通道历史/统计（通道由实时监控页的卡片派生）
    customHistory: (channelId, start, end, limit) =>
      req("GET", "/api/water/custom/history" + qs({ channel_id: channelId, start, end, limit })),
    customStats: (channelId, start, end) => req("GET", "/api/water/custom/stats" + qs({ channel_id: channelId, start, end })),
    // CSV 导出（报表 / U 盘提交）：kind=sensors|alarms|logs|custom
    exportCsv: (params) => download("/api/water/export.csv" + qs(params)),
    // 外部设备接入（Modbus TCP / 串口服务器）
    gatewayGet: () => req("GET", "/api/water/gateway"),
    gatewaySet: (gateways) => req("POST", "/api/water/gateway", { gateways }),
    gatewayTest: (id) => req("GET", "/api/water/gateway/test" + qs({ id })),
    // 定时任务（简单 cron 式）
    timersGet: () => req("GET", "/api/water/timers"),
    timersSet: (timers) => req("POST", "/api/water/timers", { timers }),
    // 通道标定（scale/offset 与两点标定）
    calibrationGet: () => req("GET", "/api/water/calibration"),
    calibrationSet: (calibration) => req("POST", "/api/water/calibration", { calibration }),
    calibrationTwoPoint: (body) => req("POST", "/api/water/calibration/two_point", body),
    // 判定服务地址与报文模板
    judgeTemplateGet: () => req("GET", "/api/water/judge/template"),
    judgeTemplateSet: (url, template) => req("POST", "/api/water/judge/template", { url, template }),
    // 恒温闭环回路（一张闭环卡 = 一路独立 PID）
    pidLoops: () => req("GET", "/api/water/pid/loops"),
    pidLoopSet: (body) => req("POST", "/api/water/pid/loops", body),
    // 历史回放（时间轴 + 按时间点取快照）
    replayAt: (ts) => req("GET", "/api/water/replay" + qs({ ts })),
    replayFrames: (start, end, limit) => req("GET", "/api/water/replay/frames" + qs({ start, end, limit })),
    // 站点文案（浏览器标题/顶栏标题/副标题；GET 无需登录）
    siteGet: () => req("GET", "/api/water/site"),
    siteSet: (site) => req("POST", "/api/water/site", site),
  };
})();
