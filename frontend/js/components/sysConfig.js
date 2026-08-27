/* 系统配置视图：灯杆管理 + 服务接口参数 + 传感器格式映射，修改即时生效、无需改代码。 */
window.ViewSysConfig = {
  name: "SysConfigView",
  emits: ["back"],
  data() {
    return {
      loading: true,
      saving: false,
      showSpec: false,   // 接口数据格式说明展开/收起
      // 灯杆管理
      lampSeeds: [ { id: "", name: "", location: "", rtsp_url: "", sensor_url: "", esp32_base: "", cfgStr: "" } ],
      // 服务接口参数
      service: { infer_url: "", infer_timeout: 60, person_detect_interval: 5, sample_interval: 2 },
      // 传感器格式映射
      sensorFields: { status: "status", temperature: "temperature", humidity: "humidity", light: "light" },
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
  },
  mounted() {
    this.load();
  },
  methods: {
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
        };
        this.sensorFields = { ...d.sensor_fields };
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
          <h4>① 传感器接口 GET {sensor_url}（对应下方"传感器格式映射"四项）</h4>
<pre>{
  "status": "ok",              // ok=全部正常 / partial=部分不可用
  "temperature": 24.1,         // 温度 ℃
  "humidity": 53.7,            // 湿度 %
  "light": 19.2,               // 光照 lx（读不到时为 null）
  "unit": { "temperature": "C", "humidity": "%", "light": "lx" },
  "lastUpdateMs": 616
}</pre>
          <h4>② 灯控接口 GET {esp32_base}/api/lamp/{on|off|state}（各灯杆路径可在"灯杆管理"单独配置）</h4>
<pre>{ "status": "ok", "lamp": true }      // lamp: true=亮 false=灭；on 须返回 true、off 须返回 false 才算生效</pre>
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

      <!-- 服务接口参数 -->
      <div class="section">
        <h3>服务接口参数 <span class="desc">AI 识别地址 / 超时 / 识别间隔 / 采样间隔</span></h3>
        <div class="alarm-rule">
          <label class="rule-item cfg-item"><span>AI 识别地址</span>
            <input class="cfg-input" style="width:260px;" v-model="service.infer_url"></label>
          <label class="rule-item cfg-item"><span>识别超时(秒)</span>
            <input class="cfg-input" style="width:70px;" type="number" v-model.number="service.infer_timeout" min="1"></label>
          <label class="rule-item cfg-item"><span>识别间隔(秒)</span>
            <input class="cfg-input" style="width:70px;" type="number" v-model.number="service.person_detect_interval" min="1"></label>
          <label class="rule-item cfg-item"><span>采样间隔(秒)</span>
            <input class="cfg-input" style="width:70px;" type="number" v-model.number="service.sample_interval" min="0.5" step="0.5"></label>
          <button class="btn-primary" :disabled="saving" @click="saveService">保存服务参数</button>
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
          <button class="btn-primary" :disabled="saving" @click="saveSensor">保存格式映射</button>
        </div>
        <div class="note">说明：以 ESP32 返回 {"status":"ok","temperature":..,"humidity":..,"light":..} 为默认，
        若换用其他设备只需把"字段名"改成其返回的 JSON key。保存后灯杆会重建以立即采用新格式。</div>
      </div>

      <div class="note" style="margin-top:6px;">
        提示：数据库连接、后端端口等危险参数不支持在线修改；配置无鉴权，请仅在局域网内使用。
      </div>
    </template>
  </div>
  `,
};