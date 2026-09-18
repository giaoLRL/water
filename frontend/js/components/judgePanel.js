/* 判定服务对接页（任务五）：启用开关 + 地址与报文模板 + 上报/轮询/反馈状态 + 最近通信记录 */
window.ViewJudgePanel = {
  name: "JudgePanelView",
  emits: ["back"],
  data() {
    return { st: {}, statusTimer: null, tplUrl: "", tplText: "", tplNote: "" };
  },
  mounted() {
    this.refresh();
    this.loadTemplate();
    this.statusTimer = setInterval(this.refresh, 2000);
  },
  beforeUnmount() {
    if (this.statusTimer) clearInterval(this.statusTimer);
  },
  methods: {
    perm(p) { return window.Auth ? window.Auth.has(p) : false; },
    async refresh() {
      try { this.st = await API.judgeStatus(); } catch (e) { /* silent */ }
    },
    async toggle(e) {
      try {
        const d = await API.judgeEnable(e.target.checked);
        this.st.enabled = d.enabled;
      } catch (err) { e.target.checked = this.st.enabled; alert(err.message); }
    },
    /* 报文模板：现场拿到组委会的字段规范后直接改这里，不用改代码 */
    async loadTemplate() {
      try {
        const d = await API.judgeTemplateGet();
        this.tplUrl = d.url || "";
        this.tplText = JSON.stringify(d.template || {}, null, 2);
      } catch (e) { /* silent */ }
    },
    fillExample() {
      this.tplText = JSON.stringify({
        headers: {},
        report: { path: "/api/report",
                  body: { deviceId: "{{device_id}}", ts: "{{ts}}",
                          flow: "{{flow_rate}}", temp1: "{{storage_temp}}",
                          temp2: "{{heater_temp}}", pressure: "{{pressure}}",
                          light: "{{light}}", pump: "{{pump_state}}" } },
        poll: { path: "/api/poll", body: { deviceId: "{{device_id}}" } },
        feedback: { path: "/api/feedback",
                    body: { deviceId: "{{device_id}}", ok: "{{result}}" } },
      }, null, 2);
      this.tplNote = "已填入示例模板：占位符 {{字段名}} 会用实时快照替换；{{data_json}} 展开为整份数据。";
    },
    async saveTemplate() {
      let tpl = {};
      try { tpl = this.tplText.trim() ? JSON.parse(this.tplText) : {}; }
      catch (e) { alert("模板不是合法 JSON：" + e.message); return; }
      try {
        const d = await API.judgeTemplateSet(this.tplUrl.trim(), tpl);
        this.tplUrl = d.url || "";
        this.tplText = JSON.stringify(d.template || {}, null, 2);
        alert("判定服务地址与报文模板已保存");
      } catch (e) { alert(e.message); }
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
        <span class="desc">地址与报文都可在下方页面填写（config.py 的 JUDGE_URL 仍作为默认值）</span>
      </div>
      <div class="stat-grid2">
        <div class="stat-box"><div class="label">设备ID</div><div class="value">{{ st.device_id || '--' }}</div></div>
        <div class="stat-box"><div class="label">服务地址</div><div class="value">{{ st.url || '未配置' }}</div></div>
        <div class="stat-box"><div class="label">最近上报</div><div class="value">{{ st.last_report || '--' }}</div></div>
        <div class="stat-box"><div class="label">最近轮询</div><div class="value">{{ st.last_poll || '--' }}</div></div>
        <div class="stat-box"><div class="label">待执行指令</div><div class="value">{{ st.pending_command ? JSON.stringify(st.pending_command) : '无' }}</div></div>
      </div>
    </div>

    <div class="section" v-if="perm('cfg_system')">
      <h3>报文模板 <span class="desc">现场按组委会的字段规范改这里即可，无需改代码；未填写的段落用内置报文</span></h3>
      <div class="alarm-rule" style="flex-wrap:wrap;align-items:center;">
        <label class="rule-item" style="flex:1;">服务地址
          <input v-model="tplUrl" placeholder="如 http://192.168.1.50:9000" style="width:280px;">
        </label>
        <button class="btn-ghost" @click="fillExample">填入示例</button>
        <button class="btn-primary" @click="saveTemplate">保存地址与模板</button>
      </div>
      <textarea v-model="tplText" rows="12" spellcheck="false"
                style="width:100%;font-family:Consolas,monospace;font-size:12px;"></textarea>
      <div class="note" v-if="tplNote">{{ tplNote }}</div>
      <div class="note">
        可用占位符：device_id / ts / flow_rate / total_liters / storage_temp / heater_temp /
        pressure / light / pump_state / heater_state / 各自定义通道 id；{{data_json}} 展开为整份快照。
        模板结构：headers + report/poll/feedback 三段，每段 {path, body}。
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
