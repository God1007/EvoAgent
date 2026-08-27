const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const titles = {
  overview: "运行总览",
  review: "发起审查",
  tasks: "任务中心",
  skills: "Skill 注册中心",
  evolution: "演进实验室",
  github: "GitHub App",
};

const stateLabels = {
  PENDING: "等待中",
  PLANNING: "规划中",
  EXECUTING: "执行中",
  REVIEWING: "汇总中",
  RETRYING: "重试中",
  SUCCESS: "已完成",
  FAILED: "失败",
  CANCELLED: "已取消",
};

let selectedTask = null;
let taskRequest = 0;
let accessToken = localStorage.getItem("evoagent_token") || "";
let currentRole = localStorage.getItem("evoagent_role") || "";
let toastTimer = null;

function escapeHtml(value) {
  const node = document.createElement("div");
  node.textContent = value ?? "";
  return node.innerHTML;
}

function formatTime(value) {
  if (!value) return "时间未知";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? String(value)
    : new Intl.DateTimeFormat("zh-CN", {
        month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
      }).format(date);
}

function formatJson(value) {
  return JSON.stringify(value, null, 2);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
  const response = await fetch(path, { ...options, headers });
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("json") ? await response.json() : await response.text();

  if (response.status === 401) {
    $("#login-overlay").classList.remove("hidden");
    $("#logout").classList.add("hidden");
  }
  if (!response.ok) {
    const message = typeof data === "object" ? data.error || data.detail : data;
    throw new Error(message || response.statusText || "请求失败");
  }
  return data;
}

function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove("show"), 2600);
}

function setButtonBusy(button, busy, busyText) {
  if (!button) return;
  if (busy) {
    button.dataset.label = button.innerHTML;
    button.disabled = true;
    button.textContent = busyText;
  } else {
    button.disabled = false;
    if (button.dataset.label) button.innerHTML = button.dataset.label;
  }
}

function applyRole() {
  const platform = currentRole === "platform_admin";
  $('.nav-item[data-view="evolution"]').classList.toggle("hidden", !platform);
  if (!platform && location.hash.slice(1) === "evolution") show("overview");
}

function show(view, updateHash = true) {
  if (!titles[view]) view = "overview";
  $$(".view").forEach((element) => element.classList.remove("active"));
  $$(".nav-item").forEach((element) => {
    const active = element.dataset.view === view;
    element.classList.toggle("active", active);
    element.setAttribute("aria-current", active ? "page" : "false");
  });
  $(`#view-${view}`).classList.add("active");
  $("#page-title").textContent = titles[view];
  document.title = `${titles[view]} · EvoAgent`;
  if (updateHash) history.replaceState(null, "", `#${view}`);

  if (view === "tasks") loadTasks();
  if (view === "skills") loadSkills();
  if (view === "evolution") loadFailures();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

$$(".nav-item").forEach((button) => button.addEventListener("click", () => show(button.dataset.view)));
$$("[data-jump]").forEach((button) => button.addEventListener("click", () => show(button.dataset.jump)));
window.addEventListener("hashchange", () => show(location.hash.slice(1), false));

function taskRows(tasks) {
  if (!tasks?.length) {
    return '<div class="empty-state"><span><b>还没有审查任务</b>提交一个 Diff 开始首次审查</span></div>';
  }
  return tasks.map((task) => {
    const state = task.retrying ? "RETRYING" : String(task.state || "PENDING").toUpperCase();
    const repository = escapeHtml(task.repository || "未命名仓库");
    const pr = task.pull_request ? `PR #${escapeHtml(task.pull_request)}` : "手动审查";
    return `
      <button class="task-row" data-task="${escapeHtml(task.id)}" type="button" aria-pressed="${task.id === selectedTask}">
        <span class="task-main">
          <span class="task-glyph">PR</span>
          <span class="task-copy">
            <span class="task-name">${repository}</span>
            <span class="task-meta"><span>${pr}</span><span>${escapeHtml(formatTime(task.created_at))}</span></span>
          </span>
        </span>
        <span class="status state-${state.toLowerCase()}">${stateLabels[state] || escapeHtml(state)}</span>
      </button>`;
  }).join("");
}

function bindTasks(root) {
  $$("[data-task]", root).forEach((row) => row.addEventListener("click", () => openTask(row.dataset.task)));
}

function statCard(label, value, note, style) {
  return `<article class="stat ${style}">
    <span class="stat-label">${label}</span>
    <b>${value}</b><small>${note}</small>
  </article>`;
}

async function loadDashboard() {
  try {
    const data = await api("/api/dashboard");
    $("#system-status").textContent = `${data.queue} · ${data.orchestrator}`;
    const stats = data.stats || {};
    const rate = Math.round(Number(stats.success_rate || 0) * 100);
    $("#stats").innerHTML = [
      statCard("总任务", stats.tasks_total ?? 0, "累计审查任务", ""),
      statCard("已完成", stats.tasks_success ?? 0, "通过质量门禁", "success"),
      statCard("失败", stats.tasks_failed ?? 0, "需要进一步处理", "failed"),
      statCard("成功率", `${rate}%`, "已结束审查成功率", "rate"),
      statCard("待处理案例", stats.unresolved_failure_cases ?? 0, "未解决反馈", "feedback"),
      statCard("活跃 Skills", stats.active_skill_versions ?? 0, "当前生效版本", "skills"),
    ].join("");
    $("#recent-tasks").innerHTML = taskRows((data.tasks || []).slice(0, 5));
    bindTasks($("#recent-tasks"));
  } catch (error) {
    $("#system-status").textContent = "服务连接异常";
    $("#stats").innerHTML = '<div class="empty-state"><span><b>暂时无法读取数据</b>请检查服务状态后重试</span></div>';
    $("#recent-tasks").innerHTML = '<div class="empty-state"><span>数据加载失败</span></div>';
    toast(error.message);
  }
}

async function loadTasks() {
  const root = $("#all-tasks");
  root.innerHTML = '<div class="list-loading"></div><div class="list-loading"></div>';
  try {
    const data = await api("/api/tasks");
    root.innerHTML = taskRows(data.tasks || []);
    bindTasks(root);
    return true;
  } catch (error) {
    root.innerHTML = '<div class="empty-state"><span>任务加载失败</span></div>';
    toast(error.message);
    return false;
  }
}

function workflowMarkup(data) {
  if (data.availability === "pruned") {
    return `<p class="workflow-note">交接记录已按保留策略清理，无法还原节点状态。清理时间：${escapeHtml(formatTime(data.artifacts_pruned_at))}</p>`;
  }
  if (data.availability === "not_recorded") {
    return '<p class="workflow-note">尚无交接记录：任务可能未开始执行，或来自未记录流程快照的旧版本。</p>';
  }
  if (data.availability !== "recorded" || !data.workflow || !Array.isArray(data.steps)) {
    throw new Error("交接状态格式异常");
  }
  const states = {
    pending: ["未派发", "status-neutral"],
    running: ["已派发", "state-executing"],
    completed: ["已完成", "state-success"],
    failed: ["失败", "state-failed"],
  };
  const completed = data.steps.filter((step) => step.status === "completed").length;
  const metadata = (items) => items.map(([label, value]) =>
    `<dt>${label}</dt><dd><code>${escapeHtml(value ?? "尚未记录")}</code></dd>`).join("");
  return `
    <div class="workflow-heading"><strong>${escapeHtml(data.workflow.name)}</strong>
      <span class="status status-neutral">任务：${escapeHtml(stateLabels[data.task_state] || data.task_state)}</span></div>
    <p class="workflow-note">${completed} / ${data.steps.length} 个节点完成 · 持久化快照，点击顶部刷新获取最新记录。</p>
    <p class="workflow-note">“已派发”仅表示最后一次派发，不代表 Worker 在线。任务取消或失败后，也可能保留此状态。</p>
    <ul class="handoff-list">${data.steps.map((step) => {
      const [label, style] = states[step.status] || ["未知状态", "status-neutral"];
      return `<li class="handoff-step">
        <div class="handoff-heading"><div><strong>${escapeHtml(step.id)}</strong><code>${escapeHtml(step.agent_id)}</code></div>
          <span class="status ${style}">${label}</span></div>
        <p class="workflow-note">尝试 ${escapeHtml(step.attempt)} 次 · ${step.updated_at ? `更新于 ${escapeHtml(formatTime(step.updated_at))}` : "尚未执行"}</p>
        ${step.blocked_by?.length ? `<p class="handoff-blocked">等待上游：${escapeHtml(step.blocked_by.join("、"))}</p>` : ""}
        ${step.error ? `<p class="handoff-error">${escapeHtml(step.error)}</p>` : ""}
        <details><summary>交接契约与标识</summary><dl class="handoff-metadata">
          ${Object.entries(step.sources).map(([port, source]) => `<dt>输入 <code>${escapeHtml(port)}</code></dt>
            <dd><code>${escapeHtml(source)}</code><small>${escapeHtml(step.inputs[port])}</small></dd>`).join("")}
          ${Object.entries(step.outputs).map(([port, type]) => `<dt>输出 <code>${escapeHtml(port)}</code></dt><dd><code>${escapeHtml(type)}</code></dd>`).join("")}
          ${metadata([["Agent 版本", step.agent_revision], ["执行代次", step.generation], ["幂等键", step.idempotency_key], ["输入摘要", step.input_sha256], ["输出摘要", step.output_sha256]])}
        </dl></details>
      </li>`;
    }).join("")}</ul>
    <details class="workflow-version"><summary>流程版本</summary><dl class="handoff-metadata">
      ${metadata([["交接协议", data.workflow.protocol_version], ["流程版本", data.workflow.revision], ["执行版本", data.workflow.execution_revision]])}
    </dl></details>`;
}

function resetTask(id = null) {
  selectedTask = id;
  taskRequest += 1;
  $("#task-selection").textContent = id ? `任务 ${id}` : "选择任务查看交接记录与报告";
  $("#task-report").textContent = id ? "正在加载任务报告…" : "请选择一个任务。";
  $("#task-workflow").textContent = id ? "正在加载交接状态…" : "请选择一个任务。";
  $("#task-workflow").setAttribute("aria-busy", String(Boolean(id)));
  $("#create-fix").classList.add("hidden");
  $$("[data-task]").forEach((row) => row.setAttribute("aria-pressed", String(row.dataset.task === id)));
}

async function openTask(id) {
  if (location.hash !== "#tasks") show("tasks");
  resetTask(id);
  const request = taskRequest;
  const path = `/v1/tasks/${encodeURIComponent(id)}`;
  // Each request owns both panes; an older response must never change a new selection.
  const results = await Promise.all([
    api(path).then((task) => {
      if (request !== taskRequest) return false;
      $("#task-report").textContent = formatJson(task);
      $("#create-fix").classList.toggle("hidden", !(task.report && task.pull_request && task.input?.head_sha));
      return true;
    }).catch((error) => {
      if (request === taskRequest) $("#task-report").textContent = error.message;
      return false;
    }),
    api(`${path}/workflow`).then((data) => {
      if (request !== taskRequest) return false;
      $("#task-workflow").innerHTML = workflowMarkup(data);
      return true;
    }).catch((error) => {
      if (request === taskRequest) $("#task-workflow").textContent = `交接状态加载失败：${error.message}。可点击顶部刷新重试，任务报告仍可独立查看。`;
      return false;
    }).finally(() => {
      if (request === taskRequest) $("#task-workflow").setAttribute("aria-busy", "false");
    }),
  ]);
  return results.every(Boolean);
}

async function loadSkills() {
  const root = $("#skill-list");
  root.innerHTML = '<div class="skill-card loading"></div><div class="skill-card loading"></div>';
  try {
    const data = await api("/api/skills");
    $("#skill-revision").textContent = data.reviewer_revision || "未知";
    const skills = data.skills || [];
    root.innerHTML = skills.length ? skills.map((skill) => `
      <article class="skill-card">
        <span class="skill-label">${skill.sandboxed ? "SANDBOXED SKILL" : "ACTIVE SKILL"}</span>
        <h3>${escapeHtml(skill.name)}</h3>
        <p>${escapeHtml(skill.description || "暂无能力描述")}</p>
        <span class="skill-meta"><i></i>v${escapeHtml(skill.version)} · ${escapeHtml(skill.source)}</span>
      </article>`).join("") : '<div class="empty-state"><span><b>尚未加载 Skill</b>部署 Skill 后重启服务以加载能力</span></div>';
  } catch (error) {
    $("#skill-revision").textContent = "读取失败";
    root.innerHTML = '<div class="empty-state"><span>Skills 加载失败</span></div>';
    toast(error.message);
  }
}

async function loadFailures() {
  try {
    const [failuresData, status, runsData] = await Promise.all([
      api("/api/failures"),
      api("/v1/evolution/status"),
      api("/v1/evolution/runs?limit=5"),
    ]);
    $("#evolution-status").textContent = formatJson(status);
    const cases = failuresData.cases || [];
    const runs = runsData.runs || [];
    const failureHtml = cases.length
      ? cases.slice(0, 8).map((item) => `
          <div class="task-row">
            <span class="task-main"><span class="task-glyph">FC</span><span class="task-copy">
              <span class="task-name">${escapeHtml(item.category)}</span>
              <span class="task-meta">${escapeHtml(item.task_id)}</span>
            </span></span>
            <span class="status ${item.resolved ? "state-success" : "state-pending"}">${item.resolved ? "已解决" : "待处理"}</span>
          </div>`).join("")
      : '<div class="empty-state"><span><b>暂无失败反馈</b>系统当前没有未处理案例</span></div>';
    const historyHtml = runs.length
      ? `<p class="history-heading">最近评测</p>${runs.map((run) => `
          <div class="task-row">
            <span class="task-main"><span class="task-glyph">V${escapeHtml(run.candidate_version)}</span><span class="task-copy">
              <span class="task-name">${escapeHtml(run.decision)}</span>
              <span class="task-meta">${Number(run.candidate_score).toFixed(3)} vs ${Number(run.baseline_score).toFixed(3)}</span>
            </span></span>
          </div>`).join("")}`
      : "";
    $("#failure-list").innerHTML = failureHtml + historyHtml;
  } catch (error) {
    $("#evolution-status").textContent = "暂时无法读取评测状态。";
    $("#failure-list").innerHTML = '<div class="empty-state"><span>反馈加载失败</span></div>';
    toast(error.message);
  }
}

$("#review-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $('button[type="submit"]', form);
  const values = new FormData(form);
  const body = { repository: values.get("repository"), diff: values.get("diff") };
  if (values.get("pull_request")) body.pull_request = Number(values.get("pull_request"));
  const asyncQuery = values.get("async") ? "?async=true" : "";
  const output = $("#review-result");
  output.classList.remove("empty");
  output.textContent = "正在提交审查任务…";
  setButtonBusy(button, true, "正在提交…");
  try {
    const data = await api(`/v1/reviews${asyncQuery}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    output.textContent = formatJson(data);
    toast("审查任务已成功提交");
    loadDashboard();
  } catch (error) {
    output.textContent = error.message;
  } finally {
    setButtonBusy(button, false);
  }
});

$("#create-fix").addEventListener("click", async () => {
  const button = $("#create-fix");
  if (!selectedTask || button.disabled || button.classList.contains("hidden")) return;
  const id = selectedTask;
  const request = taskRequest;
  setButtonBusy(button, true, "正在创建…");
  try {
    const data = await api(`/v1/tasks/${encodeURIComponent(id)}/fix`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (request === taskRequest) {
      $("#task-report").textContent = formatJson(data);
      toast("修复分支已创建");
    }
  } catch (error) {
    if (request === taskRequest) toast(error.message);
  } finally {
    setButtonBusy(button, false);
  }
});

$("#install-github").addEventListener("click", async () => {
  const button = $("#install-github");
  setButtonBusy(button, true, "正在连接…");
  try {
    const data = await api("/v1/github/installations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    window.location.assign(data.url);
  } catch (error) {
    toast(error.message);
    setButtonBusy(button, false);
  }
});

$("#evolution-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $('button[type="submit"]', form);
  const values = new FormData(form);
  setButtonBusy(button, true, "正在评测…");
  try {
    const data = await api("/v1/evolution/propose", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ skill_name: values.get("skill_name"), prompt: values.get("prompt") }),
    });
    $("#evolution-result").classList.remove("empty");
    $("#evolution-result").textContent = formatJson(data);
    toast("新旧版本回放评测已完成");
    loadFailures();
  } catch (error) {
    toast(error.message);
  } finally {
    setButtonBusy(button, false);
  }
});

$("#auto-evolve").addEventListener("click", async () => {
  const button = $("#auto-evolve");
  setButtonBusy(button, true, "正在生成…");
  try {
    const data = await api("/v1/evolution/auto", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ skill_name: "llm-review" }),
    });
    $("#evolution-result").classList.remove("empty");
    $("#evolution-result").textContent = formatJson(data);
    toast("反馈候选评测已完成");
    loadFailures();
  } catch (error) {
    toast(error.message);
  } finally {
    setButtonBusy(button, false);
  }
});

$("#refresh").addEventListener("click", async () => {
  const view = location.hash.slice(1) || "overview";
  if (view === "overview") await loadDashboard();
  else if (view === "tasks") {
    const results = await Promise.all([loadTasks(), selectedTask ? openTask(selectedTask) : true]);
    toast(results.every(Boolean) ? "数据已刷新" : "部分数据未能刷新，请查看提示");
    return;
  } else if (view === "skills") await loadSkills();
  else if (view === "evolution") await loadFailures();
  else await loadDashboard();
  toast("数据已刷新");
});

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $('button[type="submit"]', form);
  const values = new FormData(form);
  setButtonBusy(button, true, "正在登录…");
  try {
    const data = await api("/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: values.get("username"),
        password: values.get("password"),
        tenant_id: values.get("tenant_id"),
      }),
    });
    accessToken = data.access_token;
    currentRole = data.role;
    localStorage.setItem("evoagent_token", accessToken);
    localStorage.setItem("evoagent_role", currentRole);
    resetTask();
    applyRole();
    $("#login-overlay").classList.add("hidden");
    $("#logout").classList.remove("hidden");
    $("#login-error").textContent = "";
    await loadDashboard();
    if (location.hash === "#tasks") await loadTasks();
  } catch (error) {
    $("#login-error").textContent = error.message;
  } finally {
    setButtonBusy(button, false);
  }
});

$("#logout").addEventListener("click", () => {
  accessToken = "";
  currentRole = "";
  localStorage.removeItem("evoagent_token");
  localStorage.removeItem("evoagent_role");
  resetTask();
  applyRole();
  $("#login-overlay").classList.remove("hidden");
  $("#logout").classList.add("hidden");
});

applyRole();
if (accessToken) $("#logout").classList.remove("hidden");
show(location.hash.slice(1) || "overview", false);
loadDashboard();
