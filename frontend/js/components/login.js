/* 登录视图：账号密码登录，成功后加载权限并进入系统。 */
window.ViewLogin = {
  name: "LoginView",
  emits: ["logged"],
  data() {
    return {
      username: "",
      password: "",
      busy: false,
      msg: "",
      msgType: "",
    };
  },
  methods: {
    async doLogin() {
      const u = this.username.trim();
      const p = this.password;
      if (!u || !p) { this.showMsg("请输入用户名和密码", "error"); return; }
      this.busy = true;
      try {
        const d = await API.login(u, p);
        window.Auth.setSession(d);
        this.showMsg("登录成功，正在进入系统…", "ok");
        this.$emit("logged");
      } catch (e) {
        this.showMsg(e.message, "error");
      } finally {
        this.busy = false;
      }
    },
    showMsg(t, type) {
      this.msg = t;
      this.msgType = type || "";
    },
  },
  template: `
  <div class="login-wrap">
    <div class="login-card">
      <div class="login-logo">
        <svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3v18M8 6h10l-4 3 4 3H8"/><path d="M4 21h18"/></svg>
      </div>
      <h2>基于物联网的分布式机房监控系统</h2>
      <p class="login-sub">账号登录 · 权限由系统管理员分配</p>
      <input class="login-input" type="text" v-model="username" placeholder="用户名" autocomplete="username" @keyup.enter="doLogin">
      <input class="login-input" type="password" v-model="password" placeholder="密码" autocomplete="current-password" @keyup.enter="doLogin">
      <button class="btn-primary login-btn" :disabled="busy" @click="doLogin">{{ busy ? '登录中…' : '登 录' }}</button>
      <div v-if="msg" class="config-msg" :class="msgType" style="margin-top:12px;">{{ msg }}</div>
      <div class="login-tip">默认管理员账号：admin / admin123（请尽快修改密码）</div>
    </div>
  </div>
  `,
};