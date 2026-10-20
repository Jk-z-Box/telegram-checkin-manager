"use strict";
const $ = (s) => document.querySelector(s);
const state = {csrf: "", page: "tasks", accounts: [], jobs: [], paused: false, ticket: "", stage: "start", logged: false};
const expandedGroups = new Set(), groupResults = new Map();
let dialogLoadVersion = 0;
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
  expandedGroups.clear(); groupResults.clear();
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
function jobEntries() {
  const entries = [], groups = new Map();
  for(const job of state.jobs) {
    if(job.group_id == null) {entries.push(job);continue;}
    if(!groups.has(job.group_id)) {
      const group = {...job, id:job.group_id, members:[]};
      groups.set(job.group_id, group);entries.push(group);
    }
    groups.get(job.group_id).members.push(job);
  }
  for(const group of groups.values()) {
    group.enabled = group.members.every(j=>j.enabled);
    group.partial = !group.enabled && group.members.some(j=>j.enabled);
    group.next_run = Math.min(...group.members.filter(j=>j.enabled).map(j=>j.next_run));
  }
  return entries;
}
function renderJobs() {
  const term = $("#search").value.toLowerCase();
  const entries = jobEntries();
  $("#job-count").textContent = entries.length; $("#nav-count").textContent = entries.length;
  const jobs = entries.filter(j => `${j.name} ${j.target} ${j.members?.map(m=>m.account_name).join(" ") || j.account_name}`.toLowerCase().includes(term));
  $("#jobs-body").innerHTML = jobs.map(j => j.members ? `<tr>
    <td><strong>${esc(j.name)} <span class="badge on">集合</span></strong><small>${esc(j.target)}</small></td>
    <td><button class="text-button group-expand" data-action="expand-group" data-id="${j.id}" aria-expanded="${expandedGroups.has(j.id)}" aria-controls="group-members-${j.id}">${expandedGroups.has(j.id)?"▾":"▸"} ${j.members.length} 个帐号</button></td>
    <td><code>${esc(j.cron)}</code><small>${esc(j.timezone)}${j.enabled||j.partial ? " · 下次 "+date(j.next_run,j.timezone) : ""}</small></td>
    <td><button class="text-button" data-action="toggle-group" data-id="${j.id}" title="统一切换集合状态"><span class="badge ${j.enabled?"on":""}">${j.enabled?"● 已启用":j.partial?"◐ 部分启用":"○ 已暂停"}</span></button></td>
    <td><div class="table-actions"><button class="text-button" data-action="run-group" data-id="${j.id}">▷ 执行集合</button><button class="text-button" data-action="edit-group" data-id="${j.id}">编辑</button><button class="text-button delete" data-action="delete-group" data-id="${j.id}">删除</button></div></td>
    </tr><tr id="group-members-${j.id}" class="group-members" ${expandedGroups.has(j.id)?"":"hidden"}><td colspan="5"><div class="member-heading">集合成员 <span>各帐号的执行结果可在「执行记录」查看</span></div>${j.members.map(m=>{
      const account = state.accounts.find(a=>a.id===m.account_id);
      const result = groupResults.get(j.id)?.find(r=>r.job_id===m.id);
      return `<div class="group-member"><b>${esc(m.account_name)}</b><span class="badge ${m.enabled&&account?.enabled?"on":""}">${!account?.enabled?"帐号已暂停":m.enabled?"已启用":"任务已暂停"}</span><small>${result ? esc(result.status==="started"?"本次已开始执行":result.detail) : esc(account?.cooldown>Date.now()/1000?"帐号限流冷却中":m.enabled?"下次 "+date(m.next_run,m.timezone):"等待启用")}</small></div>`;
    }).join("")}</td></tr>` : `<tr>
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
  if(action === "expand-group") {
    expandedGroups.has(id) ? expandedGroups.delete(id) : expandedGroups.add(id);
    renderJobs();return;
  }
  button.disabled = true;
  try {
    const job = state.jobs.find(j=>j.id===id), account = state.accounts.find(a=>a.id===id);
    const group = jobEntries().find(j=>j.members && j.id===id);
    if(action === "edit-group") openJob({...group, id:"", group_id:id, account_ids:group.members.map(j=>j.account_id)});
    if(action === "toggle-group") await api(`/job-groups/${id}`,"PATCH",{enabled:!group.enabled});
    if(action === "run-group") {
      const result = await api(`/job-groups/${id}/run`,"POST",{});
      groupResults.set(id,result.results);expandedGroups.add(id);
      toast(`集合：已开始 ${result.started} 个，未启动 ${result.results.length-result.started} 个；展开成员查看说明`);
    }
    if(action === "delete-group" && confirm(`删除集合「${group.name}」及其 ${group.members.length} 个帐号的任务？执行记录会保留。`)) {
      await api(`/job-groups/${id}`,"DELETE");expandedGroups.delete(id);groupResults.delete(id);
    }
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
  dialogLoadVersion++;
  $("#job-account").disabled = false;$("#load-dialogs").disabled = false;$("#load-dialogs").textContent = "选择对话";
  const singleEdit = !!job?.id && !job?.members;
  $("#job-account-picker").hidden = singleEdit;
  $("#batch-target-help").hidden = singleEdit;
  $("#job-account-label").textContent = singleEdit ? "Telegram 帐号" : "从此帐号读取对话";
  const selected = job?.account_ids || [state.accounts[0].id];
  $("#job-accounts").innerHTML = state.accounts.map(a=>`<label class="account-choice"><input type="checkbox" value="${a.id}" ${selected.includes(a.id)?"checked":""}><span><b>${esc(a.name)}</b><small>${esc(a.phone)}${a.enabled?"":" · 已暂停"}</small></span></label>`).join("");
  $("#job-account").innerHTML = state.accounts.map(a=>`<option value="${a.id}">${esc(a.name)}${a.enabled?"":"（已暂停）"}</option>`).join("");
  $("#job-title").textContent = job ? "编辑签到任务" : "新建签到任务";
  if(job) for(const [key,value] of Object.entries(job)) {
    const field = form.elements.namedItem(key);
    if(field) {if(field.type === "checkbox") field.checked = !!value; else field.value = value;}
  }
  form.elements.namedItem("id").value = job?.id || "";
  form.elements.namedItem("group_id").value = job?.group_id || "";
  if(!singleEdit) syncJobAccounts();
  else $("#job-submit").textContent = "保存任务";
  syncRetryOptions();
  $("#dialog-options").hidden = true;
  form.querySelector(".modal-error").textContent = "";
  $("#job-dialog").showModal();
}
$("#new-job").onclick = () => openJob();
$("#empty-start").onclick = () => {page("accounts");openAccount();};
$("#job-form").onsubmit = e => {e.preventDefault();busy(e.target,async () => {
  const data = Object.fromEntries(new FormData(e.target)), id = data.id, groupId = data.group_id;
  delete data.id;delete data.group_id;
  data.reply_timeout = Number(data.reply_timeout);data.enabled = e.target.elements.enabled.checked;
  data.retry_enabled = e.target.elements.retry_enabled.checked;
  data.send_timeout = Number(e.target.elements.send_timeout.value);
  data.max_retries = Number(e.target.elements.max_retries.value);
  let message = "任务已保存";
  if(id) {data.account_id = Number(data.account_id);await api(`/jobs/${id}`,"PUT",data);}
  else {
    const accountIds = selectedJobAccounts();
    if(!accountIds.length) throw new Error("请至少选择一个 Telegram 帐号");
    if(accountIds.length>100) throw new Error("每个集合最多选择 100 个帐号");
    delete data.account_id;
    if(groupId || accountIds.length>1) {
      data.account_ids = accountIds;
      const result = await api(groupId ? `/job-groups/${groupId}` : "/jobs/batch",groupId?"PUT":"POST",data);
      groupResults.delete(result.group_id);
      message = `任务集合已保存 · ${result.count} 个帐号`;
    } else {
      data.account_id = accountIds[0];await api("/jobs","POST",data);
    }
  }
  $("#job-dialog").close();toast(message);await refresh();
});};
function selectedJobAccounts() {
  return [...document.querySelectorAll("#job-accounts input:checked")].map(input=>Number(input.value));
}
function syncRetryOptions() {
  const enabled = $("#retry-enabled").checked;
  $("#retry-options").hidden = !enabled;
  $("#retry-options").querySelectorAll("input").forEach(input=>input.disabled=!enabled);
}
$("#retry-enabled").onchange = syncRetryOptions;
function syncJobAccounts() {
  const selected = selectedJobAccounts(), source = $("#job-account").value;
  $("#selected-account-count").textContent = `已选 ${selected.length} 个`;
  $("#job-account").innerHTML = state.accounts.filter(a=>selected.includes(a.id)).map(a=>`<option value="${a.id}">${esc(a.name)}</option>`).join("");
  if(selected.includes(Number(source))) $("#job-account").value = source;
  $("#job-account").disabled = !selected.length;$("#load-dialogs").disabled = !selected.length;$("#load-dialogs").textContent = "选择对话";
  const editing = !!$("#job-form").elements.group_id.value, group = editing || selected.length>1;
  $("#job-title").textContent = editing ? "编辑任务集合" : group ? "新建任务集合" : "新建签到任务";
  $("#job-submit").textContent = editing ? "保存集合" : group ? `创建集合 · ${selected.length} 个帐号` : "创建任务";
  $("#job-selection-help").textContent = editing ? "统一修改集合配置；增减勾选帐号可调整成员，已有成员的历史记录保留。" : "选择多个帐号后，任务列表只显示一个集合。单次最多 100 个帐号。";
  dialogLoadVersion++;$("#dialog-options").hidden = true;
}
$("#job-accounts").onchange = syncJobAccounts;
$("#select-all-accounts").onclick = () => {
  if(state.accounts.length>100) {toast("每个集合最多 100 个帐号，请手动选择");return;}
  document.querySelectorAll("#job-accounts input").forEach(input=>input.checked=true);syncJobAccounts();
};
$("#clear-accounts").onclick = () => {document.querySelectorAll("#job-accounts input").forEach(input=>input.checked=false);syncJobAccounts();};
$("#job-account").onchange = () => {
  dialogLoadVersion++;$("#dialog-options").hidden = true;
  $("#load-dialogs").disabled = false;$("#load-dialogs").textContent = "选择对话";
  if($("#job-form").elements.id.value) $("#job-form").elements.target.value = "";
};
$("#job-dialog").addEventListener("close",()=>{dialogLoadVersion++;});
$("#load-dialogs").onclick = async e => {
  const version = ++dialogLoadVersion;
  const button = e.currentTarget;button.disabled = true;button.textContent = "加载中…";
  try {
    const dialogs = await api(`/accounts/${$("#job-account").value}/dialogs`);
    if(version !== dialogLoadVersion) return;
    $("#dialog-options").innerHTML = '<option value="">选择对话（最多显示最近 150 个）</option>' + dialogs.map(d=>`<option value="${esc(d.id)}">${esc(d.name)} · ${esc(d.id)}</option>`).join("");
    $("#dialog-options").hidden = false;
  } catch(e) {if(version === dialogLoadVersion) $("#job-form .modal-error").textContent = e.message;}
  finally {if(version === dialogLoadVersion) {button.disabled=!$("#job-account").value;button.textContent="选择对话";}}
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
