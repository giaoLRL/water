/* 仪表盘卡片体系：卡片类型、内置卡片目录、默认布局生成、取值路径解析。
 *
 * 一张卡片 = 一份 JSON 配置（存后端 sys.dashboard_layout，全局共享）：
 *   { id, type, title, unit, decimals, color, foot, builtin, catalogKey, domId,
 *     source: { kind:"realtime", path } | { kind:"custom", url, path, period },
 *     thresholds: { min, max } | { minKey, maxKey } | null,
 *     grid: { x, y, w, h } }        // 12 列网格，h 以 38px 为一行
 * type: value(数值卡) / spark(数值+迷你曲线) / line(趋势曲线) / state(状态卡) / tank(水槽可视化) /
 *       control(控制开关) / builtin(内置特殊卡)
 */
window.Widgets = (() => {
  const TYPES = {
    value: "数值卡", spark: "数值+迷你曲线", line: "趋势曲线", state: "状态卡",
    tank: "水槽可视化", control: "控制开关", builtin: "内置卡",
  };

  /* 按 "a.b.c" 路径从对象取值，取不到返回 undefined */
  function resolvePath(obj, path) {
    if (!obj || !path) return undefined;
    return String(path).split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj);
  }

  function fmtVal(v, decimals) {
    if (v === null || v === undefined || v === "" || isNaN(Number(v))) return "--";
    return Number(v).toFixed(decimals === 0 ? 0 : (decimals || 1));
  }

  /* 开关类取值归一化：把布尔、0/1、字符串 on/off/true/false 统一归到
     "on" / "off" / "unknown"。
     本机通道由后端归一化成字符串（pump_state="on"/"off"），而自定义接口常直接返回
     布尔值（如 {"pump2":true}）；控制开关卡与状态卡共用本函数后两种来源都能正确显示。 */
  function ctlState(v) {
    if (v === true) return "on";
    if (v === false) return "off";
    if (v === null || v === undefined) return "unknown";
    const s = String(v).trim().toLowerCase();
    if (s === "" ) return "unknown";
    if (s === "on" || s === "true" || s === "1") return "on";
    if (s === "off" || s === "false" || s === "0") return "off";
    return "unknown";
  }

  let seq = 0;
  const uid = () => "w" + Date.now().toString(36) + (seq++).toString(36);

  /* 内置卡片目录：feature 指向 DEVICE_FEATURES 的能力键（为空表示始终可添加）。
     domId 用于兼容既有测试/样式锚点（如 #spark-flow）。 */
  const CATALOG = [
    { key: "flow", title: "瞬时流量", type: "spark", unit: "L/min", decimals: 2, color: "#38bdf8",
      feature: "flow", source: { kind: "realtime", path: "flow_rate" }, domId: "spark-flow",
      grid: { w: 6, h: 3 } },
    { key: "total", title: "累计水量", type: "spark", unit: "L", decimals: 3, color: "#2dd4bf",
      feature: "volume", source: { kind: "realtime", path: "total_liters" }, domId: "spark-total",
      grid: { w: 6, h: 3 } },
    { key: "storage_temp", title: "储水槽水温", type: "value", unit: "℃", decimals: 1, color: "#fb923c",
      feature: "temperature", source: { kind: "realtime", path: "storage_temp" },
      thresholds: { minKey: "storage_temp_min", maxKey: "storage_temp_max" }, grid: { w: 3, h: 2 } },
    { key: "heater_temp", title: "加热槽水温", type: "value", unit: "℃", decimals: 1, color: "#f87171",
      feature: "temperature", source: { kind: "realtime", path: "heater_temp" },
      thresholds: { minKey: "heater_temp_min", maxKey: "heater_temp_max" }, grid: { w: 3, h: 2 } },
    { key: "pressure", title: "水压", type: "value", unit: "kPa", decimals: 1, color: "#a78bfa",
      feature: "pressure", source: { kind: "realtime", path: "pressure" },
      thresholds: { minKey: "pressure_min", maxKey: "pressure_max" }, grid: { w: 3, h: 2 } },
    { key: "light", title: "光照", type: "value", unit: "lx", decimals: 1, color: "#facc15",
      feature: "light", source: { kind: "realtime", path: "light" },
      thresholds: { minKey: "light_min", maxKey: "light_max" }, grid: { w: 3, h: 2 } },
    { key: "level_storage", title: "储水槽水位", type: "value", unit: "%", decimals: 1, color: "#38bdf8",
      source: { kind: "realtime", path: "level_storage" }, foot: "无传感器 · 恒为 0",
      grid: { w: 6, h: 2 } },
    { key: "level_heater", title: "加热槽水位", type: "value", unit: "%", decimals: 1, color: "#2dd4bf",
      feature: "level_heater", source: { kind: "realtime", path: "level_heater" }, foot: "超声波实测",
      grid: { w: 6, h: 2 } },
    { key: "pump_state", title: "水泵状态", type: "state", color: "#2dd4bf",
      feature: "pump", source: { kind: "realtime", path: "pump_state" }, grid: { w: 6, h: 2 } },
    { key: "heater_state", title: "加热状态", type: "state", color: "#f87171",
      feature: "heater", source: { kind: "realtime", path: "heater_state" }, grid: { w: 6, h: 2 } },
    /* 通用单水槽卡：multi 表示目录不判重，可添加任意多个。
       数据源两种形态都支持——对象（realtime.tank.tanks.*，带无传感器/离线角标与水量）
       或纯数值百分比（自定义接口常见形态）。配置里改名称/颜色/接口。 */
    { key: "tank", title: "单水槽", type: "tank", multi: true, unit: "%", decimals: 1,
      color: "#38bdf8", source: { kind: "realtime", path: "tank.tanks.heater" },
      grid: { w: 3, h: 4 } },
    /* 控制开关卡：ctl 指向内置受控通道（pump/heater）；配置里可改自定义开/关指令 URL（cmd） */
    { key: "ctl_pump", title: "水泵开关", type: "control", color: "#2dd4bf",
      ctl: "pump", feature: "pump", source: { kind: "realtime", path: "pump_state" }, grid: { w: 4, h: 2 } },
    { key: "ctl_heater", title: "加热开关", type: "control", color: "#f87171",
      ctl: "heater", feature: "heater", source: { kind: "realtime", path: "heater_state" }, grid: { w: 4, h: 2 } },
    /* 内置特殊卡：quant 定量 / pid 恒温闭环 / recent 最近操作 / reset 清零。
       perm 为 Auth 权限点门控；flag 为 realtime 顶层布尔门控（如 pid_supported 不在 features 里）。 */
    { key: "quant", title: "定量浇水", type: "builtin", builtin: "quant",
      feature: "pump_target", perm: "ctrl_light", grid: { w: 4, h: 4 } },
    { key: "pid", title: "恒温闭环", type: "builtin", builtin: "pid",
      perm: "cfg_alarm", flag: "pid_supported", grid: { w: 4, h: 4 } },
    { key: "recent", title: "最近操作", type: "builtin", builtin: "recent",
      perm: "view_log", grid: { w: 6, h: 3 } },
    { key: "reset", title: "清零累计", type: "builtin", builtin: "reset",
      feature: "volume_reset", perm: "ctrl_light", grid: { w: 4, h: 2 } },
    { key: "device", title: "采集设备", type: "builtin", builtin: "device", grid: { w: 12, h: 3 } },
    /* 竞赛加分/演示用内置卡：系统拓扑（状态可视化）与一键场景（启动/急停） */
    { key: "topo", title: "系统拓扑", type: "builtin", builtin: "topo", grid: { w: 8, h: 6 } },
    { key: "scene", title: "一键场景", type: "builtin", builtin: "scene",
      perm: "ctrl_light", grid: { w: 4, h: 3 } },
  ];

  /* 目录条目 → 卡片配置（深拷贝，补 id 与 catalogKey） */
  function fromCatalog(item) {
    const w = JSON.parse(JSON.stringify(item));
    delete w.feature;
    delete w.perm;
    delete w.flag;
    delete w.multi;
    w.catalogKey = item.key;
    w.id = uid();
    return w;
  }

  /* ---------- 共享纯函数（面板与卡片两处复用） ---------- */

  /* 定量浇水进度：设备完成一次定量会把累计值清零，此时回退为按当前累计值计算 */
  function targetProgress(realtime) {
    const rt = realtime || {};
    const target = Number(rt.pump_target || 0);
    if (!(target > 0)) return null;
    const total = Number(rt.total_liters || 0);
    const base = Number(rt.target_baseline ?? 0);
    const done = total >= base ? total - base : total;
    return {
      target,
      done: Math.max(0, done),
      percent: Math.max(0, Math.min(100, (done / target) * 100)),
    };
  }

  const ACTION_LABELS = {
    "config.alarm": "修改告警阈值",
    "config.link": "修改报警联动",
    "config.period": "修改采集周期",
    "config.tank": "修改水槽容积",
    "config.dashboard": "修改仪表盘布局",
    "config.site": "修改站点文案",
    "config.gateway": "配置外部设备",
    "config.calib": "修改通道标定",
    "config.timers": "修改定时任务",
    "config.judge": "修改判定服务模板",
    "device.custom": "自定义指令",
    "device.gateway": "外部设备线圈控制",
    "link_card_on": "联动：卡片指令 开",
    "link_card_off": "联动：卡片指令 关",
    "link_pump_on": "联动：本机水泵 开",
    "link_pump_off": "联动：本机水泵 关",
    "link_heater_on": "联动：本机加热 开",
    "link_heater_off": "联动：本机加热 关",
    "link_quant_cancel": "联动：取消定量",
    "link_fail": "联动：动作失败",
    "timer_card_on": "定时：卡片指令 开",
    "timer_card_off": "定时：卡片指令 关",
    "timer_pump_on": "定时：本机水泵 开",
    "timer_pump_off": "定时：本机水泵 关",
    "timer_heater_on": "定时：本机加热 开",
    "timer_heater_off": "定时：本机加热 关",
    "timer_quant_cancel": "定时：取消定量",
    "timer_fail": "定时：动作失败",
    "link_off_pump": "联动关水泵",
    "link_off_heater": "联动关加热",
    "link_cancel_target": "联动取消定量",
    "link_actuator": "联动执行器动作",
    "account.create": "创建账号",
    "account.password": "重置密码",
    "account.role": "修改角色",
    "account.status": "启用/禁用账号",
    "account.delete": "删除账号",
    "account.roles": "保存权限矩阵",
  };
  function actionLabel(a) { return ACTION_LABELS[a] || a; }
  /* 系统自动动作没有操作账号，用来源(source)区分 */
  function isSystemLog(l) { return l.source === "auto" || l.source === "judge" || l.source === "timer"; }
  function operatorText(l) {
    if (l.source === "auto") return "系统 · 恒温闭环";
    if (l.source === "judge") return "系统 · 判定服务";
    if (l.source === "timer") return "系统 · 定时任务";
    return l.operator || "—";
  }

  /* 默认布局：按设备能力生成（显式坐标，共 14 行，保证 1366×768 首屏零滚动）。
     单水槽为通用卡：默认生成储水槽/加热槽两个实例（绑定实时双槽对象，保留无传感器/离线角标）。
     flag 类卡片（依赖运行时标志，如 pid_supported）不进默认布局，只入目录；
     perm 类仅 recent/pid/reset 不在 plan 中，quant 带权限但无权限时渲染禁用态。 */
  function defaultLayout(features) {
    const f = features || {};
    const tankItem = CATALOG.find((c) => c.key === "tank");
    const plan = [
      { key: "flow", x: 0, y: 0 }, { key: "total", x: 6, y: 0 },
      { key: "storage_temp", x: 0, y: 3 }, { key: "heater_temp", x: 3, y: 3 },
      { key: "pressure", x: 6, y: 3 }, { key: "light", x: 9, y: 3 },
      { key: "ctl_pump", x: 8, y: 5 }, { key: "ctl_heater", x: 8, y: 7 }, { key: "quant", x: 8, y: 9 },
    ];
    const widgets = [];
    plan.forEach(({ key, x, y }) => {
      const item = CATALOG.find((c) => c.key === key);
      if (!item || item.flag) return;
      if (item.feature && !f[item.feature]) return;
      const w = fromCatalog(item);
      w.id = "b-" + key;                      // 默认卡片用稳定 id，便于测试与排查
      w.grid = Object.assign({}, w.grid, { x, y });
      widgets.push(w);
    });
    // 两张默认单水槽卡：占左下大区（行 5-13），对象源保留「无传感器/离线」角标。
    // 注意 x0..8 区间不能与右列控制卡（x8..12）重叠，否则 gridstack 会把水槽卡压到下方。
    if (tankItem) {
      const mk = (id, title, tankKey, color, foot) => {
        const w = fromCatalog(tankItem);
        w.id = id; w.title = title; w.color = color; w.foot = foot;
        w.source = { kind: "realtime", path: "tank.tanks." + tankKey };
        w.grid = { x: id === "b-tank-storage" ? 0 : 4, y: 5, w: 4, h: 9 };
        return w;
      };
      widgets.push(mk("b-tank-storage", "储水槽", "storage", "#38bdf8", "无传感器 · 恒为 0"));
      widgets.push(mk("b-tank-heater", "加热槽", "heater", "#2dd4bf", "超声波实测"));
    }
    return widgets;
  }

  return { TYPES, CATALOG, resolvePath, fmtVal, ctlState, uid, fromCatalog, defaultLayout,
           targetProgress, actionLabel, isSystemLog, operatorText };
})();
