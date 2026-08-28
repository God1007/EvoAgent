const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const titles = {
  overview: "运行总览",
  review: "发起审查",
  tasks: "任务中心",
  studio: "Agent 搭建",
  skills: "基础能力",
  evolution: "演进实验室",
  github: "GitHub App",
};

const stateLabels = {
  PENDING: "等待中",
  PLANNING: "规划中",
  EXECUTING: "执行中",
  REVIEWING: "汇总中",
  RETRYING: "重试中",
  CANCELLING: "取消中",
  SUCCESS: "已完成",
  FAILED: "失败",
  CANCELLED: "已取消",
};

let selectedTask = null;
let taskRequest = 0;
let accessToken = localStorage.getItem("evoagent_token") || "";
let currentRole = localStorage.getItem("evoagent_role") || "";
let toastTimer = null;
let authEpoch = 0;
let observedToken = accessToken, loginRequired = false;
const reviewReceipts = { manual: "evoagent_pending_review", studio: "evoagent_pending_trial" };
let reviewWorkflows = [], reviewWorkflowCursor = null, reviewWorkflowLoading = false;
let consoleCapabilities = null, evolutionReady = false;
let dashboardRequest = 0, evolutionRequest = 0;

function reviewWorkflowSelection(value) {
  if (!value) return null;
  const match = /^([0-9a-f]{32}):([1-9][0-9]*)$/.exec(value);
  if (!match || Number(match[2]) >= 2 ** 31) throw new Error("请选择有效的已发布流程版本。");
  return { id: match[1], version: Number(match[2]) };
}

function renderReviewWorkflows(selected = $("#review-workflow").value || "") {
  const picker = $("#review-workflow");
  picker.innerHTML = '<option value="">沿用仓库配置（未绑定时使用默认流程）</option>' + reviewWorkflows.map((item) => `<option value="${item.id}:${item.version}">${escapeHtml(item.name)} · v${item.version}</option>`).join("");
  picker.value = selected;
  $("#review-more-workflows").textContent = reviewWorkflowCursor ? "加载更多流程" : "刷新流程列表";
}

async function loadReviewWorkflows(more = false) {
  if (reviewWorkflowLoading) return;
  const session = authEpoch;
  reviewWorkflowLoading = true;
  $("#review-more-workflows").disabled = true;
  try {
    const data = await api(`/v1/studio/workflows${more && reviewWorkflowCursor ? `?cursor=${encodeURIComponent(reviewWorkflowCursor)}` : ""}`);
    if (session !== authEpoch) return;
    const items = await Promise.all((data.documents || []).filter((item) => item.active_version).map(async (item) => {
      const selection = reviewWorkflowSelection(`${item.id}:${item.active_version}`);
      const published = await api(`/v1/studio/workflows/${selection.id}/versions/${selection.version}`);
      return { ...selection, name: published.name || "已发布流程" };
    }));
    if (session !== authEpoch) return;
    // Retain a deliberately selected older version across catalog refreshes.
    const selected = $("#review-workflow").value || "";
    const previous = more ? reviewWorkflows : reviewWorkflows.filter((item) => `${item.id}:${item.version}` === selected);
    reviewWorkflows = [...new Map([...previous, ...items].map((item) => [`${item.id}:${item.version}`, item])).values()];
    reviewWorkflowCursor = data.next_cursor;
    renderReviewWorkflows(selected);
    $("#review-workflow-note").textContent = reviewWorkflows.length ? "已发布版本可直接使用；不会改变仓库绑定。草稿需到 Agent 搭建中试运行。" : "这一页暂无已发布流程，可加载更多或去搭建一个；也可以沿用仓库配置。";
    return true;
  } catch (error) {
    if (session === authEpoch) $("#review-workflow-note").textContent = `流程列表未更新：${error.message}。当前选择保持不变。`;
    return false;
  } finally {
    if (session === authEpoch) { reviewWorkflowLoading = false; $("#review-more-workflows").disabled = false; }
  }
}

function openReviewWorkflow(item) {
  const value = `${item.id}:${item.version}`;
  reviewWorkflowSelection(value);
  reviewWorkflows = [...reviewWorkflows.filter((entry) => `${entry.id}:${entry.version}` !== value), item];
  renderReviewWorkflows(value);
  show("review");
}

function taskState(task) {
  return task.cancel_requested && task.state !== "CANCELLED" ? "CANCELLING" : task.retrying ? "RETRYING" : String(task.state || "PENDING").toUpperCase();
}

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

const severityLabels = { critical: "严重", high: "高风险", medium: "中风险", low: "低风险" };
const agentLabels = { plan: "变更规划", planner: "变更规划", specialists: "专职审查", critic: "交叉质询", test: "复现检查", synthesize: "综合结论", synthesizer: "综合结论", fix: "修复检查", verify: "最终验证", verifier: "最终验证" };
const portLabels = { diff: "代码变更", parsed: "变更范围", findings: "发现的问题", specialist_findings: "审查发现", verified: "审查结论", plan: "审查计划", critiques: "质询结果", reproductions: "复现结果", synthesized: "综合结论", fix_ready: "修复可行性", business: "业务检查结果", security: "安全检查结果" };
const severityBadge = (value) => `<span class="status severity-${severityLabels[value] ? value : "unknown"}">${severityLabels[value] || "待评估"}</span>`;

function findingMarkup(finding, index, compact = false) {
  return `<article class="finding-card">
    <div class="finding-heading">${severityBadge(finding.severity)}<span class="finding-number">问题 ${index + 1}</span></div>
    <h4>${escapeHtml(finding.title || "待确认的问题")}</h4>
    <p class="finding-location"><code>${escapeHtml(finding.path || "位置未提供")}</code>${Number.isInteger(finding.line) ? ` · 第 ${finding.line} 行` : ""}</p>
    ${finding.explanation ? `<p class="finding-explanation">${escapeHtml(finding.explanation)}</p>` : ""}
    ${finding.evidence ? `<pre class="evidence-code"><code>${escapeHtml(finding.evidence)}</code></pre>` : ""}
    <details class="finding-advice" ${!compact && index === 0 ? "open" : ""}><summary>修复与验证建议</summary>
      <div><h5>如何修改</h5><p>${escapeHtml(finding.fix || "暂无自动修复建议，请人工确认。")}</p></div>
      <div><h5>如何验证</h5><p>${escapeHtml(finding.test || "请为此变更新增回归测试。")}</p></div>
    </details>
  </article>`;
}

function reportMarkup(task) {
  const report = task.report;
  const context = `<div class="report-context"><strong>${escapeHtml(task.repository || report?.repository || "审查任务")}</strong><span>${task.pull_request ? `PR #${escapeHtml(task.pull_request)}` : "手动审查"}</span><span>${escapeHtml(formatTime(task.created_at))}</span></div>`;
  if (!report || !Array.isArray(report.findings)) {
    const title = task.cancel_requested && task.state !== "CANCELLED" ? "正在取消审查" : task.retrying ? "正在重试审查" : ({ FAILED: "本次审查未完成", CANCELLED: "本次审查已取消", SUCCESS: "报告暂不可用" })[task.state] || "审查正在进行";
    const note = task.cancel_requested && task.state !== "CANCELLED" ? "取消请求已记录，正在执行的调用可能尚未结束。请刷新确认最终状态。" : task.retrying ? "保留原任务输入与流程版本，复用已完成节点；请刷新查看进度。" : ({ FAILED: "尚无完整报告。有操作权限时可从失败节点续跑；原始输入已清理或执行版本变化时需新建任务。", CANCELLED: "没有生成最终结论。如需继续，请重新提交审查。", SUCCESS: "请刷新重试；历史产物也可能已按保留策略清理。" })[task.state] || "结果生成后会出现在这里，可点击顶部刷新查看进度。";
    return `${context}<div class="report-empty"><h4>${title}</h4><p>${note}</p></div>`;
  }
  const order = { critical: 0, high: 1, medium: 2, low: 3 };
  const findings = [...report.findings].sort((a, b) => (order[a.severity] ?? 4) - (order[b.severity] ?? 4));
  const urgent = findings.filter((item) => ["critical", "high"].includes(item.severity)).length;
  const files = Array.isArray(report.files_reviewed) ? report.files_reviewed.length : null;
  return `${context}${task.delivery_pending ? '<p class="handoff-blocked">审查结果已生成，但回写尚未确认完成。可刷新状态，或重试结果回写；不会重新审查代码。</p>' : ""}<div class="report-overview ${urgent ? "needs-attention" : ""}">
    <span class="report-eyebrow">${task.state === "SUCCESS" ? "审查已完成" : "已保存的审查结果"}</span>
    <div class="report-title"><h4>${findings.length ? `发现 ${findings.length} 个待关注问题` : "本次未发现问题"}</h4>${severityBadge(report.risk)}</div>
    <p>${urgent ? `其中 ${urgent} 个为高风险或严重问题，建议优先确认。` : findings.length ? "请结合业务场景确认以下建议。" : "仅代表本次变更在当前规则和流程下未检出问题，不等同于安全保证。"}</p>
    <div class="report-counts">${files === null ? "" : `<span>审查范围 <b>${files}</b> 个文件</span>`}${Object.entries(severityLabels).map(([key, label]) => `<span>${label} <b>${findings.filter((item) => item.severity === key).length}</b></span>`).join("")}</div>
  </div><div class="finding-list">${findings.map((item, index) => findingMarkup(item, index)).join("")}</div>`;
}

function artifactValueMarkup(value, type) {
  if (type === "review-findings@1" && Array.isArray(value)) {
    return value.length ? `<p class="workflow-note">共 ${value.length} 个问题</p>${value.map((item, index) => findingMarkup(item, index, true)).join("")}` : '<p class="artifact-empty">未发现问题</p>';
  }
  if (type === "unified-diff@1" && typeof value === "string") {
    const lines = value.split("\n"), visible = lines.slice(0, 120);
    return `<pre class="diff-output"><code>${visible.map((line) => `<span class="${line.startsWith("+") ? "diff-added" : line.startsWith("-") ? "diff-removed" : ""}">${escapeHtml(line) || " "}</span>`).join("")}</code></pre>${lines.length > visible.length ? '<p class="workflow-note">仅展示前 120 行；完整变更请在仓库中查看。</p>' : ""}`;
  }
  if (type === "parsed-diff@1" && value && Array.isArray(value.files)) {
    return `<p>涉及 ${value.files.length} 个文件，${value.added_line_count ?? 0} 行新增代码。</p><ul class="artifact-files">${value.files.slice(0, 30).map((path) => `<li><code>${escapeHtml(path)}</code></li>`).join("")}</ul>`;
  }
  if (type === "review-plan@1" && value) {
    return `<p>审查 ${Array.isArray(value.changed_files) ? value.changed_files.length : 0} 个文件，安排 ${value.assignment_count ?? 0} 项检查。</p>${Array.isArray(value.languages) ? `<p class="workflow-note">语言：${escapeHtml(value.languages.join("、"))}</p>` : ""}`;
  }
  if (["review-critiques@1", "review-reproductions@1", "review-fix-decisions@1"].includes(type) && value && typeof value === "object") {
    return `<p>检查 ${value.checked} 项，${value.accepted} 项通过，${value.checked - value.accepted} 项未通过。</p>`;
  }
  if (type === "boolean@1" && typeof value === "boolean") return `<p>${value ? "是" : "否"}</p>`;
  if ((type === "text@1" && typeof value === "string") || (type === "integer@1" && Number.isInteger(value))) return `<p class="artifact-text">${escapeHtml(value)}</p>`;
  return '<p class="workflow-note">已接收结构化产物，当前界面尚不支持此类型的可读展示。</p>';
}

function workflowStepNames(data, task) {
  return Object.fromEntries(data.steps.map((step, index) => [step.id, task?.workflow?.steps?.[step.id] || agentLabels[step.id] || `处理步骤 ${index + 1}`]));
}

function workflowSourceName(ref, names) {
  const [node, port] = ref.split(".");
  return `${node === "$input" ? "原始变更" : names[node] || "上游步骤"} · ${portLabels[port] || port}`;
}

function artifactMarkup(artifact, step, names = {}) {
  const section = (title, values, types, sources = {}) => `<section class="artifact-section"><h5>${title}</h5>${Object.entries(types || {}).map(([port, type]) => {
    const source = sources[port], label = portLabels[port] || port;
    return `<div class="artifact-port"><strong>${escapeHtml(source ? `来自 ${workflowSourceName(source, names)}` : label)}</strong>${source ? `<p class="workflow-note">接收为 ${escapeHtml(label)}</p>` : ""}${values && Object.hasOwn(values, port) ? artifactValueMarkup(values[port], type) : '<p class="workflow-note">尚未生成或已按保留策略清理。</p>'}</div>`;
  }).join("")}</section>`;
  return section("收到的内容", artifact.inputs, step.inputs, step.sources) + section("交付的结果", artifact.status === "completed" ? artifact.outputs : null, step.outputs);
}

const decisionLabels = { approved: "评测通过，等待发布审批", rejected: "未通过评测", deferred: "暂不发布" };
function evaluationMarkup(data) {
  const score = (value) => typeof value === "number" && Number.isFinite(value) ? `${(value * 100).toFixed(1)} 分` : "暂无评分";
  return `<strong>${decisionLabels[data.decision] || "评测已记录"}</strong><p>候选版本 ${escapeHtml(data.version?.version ?? "待定")} · 尚未自动切换生产流程</p><div class="result-comparison"><span>候选评分 <b>${score(data.candidate?.score)}</b></span><span>基线评分 <b>${score(data.baseline?.score)}</b></span></div>`;
}

const consoleErrors = {
  invalid_request: "输入未通过检查，请确认必填内容、字段格式和所选配置。",
  authentication_required: "登录已失效或尚未登录，请重新登录后继续。",
  access_denied: "当前账号权限或仓库策略不允许此操作，请联系管理员确认。",
  not_found: "内容不存在或当前账号无权访问，请刷新列表后重新选择。",
  state_conflict: "当前状态不支持此操作，请刷新确认；未保存的编辑请先自行保留。",
  rate_limited: "请求过于频繁，请稍候再试。",
  unavailable: "服务暂时不可用，请稍候查看任务状态，再决定是否重试。",
  internal_error: "服务未能完成请求，请先查看任务状态；持续失败时请联系管理员。",
  invalid_repository: "请按 owner/repository 格式填写仓库名称。",
  diff_required: "请先粘贴需要审查的代码变更。",
  invalid_pull_request: "PR 编号必须是正整数；手动审查可留空。",
  submission_conflict: "本次提交与待确认记录不一致，请先在任务中心核对已提交的任务。",
  login_failed: "登录未成功，请检查账号、密码和所属租户。",
  authentication_disabled: "当前服务未启用登录认证，无需登录。",
  unsupported_view: "当前服务不支持此页面操作，请联系管理员确认前后端版本一致。",
  draft_conflict: "草稿已发生变化，请先保留未保存的编辑，再重新打开最新草稿。",
  binding_conflict: "仓库配置已变化，请重新读取当前配置后再切换。",
  unsupported_draft: "草稿结构暂不受编辑器支持，原始内容未修改。请联系管理员处理。",
  invalid_version: "所选发布版本未通过完整性检查，请联系管理员；不要继续使用该版本。",
  model_unavailable: "所选模型当前不可用，请联系管理员配置，或选择可用模型重新发布。",
  task_cancelled: "任务已取消，不能继续运行；如需审查，请重新提交。",
  payload_unavailable: "原始任务输入已不可用，无法续跑，请重新提交审查。",
  delivery_unavailable: "当前任务无法重试结果回写，请刷新确认状态。",
  fix_not_allowed: "仓库尚未允许自动修复，请联系管理员确认修复策略。",
  review_outdated: "PR 的代码已变化，请先重新审查，再生成修复。",
  pr_closed: "PR 已关闭，无法创建修复，请确认仓库中的 PR 状态。",
  pr_draft: "PR 仍为草稿，请先在仓库中将其标记为可审查。",
  invalid_identifier: "步骤和端口名称需以字母或数字开头，只能包含字母、数字、下划线和连字符，最多 64 个字符。",
  duplicate_step: "步骤名称重复，请为每个步骤设置不同名称。",
  unknown_handoff_type: "交接类型不受支持，请从列表中重新选择。",
  invalid_rule_ports: "规则 Agent 需要代码变更输入，并输出问题列表。",
  invalid_merge_ports: "汇总 Agent 只能接收问题列表，并输出汇总后的问题列表。",
  invalid_checks: "请至少选择一条已安装规则或填写文字检查；自定义检查最多 32 条。",
  invalid_tools: "请从列表中选择不重复的只读工具。",
  diff_input_required: "此 Agent 需要代码变更输入，请添加并连接 diff 端口。",
  invalid_output_limit: "模型输出上限必须是 1～4096 的整数。",
  invalid_model_output: "模型不能生成原始代码变更或变更范围，请改用文本或问题列表输出。",
  invalid_prompt: "请填写模型指令，长度为 1～16000 个字符。",
  invalid_name: "请填写名称，长度为 1～100 个字符。",
  model_required: "请选择模型；自定义模型名称最多 200 个字符。",
  workflow_steps: "完整流程需要 1～64 个步骤，请检查步骤数量。",
  invalid_workflow: "流程连线不完整或不兼容，请检查未连接端口、交接类型，以及是否形成循环。",
  report_output_required: "请为最终审查结论选择问题列表类型的输出。",
  disconnected_step: "存在未连接到最终结果的步骤，请补齐连线或移除该步骤。",
  review_capacity: "当前团队的审查任务已满，请等待已有任务完成后再提交。",
};

function consoleErrorMessage(status, data, path) {
  const code = data?.error_code;
  if (typeof code === "string" && Object.hasOwn(consoleErrors, code)) return consoleErrors[code];
  const fallback = { 400: "invalid_request", 401: path === "/v1/auth/login" ? "login_failed" : "authentication_required", 403: "access_denied", 404: "not_found", 409: "state_conflict", 429: "rate_limited", 503: "unavailable" };
  return consoleErrors[fallback[status] || "internal_error"];
}

async function api(path, options = {}) {
  const session = authEpoch;
  checkStoredSession();
  if (session !== authEpoch || (loginRequired && path !== "/v1/auth/login")) throw new Error("请先重新登录控制台。");
  const headers = { ...(options.headers || {}), "X-EvoAgent-View": "console" };
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
  let response, data;
  try {
    response = await fetch(path, { ...options, headers });
    if ((response.headers.get("content-type") || "").includes("json")) data = await response.json();
  } catch { /* Never display network/JSON parser errors containing response fragments. */ }
  checkStoredSession();
  if (session !== authEpoch) throw new Error("登录状态已变化，请重新操作。");
  if (!response) throw new Error("暂时无法连接服务，请检查网络；已提交的操作请先到任务中心确认。");
  if (response.status === 401 && path !== "/v1/auth/login") endSession(accessToken ? "登录已失效，请重新登录。" : "请登录后继续。", true);
  if (!response.ok) {
    const error = new Error(consoleErrorMessage(response.status, data, path));
    error.status = response.status;
    throw error;
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("服务返回了无法读取的结果，请刷新确认操作状态；未保存的编辑请先自行保留。");
  return data;
}

async function submitReview(body, scope) {
  const session = authEpoch, token = accessToken, slot = reviewReceipts[scope];
  if (!slot) throw new Error("未知的审查提交入口。");
  if (!window.crypto?.subtle || !window.crypto.randomUUID) throw new Error("安全提交需要 HTTPS 或本机 localhost 地址。");
  const encoded = JSON.stringify(body);
  const digest = await window.crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify([token, encoded])));
  if (session !== authEpoch || token !== accessToken) throw new Error("登录状态已变化，请重新操作。");
  const fingerprint = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
  let receipt;
  try {
    receipt = JSON.parse(sessionStorage.getItem(slot) || "null");
    if (receipt !== null && (typeof receipt !== "object" || Array.isArray(receipt) || Object.keys(receipt).length !== 2 || !/^[a-f0-9]{64}$/.test(receipt.fingerprint) || !/^[a-f0-9-]{36}$/.test(receipt.key))) throw new Error("invalid receipt");
    if (!receipt || receipt.fingerprint !== fingerprint) {
      receipt = { fingerprint, key: window.crypto.randomUUID() };
      sessionStorage.setItem(slot, JSON.stringify(receipt));
    }
  } catch {
    throw new Error("无法保存待确认提交记录，本次请求尚未发出。请先在任务中心核对，或恢复浏览器会话存储后再试。");
  }
  const uncertain = "暂时无法确认任务是否已接收。保持当前账号和输入不变再试，可找回同一次提交；也可先查看任务中心。";
  let data;
  try {
    data = await api("/v1/reviews?async=true", {
      method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": receipt.key },
      body: encoded, signal: AbortSignal.timeout(30_000),
    });
    if (!data || typeof data.task_id !== "string" || !data.task_id || !Object.hasOwn(stateLabels, data.state)) throw new Error(uncertain);
  } catch (error) {
    if (session !== authEpoch || token !== accessToken) throw new Error("登录状态已变化，请重新操作。");
    if (!error.status || error.status >= 500) throw new Error(uncertain);
    throw error;
  }
  if (session !== authEpoch || token !== accessToken) throw new Error("登录状态已变化，请重新操作。");
  try {
    if (sessionStorage.getItem(slot) === JSON.stringify(receipt)) sessionStorage.removeItem(slot);
  } catch { /* Retaining a receipt is safe: a later retry finds the original task. */ }
  return data;
}

function resetReviewSubmission() {
  authEpoch++;
  reviewWorkflows = []; reviewWorkflowCursor = null; reviewWorkflowLoading = false;
  renderReviewWorkflows("");
  $("#review-more-workflows").disabled = false;
  $("#review-workflow-note").textContent = "登录后读取已发布流程。";
  for (const slot of Object.values(reviewReceipts)) {
    try { sessionStorage.removeItem(slot); } catch { /* Identity also participates in the digest. */ }
  }
  const form = $("#review-form");
  form.reset();
  setButtonBusy($('button[type="submit"]', form), false);
  $("#review-result").classList.add("empty");
  $("#review-result").textContent = "审查结果将在这里显示";
}

function resetConsole() {
  consoleCapabilities = null; evolutionReady = false;
  resetReviewSubmission();
  resetTask();
  window.studio?.reset();
  for (const selector of ["#stats", "#recent-tasks", "#all-tasks", "#skill-list", "#evolution-status", "#failure-list"]) $(selector).textContent = "登录后读取数据。";
  for (const selector of ["#evolution-form", "#login-form"]) {
    const form = $(selector);
    form.reset();
    setButtonBusy($('button[type="submit"]', form), false);
  }
  for (const selector of ["#create-fix", "#install-github", "#auto-evolve"]) setButtonBusy($(selector), false);
  $("#evolution-result").classList.add("empty");
  $("#evolution-result").textContent = "演进结果将在这里显示";
  $("#system-status").textContent = "等待登录确认";
  clearTimeout(toastTimer);
  $("#toast").textContent = "";
  $("#toast").classList.remove("show");
  applyCapabilities();
}

function endSession(message = "", forget = false) {
  if (forget) {
    try {
      // A late response must not delete a different account's shared credentials.
      if ((localStorage.getItem("evoagent_token") || "") === accessToken) {
        localStorage.removeItem("evoagent_token");
        localStorage.removeItem("evoagent_role");
      }
      observedToken = localStorage.getItem("evoagent_token") || "";
    } catch { /* Clear the in-memory session even when browser storage is unavailable. */ }
  }
  accessToken = ""; currentRole = ""; loginRequired = true;
  resetConsole();
  applyRole();
  $(".app-shell").inert = true;
  $("#login-overlay").classList.remove("hidden");
  $("#logout").classList.add("hidden");
  $("#login-error").textContent = message;
  $('input[name="username"]', $("#login-form")).focus();
}

function checkStoredSession() {
  try {
    const stored = localStorage.getItem("evoagent_token") || "";
    if (stored !== observedToken) {
      observedToken = stored;
      endSession("登录状态已在其他页面变化，请重新登录。");
    }
  } catch { endSession("无法读取浏览器登录状态，请检查会话存储权限。"); }
}

window.addEventListener("storage", (event) => {
  if (event.storageArea === localStorage && (event.key === null || event.key === "evoagent_token")) checkStoredSession();
});
window.addEventListener("focus", checkStoredSession);
window.addEventListener("pageshow", checkStoredSession);

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
    if (button.dataset.busy !== "true") button.dataset.label = button.innerHTML;
    button.dataset.busy = "true";
    button.disabled = true;
    button.textContent = busyText;
  } else {
    delete button.dataset.busy;
    button.disabled = button.dataset.available === "false";
    if (button.dataset.label) button.innerHTML = button.dataset.label;
    delete button.dataset.label;
  }
}

function setButtonAvailable(button, available) {
  button.dataset.available = String(Boolean(available));
  button.disabled = !available || button.dataset.busy === "true";
}

function applyCapabilities() {
  const caps = consoleCapabilities;
  setButtonAvailable($('#review-form button[type="submit"]'), caps?.review === true);
  $("#review-access-note").textContent = !caps ? "正在确认操作权限；若一直未更新，请点击顶部刷新。" : caps.review ? "可提交 Diff；后台执行后可在任务中心查看结果。" : "当前账号仅可查看报告，发起审查需要维护者或管理员权限。";
  setButtonAvailable($("#install-github"), caps?.manage === true && caps.github_install_configured === true);
  $("#github-status").textContent = !caps ? "尚未确认接入状态，请刷新后重试。" : !caps.manage ? "安装接入需要管理员权限。" : !caps.github_install_configured ? "尚未完成 GitHub App 接入配置。管理员需配置登录认证、App 凭据、OAuth 回调和 Webhook 密钥；当前仍可手动提交 Diff。" : "接入配置已具备，点击后需在 GitHub 授权；Webhook 是否可达、仓库权限和真实 PR 审查仍需接入后验证。";
  for (const button of [$('#evolution-form button[type="submit"]'), $("#auto-evolve")]) setButtonAvailable(button, caps?.platform === true && evolutionReady);
  if (caps?.role) currentRole = caps.role;
  applyRole();
}

function applyRole() {
  const platform = consoleCapabilities?.platform === true;
  $('.nav-item[data-view="evolution"]').classList.toggle("hidden", !platform);
  if (consoleCapabilities && !platform && location.hash.slice(1) === "evolution") show("overview");
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
  if (view === "review") loadReviewWorkflows();
  if (view === "studio") window.studio?.load();
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
    const state = taskState(task);
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
  const session = authEpoch;
  const request = ++dashboardRequest;
  try {
    const data = await api("/api/dashboard");
    if (session !== authEpoch || request !== dashboardRequest) return false;
    consoleCapabilities = data.capabilities || null;
    applyCapabilities();
    $("#system-status").textContent = "服务已连接";
    const stats = data.stats || {};
    const rate = Math.round(Number(stats.success_rate || 0) * 100);
    $("#stats").innerHTML = [
      statCard("总任务", stats.tasks_total ?? 0, "累计审查任务", ""),
      statCard("已完成", stats.tasks_success ?? 0, "已生成审查报告", "success"),
      statCard("失败", stats.tasks_failed ?? 0, "需要进一步处理", "failed"),
      statCard("成功率", `${rate}%`, "已结束审查成功率", "rate"),
      statCard("待处理案例", stats.unresolved_failure_cases ?? 0, "未解决反馈", "feedback"),
      statCard("活跃 Skills", stats.active_skill_versions ?? 0, "当前生效版本", "skills"),
    ].join("");
    $("#recent-tasks").innerHTML = taskRows((data.tasks || []).slice(0, 5));
    bindTasks($("#recent-tasks"));
    return true;
  } catch (error) {
    if (session !== authEpoch || request !== dashboardRequest) return false;
    consoleCapabilities = null;
    applyCapabilities();
    $("#system-status").textContent = "服务连接异常";
    $("#stats").innerHTML = '<div class="empty-state"><span><b>暂时无法读取数据</b>请检查服务状态后重试</span></div>';
    $("#recent-tasks").innerHTML = '<div class="empty-state"><span>数据加载失败</span></div>';
    toast(error.message);
    return false;
  }
}

async function loadTasks() {
  const session = authEpoch;
  const root = $("#all-tasks");
  root.innerHTML = '<div class="list-loading"></div><div class="list-loading"></div>';
  try {
    const data = await api("/api/tasks");
    if (session !== authEpoch) return false;
    root.innerHTML = taskRows(data.tasks || []);
    bindTasks(root);
    return true;
  } catch (error) {
    if (session !== authEpoch) return false;
    root.innerHTML = '<div class="empty-state"><span>任务加载失败</span></div>';
    toast(error.message);
    return false;
  }
}

function workflowMarkup(data, task = null) {
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
  const pinned = task?.workflow;
  const names = workflowStepNames(data, task);
  const name = pinned?.name || (data.workflow.name === "studio" ? "自定义审查流程" : "审查流程");
  return `
    <div class="workflow-heading"><strong>${escapeHtml(name)}</strong>
      <span class="status status-neutral">任务：${escapeHtml(stateLabels[taskState({ ...task, state: task?.state || data.task_state })] || data.task_state)}</span></div>
    <p class="workflow-note">${completed} / ${data.steps.length} 个步骤完成${pinned ? ` · ${pinned.version ? `发布版本 v${pinned.version}` : `草稿试运行 r${pinned.draft_revision}`}` : ""} · 点击顶部刷新进度</p>
    <progress class="workflow-progress" value="${completed}" max="${data.steps.length || 1}" aria-label="流程完成进度"></progress>
    ${data.steps.some((step) => step.status === "running") ? '<p class="workflow-note">“已派发”是最近保存的状态，不代表 Worker 在线；取消或中断后也可能保留。</p>' : ""}
    <ul class="handoff-list">${data.steps.map((step) => {
      const [label, style] = states[step.status] || ["未知状态", "status-neutral"];
      return `<li class="handoff-step">
        <div class="handoff-heading"><div><strong>${escapeHtml(names[step.id])}</strong></div>
          <span class="status ${style}">${label}</span></div>
        <p class="workflow-note">${step.attempt > 1 ? `尝试 ${escapeHtml(step.attempt)} 次 · ` : ""}${step.updated_at ? `更新于 ${escapeHtml(formatTime(step.updated_at))}` : "尚未执行"}</p>
        ${step.blocked_by?.length ? `<p class="handoff-blocked">等待上游：${escapeHtml(step.blocked_by.map((id) => names[id] || "上游步骤").join("、"))}</p>` : ""}
        ${step.error ? '<p class="handoff-error">此步骤未能完成，可重试或请管理员检查。</p>' : ""}
        <p class="handoff-source">接收自 ${Object.values(step.sources).map((source) => escapeHtml(workflowSourceName(source, names))).join("；") || "流程输入"}</p>
        ${step.attempt ? `<details class="artifact-detail" data-artifact="${escapeHtml(step.id)}"><summary>查看处理内容与结果</summary><div class="artifact-body">展开后读取本步骤处理的内容。</div></details>` : ""}
      </li>`;
    }).join("")}</ul>`;
}

function resetTask(id = null) {
  $$(".studio-confirm [data-cancel]").forEach((button) => button.click());
  selectedTask = id;
  taskRequest += 1;
  $("#task-selection").textContent = id ? "正在读取所选审查…" : "选择任务查看审查结论与处理过程";
  $("#task-report").textContent = id ? "正在加载任务报告…" : "请选择一个任务。";
  $("#fix-result").textContent = "";
  $("#fix-note").textContent = "";
  $("#task-workflow").textContent = id ? "正在加载交接状态…" : "请选择一个任务。";
  $("#task-workflow").setAttribute("aria-busy", String(Boolean(id)));
  $("#create-fix").classList.add("hidden");
  setButtonAvailable($("#create-fix"), false);
  setButtonBusy($("#create-fix"), false);
  $("#task-controls").classList.add("hidden");
  $("#task-control-result").textContent = "";
  for (const action of ["resume", "cancel"]) {
    const button = $(`#${action}-task`);
    setButtonBusy(button, false);
    button.classList.add("hidden");
  }
  $$("[data-task]").forEach((row) => row.setAttribute("aria-pressed", String(row.dataset.task === id)));
}

async function openTask(id) {
  if (location.hash !== "#tasks") show("tasks");
  resetTask(id);
  const request = taskRequest;
  const path = `/v1/tasks/${encodeURIComponent(id)}`;
  let taskData = null, workflowData = null;
  const renderWorkflow = () => {
    if (request !== taskRequest || !workflowData) return;
    $("#task-workflow").innerHTML = workflowMarkup(workflowData, taskData);
    $$("[data-artifact]", $("#task-workflow")).forEach((detail) => detail.addEventListener("toggle", async () => {
      if (!detail.open || detail.dataset.loaded) return;
      detail.dataset.loaded = "loading";
      try {
        const artifact = await api(`${path}/workflow/${encodeURIComponent(detail.dataset.artifact)}`);
        const step = workflowData.steps.find((item) => item.id === detail.dataset.artifact);
        if (request === taskRequest) $(".artifact-body", detail).innerHTML = artifactMarkup(artifact, step, workflowStepNames(workflowData, taskData));
        detail.dataset.loaded = "true";
      } catch (error) {
        if (request === taskRequest) $(".artifact-body", detail).textContent = "暂时无法读取，内容可能已被清理。可收起后重新展开重试。";
        delete detail.dataset.loaded;
      }
    }));
  };
  // Each request owns both panes; an older response must never change a new selection.
  const results = await Promise.all([
    api(path).then((task) => {
      if (request !== taskRequest) return false;
      taskData = task;
      $("#task-selection").textContent = `${task.repository || "审查任务"} · ${task.pull_request ? `PR #${task.pull_request}` : "手动审查"}`;
      $("#task-report").innerHTML = reportMarkup(task);
      const mayReview = !accessToken || ["admin", "platform_admin", "maintainer"].includes(currentRole);
      $("#task-controls").classList.toggle("hidden", !mayReview || !(task.can_cancel || task.can_resume));
      $("#cancel-task").classList.toggle("hidden", !mayReview || !task.can_cancel);
      $("#resume-task").classList.toggle("hidden", !mayReview || !task.can_resume);
      $("#resume-task").dataset.delivery = String(Boolean(task.delivery_pending));
      $("#resume-task").textContent = task.delivery_pending ? "重试结果回写" : "从失败节点续跑";
      renderWorkflow();
      $("#create-fix").classList.toggle("hidden", !task.report);
      setButtonAvailable($("#create-fix"), task.can_fix === true);
      $("#fix-note").textContent = task.report ? task.can_fix ? "将尝试确定性修复与隔离测试；通过后才会写入独立 GitHub 分支，不会自动合并。" : ({ permission: "当前账号没有修复权限。", pr_snapshot: "这份报告没有可核验的 GitHub PR 快照，不能创建修复分支。手动填写 PR 编号不会自动获取快照。", policy: "仓库尚未允许自动修复，请管理员确认仓库策略。", rules: "仓库选择的修复规则不可用，请管理员更新策略。", installation: "GitHub 安装尚未绑定到当前租户。", github: "尚未配置此任务所需的 GitHub 凭据。", sandbox: "尚未配置隔离修复环境，不能安全执行仓库代码。", tests: "尚未配置修复回归测试命令。" })[task.fix_blocker] || "尚未确认修复条件，请刷新后重试。" : "";
      return true;
    }).catch((error) => {
      if (request === taskRequest) $("#task-report").textContent = error.message;
      return false;
    }),
    api(`${path}/workflow`).then((data) => {
      if (request !== taskRequest) return false;
      workflowData = data; renderWorkflow();
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

function confirmConsoleAction(text, isCurrent = () => true) {
  const session = authEpoch;
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "studio-dialog studio-confirm";
    dialog.setAttribute("aria-label", "确认操作");
    dialog.innerHTML = `<p>${escapeHtml(text)}</p><div class="studio-actions"><button type="button" class="button secondary" data-cancel>取消</button><button type="button" class="button" data-confirm>确认继续</button></div>`;
    const finish = (accepted) => { dialog.close(); dialog.remove(); resolve(accepted && session === authEpoch && isCurrent()); };
    $("[data-cancel]", dialog).addEventListener("click", () => finish(false));
    $("[data-confirm]", dialog).addEventListener("click", () => finish(true));
    dialog.addEventListener("cancel", (event) => { event.preventDefault(); finish(false); });
    document.body.appendChild(dialog); dialog.showModal();
  });
}

async function controlTask(action) {
  const button = $(`#${action}-task`);
  if (!selectedTask || button.disabled || button.classList.contains("hidden")) return;
  const id = selectedTask, request = taskRequest, session = authEpoch;
  const current = () => request === taskRequest && session === authEpoch;
  const delivery = action === "resume" && button.dataset.delivery === "true";
  const prompt = action === "cancel" ? "取消所选任务？取消后不能续跑，也不会撤销已完成的外部操作。" : delivery ? "重试所选任务的结果回写？只处理已有报告，不会重新审查代码。" : "按原始输入和固定流程版本续跑？已完成节点不重跑。输入已清理、契约错误或执行版本变化时需新建任务。";
  for (const name of ["resume", "cancel"]) setButtonBusy($(`#${name}-task`), true, "等待确认…");
  try {
    if (!await confirmConsoleAction(`${$("#task-selection").textContent}\n\n${prompt}`, current) || !current()) return;
    $("#task-control-result").textContent = "正在提交操作…";
    const result = await api(`/v1/tasks/${encodeURIComponent(id)}/${action}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}", signal: AbortSignal.timeout(30000),
    });
    if (!current()) return;
    let message;
    if (action === "cancel" && result.cancel_requested) message = "取消请求已记录，请以当前任务状态为准。";
    else if (action === "resume" && result.delivery_resumed) message = "已请求重试结果回写，不会重新审查代码。";
    else if (action === "resume" && result.delivery_already_active) message = "结果回写已在处理中，未重复派发。";
    else if (action === "resume" && result.resumed) message = "已请求按原版本续跑，复用已完成节点。";
    else if (action === "resume" && result.already_active) message = "任务已在执行或重试中，未重复派发。";
    else if (action === "resume" && (result.delivery_complete || result.state === "SUCCESS")) message = "任务已完成，无需再次重试。";
    else throw new Error("unconfirmed operation");
    const refreshed = openTask(id), updatedRequest = taskRequest;
    const results = await Promise.all([refreshed, loadTasks()]);
    if (session === authEpoch && updatedRequest === taskRequest) $("#task-control-result").textContent = message + (results.every(Boolean) ? "" : " 最新状态未完整读取，请手动刷新核对。");
  } catch (error) {
    if (current()) $("#task-control-result").textContent = ({
      403: "当前账号没有操作权限，请联系管理员。",
      404: "任务不存在或不可见，请刷新任务列表。",
      409: "任务状态或恢复条件已变化，请刷新核对。原始输入被清理或版本不兼容时需新建任务。",
      429: "当前执行容量已满，请稍后再试。",
    })[error.status] || "暂时无法确认操作是否已被接收，请先刷新任务状态再决定是否重试。";
  } finally {
    if (current()) for (const name of ["resume", "cancel"]) setButtonBusy($(`#${name}-task`), false);
  }
}

for (const action of ["resume", "cancel"]) $(`#${action}-task`).addEventListener("click", () => controlTask(action));

async function loadSkills() {
  const session = authEpoch;
  const root = $("#skill-list");
  root.innerHTML = '<div class="skill-card loading"></div><div class="skill-card loading"></div>';
  try {
    const data = await api("/api/skills");
    if (session !== authEpoch) return false;
    const skills = data.skills || [];
    root.innerHTML = skills.length ? skills.map((skill) => `
      <article class="skill-card">
        <span class="skill-label">${skill.sandboxed ? "SANDBOXED SKILL" : "ACTIVE SKILL"}</span>
        <h3>${escapeHtml(skill.name)}</h3>
        <p>${escapeHtml(skill.description || "暂无能力描述")}</p>
        <span class="skill-meta"><i></i>v${escapeHtml(skill.version)} · ${skill.sandboxed ? "隔离执行" : "内置能力"}</span>
      </article>`).join("") : '<div class="empty-state"><span><b>尚未加载 Skill</b>部署 Skill 后重启服务以加载能力</span></div>';
    return true;
  } catch (error) {
    if (session !== authEpoch) return false;
    root.innerHTML = '<div class="empty-state"><span>Skills 加载失败</span></div>';
    toast(error.message);
    return false;
  }
}

async function loadFailures() {
  const session = authEpoch;
  const request = ++evolutionRequest;
  evolutionReady = false;
  applyCapabilities();
  try {
    const [failuresData, status, runsData] = await Promise.all([
      api("/api/failures"),
      api("/v1/evolution/status"),
      api("/v1/evolution/runs?limit=5"),
    ]);
    if (session !== authEpoch || request !== evolutionRequest) return false;
    evolutionReady = status.ready === true;
    applyCapabilities();
    $("#evolution-status").innerHTML = `<strong>${status.ready ? "可以开始评测" : "还需完成以下准备"}</strong><ul class="readiness-list"><li>${status.model_configured ? "✓ 模型已配置" : "○ 尚未配置模型"}</li><li>验证样本：${Number(status.validation_cases || 0)} / 至少 ${Number(status.minimum_cases || 0)}</li><li>独立保留样本：${Number(status.holdout_cases || 0)} / 至少 ${Number(status.minimum_holdout_cases || 0)}</li></ul>`;
    const cases = failuresData.cases || [];
    const runs = runsData.runs || [];
    const failureHtml = cases.length
      ? cases.slice(0, 8).map((item) => `
          <div class="task-row">
            <span class="task-main"><span class="task-glyph">FC</span><span class="task-copy">
              <span class="task-name">${escapeHtml(item.category)}</span>
              <span class="task-meta">${escapeHtml(formatTime(item.created_at))}</span>
            </span></span>
            <span class="status ${item.resolved ? "state-success" : "state-pending"}">${item.resolved ? "已解决" : "待处理"}</span>
          </div>`).join("")
      : '<div class="empty-state"><span><b>暂无失败反馈</b>系统当前没有未处理案例</span></div>';
    const historyHtml = runs.length
      ? `<p class="history-heading">最近评测</p>${runs.map((run) => `
          <div class="task-row">
            <span class="task-main"><span class="task-glyph">V${escapeHtml(run.candidate_version)}</span><span class="task-copy">
              <span class="task-name">${decisionLabels[run.decision] || "评测已记录"}</span>
              <span class="task-meta">${Number(run.candidate_score).toFixed(3)} vs ${Number(run.baseline_score).toFixed(3)}</span>
            </span></span>
          </div>`).join("")}`
      : "";
    $("#failure-list").innerHTML = failureHtml + historyHtml;
    return true;
  } catch (error) {
    if (session !== authEpoch || request !== evolutionRequest) return false;
    $("#evolution-status").textContent = "暂时无法读取评测状态。";
    $("#failure-list").innerHTML = '<div class="empty-state"><span>反馈加载失败</span></div>';
    toast(error.message);
    return false;
  }
}

$("#review-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $('button[type="submit"]', form);
  if (button.disabled) return;
  const session = authEpoch;
  const values = new FormData(form);
  const body = { repository: values.get("repository"), diff: values.get("diff") };
  if (values.get("pull_request")) body.pull_request = Number(values.get("pull_request"));
  const output = $("#review-result");
  output.classList.remove("empty");
  output.textContent = "正在提交审查任务…";
  setButtonBusy(button, true, "正在提交…");
  try {
    const workflow = reviewWorkflowSelection(values.get("workflow"));
    if (workflow) body.workflow = workflow;
    const data = await submitReview(body, "manual");
    if (session !== authEpoch) return;
    output.innerHTML = `<strong>${data.replayed ? "已找回原任务，未重复创建" : data.state === "SUCCESS" ? "审查已完成" : "审查任务已提交"}</strong><p>${escapeHtml(body.repository)} · ${data.state === "SUCCESS" ? "报告已生成，可查看问题与修复建议。" : "可进入任务中心查看当前进度与结果。"}</p><button type="button" class="button secondary" id="open-review-result">查看审查结果</button>`;
    $("#open-review-result").addEventListener("click", () => openTask(data.task_id));
    toast(data.replayed ? "已找回同一次提交的任务" : "审查任务已成功提交");
    loadDashboard();
  } catch (error) {
    if (session === authEpoch) output.textContent = error.message;
  } finally {
    if (session === authEpoch) setButtonBusy(button, false);
  }
});

$("#review-more-workflows").addEventListener("click", () => loadReviewWorkflows(Boolean(reviewWorkflowCursor)));

$("#create-fix").addEventListener("click", async () => {
  const button = $("#create-fix");
  if (!selectedTask || button.disabled || button.classList.contains("hidden")) return;
  const id = selectedTask;
  const request = taskRequest;
  const session = authEpoch;
  setButtonBusy(button, true, "等待确认…");
  try {
    if (!await confirmConsoleAction("确认尝试修复？测试通过后会向 GitHub 写入独立分支和提交，不会自动合并。", () => session === authEpoch && request === taskRequest)) return;
    if (session !== authEpoch || request !== taskRequest) return;
    setButtonBusy(button, true, "正在创建…");
    const data = await api(`/v1/tasks/${encodeURIComponent(id)}/fix`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (request === taskRequest) {
      const published = Boolean(data.branch);
      const target = $("#fix-result");
      target.innerHTML = `<strong>${published ? "修复分支已准备好" : "本次没有生成自动修复"}</strong><p>${published ? "修改保留在独立分支，尚未合并到原仓库。请在 GitHub 中检查修复和测试结果。" : "问题可能不适用确定性修复，或未通过验证。原仓库没有被修改。"}</p>${published ? `<p>分支：<code>${escapeHtml(data.branch)}</code></p>` : ""}`;
      toast(published ? "修复分支已创建，请人工确认" : "未生成修复，请查看提示");
    }
  } catch (error) {
    if (request === taskRequest) toast(error.message);
  } finally {
    if (session === authEpoch && request === taskRequest) setButtonBusy(button, false);
  }
});

$("#install-github").addEventListener("click", async () => {
  const button = $("#install-github");
  if (button.disabled) return;
  const session = authEpoch;
  setButtonBusy(button, true, "正在连接…");
  try {
    const data = await api("/v1/github/installations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (session === authEpoch) window.location.assign(data.url);
  } catch (error) {
    if (session === authEpoch) { toast(error.message); setButtonBusy(button, false); }
  }
});

$("#evolution-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $('button[type="submit"]', form);
  if (button.disabled) return;
  const session = authEpoch;
  const values = new FormData(form);
  setButtonBusy(button, true, "正在评测…");
  try {
    const data = await api("/v1/evolution/propose", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ skill_name: values.get("skill_name"), prompt: values.get("prompt") }),
    });
    if (session !== authEpoch) return;
    $("#evolution-result").classList.remove("empty");
    $("#evolution-result").innerHTML = evaluationMarkup(data);
    toast(data.decision === "deferred" ? "候选已记录，本次未完成评测或没有新的候选。" : "候选评测已记录，请查看结论。");
    loadFailures();
  } catch (error) {
    if (session === authEpoch) toast(error.message);
  } finally {
    if (session === authEpoch) setButtonBusy(button, false);
  }
});

$("#auto-evolve").addEventListener("click", async () => {
  const button = $("#auto-evolve");
  if (button.disabled) return;
  const session = authEpoch;
  setButtonBusy(button, true, "正在生成…");
  try {
    const data = await api("/v1/evolution/auto", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ skill_name: "llm-review" }),
    });
    if (session !== authEpoch) return;
    $("#evolution-result").classList.remove("empty");
    $("#evolution-result").innerHTML = evaluationMarkup(data);
    toast(data.decision === "deferred" ? "未生成新的合格候选，请检查反馈与评测条件。" : "反馈候选评测已记录，请查看结论。");
    loadFailures();
  } catch (error) {
    if (session === authEpoch) toast(error.message);
  } finally {
    if (session === authEpoch) setButtonBusy(button, false);
  }
});

$("#refresh").addEventListener("click", async () => {
  const session = authEpoch;
  const view = location.hash.slice(1) || "overview";
  if (view !== "overview") await loadDashboard();
  if (session !== authEpoch) return;
  let refreshed;
  if (view === "overview") refreshed = await loadDashboard();
  else if (view === "tasks") {
    const results = await Promise.all([loadTasks(), selectedTask ? openTask(selectedTask) : true]);
    refreshed = results.every(Boolean);
  } else if (view === "review") refreshed = await loadReviewWorkflows();
  else if (view === "skills") refreshed = await loadSkills();
  else if (view === "studio") { await window.studio?.load(); return; }
  else if (view === "evolution") refreshed = await loadFailures();
  else refreshed = await loadDashboard();
  if (session === authEpoch) toast(refreshed ? "数据已刷新" : "部分数据未能刷新，请查看提示");
});

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $('button[type="submit"]', form);
  if (button.disabled) return;
  const session = authEpoch;
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
    if (session !== authEpoch) return;
    localStorage.setItem("evoagent_role", data.role);
    localStorage.setItem("evoagent_token", data.access_token);
    accessToken = data.access_token; currentRole = data.role;
    observedToken = accessToken; loginRequired = false;
    resetConsole();
    applyRole();
    $(".app-shell").inert = false;
    $("#login-overlay").classList.add("hidden");
    $("#logout").classList.remove("hidden");
    $("#login-error").textContent = "";
    loadDashboard();
    show(location.hash.slice(1) || "overview", false);
  } catch (error) {
    if (session === authEpoch) $("#login-error").textContent = error.message;
  } finally {
    if (session === authEpoch) setButtonBusy(button, false);
  }
});

$("#logout").addEventListener("click", () => endSession("", true));

applyCapabilities();
if (accessToken) $("#logout").classList.remove("hidden");
show(location.hash.slice(1) || "overview", false);
loadDashboard();
