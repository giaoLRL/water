/* 后端接口封装：统一处理返回格式与错误。 */
window.API = (() => {
  const BASE = "";

  async function req(method, path, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let resp;
    try {
      resp = await fetch(BASE + path, opts);
    } catch (e) {
      throw new Error("网络请求失败，请检查后端服务是否运行");
    }
    const json = await resp.json().catch(() => ({ code: -1, msg: "响应解析失败", data: null }));
    if (json.code !== 0) throw new Error(json.msg || "请求失败");
    return json.data;
  }

  return {
    lamps: () => req("GET", "/api/lampposts"),
    lamp: (id) => req("GET", `/api/lampposts/${id}`),
    history: (id, start, end) =>
      req("GET", `/api/lampposts/${id}/history?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`),
    stats: (id, start, end) =>
      req("GET", `/api/lampposts/${id}/stats?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`),
    control: (id, action) => req("POST", `/api/lampposts/${id}/control`, { action }),
    detect: (id) => req("POST", `/api/lampposts/${id}/detect`),
    detectCurrent: (id) => req("GET", `/api/lampposts/${id}/detect/current`),
    detections: (lampId) => req("GET", "/api/detections" + (lampId ? `?lamp_id=${lampId}` : "")),
    detection: (id) => req("GET", `/api/detections/${id}`),
    alarms: (lampId) => req("GET", "/api/alarms" + (lampId ? `?lamp_id=${lampId}` : "")),
    alarmConfigGet: () => req("GET", "/api/alarm/config"),
    alarmConfigSet: (cfg) => req("POST", "/api/alarm/config", cfg),
    logs: (lampId) => req("GET", "/api/logs" + (lampId ? `?lamp_id=${lampId}` : "")),
    system: () => req("GET", "/api/system"),
    videoUrl: (id) => `/api/lampposts/${id}/video`,
    detectVideoUrl: (id) => `/api/lampposts/${id}/detect_video`,
  };
})();