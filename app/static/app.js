"use strict";
const $ = (s) => document.querySelector(s);
const state = {csrf: "", page: "tasks", accounts: [], jobs: [], paused: false, ticket: "", stage: "start", logged: false};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const date = (v, zone) => new Date(v * 1000).toLocaleString("zh-CN", {timeZone: zone || "Asia/Shanghai", month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false});
let toastTimer, authEpoch = 0;
function toast(message) { $("#toast").textContent = message; $("#toast").hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $("#toast").hidden = true, 5000); }
async function api(path, method="GET", data) {
  const epoch = authEpoch;
  const response = await fetch("/api" + path, {method, headers: {"Content-Type":"application/json","X-CSRF-Token":state.csrf}, body:data === undefined ? undefined : JSON.stringify(data)});
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && path !== "/login" && epoch === authEpoch) logoutView();
    throw new Error(typeof result.detail === "string" ? result.detail : "请求失败，请稍后重试");
  }
  return result;
}
function logoutView() {
  authEpoch++;
  state.logged = false; state.csrf = ""; state.accounts = []; state.jobs = [];
  document.querySelectorAll("dialog[open]").forEach(d => d.close());
  $("#app-view").hidden = true; $("#login-view").hidden = false;
}
function enter(user) {
  authEpoch++;
  state.logged = true; state.csrf = user.csrf;
  $("#username").textContent = user.username;
  $("#login-view").hidden = true; $("#app-view").hidden = false;
  $("#login-form").reset(); $("#login-error").textContent = "";
  return refresh();
}
async function busy(form, action) {
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  const error = form.querySelector(".form-error");
  if(error) error.textContent = "";
  try { await action(); } catch(e) { if(error) error.textContent = e.message; else toast(e.message); }
  finally { button.disabled = false; }
}
$("#login-form").addEventListener("submit", e => {
  e.preventDefault();
  busy(e.target, async () => enter(await api("/login","POST", Object.fromEntries(new FormData(e.target)))));
});
$("#logout").onclick = async () => {try {await api("/logout","POST",{});logoutView();}catch(e){toast(e.message);}};
$("#mobile-logout").onclick = $("#logout").onclick;
function page(name) {
  state.page = name;
  document.querySelectorAll(".page").forEach(el => el.hidden = el.id !== "page-"+name);
  document.querySelectorAll(".nav-item").forEach(el => el.classList.toggle("active", el.dataset.page === name));
  $("#breadcrumb").textContent = "工作空间 / " + ({tasks:"签到任务", accounts:"Telegram 帐号",logs:"执行记录",settings:"安全设置"}[name]);
  if(name === "logs") loadLogs().catch(e => toast(e.message));
}
document.querySelectorAll("[data-page]").forEach(el => el.onclick = () => page(el.dataset.page));
async function refresh() {
  if(!state.logged) return;
  try {
    const [overview, accounts, jobs] = await Promise.all([api("/overview"),api("/accounts"),api("/jobs")]);
    state.accounts = accounts; state.jobs = jobs; state.paused = overview.paused;
    $("#stat-accounts").textContent = overview.accounts; $("#stat-jobs").textContent = overview.jobs;
    $("#stat-runs").textContent = overview.runs24h; $("#stat-confirmed").textContent = overview.confirmed24h;
    $("#job-count").textContent = jobs.length; $("#nav-count").textContent = jobs.length;
    $("#schedule-label").textContent = state.paused ? "自动调度已暂停" : "自动调度运行中";
    $("#schedule-detail").textContent = state.paused ? "恢复后继续执行未来的计划；不会补发停用期间的任务。" : "按各任务的时区和计划自动执行。";
    $("#pause-all").textContent = state.paused ? "恢复调度" : "暂停全部";
    $("#global-error").hidden = true; $("#connection").innerHTML = '<span class="dot"></span> 已连接';
    renderJobs(); renderAccounts();
    if(state.page === "logs") await loadLogs();
  } catch(e) {
    $("#global-error").textContent = "同步失败：" + e.message; $("#global-error").hidden = false;
    $("#connection").textContent = "连接中断";
  }
}
function renderJobs() {
  const term = $("#search").value.toLowerCase();
  const jobs = state.jobs.filter(j => `${j.name} ${j.target} ${j.account_name}`.toLowerCase().includes(term));
  $("#jobs-body").innerHTML = jobs.map(j => `<tr>
    <td><strong>${esc(j.name)}</strong><small>${esc(j.target)}</small></td>
    <td>${esc(j.account_name)}</td>
    <td><code>${esc(j.cron)}</code><small>${esc(j.timezone)}${j.enabled ? " · 下次 "+date(j.next_run,j.timezone) : ""}</small></td>
    <td><button class="text-button" data-action="toggle-job" data-id="${j.id}" title="切换启用状态"><span class="badge ${j.enabled?"on":""}">${j.enabled?"● 已启用":"○ 已暂停"}</span></button></td>
    <td><div class="table-actions"><button class="text-button" data-action="run" data-id="${j.id}">▷ 执行</button><button class="text-button" data-action="edit" data-id="${j.id}">编辑</button><button class="text-button delete" data-action="delete-job" data-id="${j.id}">删除</button></div></td></tr>`).join("");
  $("#jobs-empty").hidden = state.jobs.length !== 0;
  if(!jobs.length && state.jobs.length) $("#jobs-body").innerHTML = '<tr><td colspan="5" class="muted">没有匹配的任务</td></tr>';
}
$("#search").oninput = renderJobs;
function renderAccounts() {
  $("#accounts-grid").innerHTML = state.accounts.length ? state.accounts.map(a => `<article class="account-card"><header><span class="avatar">${esc(a.name.slice(0,1).toUpperCase())}</span><span class="badge ${a.enabled?"on":""}">${a.cooldown>Date.now()/1000?"冷却中":a.enabled?"● 已启用":"已暂停"}</span></header><h3>${esc(a.name)}</h3><p>${esc(a.phone)}<br>帐号 ID ${a.uid}</p>${a.cooldown>Date.now()/1000?`<p>冷却结束：${date(a.cooldown)}（北京时间）</p>`:""}<footer><button class="text-button" data-action="toggle-account" data-id="${a.id}">${a.enabled?"暂停帐号":"启用帐号"}</button><button class="text-button delete" data-action="delete-account" data-id="${a.id}">移除帐号</button></footer></article>`).join("") : '<section class="panel empty"><span>◎</span><h3>还没有连接帐号</h3><p>点击「连接帐号」开始设置。</p></section>';
}
const labels = {sent:"已发送",confirmed:"已确认",unconfirmed:"未确认",failed:"失败",unknown:"结果未知",running:"执行中",skipped:"已跳过"};
async function loadLogs() {
  const logs = await api("/runs");
  $("#logs-body").innerHTML = logs.map(r => `<tr><td>${date(r.started)}<small>北京时间 · ${r.source === "manual"?"手动执行":"自动调度"}</small></td><td><strong>${esc(r.job_name)}</strong><small>${esc(r.account_name)}</small></td><td><span class="badge ${esc(r.status)}">${labels[r.status]||esc(r.status)}</span></td><td class="log-detail">${esc(r.detail||"正在等待 Telegram 响应…")}</td></tr>`).join("");
  $("#logs-empty").hidden = logs.length !== 0;
}
$("#refresh-logs").onclick = () => loadLogs().catch(e => toast(e.message));
$("#pause-all").onclick = async () => {try {await api("/pause","POST",{paused:!state.paused});await refresh();}catch(e){toast(e.message);}};
document.addEventListener("click", async e => {
  const button = e.target.closest("[data-action]");
  if(!button) return;
  const id = Number(button.dataset.id), action = button.dataset.action;
  button.disabled = true;
  try {
    const job = state.jobs.find(j=>j.id===id), account = state.accounts.find(a=>a.id===id);
    if(action === "edit") openJob(job);
    if(action === "run") {await api(`/jobs/${id}/run`,"POST",{});toast("任务已开始执行，请在执行记录查看结果");}
    if(action === "toggle-job") await api(`/jobs/${id}`,"PATCH",{enabled:!job.enabled});
    if(action === "toggle-account") await api(`/accounts/${id}`,"PATCH",{enabled:!account.enabled});
    if(action === "delete-job" && confirm(`删除任务「${job.name}」？执行记录会保留。`)) await api(`/jobs/${id}`,"DELETE");
    if(action === "delete-account" && confirm(`移除「${account.name}」及其所有任务？如需撤销 Telegram 会话，请到 Telegram 的设备设置中操作。`)) await api(`/accounts/${id}`,"DELETE");
    await refresh();
  } catch(error) {toast(error.message);} finally {button.disabled = false;}
});
document.querySelectorAll(".close-modal").forEach(b => b.onclick = () => b.closest("dialog").close());
function openJob(job) {
  if(!state.accounts.length) {toast("请先连接一个 Telegram 帐号");page("accounts");openAccount();return;}
  const form = $("#job-form"); form.reset();
  $("#job-account").innerHTML = state.accounts.map(a=>`<option value="${a.id}">${esc(a.name)}${a.enabled?"":"（已暂停）"}</option>`).join("");
  $("#job-title").textContent = job ? "编辑签到任务" : "新建签到任务";
  if(job) for(const [key,value] of Object.entries(job)) {
    const field = form.elements.namedItem(key);
    if(field) {if(field.type === "checkbox") field.checked = !!value; else field.value = value;}
  }
  form.elements.namedItem("id").value = job?.id || "";
  $("#dialog-options").hidden = true;
  form.querySelector(".modal-error").textContent = "";
  $("#job-dialog").showModal();
}
$("#new-job").onclick = () => openJob();
$("#empty-start").onclick = () => {page("accounts");openAccount();};
$("#job-form").onsubmit = e => {e.preventDefault();busy(e.target,async () => {
  const data = Object.fromEntries(new FormData(e.target)), id = data.id;
  delete data.id; data.account_id = Number(data.account_id);data.reply_timeout = Number(data.reply_timeout);data.enabled = e.target.elements.enabled.checked;
  await api(id ? `/jobs/${id}` : "/jobs",id ? "PUT" : "POST",data);
  $("#job-dialog").close();toast("任务已保存");await refresh();
});};
$("#job-account").onchange = () => {$("#dialog-options").hidden = true;$("#job-form").elements.target.value = "";};
$("#load-dialogs").onclick = async e => {
  const button = e.currentTarget;button.disabled = true;button.textContent = "加载中…";
  try {
    const dialogs = await api(`/accounts/${$("#job-account").value}/dialogs`);
    $("#dialog-options").innerHTML = '<option value="">选择对话（最多显示最近 150 个）</option>' + dialogs.map(d=>`<option value="${esc(d.id)}">${esc(d.name)} · ${esc(d.id)}</option>`).join("");
    $("#dialog-options").hidden = false;
  } catch(e) {$("#job-form .modal-error").textContent = e.message;} finally {button.disabled=false;button.textContent="选择对话";}
};
$("#dialog-options").onchange = e => {if(e.target.value) $("#job-form").elements.target.value = e.target.value;};
function setStage(stage) {
  state.stage = stage;
  for(const key of ["start","code","password"]) {
    const block = $("#account-step-"+key);block.hidden = stage !== key;
    block.querySelectorAll("input").forEach(i=>i.disabled = stage !== key);
  }
  $("#account-submit").textContent = ({start:"发送验证码",code:"验证并登录",password:"完成登录"})[stage];
}
function openAccount() {$("#account-form").reset();state.ticket="";setStage("start");$("#account-form .modal-error").textContent="";$("#account-dialog").showModal();}
$("#new-account").onclick = openAccount;
$("#account-dialog").addEventListener("close", () => {$("#account-form").reset();state.ticket="";});
$("#account-form").onsubmit = e => {e.preventDefault();busy(e.target, async () => {
  const data = Object.fromEntries(new FormData(e.target));
  if(state.stage === "start") {data.api_id = Number(data.api_id);const result = await api("/accounts/start","POST",data);state.ticket = result.ticket;setStage("code");}
  else {
    const result = await api("/accounts/finish","POST",{ticket:state.ticket,code:data.code||"",password:data.password||""});
    if(result.need_password) setStage("password");
    else {$("#account-dialog").close();toast("Telegram 帐号已连接");await refresh();}
  }
});};
$("#password-form").onsubmit = e => {e.preventDefault();busy(e.target,async () => {
  const data = Object.fromEntries(new FormData(e.target));
  if(data.new_password !== data.confirm) throw new Error("两次新密码不一致");
  delete data.confirm;await api("/password","POST",data);e.target.reset();logoutView();toast("密码已更新，请重新登录");
});};
$("#clock").textContent = new Date().toLocaleDateString("zh-CN",{timeZone:"Asia/Shanghai",month:"long",day:"numeric",weekday:"short"});
api("/me").then(user => {if(!state.logged) enter(user);}).catch(() => {if(!state.logged) logoutView();});
setInterval(() => {if(state.logged && !document.hidden) refresh();}, 10000);
