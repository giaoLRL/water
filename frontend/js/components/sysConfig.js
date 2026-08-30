/* 系统配置视图：灯杆管理 + 服务接口参数 + 传感器格式映射 + 账号与权限，修改即时生效。 */
window.ViewSysConfig = {
  name: "SysConfigView",
  emits: ["back"],
  data() {
    return {
      loading: true,
      saving: false,
      showSpec: false,   // 接口数据格式说明展开/收起
      // 账号与权限
      users: [],
      permsList: [],            // [[key, label], ...]
      roleMatrix: {},           // {role: [label, perms[]] }
      newUser: { username: "", password: "", role: "viewer" },
      rolePerms: {},            // 勾选态 {role: {permKey: bool}}（仅非 admin）
      newRoleKey: "",
      newRoleLabel: "",
      pwd: { old: "", new: "", new2: "" },   // 修改自己的密码
      // 灯杆管理
      lampSeeds: [ { id: "", name: "", location: "", rtsp_url: "", sensor_url: "", esp32_base: "", cfgStr: "" } ],
      // 服务接口参数
      service: { infer_url: "", infer_timeout: 60, person_detect_interval: 5, sample_interval: 2, sensor_timeout: 3, sensor_ttl: 5, sensor_trip: 3, sensor_cooldown: 15 },
      // 设备在线检测参数
      monitor: { interval: 10, timeout: 1.5 },
      // 灯控接口全局默认格式（fallback）
      lampCtrlDefault: { on: "/api/lamp/on", off: "/api/lamp/off", state: "/api/lamp/state", field: "lamp", status: "status" },
      // 传感器格式映射
      sensorFields: { status: "status", temperature: "temperature", humidity: "humidity", light: "light", smoke: "smokeRaw", smoke_alarm: "smokeAlarm" },
      msg: "",
      msgType: "",
    };
  },
  computed: {
    lampPosts() {
      // 灯杆提交数据：把"灯控接口"简写串解析为 lamp_ctrl 配置
      return this.lampSeeds.filter((l) => l.id && String(l.id).trim()).map((l) => {
        const p = { ...l };
        const s = String(l.cfgStr || "").trim();
        if (s) {
          const parts = s.split("|").map((x) => x.trim());
          const o = {};
          ["on", "off", "state", "field"].forEach((k, i) => { if (parts[i]) o[k] = parts[i]; });
          p.lamp_ctrl = o;
        } else {
          delete p.lamp_ctrl;
        }
        delete p.cfgStr;
        return p;
      });
    },
    roleRows() {
      // 矩阵表格行：角色 key + 显示名 + 权限列表（可编辑的勾选态）
      return Object.keys(this.roleMatrix).map((key) => {
        const item = this.roleMatrix[key];
        const label = Array.isArray(item) && item.length ? item[0] : key;
        const perms = Array.isArray(item) && item.length > 1 && Array.isArray(item[1]) ? item[1] : [];
        return { key, label, perms: perms.slice() };
      });
    },
    canManageAccount() {
      return window.Auth ? window.Auth.has("account_manage") : false;
    },
  },
  mounted() {
    this.load();
  },
  methods: {
    perm(p) {
      return window.Auth ? window.Auth.has(p) : false;
    },
    async load() {
      this.loading = true;
      try {
        const d = await API.sysConfigGet();
        this.lampSeeds = (d.lamp_posts || []).map((l) => {
          const lc = l.lamp_ctrl || {};
          return {
            ...l,
            // 灯杆级灯控接口简写：on|off|state|field
            cfgStr: [lc.on, lc.off, lc.state, lc.field].filter(Boolean).join("|"),
          };
        });
        this.service = {
          infer_url: d.infer_url,
          infer_timeout: d.infer_timeout,
          person_detect_interval: d.person_detect_interval,
          sample_interval: d.sample_interval,
          sensor_timeout: d.sensor_timeout,
          sensor_ttl: d.sensor_ttl,
          sensor_trip: d.sensor_trip,
          sensor_cooldown: d.sensor_cooldown,
        };
        this.monitor = { interval: d.monitor_interval, timeout: d.monitor_timeout };
        this.lampCtrlDefault = { on: "/api/lamp/on", off: "/api/lamp/off", state: "/api/lamp/state", field: "lamp", status: "status", ...d.lamp_ctrl_default };
        this.sensorFields = { smoke: "smokeRaw", smoke_alarm: "smokeAlarm", ...d.sensor_fields };
        // 账号与权限（有管理权限才拉取）
        if (this.canManageAccount) {
          await this.loadAccount();
        }
      } catch (e) {
        this.showMsg(e.message, "error");
      } finally {
        this.loading = false;
      }
    },
    addLamp() {
      this.lampSeeds.push({ id: "", name: "", location: "", rtsp_url: "", sensor_url: "", esp32_base: "" });
    },
    removeLamp(i) {
      this.lampSeeds.splice(i, 1);
    },
    async saveLamps() {
      if (!this.lampPosts.length) { this.showMsg("至少保留 1 个灯杆（或先只改不改删）", "error"); return; }
      this.saving = true;
      try {
        const d = await API.sysConfigSet({ lamp_posts: this.lampPosts });
        this.showMsg(d.msg, "ok");
      } catch (e) {
        this.showMsg(e.message, "error");
      } finally {
        this.saving = false;
        this.load();
      }
    },
    async saveService() {
      this.saving = true;
      try {
        const d = await API.sysConfigSet({ service: this.service });
        this.showMsg(d.msg, "ok");
      } catch (e) {
        this.showMsg(e.message, "error");
      } finally {
        this.saving = false;
      }
    },
    async saveSensor() {
      this.saving = true;
      try {
        const d = await API.sysConfigSet({ sensor_fields: this.sensorFields });
        this.showMsg(d.msg, "ok");
      } catch (e) {
        this.showMsg(e.message, "error");
      } finally {
        this.saving = false;
        this.load();
      }
    },
    async saveLampCtrl() {
      this.saving = true;
      try {
        const d = await API.sysConfigSet({ lamp_ctrl_default: this.lampCtrlDefault });
        this.showMsg(d.msg, "ok");
      } catch (e) {
        this.showMsg(e.message, "error");
      } finally {
        this.saving = false;
      }
    },
    async saveMonitor() {
      this.saving = true;
      try {
        const d = await API.sysConfigSet({ service: { monitor_interval: this.monitor.interval, monitor_timeout: this.monitor.timeout } });
        this.showMsg(d.msg, "ok");
      } catch (e) {
        this.showMsg(e.message, "error");
      } finally {
        this.saving = false;
      }
    },
    // ---- 账号与权限 ----
    async loadAccount() {
      try {
        const [u, r] = await Promise.all([API.users(), API.roles()]);
        this.users = u.users || [];
        this.permsList = (r.permissions || []).map((p) => (Array.isArray(p) ? p : [p, p]));
        this.roleMatrix = r.roles || {};
      } catch (e) { /* silent */ }
    },
    roleLabel(key) {
      const item = this.roleMatrix[key];
      if (Array.isArray(item) && item.length) return item[0];
      return key;
    },
    roleHas(key, perm) {
      const item = this.roleMatrix[key];
      if (!Array.isArray(item) || item.length < 2) return false;
      if (!Array.isArray(item[1])) return false;
      return item[1].includes(perm);
    },
    toggleRolePerm(key, perm) {
      const item = this.roleMatrix[key];
      if (!Array.isArray(item) || item.length < 2 || !Array.isArray(item[1])) return;
      const list = item[1];
      if (list.includes(perm)) item[1] = list.filter((p) => p !== perm);
      else item[1] = list.concat([perm]);
      // 触发视图更新
      this.roleMatrix = Object.assign({}, this.roleMatrix);
    },
    async saveRolesMatrix() {
      const roles = {};
      for (const row of this.roleRows) {
        const item = this.roleMatrix[row.key];
        const perms = Array.isArray(item) && item.length > 1 && Array.isArray(item[1]) ? item[1] : [];
        roles[row.key] = { label: row.label, perms };
      }
      this.saving = true;
      try {
        const d = await API.saveRoles(roles);
        this.roleMatrix = d.roles;
        this.showMsg("角色矩阵已保存", "ok");
      } catch (e) {
        this.showMsg(e.message, "error");
      } finally {
        this.saving = false;
      }
    },
    addRole() {
      const key = String(this.newRoleKey || "").trim();
      const label = String(this.newRoleLabel || "").trim() || key;
      if (!key) { this.showMsg("请输入角色标识（英文/数字）", "error"); return; }
      if (this.roleMatrix[key]) { this.showMsg("角色已存在", "error"); return; }
      this.roleMatrix[key] = [label, []];
      this.roleMatrix = Object.assign({}, this.roleMatrix);
      this.newRoleKey = "";
      this.newRoleLabel = "";
      this.showMsg(`已新增角色 ${key}（点击下方勾选权限后保存生效）`, "ok");
    },
    async createUser() {
      const u = String(this.newUser.username || "").trim();
      const p = this.newUser.password || "";
      if (!u || !p) { this.showMsg("请填写用户名和密码", "error"); return; }
      if (!this.roleMatrix[this.newUser.role]) { this.showMsg("角色不存在", "error"); return; }
      this.saving = true;
      try {
        await API.createUser({ username: u, password: p, role: this.newUser.role });
        this.showMsg("用户已创建", "ok");
        this.newUser = { username: "", password: "", role: "viewer" };
        await this.loadAccount();
      } catch (e) {
        this.showMsg(e.message, "error");
      } finally {
        this.saving = false;
      }
    },
    async resetPwd(user) {
      const p = window.prompt(`输入 ${user.username} 的新密码：`);
      if (p == null) return;
      if (!p.trim()) { this.showMsg("密码不能为空", "error"); return; }
      try {
        await API.resetPassword(user.id, p.trim());
        this.showMsg("密码已重置", "ok");
      } catch (e) {
        this.showMsg(e.message, "error");
      }
    },
    async toggleUserStatus(user) {
      const next = user.status === "active" ? "disabled" : "active";
      try {
        await API.setUserStatus(user.id, next);
        await this.loadAccount();
      } catch (e) {
        this.showMsg(e.message, "error");
      }
    },
    async removeUser(user) {
      if (!window.confirm(`确认删除用户 ${user.username}？`)) return;
      try {
        await API.deleteUser(user.id);
        this.showMsg("用户已删除", "ok");
        await this.loadAccount();
      } catch (e) {
        this.showMsg(e.message, "error");
      }
    },
    async setUserRole(user, role) {
      try {
        await API.setUserRole(user.id, role);
        this.showMsg(`已将 ${user.username} 设为角色「${this.roleLabel(role)}」`, "ok");
      } catch (e) {
        this.showMsg(e.message, "error");
      }
    },
    async savePwd() {
      if (!this.pwd.new) { this.showMsg("请输入新密码", "error"); return; }
      if (this.pwd.new !== this.pwd.new2) { this.showMsg("两次输入的新密码不一致", "error"); return; }
      try {
        await API.changePassword(this.pwd.new);
        this.pwd = { old: "", new: "", new2: "" };
        this.showMsg("密码已修改", "ok");
      } catch (e) {
        this.showMsg(e.message, "error");
      }
    },
    showMsg(t, type) {
      this.msg = t;
      this.msgType = type || "";
      setTimeout(() => { this.msg = ""; this.msgType = ""; }, 6000);
    },
  },
  template: `
  <div class="view-page">
    <div class="detail-head">
      <button class="btn-ghost" @click="$emit('back')">← 返回</button>
      <h2>系统配置</h2>
      <span class="desc">修改即时生效，无需改代码或重启后端</span>
    </div>

    <div v-if="msg" class="config-msg" :class="msgType">{{ msg }}</div>
    <p v-if="loading" class="note" style="padding:20px 0;">配置加载中…</p>

    <template v-if="!loading">
      <!-- 接口数据格式说明（可点开） -->
      <div class="section" style="padding-bottom:0;">
        <div class="spec-toggle" @click="showSpec = !showSpec">
          <h3 style="margin-bottom:0;">
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-3px; margin-right:6px;"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>
            接口数据格式说明
          </h3>
          <span class="desc">
            {{ showSpec ? '点击收起' : '点开查看传感器 / 灯控 / 系统接口的返回格式与错误码' }}
            <svg v-if="showSpec" viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-1px;"><polyline points="18 15 12 9 6 15"/></svg>
            <svg v-else viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-1px;"><polyline points="6 9 12 15 18 9"/></svg>
          </span>
        </div>
        <div v-if="showSpec" class="spec-body">
          <h4>① 传感器接口 GET {sensor_url}（对应下方"传感器格式映射"六项）</h4>
<pre>{
  "status": "ok",              // ok=全部正常 / partial=部分不可用
  "temperature": 24.1,         // 温度 ℃
  "humidity": 53.7,            // 湿度 %
  "light": 19.2,               // 光照 lx（读不到时为 null）
  "smokeRaw": 1234,            // 烟雾浓度（MQ-2 AO 原始值 0~4095，可空）
  "smokeAlarm": false,         // 烟雾报警（MQ-2 DO，true=超标，可空）
  "unit": { "temperature": "C", "humidity": "%", "light": "lx" },
  "lastUpdateMs": 616
}</pre>
          <h4>② 灯控接口 GET {esp32_base}{on|off|state 路径}（路径可在"灯杆管理"或"灯控全局默认格式"配置）</h4>
<pre>{ "status": "ok", "lamp": true }      // lamp: true=亮 false=灭；on 须返回 true、off 须返回 false 才算生效
// 未单独配置灯控接口的灯杆，使用"灯控全局默认格式"里的 on/off/state/字段 组装请求</pre>
          <h4>③ 系统接口统一返回格式（所有 /api/*）</h4>
<pre>{
  "code": 0,        // 0=成功，见下方错误码
  "msg": "ok",
  "data": { ... }   // 业务数据
}</pre>
          <div style="overflow-x:auto;">
            <table>
              <thead><tr><th>错误码</th><th>含义</th></tr></thead>
              <tbody>
                <tr><td>0</td><td>成功</td></tr>
                <tr><td>40002</td><td>参数错误</td></tr>
                <tr><td>40003</td><td>控制指令执行失败（如灯控时 ESP32 离线）</td></tr>
                <tr><td>40004</td><td>灯杆 / 记录不存在</td></tr>
                <tr><td>40005</td><td>视频流暂无画面</td></tr>
                <tr><td>50000</td><td>智能识别服务调用失败</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- 灯杆管理 -->
      <div class="section">
        <h3>灯杆管理 <span class="desc">ID / 名称 / 位置 / 视频流 / 传感器 / 灯控地址，支持增删</span></h3>
        <div style="overflow-x:auto;">
          <table>
            <thead><tr>
              <th>ID</th><th>名称</th><th>位置</th><th>RTSP 视频流</th><th>传感器 URL</th><th>灯控地址</th><th>灯控接口(on/off/state/字段)</th><th></th>
            </tr></thead>
            <tbody>
              <tr v-for="(l, i) in lampSeeds" :key="i">
                <td><input class="cfg-input" style="width:52px;" v-model="l.id" placeholder="01"></td>
                <td><input class="cfg-input" style="width:86px;" v-model="l.name" placeholder="灯杆01"></td>
                <td><input class="cfg-input" style="width:110px;" v-model="l.location" placeholder="东门主干道"></td>
                <td><input class="cfg-input" v-model="l.rtsp_url" placeholder="rtsp://... 空=无视频"></td>
                <td><input class="cfg-input" style="width:200px;" v-model="l.sensor_url" placeholder="http://.../api/data"></td>
                <td><input class="cfg-input" style="width:170px;" v-model="l.esp32_base" placeholder="http://...灯控"></td>
                <td><input class="cfg-input" style="width:230px;" v-model="l.cfgStr" placeholder="如 /api/mos?state=1|/api/mos?state=0|/api/mos|mos（留空=默认 /api/lamp/*）"></td>
                <td><button class="btn-ghost" style="color:var(--danger);" @click="removeLamp(i)">删除</button></td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="table-actions" style="margin-top:10px;">
          <button class="btn-ghost" @click="addLamp">+ 新增灯杆</button>
          <button class="btn-primary" :disabled="saving" @click="saveLamps">保存灯杆配置</button>
          <span class="desc">保存仅重建/删除变化的灯杆，其余视频与识别不中断</span>
        </div>
      </div>

      <!-- 灯控接口全局默认格式 -->
      <div class="section">
        <h3>灯控接口全局默认格式 <span class="desc">fallback：未单独配置灯控接口的灯杆使用</span></h3>
        <div class="alarm-rule">
          <label class="rule-item cfg-item"><span>开灯路径</span>
            <input class="cfg-input" style="width:150px;" v-model="lampCtrlDefault.on"></label>
          <label class="rule-item cfg-item"><span>关灯路径</span>
            <input class="cfg-input" style="width:150px;" v-model="lampCtrlDefault.off"></label>
          <label class="rule-item cfg-item"><span>状态路径</span>
            <input class="cfg-input" style="width:150px;" v-model="lampCtrlDefault.state"></label>
          <label class="rule-item cfg-item"><span>状态字段</span>
            <input class="cfg-input" style="width:90px;" v-model="lampCtrlDefault.field"></label>
          <label class="rule-item cfg-item"><span>status 字段</span>
            <input class="cfg-input" style="width:90px;" v-model="lampCtrlDefault.status"></label>
          <button class="btn-primary" :disabled="saving" @click="saveLampCtrl">保存默认格式</button>
        </div>
        <div class="note">说明：未在"灯杆管理"里单独填写灯控接口的灯杆，用这里的 on/off/state/字段 组装请求 URL 与解析返回；在灯杆管理里填了就覆盖此项。</div>
      </div>

      <!-- 服务接口参数 -->
      <div class="section">
        <h3>服务接口参数 <span class="desc">AI 识别地址 / 超时 / 识别间隔 / 采样间隔 / 传感器超时与熔断</span></h3>
        <div class="alarm-rule">
          <label class="rule-item cfg-item"><span>AI 识别地址</span>
            <input class="cfg-input" style="width:260px;" v-model="service.infer_url"></label>
          <label class="rule-item cfg-item"><span>识别超时(秒)</span>
            <input class="cfg-input" style="width:70px;" type="number" v-model.number="service.infer_timeout" min="1"></label>
          <label class="rule-item cfg-item"><span>识别间隔(秒)</span>
            <input class="cfg-input" style="width:70px;" type="number" v-model.number="service.person_detect_interval" min="1"></label>
          <label class="rule-item cfg-item"><span>采样间隔(秒)</span>
            <input class="cfg-input" style="width:70px;" type="number" v-model.number="service.sample_interval" min="0.5" step="0.5"></label>
          <label class="rule-item cfg-item"><span>传感器超时(秒)</span>
            <input class="cfg-input" style="width:70px;" type="number" v-model.number="service.sensor_timeout" min="0.5" step="0.5"></label>
          <label class="rule-item cfg-item"><span>传感器缓存(秒)</span>
            <input class="cfg-input" style="width:70px;" type="number" v-model.number="service.sensor_ttl" min="0.5" step="0.5"></label>
          <label class="rule-item cfg-item"><span>熔断失败次数</span>
            <input class="cfg-input" style="width:70px;" type="number" v-model.number="service.sensor_trip" min="1" step="1"></label>
          <label class="rule-item cfg-item"><span>熔断冷却(秒)</span>
            <input class="cfg-input" style="width:70px;" type="number" v-model.number="service.sensor_cooldown" min="1" step="1"></label>
          <button class="btn-primary" :disabled="saving" @click="saveService">保存服务参数</button>
        </div>
        <div class="note">说明：ESP32 /api/data 读取温湿度/光照/烟雾较慢（约 1~2 秒），传感器超时太短会导致请求频繁失败并触发熔断、数据变 0；一般超时设 3 秒、缓存 5 秒即可。</div>
      </div>

      <!-- 设备在线检测参数 -->
      <div class="section">
        <h3>设备在线检测参数 <span class="desc">后端服务 / 数据库 / AI 识别 / 传感器视频的在线状态探测</span></h3>
        <div class="alarm-rule">
          <label class="rule-item cfg-item"><span>探测间隔(秒)</span>
            <input class="cfg-input" style="width:70px;" type="number" v-model.number="monitor.interval" min="5" step="1"></label>
          <label class="rule-item cfg-item"><span>探测超时(秒)</span>
            <input class="cfg-input" style="width:70px;" type="number" v-model.number="monitor.timeout" min="0.5" step="0.5"></label>
          <button class="btn-primary" :disabled="saving" @click="saveMonitor">保存检测参数</button>
        </div>
      </div>

      <!-- 传感器格式映射 -->
      <div class="section">
        <h3>传感器格式映射 <span class="desc">接口返回的字段名（适配不同品牌传感器）</span></h3>
        <div class="alarm-rule">
          <label class="rule-item cfg-item"><span>状态字段</span>
            <input class="cfg-input" style="width:90px;" v-model="sensorFields.status"></label>
          <label class="rule-item cfg-item"><span>温度字段</span>
            <input class="cfg-input" style="width:110px;" v-model="sensorFields.temperature"></label>
          <label class="rule-item cfg-item"><span>湿度字段</span>
            <input class="cfg-input" style="width:110px;" v-model="sensorFields.humidity"></label>
          <label class="rule-item cfg-item"><span>光照字段</span>
            <input class="cfg-input" style="width:110px;" v-model="sensorFields.light"></label>
          <label class="rule-item cfg-item"><span>烟雾浓度字段</span>
            <input class="cfg-input" style="width:110px;" v-model="sensorFields.smoke"></label>
          <label class="rule-item cfg-item"><span>烟雾报警字段</span>
            <input class="cfg-input" style="width:110px;" v-model="sensorFields.smoke_alarm"></label>
          <button class="btn-primary" :disabled="saving" @click="saveSensor">保存格式映射</button>
        </div>
        <div class="note">说明：以 ESP32 返回 {"status":"ok","temperature":..,"humidity":..,"light":..,"smokeRaw":..,"smokeAlarm":..} 为默认，
        若换用其他设备只需把"字段名"改成其返回的 JSON key。烟雾字段可选（无 MQ-2 的设备留空即可，自动回退默认）。保存后灯杆会重建以立即采用新格式。</div>
      </div>

      <!-- 账号与权限 -->
      <div class="section" v-if="perm('account_manage')">
        <h3>账号与权限 <span class="desc">用户管理 + 角色权限矩阵，逐项勾选即时生效</span></h3>

        <!-- 修改自己的密码 -->
        <div class="alarm-rule" style="margin-bottom:14px;">
          <span class="desc" style="flex:1;">修改我的密码</span>
          <label class="rule-item cfg-item"><span>新密码</span>
            <input class="cfg-input" style="width:140px;" type="password" v-model="pwd.new"></label>
          <label class="rule-item cfg-item"><span>确认密码</span>
            <input class="cfg-input" style="width:140px;" type="password" v-model="pwd.new2"></label>
          <button class="btn-ghost" :disabled="saving" @click="savePwd">修改密码</button>
        </div>

        <!-- 用户列表 -->
        <h4 class="sub-head">用户列表</h4>
        <div style="overflow-x:auto;">
          <table>
            <thead><tr><th>ID</th><th>用户名</th><th>角色</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead>
            <tbody>
              <tr v-for="u in users" :key="u.id">
                <td>{{ u.id }}</td>
                <td>{{ u.username }}</td>
                <td>
                  <select class="cfg-input" style="width:110px;" :disabled="u.username === 'admin'" :value="u.role" @change="setUserRole(u, $event.target.value)">
                    <option v-for="rk in Object.keys(roleMatrix)" :key="rk" :value="rk">{{ roleLabel(rk) }}</option>
                  </select>
                </td>
                <td><span class="badge" :class="u.status === 'active' ? 'ok' : 'fail'">{{ u.status === 'active' ? '正常' : '已禁用' }}</span></td>
                <td>{{ u.created_at }}</td>
                <td>
                  <button class="btn-ghost" @click="resetPwd(u)">重置密码</button>
                  <button class="btn-ghost" :disabled="u.username === 'admin'" @click="toggleUserStatus(u)">{{ u.status === 'active' ? '禁用' : '启用' }}</button>
                  <button class="btn-ghost" style="color:var(--danger);" :disabled="u.username === 'admin'" @click="removeUser(u)">删除</button>
                </td>
              </tr>
              <tr v-if="!users.length"><td colspan="6" style="text-align:center;color:#6b7a90;">暂无用户</td></tr>
            </tbody>
          </table>
        </div>
        <!-- 新增用户 -->
        <div class="alarm-rule" style="margin-top:10px;">
          <span class="desc" style="flex:1;">新增用户</span>
          <input class="cfg-input" style="width:120px;" v-model="newUser.username" placeholder="用户名">
          <input class="cfg-input" style="width:120px;" type="password" v-model="newUser.password" placeholder="初始密码">
          <select class="cfg-input" style="width:110px;" v-model="newUser.role">
            <option v-for="rk in Object.keys(roleMatrix).filter(r => r !== 'admin')" :key="rk" :value="rk">{{ roleLabel(rk) }}</option>
          </select>
          <button class="btn-primary" :disabled="saving" @click="createUser">创建用户</button>
        </div>

        <!-- 角色权限矩阵 -->
        <h4 class="sub-head">角色权限矩阵 <span class="desc">勾选权限点（账号重登录后生效）</span></h4>
        <div class="matrix-wrap">
          <table class="perm-matrix">
            <thead>
              <tr>
                <th class="matrix-role-col">角色 \\ 权限</th>
                <th v-for="(p, pi) in permsList" :key="p[0]" :title="p[1]">{{ p[1] }}</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in roleRows" :key="row.key">
                <td class="matrix-role-col">
                  <b>{{ row.label }}</b>
                  <span class="matrix-role-key">{{ row.key }}</span>
                  <em v-if="row.key === 'admin'" class="role-tag">全权限</em>
                </td>
                <td v-for="(p, pi) in permsList" :key="p[0]" class="matrix-cell">
                  <input v-if="row.key !== 'admin'" type="checkbox"
                    :checked="roleHas(row.key, p[0])" @change="toggleRolePerm(row.key, p[0])">
                  <span v-else class="matrix-all">✓</span>
                </td>
                <td class="matrix-note">
                  <span v-if="row.key === 'admin'">管理员必为全权限，不可修改</span>
                  <span v-else>{{ row.perms.length }}/{{ permsList.length }} 项</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="table-actions" style="margin-top:10px;">
          <input class="cfg-input" style="width:110px;" v-model="newRoleKey" placeholder="角色标识(如 guard)">
          <input class="cfg-input" style="width:110px;" v-model="newRoleLabel" placeholder="角色名(如 安保)">
          <button class="btn-ghost" @click="addRole">+ 新增角色</button>
          <button class="btn-primary" :disabled="saving" @click="saveRolesMatrix">保存角色矩阵</button>
          <span class="desc">已登录用户需重新登录后按新权限生效</span>
        </div>
      </div>

      <div class="note" style="margin-top:6px;">
        提示：数据库连接、后端端口等危险参数不支持在线修改；配置无鉴权，请仅在局域网内使用。
      </div>
    </template>
  </div>
  `,
};