/* ECharts 辅助：统一坐标轴样式与图表管理。 */
window.Charts = (() => {
  const registry = {};

  const axisStyle = {
    axisLine: { lineStyle: { color: "#2a3442" } },
    axisLabel: { color: "#7d8b99" },
    splitLine: { lineStyle: { color: "#1e2631" } },
  };

  function baseOption(times, yName) {
    return {
      tooltip: {
        trigger: "axis",
        backgroundColor: "#1a222d",
        borderColor: "#2a3442",
        textStyle: { color: "#d7e0ea" },
        axisPointer: { lineStyle: { color: "#3b4757" } },
      },
      legend: { top: 0, textStyle: { color: "#7d8b99" } },
      grid: { left: 54, right: 20, top: 36, bottom: 30 },
      xAxis: { type: "category", data: times, boundaryGap: false, ...axisStyle },
      yAxis: { type: "value", name: yName, nameTextStyle: { color: "#7d8b99" }, ...axisStyle },
    };
  }

  function lineSeries(name, data, color, yAxisIndex = 0) {
    return {
      name,
      type: "line",
      yAxisIndex,
      data,
      smooth: true,
      symbol: "none",
      lineStyle: { width: 2, color },
      areaStyle: { opacity: 0.05, color },
    };
  }

  function barSeries(name, data, color) {
    return {
      name,
      type: "bar",
      data,
      barMaxWidth: 26,
      itemStyle: { color, borderRadius: [3, 3, 0, 0] },
    };
  }

  /* 柱状图基础配置 */
  function barOption(xData, yName) {
    return {
      tooltip: {
        trigger: "axis",
        backgroundColor: "#1a222d",
        borderColor: "#2a3442",
        textStyle: { color: "#d7e0ea" },
      },
      grid: { left: 50, right: 16, top: 28, bottom: 26 },
      xAxis: { type: "category", data: xData, axisLabel: { color: "#7d8b99" }, axisLine: { lineStyle: { color: "#2a3442" } }, axisTick: { show: false } },
      yAxis: { type: "value", name: yName, ...axisStyle },
    };
  }

  /* 饼图配置（环形） */
  function pieOption(items, colors) {
    const palette = colors || ["#f87171", "#fbbf24", "#38bdf8", "#2dd4bf", "#a78bfa"];
    return {
      tooltip: { trigger: "item", backgroundColor: "#1a222d", borderColor: "#2a3442", textStyle: { color: "#d7e0ea" } },
      legend: { bottom: 0, textStyle: { color: "#7d8b99" }, itemWidth: 10, itemHeight: 10 },
      series: [{
        type: "pie",
        radius: ["32%", "74%"],
        center: ["50%", "46%"],
        itemStyle: { borderColor: "#151b24", borderWidth: 2 },
        label: { color: "#7d8b99", fontSize: 11 },
        data: items.map((it, i) => ({ ...it, itemStyle: { color: palette[i % palette.length] } })),
      }],
    };
  }

  /* 仪表盘配置 */
  function gaugeOption(value, min, max, unit, color) {
    return {
      series: [{
        type: "gauge",
        startAngle: 205, endAngle: -25,
        min, max,
        radius: "98%",
        progress: { show: true, width: 12, itemStyle: { color } },
        axisLine: { lineStyle: { width: 12, color: [[1, "#262f3b"]] } },
        pointer: { show: false },
        axisTick: { show: false },
        splitLine: { show: false },
        axisLabel: { show: false },
        detail: {
          valueAnimation: true,
          formatter: (v) => `${v} ${unit}`,
          fontSize: 19,
          color: "#d7e0ea",
          offsetCenter: [0, "30%"],
        },
        data: [{ value: Math.round(value * 10) / 10 }],
      }],
    };
  }

  function init(id, option, notMerge = true) {
    const el = document.getElementById(id);
    if (!el || !window.echarts) return null;
    let chart = registry[id];
    // 实例可能已被 dispose（getDom() 为 null）或 DOM 已随组件销毁断开：
    // 两种情况都要清理 registry，否则残留已销毁实例会让后续调用再次崩溃
    if (chart && (!chart.getDom() || !chart.getDom().isConnected)) {
      try { chart.dispose(); } catch (e) { /* ignore */ }
      delete registry[id];
      chart = null;
    }
    // 容器隐藏（display:none）时 clientWidth 为 0，此时初始化会得到错误的画布宽度。
    // 直接跳过：切换 tab 时调用方会触发重绘（见各 tab 的 renderXxx / resizeAll）。
    if (el.clientWidth === 0) return null;
    if (!chart) chart = registry[id] = window.echarts.init(el);
    if (option) chart.setOption(option, notMerge);
    return chart;
  }

  function set(id, option, notMerge) {
    if (registry[id]) registry[id].setOption(option, !!notMerge);
  }

  function resizeAll() {
    Object.values(registry).forEach((c) => {
      try { c.resize(); } catch (e) { /* ignore */ }
    });
  }

  return { init, set, resizeAll, lineSeries, barSeries, baseOption, barOption, pieOption, gaugeOption, axisStyle };
})();
