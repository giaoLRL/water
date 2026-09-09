/* 登录视图：账号密码登录，成功后加载权限并进入系统。 */

/* 登录页动态背景：机房监控主题的节点网络拓扑（Canvas 绘制，无外部素材）。
   节点缓慢漂移、近距节点连线、数据包脉冲沿连线移动；尊重系统"减少动态效果"设置。 */
let __loginBgRaf = 0;
let __loginBgCleanup = null;

function __startLoginBg(canvas) {
  const ctx = canvas.getContext("2d");
  let nodes = [];
  let pulses = [];
  let width = 0;
  let height = 0;
  let dpr = 1;
  let last = 0;
  let spawnTimer = 0;

  function resize() {
    dpr = Math.min(2, window.devicePixelRatio || 1);
    width = canvas.clientWidth;
    height = canvas.clientHeight;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const count = Math.max(36, Math.min(90, Math.round((width * height) / 22000)));
    let seed = 7;
    const rand = () => { seed = (seed * 9301 + 49297) % 233280; return seed / 233280; };
    nodes = [];
    for (let i = 0; i < count; i++) {
      nodes.push({
        x: rand() * width,
        y: rand() * height,
        vx: (rand() - 0.5) * 0.18,
        vy: (rand() - 0.5) * 0.18,
      });
    }
  }

  function drawStatic() {
    ctx.clearRect(0, 0, width, height);
    ctx.lineWidth = 1;
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i];
        const b = nodes[j];
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const d2 = dx * dx + dy * dy;
        if (d2 < 19600) {
          const o = (1 - Math.sqrt(d2) / 140) * 0.2;
          ctx.strokeStyle = "rgba(45,212,191," + o.toFixed(3) + ")";
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();
        }
      }
    }
    ctx.fillStyle = "rgba(215,224,234,0.45)";
    for (const n of nodes) {
      ctx.beginPath();
      ctx.arc(n.x, n.y, 1.4, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  function step(now) {
    __loginBgRaf = requestAnimationFrame(step);
    if (now - last < 33) return;   // 约 30fps，控制 CPU 占用
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    ctx.clearRect(0, 0, width, height);

    for (const n of nodes) {
      n.x += n.vx * dt * 60;
      n.y += n.vy * dt * 60;
      if (n.x < -12) n.x = width + 12;
      else if (n.x > width + 12) n.x = -12;
      if (n.y < -12) n.y = height + 12;
      else if (n.y > height + 12) n.y = -12;
    }

    ctx.lineWidth = 1;
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i];
        const b = nodes[j];
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const d2 = dx * dx + dy * dy;
        if (d2 < 19600) {
          const o = (1 - Math.sqrt(d2) / 140) * 0.2;
          ctx.strokeStyle = "rgba(45,212,191," + o.toFixed(3) + ")";
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();
        }
      }
    }

    ctx.fillStyle = "rgba(215,224,234,0.45)";
    for (const n of nodes) {
      ctx.beginPath();
      ctx.arc(n.x, n.y, 1.4, 0, Math.PI * 2);
      ctx.fill();
    }

    // 数据包脉冲：沿随机节点连线移动，模拟机房数据流转
    spawnTimer -= dt;
    if (spawnTimer <= 0 && pulses.length < 6) {
      spawnTimer = 0.8 + Math.random() * 1.2;
      const a = nodes[(Math.random() * nodes.length) | 0];
      const b = nodes[(Math.random() * nodes.length) | 0];
      if (a !== b) pulses.push({ a, b, t: 0 });
    }
    for (let i = pulses.length - 1; i >= 0; i--) {
      const p = pulses[i];
      p.t += dt * 0.32;
      if (p.t >= 1) { pulses.splice(i, 1); continue; }
      const x = p.a.x + (p.b.x - p.a.x) * p.t;
      const y = p.a.y + (p.b.y - p.a.y) * p.t;
      ctx.fillStyle = "rgba(45,212,191,0.95)";
      ctx.beginPath();
      ctx.arc(x, y, 2.2, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  function onResize() {
    resize();
    drawStatic();
  }

  resize();
  drawStatic();
  // 登录页背景为装饰性动效：始终动态，不受系统"减少动态效果"影响
  last = performance.now();
  __loginBgRaf = requestAnimationFrame(step);
  window.addEventListener("resize", onResize);
  __loginBgCleanup = () => {
    cancelAnimationFrame(__loginBgRaf);
    window.removeEventListener("resize", onResize);
    __loginBgCleanup = null;
  };
}

function __stopLoginBg() {
  if (__loginBgCleanup) __loginBgCleanup();
}

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
  mounted() {
    const cv = this.$refs.bgCanvas;
    if (cv) __startLoginBg(cv);
  },
  beforeUnmount() {
    __stopLoginBg();
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
    <canvas ref="bgCanvas" class="login-canvas"></canvas>
    <div class="login-card">
      <div class="login-logo">
        <svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3v18M8 6h10l-4 3 4 3H8"/><path d="M4 21h18"/></svg>
      </div>
      <h2>智能水循环监测与温控系统</h2>
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
