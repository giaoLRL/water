/* 系统配置页：告警阈值 + 恒温PID参数 + 采集周期。账号/角色管理复用后端接口。 */
window.ViewSysConfig = {
  name: "SysConfigView",
  emits: ["back"],
  data() {
    return {
      tab: "alarm",   // alarm / pid / account
      thresholds: {},
      pid: { kp: 16, ki: 0.3, kd: 25 },
      period: 1.0,
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
      try { this.pid = await API.pidGet(); } catch (e) { /* silent */ }
      try { const s = await API.system(); this.period = s.period || 1.0; } catch (e) { /* silent */ }
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
    async saveEnvUsed() {},
    async saveThresh() {
      const keys = ["storage_temp_max", "storage_temp_min", "heater_temp_max", "heater_temp_min",
                    "flow_max", "flow_min", "pressure_max", "pressure_min"];
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
    async savePid() {
      try {
        await API.pidSet({ kp: this.pid.kp, ki: this.pid.ki, kd: this.pid.kd });
        alert("PID 参数已保存");
      } catch (e) { alert(e.message); }
    },
    async savePeriod() {
      try {
        const d = await API.setPeriod(this.period);
        this.period = d.period;
        alert("采集周期已保存（即时生效）");
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
      <span class="desc">告警阈值 · 恒温PID · 采集周期</span>
    </div>

    <div class="tabs">
      <span class="tab" :class="{ active: tab==='alarm' }" @click="tab='alarm'">告警阈值 / PID / 周期</span>
      <span class="tab" v-if="perm('account_manage')" :class="{ active: tab==='account' }" @click="tab='account'; load()">账号管理</span>
    </div>

    <div class="grid-2 config-grid" v-show="tab==='alarm'">
      <div class="section">
        <h3>告警阈值 <span class="desc">温度/流量/压力上下限 · 超限即告警（任务六）</span></h3>
        <div class="alarm-rule" style="flex-direction:column;align-items:stretch;gap:10px;">
          <div class="grid-2">
            <label class="rule-item"><span>储水槽温度上限 ℃</span><input type="number" v-model.number="thresholds.storage_temp_max"></label>
            <label class="rule-item"><span>储水槽温度下限 ℃</span><input type="number" v-model.number="thresholds.storage_temp_min"></label>
            <label class="rule-item"><span>加热槽温度上限 ℃</span><input type="number" v-model.number="thresholds.heater_temp_max"></label>
            <label class="rule-item"><span>加热槽温度下限 ℃</span><input type="number" v-model.number="thresholds.heater_temp_min"></label>
            <label class="rule-item"><span>水流量上限 L/min</span><input type="number" v-model.number="thresholds.flow_max"></label>
            <label class="rule-item"><span>水流量下限 L/min</span><input type="number" v-model.number="thresholds.flow_min"></label>
            <label class="rule-item"><span>水压上限 kPa</span><input type="number" v-model.number="thresholds.pressure_max"></label>
            <label class="rule-item"><span>水压下限 kPa</span><input type="number" v-model.number="thresholds.pressure_min"></label>
          </div>
          <button class="btn-primary" @click="saveThresh">保存告警阈值</button>
        </div>
      </div>
      <div class="section">
        <h3>采集周期</h3>
        <div class="alarm-rule">
          <label class="rule-item"><span>采集周期(秒)</span><input type="number" v-model.number="period" min="0.5" step="0.5" style="width:90px;"></label>
          <button class="btn-primary" @click="savePeriod">保存</button>
        </div>
        <h3 style="margin-top:18px;">恒温PID参数 <span class="desc">任务六本地恒温</span></h3>
        <div class="alarm-rule">
          <label class="rule-item"><span>Kp</span><input type="number" v-model.number="pid.kp" style="width:90px;"></label>
          <label class="rule-item"><span>Ki</span><input type="number" v-model.number="pid.ki" style="width:90px;"></label>
          <label class="rule-item"><span>Kd</span><input type="number" v-model.number="pid.kd" style="width:90px;"></label>
          <button class="btn-primary" @click="savePid">保存PID</button>
        </div>
        <div class="note">判定服务地址 / 采集端 / 继电器地址等硬参数集中在 backend/config.py 修改。</div>
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