/* 登录态与权限：全局 reactive 状态，token 持久化到 localStorage，页面/按钮级权限判断。 */
window.Auth = Vue.reactive({
  token: localStorage.getItem("iot_token") || "",
  user: null,          // { username, role, perms:[...], role_label }
  ready: false,        // 是否已完成启动校验（me 请求返回或失败）

  has(perm) {
    // 未登录或未初始化视为无权限；perms 含 "*" 表示全部
    if (!this.user || !Array.isArray(this.user.perms)) return false;
    return this.user.perms.includes("*") || this.user.perms.includes(perm);
  },
  setSession(data) {
    // data: { token, user }
    this.token = data.token;
    this.user = data.user;
    localStorage.setItem("iot_token", data.token);
  },
  logout() {
    this.token = "";
    this.user = null;
    localStorage.removeItem("iot_token");
  },
});

/* 启动校验：有 token 则尝试拉取当前用户；失败清除并留在登录页 */
window.Auth.init = async function () {
  if (!window.Auth.token) {
    window.Auth.ready = true;
    return;
  }
  try {
    const me = await API.me();
    window.Auth.user = {
      username: me.username,
      role: me.role,
      perms: me.perms || [],
      role_label: me.role_label || me.role,
    };
  } catch (e) {
    window.Auth.logout();
  } finally {
    window.Auth.ready = true;
  }
};