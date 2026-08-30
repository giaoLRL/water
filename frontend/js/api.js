/* 后端接口封装：统一处理返回格式、错误、token 注入与登录态。 */
window.API = (() => {
  const BASE = "";

  /* 401 登录失效 → 通知 app 跳登录页；403 无权限 → 静默（由调用处提示） */
  function notify(...args) {
    window.dispatchEvent(new CustomEvent("api-error", { detail: args[0] }));
  }

  async function req(method, path, body, opts = {}) {
    const headers = {};
    if (window.Auth && window.Auth.token) {
      headers["Authorization"] = "Bearer " + window.Auth.token;
    }
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
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
      // 登录失效：清空凭证，广播跳登录
      if (window.Auth) window.Auth.logout();
      notify({ code: 40101, msg: json.msg });
      if (opts.silent) throw new Error(json.msg || "登录已过期");
      return json.data;
    }
    if (json.code === 40301) {
      if (!opts.silent) {
        // 有明确交互时提示；轮询等静默场景不打扰
        notify({ code: 40301, msg: json.msg || "无权限执行此操作" });
      }
      throw new Error(json.msg || "无权限");
    }
    if (json.code !== 0) throw new Error(json.msg || "请求失败");
    return json.data;
  }

  // 拼接查询串：自动合并 lamp_id，过滤 undefined 值，避免 URLSearchParams 把它们序列化成 "undefined" 字符串
  function qs(lampId, params) {
    const p = {};
    if (lampId) p.lamp_id = lampId;
    if (params) {
      for (const [k, v] of Object.entries(params)) {
        if (v !== undefined && v !== null && v !== "") p[k] = v;
      }
    }
    const s = new URLSearchParams(p).toString();
    return s ? "?" + s : "";
  }

  function videoUrlOf(path) {
    // 视频流用 <img> 直连，token 挂 query；未登录时返回空串（前端不会渲染）
    const token = (window.Auth && window.Auth.token) || "";
    return token ? `${path}?token=${encodeURIComponent(token)}` : "";
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

    // 业务
    lamps: () => req("GET", "/api/lampposts"),
    lamp: (id) => req("GET", `/api/lampposts/${id}`),
    history: (id, start, end) =>
      req("GET", `/api/lampposts/${id}/history?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`),
    stats: (id, start, end) =>
      req("GET", `/api/lampposts/${id}/stats?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`),
    control: (id, action) => req("POST", `/api/lampposts/${id}/control`, { action }),
    detect: (id) => req("POST", `/api/lampposts/${id}/detect`),
    detectCurrent: (id) => req("GET", `/api/lampposts/${id}/detect/current`),
    detections: (lampId, params) => req("GET", "/api/detections" + qs(lampId, params)),
    detection: (id) => req("GET", `/api/detections/${id}`),
    alarms: (lampId, params) => req("GET", "/api/alarms" + qs(lampId, params)),
    alarm: (id) => req("GET", `/api/alarms/${id}`),
    alarmStats: (lampId) => req("GET", "/api/alarm/stats" + qs(lampId, {})),
    alarmConfigGet: () => req("GET", "/api/alarm/config"),
    alarmConfigSet: (cfg) => req("POST", "/api/alarm/config", cfg),
    logs: (lampId, params) => req("GET", "/api/logs" + qs(lampId, params)),
    system: () => req("GET", "/api/system"),
    sysConfigGet: () => req("GET", "/api/sysconfig"),
    sysConfigSet: (body) => req("POST", "/api/sysconfig", body),
    videoUrl: (id) => videoUrlOf(`/api/lampposts/${id}/video`),
    detectVideoUrl: (id) => videoUrlOf(`/api/lampposts/${id}/detect_video`),
  };
})();