/* 系统配置页：告警阈值 + 采集周期 + 设备信息。账号/角色管理复用后端接口。 */
window.ViewSysConfig = {
  name: "SysConfigView",
  emits: ["back"],
  data() {
    return {
      tab: "alarm",   // alarm / account
      thresholds: {},
      features: {},            // 设备通道能力（/api/system 返回），用于裁剪阈值表单
      period: 1.0,
      tankCap: { storage: 1000, heater: 1000 },
      system: {},
      // 界面文案（站点标题/副标题/浏览器标签标题）
      siteCopy: { title: "", subtitle: "", browser: "" },
      // 报警联动（v3 卡片即来源）：触发源与动作都直接选自实时监控页的卡片
      links: [],
      cards: [],               // 实时监控页的卡片（内置 + 自建），联动选择项的唯一来源
      // 账号
      users: [],
      roles: [],
      permMatrix: [],          // 权限点列表 [key, 显示名]
      selRoles: {},            // 各角色勾选的权限 { key: {label, perms:[...]} }
      newUser: { username: "", password: "", role: "viewer" },
      newRoleName: "",   // 新增角色名
    };
  },
  mounted() {
    this.load();
  },
  computed: {
    /* 可作触发源的卡片：数值卡 / 迷你曲线 / 趋势曲线 / 单水槽卡（水槽取 percent） */
    sourceCards() {
      const ok = ["value", "spark", "line", "tank"];
      return this.cards.filter((c) => ok.indexOf(c.type) !== -1 && c.source && c.source.kind);
    },
    /* 可作动作目标的卡片：控制开关卡（内置泵/加热或自定义指令）+ 定量浇水卡 */
    targetCards() {
      return this.cards.filter((c) => c.type === "control"
        || (c.type === "builtin" && c.builtin === "quant"));
    },
  },
  methods: {
    perm(p) { return window.Auth ? window.Auth.has(p) : false; },
    _decodeRole(item, key) {
      // roles[key] 可能是 [label, perms] / [perms] / '*'，统一为 {label, perms}
      let label = key, perms = [];
      if (Array.isArray(item)) {
        if (typeof item[0] === "string") { label = item[0]; perms = (item[1] === "*" || item[1] === undefined) ? "*" : item[1]; }
        else { perms = item; }
      } else if (item === "*") { perms = "*"; }
      return { label, perms: perms === "*" ? this.permMatrix.map((p) => p[0]) : (perms || []) };
    },
    async load() {
      try {
        const cfg = await API.alarmConfigGet();
        this.thresholds = cfg.thresholds || {};
      } catch (e) { /* silent */ }
      try { this.system = await API.system(); this.period = this.system.period || 1.0; } catch (e) { /* silent */ }
      if (this.system.features) this.features = this.system.features;
      // 联动选择项来源 = 实时监控页的卡片（内置 + 自建）；布局从未保存时用前端默认布局
      try {
        const saved = (await API.dashboardLayoutGet()).layout;
        this.cards = (saved && Array.isArray(saved.widgets) && saved.widgets.length)
          ? saved.widgets : window.Widgets.defaultLayout(this.features || {});
      } catch (e) {
        try { this.cards = window.Widgets.defaultLayout(this.features || {}); }
        catch (e2) { this.cards = []; }
      }
      if (this.perm("cfg_alarm")) {
        try { this.links = (await API.alarmLinksGet()).links || []; } catch (e) { /* silent */ }
      }
      try { this.siteCopy = Object.assign(this.siteCopy, await API.siteGet()); }
      catch (e) { /* silent */ }
      const caps = this.system.tank_capacities;
      if (caps) this.tankCap = { storage: caps.storage, heater: caps.heater };
      if (this.perm("account_manage")) {
        try { this.users = (await API.users()).users || []; } catch (e) { /* silent */ }
        try {
          const r = await API.roles();
          this.roles = r.roles || {};
          this.permMatrix = r.permissions || [];
          this.selRoles = {};
          for (const k of Object.keys(this.roles)) {
            if (k === "admin") continue;   // 管理员固定全权限
            this.selRoles[k] = this._decodeRole(this.roles[k], k);
          }
        } catch (e) { /* silent */ }
      }
    },
    async saveThresh() {
      // 仅保存当前采集设备支持的通道；温度阈值单位℃，压力 kPa，光照 lx
      const keys = ["flow_max", "flow_min"];
      if (this.features.temperature) keys.push("storage_temp_max", "storage_temp_min", "heater_temp_max", "heater_temp_min");
      if (this.features.pressure) keys.push("pressure_max", "pressure_min");
      if (this.features.light) keys.push("light_max", "light_min");
      const cfg = {};
      for (const k of keys) {
        const v = parseFloat(this.thresholds[k]);
        if (isNaN(v)) { alert(`请填写有效数值: ${k}`); return; }
        cfg[k] = v;
      }
      try {
        const d = await API.alarmConfigSet(cfg);
        this.thresholds = d.thresholds || {};
        alert("告警阈值已保存");
      } catch (e) { alert(e.message); }
    },
    async savePeriod() {
      try {
        const d = await API.setPeriod(this.period);
        this.period = d.period;
        alert("采集周期已保存（即时生效）");
      } catch (e) { alert(e.message); }
    },
    async saveTank() {
      // 先固化两个输入值：保存第一个水槽后回读 capacities 会覆盖尚未提交的另一槽输入
      const caps = {
        storage: parseFloat(this.tankCap.storage),
        heater: parseFloat(this.tankCap.heater),
      };
      for (const [tank, label] of [["storage", "储水槽"], ["heater", "加热槽"]]) {
        if (isNaN(caps[tank]) || caps[tank] <= 0) { alert(`请输入有效的${label}容积(L)`); return; }
      }
      try {
        let last = null;
        for (const tank of ["storage", "heater"]) {
          last = await API.tankSet(tank, caps[tank]);
        }
        if (last && last.capacities) {
          this.tankCap = { storage: last.capacities.storage, heater: last.capacities.heater };
        }
        alert("水槽容积已保存（实时水位界面即时生效）");
      } catch (e) { alert(e.message); }
    },
    // ---------- 报警联动（v3 卡片即来源） ----------
    cardById(id) { return this.cards.find((c) => c.id === id) || null; },
    cardTitle(id) { const c = this.cardById(id); return c ? (c.title || c.id) : (id || "?"); },
    /* 卡片当前数据源快照：保存规则时一并落库，卡片被删后规则仍可兜底运行 */
    sourceOf(card) {
      const s = (card && card.source) || {};
      return { kind: s.kind, url: s.url || "", path: s.path || "", period: s.period || 5 };
    },
    /* 卡片当前动作快照：自定义指令 URL → url；内置泵/加热 → pump/heater；定量卡 → quant */
    actionOf(card, state) {
      const st = state === "on" ? "on" : "off";
      const cmd = (card && card.cmd) || {};
      const url = st === "on" ? (cmd.on || "") : (cmd.off || "");
      if (url) return { card: card.id, state: st, kind: "url", url };
      if (card && card.builtin === "quant") return { card: card.id, state: "off", kind: "quant", url: "" };
      return { card: card.id, state: st, kind: (card && card.ctl === "heater") ? "heater" : "pump", url: "" };
    },
    actionText(a) {
      const t = this.cardTitle(a.card);
      if (a.kind === "quant") return t + " · 取消定量";
      return t + " · " + (a.state === "on" ? "开" : "关");
    },
    /* 换卡片或换开/关后，按卡片当前配置重新生成动作（指令 URL 随之更新） */
    syncAction(a) {
      const card = this.cardById(a.card);
      if (card) Object.assign(a, this.actionOf(card, a.state));
    },
    // 联动规则
    addLink() {
      const src = this.sourceCards[0];
      const tgt = this.targetCards[0];
      this.links.push({
        id: "r" + Date.now().toString(36) + Math.floor(Math.random() * 1296).toString(36),
        name: "", enabled: true,
        source_card: src ? src.id : "",
        source: this.sourceOf(src),
        threshold: 0, direction: "above",
        actions: tgt ? [this.actionOf(tgt, "off")] : [],
        recover: { actions: [] },
      });
    },
    delLink(i) {
      if (!confirm("删除该联动规则？")) return;
      this.links.splice(i, 1);
    },
    addAction(actions, e) {
      const card = this.cardById(e.target.value);
      if (!card) { e.target.value = ""; return; }
      actions.push(this.actionOf(card, "off"));
      e.target.value = "";
    },
    delAction(actions, j) {
      actions.splice(j, 1);
    },
    async saveLinks() {
      for (const l of this.links) {
        const card = this.cardById(l.source_card);
        if (!card) { alert(`规则[${l.name || l.id}] 的触发源卡片不存在，请重新选择`); return; }
        Object.assign(l, { source: this.sourceOf(card) });   // 以卡片当前配置为准
        if (l.source.kind === "custom" && !/^https?:\/\//.test((l.source.url || "").trim())) {
          alert(`触发源卡片[${this.cardTitle(card.id)}] 未配置接口 URL`); return;
        }
        if (isNaN(Number(l.threshold))) { alert(`规则[${l.name || l.id}] 阈值必须为数值`); return; }
        if (l.direction !== "above" && l.direction !== "below") { alert(`规则[${l.name || l.id}] 触发方向非法`); return; }
        const acts = [...(l.actions || []), ...((l.recover || {}).actions || [])];
        if (!acts.length) { alert(`规则[${l.name || l.id}] 至少配置一个动作`); return; }
        for (const a of acts) {
          const c = this.cardById(a.card);
          if (!c) { alert(`规则[${l.name || l.id}] 的动作卡片不存在，请重新选择`); return; }
          Object.assign(a, this.actionOf(c, a.state));      // 以卡片当前配置为准
          if (a.kind === "url" && !a.url) {
            alert(`卡片[${this.cardTitle(c.id)}] 未配置${a.state === "on" ? "开" : "关"}指令 URL`); return;
          }
        }
      }
      try {
        const d = await API.alarmLinksSet(JSON.parse(JSON.stringify(this.links)));
        this.links = d.links || [];
        alert("报警联动已保存");
      } catch (e) { alert(e.message); }
    },
    // ---------- 界面文案 ----------
    async saveSite() {
      try {
        const s = await API.siteSet(JSON.parse(JSON.stringify(this.siteCopy)));
        this.siteCopy = Object.assign(this.siteCopy, s);
        // 同步顶栏与浏览器标签（app.js 全局响应式对象）
        if (window.AppSite) {
          window.AppSite.title = s.title;
          window.AppSite.subtitle = s.subtitle;
          window.AppSite.browser = s.browser || "";
        }
        document.title = s.browser || s.title;
        alert("界面文案已保存（顶栏与浏览器标签即时生效）");
      } catch (e) { alert(e.message); }
    },
    async createUser() {
      const u = this.newUser.username.trim();
      if (!u || !this.newUser.password) { alert("请输入用户名和密码"); return; }
      try {
        await API.createUser({ username: u, password: this.newUser.password, role: this.newUser.role });
        alert("用户已创建");
        this.newUser = { username: "", password: "", role: "viewer" };
        this.users = (await API.users()).users || [];
      } catch (e) { alert(e.message); }
    },
    async resetPwd(id) {
      const p = prompt("请输入新密码");
      if (!p) return;
      try { await API.resetPassword(id, p); alert("密码已重置"); } catch (e) { alert(e.message); }
    },
    roleLabel(role) {
      const item = this.roles[role];
      return item ? (Array.isArray(item) ? item[0] : role) : role;
    },
    hasPerm(roleKey, permKey) {
      const r = this.selRoles[roleKey];
      return !!(r && r.perms.indexOf(permKey) !== -1);
    },
    togglePerm(roleKey, permKey, e) {
      const r = this.selRoles[roleKey];
      if (!r) return;
      const i = r.perms.indexOf(permKey);
      if (e.target.checked && i === -1) r.perms.push(permKey);
      if (!e.target.checked && i !== -1) r.perms.splice(i, 1);
    },
    async saveMatrix() {
      // 组装 { 角色: [显示名, 权限列表] }，admin 由后端强制为全权限
      const payload = {};
      for (const k of Object.keys(this.selRoles)) {
        payload[k] = [this.selRoles[k].label || k, this.selRoles[k].perms];
      }
      try {
        await API.saveRoles(payload);
        await this._refreshRoles();
        alert("角色权限矩阵已保存");
      } catch (e) { alert(e.message); }
    },
    addRole() {
      const name = this.newRoleName.trim();
      if (!name) { alert("请输入角色名"); return; }
      if (name === "admin") { alert("不能新增 admin 角色"); return; }
      if (this.selRoles[name] || this.roles[name]) { alert(`角色 ${name} 已存在`); return; }
      this.selRoles[name] = { label: name, perms: [] };
      this.newRoleName = "";
    },
    async delRole(key) {
      if (!confirm(`确定删除角色 ${this.selRoles[key].label || key}？`)) return;
      delete this.selRoles[key];
      try {
        await API.saveRoles(this._matrixPayload());
        await this._refreshRoles();
      } catch (e) { alert(e.message); }
    },
    _matrixPayload() {
      const payload = {};
      for (const k of Object.keys(this.selRoles)) {
        payload[k] = [this.selRoles[k].label || k, this.selRoles[k].perms];
      }
      return payload;
    },
    async _refreshRoles() {
      // 保存/删除角色后重新拉取角色列表，让"创建用户"下拉与角色列即时出现新角色
      try {
        const r = await API.roles();
        this.roles = r.roles || {};
      } catch (e) { /* silent */ }
    },
    async setRole(u) {
      try { await API.setUserRole(u.id, u.role); alert("角色已更新"); } catch (e) { alert(e.message); this.load(); }
    },
    async toggleStatus(u) {
      const target = u.status === "active" ? "disabled" : "active";
      try { await API.setUserStatus(u.id, target); u.status = target; } catch (e) { alert(e.message); }
    },
    async delUser(u) {
      if (!confirm(`确定删除用户 ${u.username}？`)) return;
      try { await API.deleteUser(u.id); this.users = (await API.users()).users || []; }
      catch (e) { alert(e.message); }
    },
  },
  template: `
  <div class="view-page">
    <div class="detail-head">
      <button class="btn-ghost" @click="$emit('back')">← 返回</button>
      <h2>系统配置</h2>
      <span class="desc">告警阈值 · 采集周期 · 设备信息</span>
    </div>

    <div class="tabs">
      <span class="tab" :class="{ active: tab==='alarm' }" @click="tab='alarm'">告警阈值 / 采集周期</span>
      <span class="tab" v-if="perm('account_manage')" :class="{ active: tab==='account' }" @click="tab='account'; load()">账号管理</span>
    </div>

    <div class="grid-2 config-grid" v-show="tab==='alarm'">
      <!-- 界面文案：浏览器标签 + 顶栏标题/副标题，免改代码 -->
      <div class="section" style="grid-column: 1 / -1;">
        <h3>界面文案 <span class="desc">浏览器标签标题 + 顶栏大标题/副标题，留空恢复默认文案</span></h3>
        <div class="alarm-rule" style="flex-wrap:wrap;align-items:center;">
          <label class="rule-item"><span>站点标题</span>
            <input v-model="siteCopy.title" :disabled="!perm('cfg_system')" style="width:230px;"></label>
          <label class="rule-item"><span>副标题</span>
            <input v-model="siteCopy.subtitle" :disabled="!perm('cfg_system')" style="width:300px;"></label>
          <label class="rule-item"><span>浏览器标签标题</span>
            <input v-model="siteCopy.browser" :disabled="!perm('cfg_system')" style="width:200px;" placeholder="留空则同站点标题"></label>
          <button class="btn-primary" :disabled="!perm('cfg_system')" @click="saveSite">保存界面文案</button>
        </div>
      </div>
      <div class="section">
        <h3>告警阈值 <span class="desc">流量 / 温度 / 压力上下限 · 超限即告警（任务六）</span></h3>
        <div class="alarm-rule" style="flex-direction:column;align-items:stretch;gap:10px;">
          <div class="grid-2">
            <label class="rule-item"><span>水流量上限 L/min</span><input type="number" v-model.number="thresholds.flow_max"></label>
            <label class="rule-item"><span>水流量下限 L/min</span><input type="number" v-model.number="thresholds.flow_min"></label>
          </div>
          <div class="grid-2" v-if="features.temperature">
            <label class="rule-item"><span>储水槽温度上限 ℃</span><input type="number" v-model.number="thresholds.storage_temp_max"></label>
            <label class="rule-item"><span>储水槽温度下限 ℃</span><input type="number" v-model.number="thresholds.storage_temp_min"></label>
          </div>
          <div class="grid-2" v-if="features.temperature">
            <label class="rule-item"><span>加热槽温度上限 ℃</span><input type="number" v-model.number="thresholds.heater_temp_max"></label>
            <label class="rule-item"><span>加热槽温度下限 ℃</span><input type="number" v-model.number="thresholds.heater_temp_min"></label>
          </div>
          <div class="grid-2" v-if="features.pressure">
            <label class="rule-item"><span>水压上限 kPa</span><input type="number" v-model.number="thresholds.pressure_max"></label>
            <label class="rule-item"><span>水压下限 kPa</span><input type="number" v-model.number="thresholds.pressure_min"></label>
          </div>
          <div class="grid-2" v-if="features.light">
            <label class="rule-item"><span>光照上限 lx</span><input type="number" v-model.number="thresholds.light_max"></label>
            <label class="rule-item"><span>光照下限 lx</span><input type="number" v-model.number="thresholds.light_min"></label>
          </div>
          <button class="btn-primary" @click="saveThresh">保存告警阈值</button>
          <div class="note" style="margin-top:0;">
            仅展示当前固件支持的通道；压力单位为 kPa（设备固件上报 MPa，后端已按 ×1000 统一换算），
            光照单位为 lx（GY-302）。温度通道启用后，恒温闭环(PID)可在实时面板开启。
          </div>
        </div>
      </div>
      <div class="section">
        <h3>采集周期</h3>
        <div class="alarm-rule">
          <label class="rule-item"><span>采集周期(秒)</span><input type="number" v-model.number="period" min="0.5" step="0.5" style="width:90px;"></label>
          <button class="btn-primary" @click="savePeriod">保存</button>
        </div>
        <h3 style="margin-top:18px;">双水槽容积 <span class="desc">实时水位动画</span></h3>
        <div class="alarm-rule">
          <label class="rule-item"><span>储水槽(L)</span><input type="number" v-model.number="tankCap.storage" min="1" step="1" style="width:100px;"></label>
          <label class="rule-item"><span>加热槽(L)</span><input type="number" v-model.number="tankCap.heater" min="1" step="1" style="width:100px;"></label>
          <button class="btn-primary" @click="saveTank">保存</button>
          <span class="desc">用于把水位%换算成估算水量</span>
        </div>
        <div class="note" style="margin-top:0;">
          液位说明：加热槽已接<b>超声波传感器（实测）</b>；储水槽暂无传感器，水位<b>恒显示 0</b>
          并标注「无传感器」（已清除全部模拟数据，无数据一律保持 0）。
          传感器接入后，把 backend/config.py 的 <code>DEVICE_FEATURES["level_storage"]</code> 改为
          <code>True</code> 并填写 <code>LEVEL_PATH_STORAGE</code>，储水槽会自动切换为实测值。
        </div>
        <h3 style="margin-top:18px;">采集设备 <span class="desc">任务一/二：链路与通道</span></h3>
        <div class="stat-grid2">
          <div class="stat-box"><div class="label">设备地址</div><div class="value">{{ system.device_url || '--' }}</div></div>
          <div class="stat-box"><div class="label">在线状态</div><div class="value">{{ system.device_online ? '在线' : '离线' }}</div></div>
          <div class="stat-box"><div class="label">数据库</div><div class="value">{{ system.database_ok ? '正常' : '异常' }}</div></div>
          <div class="stat-box"><div class="label">服务运行时长</div><div class="value">{{ system.uptime_s ?? '--' }} s</div></div>
        </div>
        <div class="note">设备地址 / 超时 / 通道开关等硬参数集中在 backend/config.py 修改（DEVICE_URL、DEVICE_FEATURES）。</div>
      </div>
    </div>

    <!-- 报警联动（v3 卡片即来源）：触发源与动作都直接选自实时监控页的卡片 -->
    <div class="section" v-show="tab==='alarm'" style="margin-top:14px;">
      <h3>报警联动 <span class="desc">触发源与动作都选自实时监控页的卡片 · 越限沿触发，回正常沿可选恢复</span></h3>

      <!-- 联动规则 -->
      <div class="alarm-rule" style="font-weight:600;margin-top:16px;">联动规则
        <span class="desc" style="font-weight:400;">每条规则自带阈值：选一张实时监控页的卡片作触发源，越限时执行选中的卡片动作</span>
      </div>
      <div v-for="(l, i) in links" :key="l.id" class="link-rule">
        <div class="alarm-rule" style="flex-wrap:wrap;align-items:center;">
          <input v-model="l.name" placeholder="规则名(如 光照过强停泵)" style="width:170px;" :disabled="!perm('cfg_alarm')">
          <span class="desc">触发源</span>
          <select v-model="l.source_card" :disabled="!perm('cfg_alarm')" title="触发源卡片（实时监控页）">
            <option v-for="c in sourceCards" :key="c.id" :value="c.id">{{ c.title || c.id }}</option>
          </select>
          <span class="desc" v-if="!sourceCards.length">实时监控页暂无可取数值的卡片</span>
          <label class="rule-item" style="flex:none;gap:4px;">启用
            <input type="checkbox" v-model="l.enabled" :disabled="!perm('cfg_alarm')">
          </label>
          <button class="btn-ghost danger" :disabled="!perm('cfg_alarm')" @click="delLink(i)">删除规则</button>
        </div>
        <div class="alarm-rule" style="flex-wrap:wrap;align-items:center;">
          <label class="rule-item" style="flex:none;gap:4px;">
            <select v-model="l.direction" :disabled="!perm('cfg_alarm')" title="触发方向">
              <option value="above">超上限</option>
              <option value="below">低于下限</option>
            </select>
          </label>
          <label class="rule-item" style="flex:none;gap:4px;">阈值
            <input type="number" v-model.number="l.threshold" style="width:90px;" :disabled="!perm('cfg_alarm')">
          </label>
          <span class="desc">越限时执行：</span>
          <span v-for="(a, j) in l.actions" :key="'a'+j" class="badge ok" style="align-items:center;gap:5px;">
            {{ actionText(a) }}
            <select v-model="a.card" :disabled="!perm('cfg_alarm')" style="padding:0 4px;font-size:11px;" @change="syncAction(a)">
              <option v-for="x in targetCards" :key="x.id" :value="x.id">{{ x.title || x.id }}</option>
            </select>
            <select v-if="a.kind !== 'quant'" v-model="a.state" :disabled="!perm('cfg_alarm')" style="padding:0 4px;font-size:11px;" @change="syncAction(a)">
              <option value="off">关</option><option value="on">开</option>
            </select>
            <button class="btn-ghost" style="padding:0 5px;font-size:11px;" :disabled="!perm('cfg_alarm')" @click="delAction(l.actions, j)">×</button>
          </span>
          <select :disabled="!perm('cfg_alarm') || !targetCards.length" @change="addAction(l.actions, $event)">
            <option value="">+ 添加动作</option>
            <option v-for="x in targetCards" :key="'t'+x.id" :value="x.id">{{ x.title || x.id }}</option>
          </select>
          <span class="desc" v-if="!targetCards.length">实时监控页暂无控制开关卡</span>
        </div>
        <div class="alarm-rule" style="flex-wrap:wrap;align-items:center;">
          <span class="desc">恢复动作(可选，读数回正常时执行)：</span>
          <span v-for="(a, j) in l.recover.actions" :key="'r'+j" class="badge" style="align-items:center;gap:5px;background:var(--ok-soft);color:var(--ok);">
            {{ actionText(a) }}
            <select v-model="a.card" :disabled="!perm('cfg_alarm')" style="padding:0 4px;font-size:11px;" @change="syncAction(a)">
              <option v-for="x in targetCards" :key="x.id" :value="x.id">{{ x.title || x.id }}</option>
            </select>
            <select v-if="a.kind !== 'quant'" v-model="a.state" :disabled="!perm('cfg_alarm')" style="padding:0 4px;font-size:11px;" @change="syncAction(a)">
              <option value="off">关</option><option value="on">开</option>
            </select>
            <button class="btn-ghost" style="padding:0 5px;font-size:11px;" :disabled="!perm('cfg_alarm')" @click="delAction(l.recover.actions, j)">×</button>
          </span>
          <select :disabled="!perm('cfg_alarm') || !targetCards.length" @change="addAction(l.recover.actions, $event)">
            <option value="">+ 添加恢复动作</option>
            <option v-for="x in targetCards" :key="'tr'+x.id" :value="x.id">{{ x.title || x.id }}</option>
          </select>
        </div>
      </div>
      <div class="alarm-rule" style="margin-top:10px;">
        <button class="btn-ghost" :disabled="!perm('cfg_alarm')" @click="addLink">+ 添加规则</button>
        <button class="btn-primary" :disabled="!perm('cfg_alarm')" @click="saveLinks">保存报警联动</button>
        <span class="desc">触发源与动作都取自实时监控页的卡片（内置 + 自建）；自定义指令经后端代理 GET 下发（仅白名单主机）并写操作日志。</span>
      </div>
      <div class="note" style="margin-top:0;">
        触发语义：读数越过阈值瞬间执行「触发动作」一次，回到正常区间执行「恢复动作」（未配置则不做）。
        加热槽温度建议直接使用恒温闭环(PID)；联动规则建议不超过 10 条。保存后立即生效。
        自定义传感器直接加「自定义接口」卡即可：卡片即声明，历史曲线、数据统计与联动会自动跟随，
        不需要在配置页再填一遍接口。
      </div>
    </div>

    <div class="section" v-show="tab==='account'" v-if="perm('account_manage')">
      <h3>账号管理</h3>
      <div class="alarm-rule">
        <input class="login-input" style="width:150px;" v-model="newUser.username" placeholder="用户名">
        <input class="login-input" style="width:150px;" type="password" v-model="newUser.password" placeholder="密码">
        <select v-model="newUser.role">
          <option v-for="(v,k) in roles" :key="k" :value="k">{{ roleLabel(k) }}</option>
        </select>
        <button class="btn-primary" @click="createUser">创建用户</button>
      </div>
      <table>
        <thead><tr><th>ID</th><th>用户名</th><th>角色</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="u in users" :key="u.id">
            <td>{{ u.id }}</td><td>{{ u.username }}</td>
            <td>
              <select :value="u.role" :disabled="u.role==='admin'" @change="u.role=$event.target.value; setRole(u)">
                <option v-for="(v,k) in roles" :key="k" :value="k">{{ roleLabel(k) }}</option>
              </select>
            </td>
            <td><span class="badge" :class="u.status==='active' ? 'ok' : 'fail'">{{ u.status==='active' ? '正常':'禁用' }}</span></td>
            <td class="cell-ops">
              <button class="btn-ghost" @click="resetPwd(u.id)">重置密码</button>
              <button class="btn-ghost" @click="toggleStatus(u)">{{ u.status==='active' ? '禁用' : '启用' }}</button>
              <button class="btn-ghost" v-if="u.role!=='admin'" @click="delUser(u)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="section" v-show="tab==='account'" v-if="perm('account_manage')">
      <h3>角色权限矩阵 <span class="desc">新增/删除角色 · 勾选每个角色可访问的功能（管理员固定全权限）</span></h3>
      <div class="alarm-rule">
        <input class="login-input" style="width:160px;" v-model="newRoleName" placeholder="新角色名(英文/数字)" @keyup.enter="addRole">
        <button class="btn-primary" @click="addRole">新增角色</button>
        <span class="desc">新增后先在下方勾选权限并保存，即可在创建用户时选用。</span>
      </div>
      <div class="matrix-wrap">
        <table class="perm-matrix">
          <thead>
            <tr>
              <th style="min-width:90px;">权限 \ 角色</th>
              <th v-for="(r, k) in selRoles" :key="k">
                {{ r.label || k }}
                <button class="btn-ghost" v-if="true" title="删除该角色" style="padding:0 4px;font-size:11px;" @click="delRole(k)">×</button>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="p in permMatrix" :key="p[0]">
              <td>{{ p[1] }}</td>
              <td v-for="(r, k) in selRoles" :key="k" style="text-align:center;">
                <input type="checkbox" :checked="hasPerm(k, p[0])" @change="togglePerm(k, p[0], $event)">
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="alarm-rule" style="margin-top:12px;">
        <button class="btn-primary" @click="saveMatrix">保存权限矩阵</button>
      </div>
    </div>
  </div>
  `,
};
