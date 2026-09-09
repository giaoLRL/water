/* 判定服务对接页（任务五）：启用开关 + 上报/轮询/反馈状态 + 最近通信记录 */
window.ViewJudgePanel = {
  name: "JudgePanelView",
  emits: ["back"],
  data() {
    return { st: {}, statusTimer: null };
  },
  mounted() {
    this.refresh();
    this.statusTimer = setInterval(this.refresh, 2000);
  },
  beforeUnmount() {
    if (this.statusTimer) clearInterval(this.statusTimer);
  },
  methods: {
    async refresh() {
      try { this.st = await API.judgeStatus(); } catch (e) { /* silent */ }
    },
    async toggle(e) {
      try {
        const d = await API.judgeEnable(e.target.checked);
        this.st.enabled = d.enabled;
      } catch (err) { e.target.checked = this.st.enabled; alert(err.message); }
    },
  },
  template: `
  <div class="view-page">
    <div class="detail-head">
      <button class="btn-ghost" @click="$emit('back')">← 返回</button>
      <h2>组委会智能判定服务对接</h2>
      <span class="desc">数据上报 → 指令接收 → 执行 → 结果反馈（任务五）</span>
    </div>

    <div class="section">
      <h3>对接状态</h3>
      <div class="alarm-rule">
        <label class="rule-item"><span>启用数据上报</span>
          <input type="checkbox" :checked="st.enabled" @change="toggle">
        </label>
        <span class="desc">判定服务地址在 config.py 的 JUDGE_URL 修改（现场按裁判公告填写）</span>
      </div>
      <div class="stat-grid2">
        <div class="stat-box"><div class="label">设备ID</div><div class="value">{{ st.device_id || '--' }}</div></div>
        <div class="stat-box"><div class="label">服务地址</div><div class="value">{{ st.url || '未配置' }}</div></div>
        <div class="stat-box"><div class="label">最近上报</div><div class="value">{{ st.last_report || '--' }}</div></div>
        <div class="stat-box"><div class="label">最近轮询</div><div class="value">{{ st.last_poll || '--' }}</div></div>
        <div class="stat-box"><div class="label">待执行指令</div><div class="value">{{ st.pending_command ? JSON.stringify(st.pending_command) : '无' }}</div></div>
      </div>
    </div>

    <div class="section">
      <h3>最近通信记录 <span class="desc">每 1 秒上报 / 轮询</span></h3>
      <div style="overflow-x:auto;">
        <table>
          <thead><tr><th>时间</th><th>动作</th><th>结果</th><th>说明</th></tr></thead>
          <tbody>
            <tr v-for="(l,i) in (st.log || [])" :key="i">
              <td>{{ l.time }}</td><td>{{ l.action }}</td>
              <td><span class="badge" :class="l.ok ? 'ok' : 'fail'">{{ l.ok ? '成功' : '失败' }}</span></td>
              <td>{{ l.detail || '' }}</td>
            </tr>
            <tr v-if="!st.log || !st.log.length"><td colspan="4" style="text-align:center;color:#6b7a90;">尚未上报（未启用或地址未配置）</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
  `,
};