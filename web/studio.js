/* Studio edits definitions through authenticated APIs; no executable user code. */
function studioGraphLevels(steps) {
  const levels = [], done = new Set(["$input"]);
  let pending = [...steps];
  // ponytail: repeated scans are bounded by 64 nodes; no layout dependency needed.
  while (pending.length) {
    const ready = pending.filter((step) => Object.values(step.sources).every((ref) => !ref || done.has(ref.split(".")[0])));
    if (!ready.length) { levels.push(pending); break; } // Keep invalid drafts visible; publication rejects cycles.
    levels.push(ready);
    ready.forEach((step) => done.add(step.id));
    pending = pending.filter((step) => !done.has(step.id));
  }
  return levels;
}

function studioRenameStep(definition, id, name) {
  if (!/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/.test(name) || definition.steps.some((step) => step.id !== id && step.id === name)) {
    throw new Error("节点标识须为唯一的字母、数字、短横线或下划线。");
  }
  const rename = (ref) => ref.startsWith(`${id}.`) ? name + ref.slice(id.length) : ref;
  for (const step of definition.steps) {
    for (const port of Object.keys(step.sources)) step.sources[port] = rename(step.sources[port]);
    if (step.id === id) step.id = name;
  }
  for (const port of Object.keys(definition.outputs)) definition.outputs[port] = rename(definition.outputs[port]);
}

function studioRemoveStep(definition, id) {
  definition.steps = definition.steps.filter((step) => step.id !== id);
  const disconnect = (ports) => { for (const port of Object.keys(ports)) if (ports[port].startsWith(`${id}.`)) ports[port] = ""; };
  definition.steps.forEach((step) => disconnect(step.sources));
  disconnect(definition.outputs);
}

function studioOptionItems(items, value) {
  const selected = String(value ?? "");
  return items.some(([key]) => String(key) === selected) ? items
    : [[selected, selected ? `${selected}（当前不可用）` : "请选择"], ...items];
}

function studioInputSource(step, port) {
  return Object.hasOwn(step.sources, port) ? step.sources[port] : "";
}

function studioPlaybook(config, name) {
  const value = config?.playbook;
  if (value && [value.identity, value.objective, value.instructions].every((item) => typeof item === "string")) return value;
  return { identity: name || "代码审查 Agent", objective: typeof config?.prompt === "string" ? config.prompt : "", instructions: "" };
}

function studioCanConnect(steps, source, target) {
  const pending = [source.split(".")[0]], seen = new Set();
  while (pending.length) {
    const id = pending.pop();
    if (id === target) return false;
    if (seen.has(id)) continue;
    seen.add(id);
    const step = steps.find((item) => item.id === id);
    if (step) pending.push(...Object.values(step.sources).filter(Boolean).map((ref) => ref.split(".")[0]));
  }
  return true;
}

function studioReplaceAgent(definition, id, previous, next) {
  const step = definition.steps.find((item) => item.id === id);
  const sources = Object.fromEntries(Object.entries(next.inputs).map(([port, type]) => [port,
    previous?.inputs[port] === type ? studioInputSource(step, port) : "",
  ]));
  const keep = (ref) => {
    const [node, port] = ref.split(".");
    return node !== id || (Object.hasOwn(next.outputs, port) && previous?.outputs[port] === next.outputs[port]) ? ref : "";
  };
  for (const ports of [...definition.steps.map((item) => item.sources), definition.outputs]) {
    for (const port of Object.keys(ports)) ports[port] = keep(ports[port]);
  }
  Object.assign(step, { agent: next.id, version: next.version, sources });
}

(() => {
  const root = $("#studio-root");
  const diffType = "unified-diff@1", findingsType = "review-findings@1";
  const labels = { rules: "规则 Agent", llm: "模型 Agent", merge: "结果汇总" };
  const builtinLabels = { planner: "规划", specialists: "内置审查组", critic: "质询", test: "复现判断", synthesizer: "综合", fix: "修复判断", verifier: "验证", "standards-review": "规范轴审查", "spec-review": "需求轴审查", "axis-merge": "双轴结果汇总" };
  const typeLabels = { "unified-diff@1": "代码变更", "parsed-diff@1": "文件与行号", "review-context@1": "需求与项目规范", "repository-evidence@1": "仓库影响证据", "review-findings@1": "问题与修复建议", "review-plan@1": "审查计划", "review-critiques@1": "质询结果", "review-reproductions@1": "复现结果", "review-fix-decisions@1": "修复判断", "text@1": "文本", "integer@1": "整数", "boolean@1": "是 / 否" };
  const typeLabel = (type) => Object.hasOwn(typeLabels, type) ? typeLabels[type] : "结构化内容";
  const portLabel = (port) => Object.hasOwn(portLabels, port) ? portLabels[port] : port;
  let catalog = null, documents = [], agents = [], available = [], flow = null, selected = null;
  let dirty = false, busy = false, epoch = 0, notice = "", errorNotice = false, editor = null, editorDirty = false;
  let run = null, dragAgent = null, selectedOutput = null;
  let pages = {};
  let trial = { repository: "", diff: "", title: "", spec: "", standards: "", target: "draft", version: "" }, bindingRepository = "", bindingVersion = "", bindingSnapshot = null;
  const manage = () => ["admin", "platform_admin"].includes(currentRole) || !accessToken;
  const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  // Escape quotes too: labels are rendered in both text and attribute contexts.
  const esc = (value) => escapeHtml(String(value ?? "")).replaceAll('"', "&quot;").replaceAll("'", "&#39;");
  const options = (items, selectedValue) => studioOptionItems(items, selectedValue).map(([value, label]) => `<option value="${esc(value)}" ${String(value) === String(selectedValue ?? "") ? "selected" : ""}>${esc(label)}</option>`).join("");
  const agentKey = (agent) => `${agent.id}:${agent.version}`;
  const specFor = (step) => available.find((agent) => agent.id === step.agent && agent.version === step.version);
  const button = (action, label, primary = false) => `<button type="button" class="button ${primary ? "" : "secondary"}" data-studio-action="${action}">${label}</button>`;
  const newFlow = () => ({ revision: 0, active_version: null, definition: { name: "我的审查流程", steps: [], outputs: { verified: "" } } });
  const bindingLabel = (value) => value?.workflow_id ? `${value.name || "已发布流程"} · v${value.version}` : "部署默认流程";
  const nodeLabel = (step) => `${specFor(step)?.name || "缺失的 Agent"} · ${flow.definition.steps.indexOf(step) + 1}`;
  const sourceLabel = (ref) => {
    if (!ref) return "待连接";
    const [id, port] = ref.split("."), step = flow.definition.steps.find((item) => item.id === id);
    return `${id === "$input" ? "PR 输入" : step ? nodeLabel(step) : "缺失的上游"} / ${portLabel(port)}`;
  };

  function confirmAction(text) {
    const request = epoch;
    return confirmConsoleAction(text, () => request === epoch);
  }

  function message(text, failure = false) {
    notice = text; errorNotice = failure;
    const target = $("#studio-notice");
    if (target) { target.textContent = text; target.classList.toggle("studio-error", failure); }
  }

  async function action(work) {
    if (busy) return;
    captureForms();
    busy = true;
    const request = epoch;
    $$("button, input, select, textarea", root).forEach((node) => { node.disabled = true; });
    try { await work(request); }
    catch (error) { if (request === epoch) message(error.message, true); }
    finally { if (request === epoch) { busy = false; render(); } }
  }

  const publishedAgents = (items) => Promise.all(items.filter((item) => item.active_version).map(async (item) => {
    const value = await api(`/v1/studio/agents/${item.id}/versions/${item.active_version}`);
    return { id: item.id, version: value.version, ...value.definition };
  }));

  async function refreshCatalog(request, candidate = flow) {
    const [catalogData, flowsData, agentsData] = await Promise.all([
      api("/v1/studio/catalog"), api("/v1/studio/workflows"), api("/v1/studio/agents"),
    ]);
    if (request !== epoch) return;
    const published = await publishedAgents(agentsData.documents);
    if (request !== epoch) return;
    // Older pinned agents remain usable when a different version is published.
    const needed = candidate?.definition.steps.filter((step) => step.version && !published.some((agent) => agent.id === step.agent && agent.version === step.version)) || [];
    const historical = await Promise.all(needed.map(async (step) => {
      try {
        const value = await api(`/v1/studio/agents/${step.agent}/versions/${step.version}`);
        return { id: step.agent, version: step.version, ...value.definition };
      } catch (error) {
        if (error.status === 404) return null; // Unresolved draft references remain removable.
        throw error;
      }
    }));
    if (request !== epoch) return;
    catalog = catalogData; documents = flowsData.documents; agents = agentsData.documents;
    pages = { workflows: flowsData.next_cursor, agents: agentsData.next_cursor };
    if (candidate?.id && !documents.some((item) => item.id === candidate.id)) documents = [...documents, { id: candidate.id, name: candidate.definition.name }];
    available = [...published, ...historical.filter(Boolean), ...catalog.builtins.map((item) => ({ ...item, name: builtinLabels[item.id] || item.id, kind: "builtin" }))];
  }

  async function loadMore(kind) {
    if (busy || !pages[kind]) return;
    const request = epoch, renameDraft = $("#studio-step-name")?.value;
    let firstAdded = null;
    await action(async (request) => {
      message("正在加载更多，当前草稿和连线会保留。");
      const page = await api(`/v1/studio/${kind}?cursor=${encodeURIComponent(pages[kind])}`);
      if (request !== epoch) return;
      const published = kind === "agents" ? await publishedAgents(page.documents) : [];
      if (request !== epoch) return;
      const previous = kind === "agents" ? agents : documents;
      firstAdded = page.documents.find((item) => !previous.some((known) => known.id === item.id))?.id;
      const merged = [...new Map([...previous, ...page.documents].map((item) => [item.id, item])).values()];
      if (kind === "agents") {
        agents = merged;
        available = [...new Map([...available, ...published].map((item) => [agentKey(item), item])).values()];
      } else documents = merged;
      pages[kind] = page.next_cursor;
      message(kind === "agents" ? `已加载 ${agents.length} 个 Agent，当前草稿未改变。` : `已加载 ${documents.length} 个流程，当前草稿未改变。`);
    });
    if (request !== epoch) return;
    const rename = $("#studio-step-name");
    if (rename && renameDraft !== undefined) rename.value = renameDraft;
    const target = kind === "workflows" ? $("#studio-flow-picker") : firstAdded ? $(`[data-edit-agent="${firstAdded}"]`, root) : $('[data-studio-action="more-agents"]', root);
    target?.focus();
  }

  function denyDesignAccess() {
    window.studio.reset();
    root.innerHTML = '<p class="workflow-note">流程设计与完整配置需要管理员权限。你仍可在任务中心查看审查报告和可读交接记录。</p>';
  }

  async function load() {
    if (!manage()) { denyDesignAccess(); return; }
    await action(async (request) => {
      try { await refreshCatalog(request); }
      catch (error) {
        if (request !== epoch) return;
        if (error.status === 403) { denyDesignAccess(); return; }
        throw error;
      }
      if (request !== epoch) return;
      if (!flow) flow = newFlow();
      message("先选 Agent，再连线；可以随时更换组件。草稿不会影响已发布流程。");
    });
  }

  function sources(type, targetId = "") {
    const result = Object.entries(catalog.inputs).filter(([, kind]) => kind === type).map(([port]) => [`$input.${port}`, `PR 输入 / ${portLabel(port)}`]);
    for (const step of flow.definition.steps) {
      if (!studioCanConnect(flow.definition.steps, `${step.id}.output`, targetId)) continue;
      const agent = specFor(step);
      for (const [port, kind] of Object.entries(agent?.outputs || {})) {
        if (kind === type) result.push([`${step.id}.${port}`, `${nodeLabel(step)} / ${portLabel(port)}`]);
      }
    }
    return result;
  }

  function graphMarkup() {
    const steps = flow.definition.steps;
    return `<div class="studio-canvas" id="studio-canvas" aria-label="流程画布，拖入 Agent 或点击添加">
      <svg class="studio-edges" aria-hidden="true"></svg>
      <div class="studio-graph-content">
        <article class="studio-node studio-boundary" data-graph-node="$input"><strong>PR 变更输入</strong><small>运行时提供真实 Diff</small>
          ${Object.entries(catalog.inputs).map(([port, type]) => `<button class="studio-port" type="button" data-output="$input.${port}" data-type="${esc(type)}">${esc(portLabel(port))} <span>输出 →</span></button>`).join("")}</article>
        <div class="studio-nodes">${steps.length ? studioGraphLevels(steps).map((level) => `<div class="studio-node-level">${level.map((step) => {
          const agent = specFor(step);
          return `<article class="studio-node ${step.id === selected ? "selected" : ""}" data-graph-node="${esc(step.id)}">
            <button class="studio-node-title" type="button" data-select-node="${esc(step.id)}" aria-pressed="${step.id === selected}"><small>${esc(agent ? labels[agent.kind] || "内置 Agent" : "待修复")}${step.version ? ` · v${step.version}` : ""}</small><strong>${esc(nodeLabel(step))}</strong><span>配置 / 更换 Agent ↗</span></button>
            <div class="studio-node-ports">${Object.entries(agent?.inputs || {}).map(([port, type]) => `<button class="studio-port input-port" type="button" data-input-node="${esc(step.id)}" data-input-port="${esc(port)}" data-type="${esc(type)}" aria-label="连接 ${esc(nodeLabel(step))} 的${esc(portLabel(port))}"><span>${studioInputSource(step, port) ? "●" : "○"} ${esc(portLabel(port))}</span><small>${esc(sourceLabel(studioInputSource(step, port)))}</small></button>`).join("")}
            ${Object.entries(agent?.outputs || {}).map(([port, type]) => `<button class="studio-port output-port" type="button" data-output="${esc(step.id)}.${esc(port)}" data-type="${esc(type)}" aria-label="选择 ${esc(nodeLabel(step))} 的${esc(portLabel(port))}输出">${esc(portLabel(port))} <span>输出 →</span></button>`).join("")}</div>
          </article>`;
        }).join("")}</div>`).join("") : `<div class="studio-empty"><b>把审查职责拆成可以组合的积木</b><p>例如：安全检查 + 业务检查 → 结果汇总。<br>从左侧添加组件，或创建你自己的第一个 Agent。</p>${button("new-agent", "＋ 创建并加入 Agent", true)}</div>`}</div>
        <article class="studio-node studio-boundary" data-graph-node="$output"><strong>最终审查报告</strong><small>只有连接到这里的结果才会进入报告</small><label>最终结果来源<select id="studio-final-source" data-write>${options([["", "选择一个 Agent 的检查结果"], ...sources(findingsType)], flow.definition.outputs.verified)}</select></label></article>
      </div>
    </div>`;
  }

  function inspectorMarkup() {
    const step = flow.definition.steps.find((item) => item.id === selected), agent = step && specFor(step);
    if (!step) return '<h3>这条线交接什么？</h3><p class="workflow-note">选中一个 Agent，指定它接收谁的内容。输出可以交给多个 Agent；用“结果汇总”合并多份检查结果。</p><p class="workflow-note">点输出 → 点输入即可连线，也可以用下拉框完成全部操作。</p>';
    if (!agent) return `<h3>引用的 Agent 不可用</h3><p class="workflow-note">该节点的已发布版本不存在或不可见。可移除节点，重新添加可用 Agent 并连线；原发布版本不受影响。</p>${button("remove-node", "移除节点")}`;
    return `<h3>${esc(nodeLabel(step))}</h3><label>使用哪个 Agent<select id="studio-node-agent" data-write>${options(available.map((item) => [agentKey(item), `${item.name}${item.version ? ` · v${item.version}` : "（内置）"}`]), agentKey(agent))}</select></label>
      <p class="workflow-note">更换时保留同名、同类型连线；不兼容的连接会断开，等待重新指定。</p>
      <h4>从谁接收</h4>${Object.entries(agent.inputs).map(([port, type]) => `<label>${esc(portLabel(port))}<small class="studio-type">需要${esc(typeLabel(type))}</small><select data-source-port="${esc(port)}" data-write>${options([["", "尚未连接"], ...sources(type, step.id)], studioInputSource(step, port))}</select></label>`).join("")}
      <h4>交付给下游</h4>${Object.entries(agent.outputs).map(([port, type]) => `<p class="workflow-note">${esc(portLabel(port))} · ${esc(typeLabel(type))}</p>${type === findingsType ? `<button type="button" class="link-button" data-report-port="${esc(port)}" data-write>将此输出作为最终报告 →</button>` : ""}`).join("")}
      <details class="studio-advanced"><summary>高级：节点标识与交接契约</summary><label>节点标识<input id="studio-step-name" value="${esc(step.id)}" maxlength="64" pattern="[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}" data-write></label><button type="button" class="link-button" id="studio-rename-step" data-write>应用节点标识</button>${Object.entries(agent.inputs).map(([port, type]) => `<p class="workflow-note">${esc(port)} · ${esc(type)}</p>`).join("")}</details>
      ${button("remove-node", "移除节点")}`;
  }

  function captureForms() {
    // Preserve user input across graph redraws and async saves.
    const trialForm = $("#studio-run-form", root), bindingForm = $("#studio-bind-form", root);
    if (trialForm && !$("input", trialForm).disabled) trial = { ...trial, ...Object.fromEntries(new FormData(trialForm)) };
    if (bindingForm && !$("input", bindingForm).disabled) {
      const values = new FormData(bindingForm);
      bindingRepository = values.get("repository"); bindingVersion = values.get("version");
    }
  }

  function render() {
    captureForms();
    if (!catalog || !flow) {
      root.innerHTML = `<p id="studio-notice" class="workflow-note ${errorNotice ? "studio-error" : ""}">${esc(notice || "正在读取工作室…")}</p>${button("reload", "重新读取")}`;
      $("[data-studio-action]", root)?.addEventListener("click", load);
      return;
    }
    root.innerHTML = `<nav class="studio-journey" aria-label="搭建步骤"><button type="button" data-studio-scroll="studio-workspace"><b>01</b><span><strong>选择与组装</strong><small>创建 Agent，连接上下游</small></span></button><button type="button" data-studio-scroll="studio-trial"><b>02</b><span><strong>试运行</strong><small>用 Diff 验证每一步交接</small></span></button><button type="button" data-studio-scroll="studio-release"><b>03</b><span><strong>用于 PR 审查</strong><small>发布版本，手动使用或绑定仓库</small></span></button></nav>
      <div class="studio-toolbar"><label class="studio-flow-picker">工作流<select id="studio-flow-picker">${options([["", "新建流程"], ...documents.map((item) => [item.id, item.name])], flow.id || "")}</select></label>
      ${pages.workflows ? button("more-workflows", "加载更多流程") : ""}
      <label class="studio-name-label">流程名称<input id="studio-name" value="${esc(flow.definition.name)}" maxlength="100" data-write></label>
      <div class="studio-actions">${button("save", "保存草稿")}${button("validate", "校验交接")}${button("publish", "发布版本", true)}</div></div>
      <div class="studio-status"><span>${dirty ? "有未保存修改" : flow.id ? `草稿 r${flow.revision}` : "尚未保存"}</span><span>${flow.active_version ? `最新发布 v${flow.active_version}` : "未发布"}</span><span>画布显示草稿；发布不会自动切换仓库</span></div>
      <p id="studio-notice" class="workflow-note ${errorNotice ? "studio-error" : ""}" role="status">${esc(notice)}</p>
      <div class="studio-workspace" id="studio-workspace" tabindex="-1"><aside class="studio-library"><h3>Agent 组件库</h3>
        <p class="workflow-note">选一个组件加入画布，或自己定义。</p>
        ${(catalog.templates || []).length ? `<div class="studio-create-types">${catalog.templates.map((template) => `<button type="button" data-studio-action="use-template-${esc(template.id)}" ${template.available ? "" : "disabled"}><strong>◎ ${esc(template.name)}</strong><small>${esc(template.available ? template.description : template.reason || "当前不可用")}</small></button>`).join("")}</div>` : ""}
        ${(catalog.agent_recipes || []).length ? `<h4>Agent 配方 · 复制后可修改</h4><div class="studio-create-types">${catalog.agent_recipes.map((recipe) => `<button type="button" data-studio-action="use-agent-recipe-${esc(recipe.id)}"><strong>◇ ${esc(recipe.name)}</strong><small>${esc(recipe.description)}</small></button>`).join("")}</div>` : ""}
        <div class="studio-create-types">${[["rules", "＋ 规则检查", "勾选检查项，或定义业务规则"], ["llm", "＋ 模型分析", "定义 Playbook、模型和只读工具"], ["merge", "＋ 结果汇总", "接收多份结果，去重汇总"]].map(([kind, label, note]) => `<button type="button" data-studio-action="create-${kind}"><strong>${label}</strong><small>${note}</small></button>`).join("")}</div>
        <h4>我的 Agent · 点击添加</h4>
        <div class="studio-agent-list">${agents.map((item) => `<div class="studio-library-item"><button type="button" class="studio-library-name" data-edit-agent="${esc(item.id)}"><strong>${esc(item.name)}</strong><small>${item.active_version ? `v${item.active_version}` : "草稿，尚未发布"}</small></button>${item.active_version ? `<button type="button" class="link-button" draggable="true" data-add-agent="${esc(item.id)}:${item.active_version}" aria-label="添加 ${esc(item.name)}">添加</button>` : ""}</div>`).join("") || '<p class="workflow-note">还没有自定义 Agent，点击“创建”开始。</p>'}</div>
        ${pages.agents ? button("more-agents", "加载更多 Agent") : '<p class="workflow-note">已显示全部 Agent</p>'}
        <details class="studio-builtins" open><summary>内置 Agent · 即取即用</summary>${available.filter((item) => item.kind === "builtin").map((item) => `<button type="button" class="studio-builtin" draggable="true" data-add-agent="${esc(agentKey(item))}">${esc(item.name)} <span>+</span></button>`).join("")}</details></aside>
        <div class="studio-graph"><div class="studio-graph-head"><span>${flow.definition.steps.length} 个节点</span><span>点输出 → 点输入</span><button type="button" class="link-button hidden" id="studio-cancel-link">取消连线</button></div>${graphMarkup()}</div>
        <aside class="studio-inspector">${inspectorMarkup()}</aside></div>
      <div class="studio-bottom"><section class="panel" id="studio-trial" tabindex="-1"><div class="panel-head"><h3>02 · 试运行流程</h3><span class="status status-neutral">不会发布 GitHub 评论</span></div>
        <form id="studio-run-form"><div class="studio-form-row"><label>试运行对象<select name="target" id="studio-run-target" data-write>${options([["draft", "当前草稿"], ["published", "指定发布版本"]], trial.target)}</select></label>
          ${trial.target === "published" ? `<label>试运行版本<input name="version" type="number" min="1" max="2147483647" step="1" list="studio-versions" value="${esc(trial.version)}" placeholder="例如 1" required data-write></label>` : ""}</div>
          <p class="workflow-note">${trial.target === "published" ? "执行所选版本的不可变快照，不保存或使用当前草稿修改。" : "保存当前草稿后执行，不会发布或改变仓库配置。"}</p>
          <label>仓库<input name="repository" value="${esc(trial.repository)}" placeholder="owner/repository" required></label><label>Unified Diff<textarea name="diff" rows="5" spellcheck="false" placeholder="粘贴要验证的变更" required>${esc(trial.diff)}</textarea></label>
          <details class="studio-advanced"><summary>双轴审查上下文（可选）</summary><label>变更标题<input name="title" value="${esc(trial.title)}" maxlength="512"></label><label>需求 / Spec<textarea name="spec" rows="4">${esc(trial.spec)}</textarea></label><label>项目规范<textarea name="standards" rows="4">${esc(trial.standards)}</textarea></label></details>
          <button class="button" type="submit" data-write>${trial.target === "published" ? "试运行此发布版本" : "保存并试运行草稿"}</button></form>
        ${run ? `<p class="workflow-note">最近一次试运行：${esc(run.workflow?.name || "流程")} · ${run.workflow?.version ? `发布 v${esc(run.workflow.version)}` : run.workflow?.draft_revision ? `草稿 r${esc(run.workflow.draft_revision)}` : "版本信息暂不可用"} · ${esc(run.repository)}</p>
          <div class="studio-run-status"><span>任务 ${esc(stateLabels[taskState(run)] || "已接收")}</span><button class="link-button" type="button" data-studio-action="refresh-run">刷新结果</button><button class="link-button" type="button" data-studio-action="open-run">打开交接记录</button>${run.can_resume ? button("resume-run", "从失败节点重试") : run.can_cancel ? button("cancel-run", "取消试运行") : ""}</div>
          <p class="workflow-note">结果仅对应提交时的版本、仓库和 Diff。试运行成功不代表质量评测或上线审批通过。</p>` : ""}
      </section><section class="panel" id="studio-release" tabindex="-1"><h3>03 · 用于 PR 审查</h3><p class="workflow-note">先在顶部发布流程。发布后，可以在“发起审查”中选择这个版本，也可以绑定仓库以处理后续 PR。</p>${button("use-flow", "选择发布版本并发起审查", true)}<h4>仓库自动使用</h4><p class="workflow-note">先读取当前配置，再切换版本。填写旧版本号可回退；发布新版不会自动上线。</p>
        <form id="studio-bind-form"><label>仓库<input name="repository" value="${esc(bindingRepository)}" placeholder="owner/repository" required></label>${button("load-binding", "读取当前配置")}
          <p id="studio-binding-current" class="workflow-note" role="status">${bindingSnapshot?.repository === bindingRepository ? `上次读取：${esc(bindingLabel(bindingSnapshot.value))}` : "尚未读取该仓库配置。"}</p>
          <label>目标发布版本<input name="version" type="number" min="1" max="2147483647" step="1" list="studio-versions" value="${esc(bindingVersion)}" placeholder="例如 1" required data-write></label>
          <datalist id="studio-versions">${(flow.versions || []).map((item) => `<option value="${item.version}">v${item.version}</option>`).join("")}</datalist>
          <div class="studio-actions"><button class="button" type="submit" data-write>切换到此版本</button>${button("unbind", "恢复默认流程")}</div>
        </form><p class="workflow-note">仅影响未显式选择流程的新审查和后续 PR。已有任务不变。接收 PR 还需完成 GitHub 接入。</p></section></div>
      <dialog id="studio-agent-dialog" class="studio-dialog" aria-labelledby="studio-agent-title"></dialog>`;
    bind();
    if (editor) renderEditor();
    if (!manage()) $$("[data-write], [data-add-agent], [data-studio-action]:not([data-studio-action='refresh-run']):not([data-studio-action='open-run']):not([data-studio-action='load-binding']), [data-output], [data-input-node]", root).forEach((node) => { node.disabled = true; });
    requestAnimationFrame(drawEdges);
  }

  function changed() { dirty = true; message("草稿已修改，保存后可试运行或发布。"); }

  function addAgent(key) {
    const agent = available.find((item) => agentKey(item) === key);
    if (!agent || !manage()) return false;
    if (flow.definition.steps.length >= 64) { message("每条流程最多 64 个节点。", true); return false; }
    let index = 1;
    while (flow.definition.steps.some((step) => step.id === `agent_${index}`)) index++;
    const step = { id: `agent_${index}`, agent: agent.id, version: agent.version, sources: {} };
    for (const [port, type] of Object.entries(agent.inputs)) {
      const choices = sources(type);
      step.sources[port] = choices.length === 1 ? choices[0][0] : "";
    }
    flow.definition.steps.push(step); selected = step.id;
    if (!flow.definition.outputs.verified && Object.values(agent.outputs).includes(findingsType)) {
      flow.definition.outputs.verified = `${step.id}.${Object.keys(agent.outputs).find((port) => agent.outputs[port] === findingsType)}`;
    }
    changed(); render(); return true;
  }

  function bind() {
    $$("[data-studio-scroll]", root).forEach((node) => node.addEventListener("click", () => {
      const section = $(`#${node.dataset.studioScroll}`);
      section.focus({ preventScroll: true }); section.scrollIntoView({ behavior: "smooth", block: "start" });
    }));
    $("#studio-name").addEventListener("input", (event) => { flow.definition.name = event.target.value; changed(); });
    $("#studio-flow-picker").addEventListener("change", async (event) => {
      const id = event.target.value;
      if (dirty && !await confirmAction("切换流程会放弃未保存的修改，继续吗？")) { event.target.value = flow.id || ""; return; }
      action(async (request) => {
        const next = id ? await api(`/v1/studio/workflows/${id}`) : newFlow();
        await refreshCatalog(request, next);
        if (request !== epoch) return;
        flow = next; selected = null; dirty = false; run = null; selectedOutput = null; bindingVersion = next.active_version || "";
        trial.version = next.active_version || "";
        message("已读取草稿。发布版本和运行中任务不受草稿编辑影响。");
      });
    });
    $$("[data-add-agent]", root).forEach((node) => {
      node.addEventListener("click", () => addAgent(node.dataset.addAgent));
      node.addEventListener("dragstart", (event) => { dragAgent = node.dataset.addAgent; event.dataTransfer.setData("text/plain", dragAgent); });
      node.addEventListener("dragend", () => { dragAgent = null; });
    });
    const canvas = $("#studio-canvas");
    canvas.addEventListener("dragover", (event) => { if (dragAgent && manage()) event.preventDefault(); });
    canvas.addEventListener("drop", (event) => { event.preventDefault(); if (dragAgent) addAgent(dragAgent); dragAgent = null; });
    $$("[data-select-node]", root).forEach((node) => node.addEventListener("click", () => { selected = node.dataset.selectNode; render(); }));
    $$("[data-output]", root).forEach((node) => node.addEventListener("click", () => {
      selectedOutput = { source: node.dataset.output, type: node.dataset.type };
      message(`正在交接：${sourceLabel(selectedOutput.source)}。点击高亮的输入，也可在右侧选择来源。`);
      highlightConnections();
    }));
    $("#studio-cancel-link").addEventListener("click", () => { selectedOutput = null; highlightConnections(); message("已取消连线，原有连接未改变。"); });
    $$("[data-input-node]", root).forEach((node) => node.addEventListener("click", () => {
      if (!manage()) return;
      const step = flow.definition.steps.find((item) => item.id === node.dataset.inputNode);
      if (!selectedOutput) { selected = step.id; render(); return; }
      connectSource(step, node.dataset.inputPort, selectedOutput.source);
    }));
    $$("[data-source-port]", root).forEach((node) => node.addEventListener("change", () => {
      connectSource(flow.definition.steps.find((step) => step.id === selected), node.dataset.sourcePort, node.value);
    }));
    $("#studio-node-agent")?.addEventListener("change", (event) => {
      const step = flow.definition.steps.find((item) => item.id === selected), next = available.find((item) => agentKey(item) === event.target.value);
      if (!next || !manage()) return;
      studioReplaceAgent(flow.definition, selected, specFor(step), next);
      selectedOutput = null; changed(); render();
      message("已更换 Agent，并保留兼容连线。请确认各输入和最终报告来源，再校验交接。");
    });
    $$("[data-report-port]", root).forEach((node) => node.addEventListener("click", () => {
      flow.definition.outputs.verified = `${selected}.${node.dataset.reportPort}`;
      changed(); render(); message("已更新最终报告来源，其他结果需要连接到此 Agent 才会进入报告。");
    }));
    $("#studio-final-source").addEventListener("change", (event) => { flow.definition.outputs.verified = event.target.value; changed(); drawEdges(); });
    $("#studio-rename-step")?.addEventListener("click", () => {
      const name = $("#studio-step-name").value;
      try { studioRenameStep(flow.definition, selected, name); selected = name; selectedOutput = null; changed(); render(); }
      catch (error) { message(error.message, true); }
    });
    $$("[data-edit-agent]", root).forEach((node) => node.addEventListener("click", () => action(async (request) => {
      const value = await api(`/v1/studio/agents/${node.dataset.editAgent}`);
      if (request === epoch) { editor = value; editorDirty = false; }
    })));
    $$("[data-studio-action]", root).forEach((node) => node.addEventListener("click", () => handleAction(node.dataset.studioAction)));
    $("#studio-run-target").addEventListener("change", () => { captureForms(); render(); $("#studio-run-target").focus(); });
    $("#studio-run-form").addEventListener("submit", (event) => {
      event.preventDefault(); const values = new FormData(event.target);
      action(async (request) => {
        let selection, name;
        if (values.get("target") === "published") {
          const published = await readPublication(values.get("version"));
          if (request !== epoch) return;
          selection = { id: published.id, version: published.version }; name = published.name;
        } else if (values.get("target") === "draft") {
          await saveFlow(request); if (request !== epoch) return;
          selection = { id: flow.id, draft_revision: flow.revision }; name = flow.definition.name;
        } else {
          throw new Error("请选择试运行对象。");
        }
        const repository = values.get("repository");
        const context = Object.fromEntries(["title", "spec", "standards"].map((key) => [key, values.get(key) || ""]));
        const requestBody = { repository, diff: values.get("diff"), workflow: selection };
        if (Object.values(context).some(Boolean)) requestBody.context = context;
        const result = await submitReview(requestBody, "studio");
        if (request !== epoch) return;
        run = { id: result.task_id, state: result.state, repository, workflow: { ...selection, name } };
        message(result.replayed ? "已找回原试运行任务，未重复创建；可刷新结果或打开交接记录。" : selection.version ? `已提交发布 v${selection.version} 的试运行，当前草稿和仓库配置未改变。` : "已提交草稿试运行，可刷新结果或打开逐节点交接记录。");
      });
    });
    $("#studio-bind-form input[name='repository']").addEventListener("input", () => {
      bindingSnapshot = null;
      $("#studio-binding-current").textContent = "仓库已修改，请重新读取当前配置。";
    });
    $("#studio-bind-form").addEventListener("submit", (event) => {
      event.preventDefault(); changeBinding();
    });
    highlightConnections();
  }

  function connectSource(step, port, source) {
    if (!manage()) return;
    if (source && !sources(specFor(step)?.inputs[port], step.id).some(([ref]) => ref === source)) {
      message("无法连接：内容类型不匹配，或这条线会形成循环。请选择高亮输入。", true); return;
    }
    step.sources[port] = source; selectedOutput = null; changed(); render();
  }

  function highlightConnections() {
    $("#studio-cancel-link").classList.toggle("hidden", !selectedOutput);
    $$("[data-output]", root).forEach((node) => node.setAttribute("aria-pressed", String(selectedOutput?.source === node.dataset.output)));
    $$("[data-input-node]", root).forEach((node) => {
      const compatible = selectedOutput && node.dataset.type === selectedOutput.type && studioCanConnect(flow.definition.steps, selectedOutput.source, node.dataset.inputNode);
      node.classList.toggle("studio-compatible", Boolean(compatible));
      node.classList.toggle("studio-incompatible", Boolean(selectedOutput && !compatible));
    });
  }

  async function readPublication(value) {
    const id = flow.id, version = Number(value);
    if (!id || !Number.isInteger(version) || version < 1 || version >= 2 ** 31) throw new Error("请选择已保存的流程，并填写有效的发布版本号。");
    try {
      const published = await api(`/v1/studio/workflows/${id}/versions/${version}`);
      return { id, version, name: published.name };
    } catch (error) {
      if (error.status === 404) throw new Error("指定的发布版本不存在或当前账号不可见，请核对版本号。");
      throw error;
    }
  }

  function changeBinding(unbind = false) {
    return action(async (request) => {
      const repository = bindingRepository;
      if (!bindingSnapshot || bindingSnapshot.repository !== repository) throw new Error("请先读取该仓库的当前配置，再切换版本。");
      const target = unbind ? null : await readPublication(bindingVersion);
      if (request !== epoch) return;
      const key = target?.id || null, version = target?.version || null;
      const targetLabel = unbind ? "部署默认流程" : `${target.name} · v${version}`;
      const revision = bindingSnapshot.value?.revision || 0;
      if (!await confirmAction(`${repository}：${bindingLabel(bindingSnapshot.value)} → ${targetLabel}。仅切换后续任务，已有任务不变。确认继续？`)) return;
      try {
        const result = await post("/v1/studio/binding", { repository, workflow_id: key, version, revision });
        if (request !== epoch) return;
        bindingSnapshot = { repository, value: result.binding };
        message(`已切换到${bindingLabel(result.binding)}。以后发布新版也不会自动改变此配置。`);
      } catch (error) {
        if (request === epoch) bindingSnapshot = null;
        if (error.status === 403) throw new Error("切换未执行：当前账号权限或仓库的审查器、模型策略不允许此配置。请联系管理员核对后重新读取。");
        throw error;
      }
    });
  }

  async function saveFlow(request) {
    if (flow.id && !dirty) return;
    const saved = await post("/v1/studio/workflows", { ...(flow.id ? { id: flow.id } : {}), revision: flow.revision, definition: flow.definition });
    if (request !== epoch) return;
    flow = { ...saved, versions: flow.versions || [] }; dirty = false; await refreshCatalog(request);
  }

  async function handleAction(name) {
    if (name === "more-agents" || name === "more-workflows") return loadMore(name.slice(5));
    if (name.startsWith("use-agent-recipe-")) {
      const recipe = (catalog.agent_recipes || []).find((item) => `use-agent-recipe-${item.id}` === name);
      if (!recipe) return;
      editor = { revision: 0, definition: structuredClone(recipe.definition) };
      editorDirty = false; render(); return;
    }
    if (name.startsWith("use-template-")) {
      const template = (catalog.templates || []).find((item) => `use-template-${item.id}` === name);
      if (!template?.available || (dirty && !await confirmAction("用模板替换当前未保存的流程？"))) return;
      flow = { ...newFlow(), definition: structuredClone(template.definition) };
      selected = null; selectedOutput = null; dirty = true;
      message("双轴模板已放入画布：规范与需求并行审查，随后质询、复现并验证。请保存后试运行。");
      render(); return;
    }
    if (name === "new-agent" || name.startsWith("create-")) { editor = { revision: 0, definition: defaultAgent(name === "new-agent" ? "rules" : name.slice(7)) }; editorDirty = false; render(); return; }
    if (name === "open-run") { openTask(run.id); return; }
    if (name === "remove-node") {
      studioRemoveStep(flow.definition, selected);
      selected = null; selectedOutput = null; changed(); render(); return;
    }
    if (name === "unbind") return changeBinding(true);
    if (name === "resume-run" && !await confirmAction("用这次任务原有的流程版本和 Diff 重试？已完成节点不重跑；当前草稿修改不会被带入。")) return;
    if (name === "cancel-run" && !await confirmAction("取消这次试运行？取消后不能续跑，需要重新提交。")) return;
    action(async (request) => {
      if (name === "use-flow") {
        const published = await readPublication(flow.active_version);
        if (request === epoch) openReviewWorkflow(published);
      }
      if (name === "save") { await saveFlow(request); message("草稿已保存。"); }
      if (name === "validate") { await post("/v1/studio/validate", { definition: flow.definition }); message("交接校验通过：类型匹配、依赖完整、无环路。尚未执行模型或规则。"); }
      if (name === "publish") {
        if (!await confirmAction("发布将生成不可变版本，不会切换任何仓库。确认发布？")) return;
        await saveFlow(request); if (request !== epoch) return;
        const value = await post(`/v1/studio/workflows/${flow.id}/publish`, { revision: flow.revision });
        if (request !== epoch) return;
        flow.active_version = value.version; bindingVersion = value.version;
        flow.versions = [value, ...(flow.versions || []).filter((item) => item.version !== value.version)].slice(0, 100);
        message(`已发布 v${value.version}。接下来可发起一次审查，或在“用于 PR 审查”中绑定仓库。`); await refreshCatalog(request);
      }
      if (name === "refresh-run") { const task = await api(`/v1/tasks/${run.id}`); if (request === epoch) { run = task; message(task.state === "FAILED" ? "试运行未完成，可查看处理过程定位失败步骤。" : `试运行状态：${stateLabels[task.state] || task.state}`, task.state === "FAILED"); } }
      if (name === "resume-run" || name === "cancel-run") {
        await post(`/v1/tasks/${run.id}/${name === "resume-run" ? "resume" : "cancel"}`, {});
        if (request !== epoch) return;
        const task = await api(`/v1/tasks/${run.id}`);
        if (request === epoch) { run = task; message(name === "resume-run" ? "已请求续跑，保留原任务版本和已完成产物。契约错误需修改后新建任务。" : "已请求取消，可刷新查看最终状态。"); }
      }
      if (name === "load-binding") {
        if (!bindingRepository) throw new Error("请先填写仓库。");
        const repository = bindingRepository;
        const result = await api(`/v1/studio/binding?repository=${encodeURIComponent(repository)}`);
        if (request === epoch) { bindingSnapshot = { repository, value: result.binding }; message(`已读取：${bindingLabel(result.binding)}。`); }
      }
    });
  }

  function defaultAgent(kind) {
    return { name: kind === "merge" ? "报告汇总" : "我的 Agent", kind,
      inputs: kind === "merge" ? { security: findingsType, business: findingsType } : { diff: diffType },
      outputs: { findings: findingsType }, config: kind === "rules" ? { rules: [], checks: [] }
        : kind === "llm" ? { playbook: { identity: "资深代码审查 Agent", objective: "检查新增代码中的业务风险并给出可执行结论。", instructions: "只报告有具体代码依据的问题，并提供修复建议与验证方法。" }, model: catalog.models[0]?.model || "", tools: [], max_output_tokens: 2048 } : {} };
  }

  function portEditor(direction) {
    return `<fieldset class="studio-port-editor"><legend>${direction === "inputs" ? "接收哪些内容" : "交付哪些结果"}</legend><p class="workflow-note">名称供连线时辨认，使用字母、数字或下划线。只有相同内容类型可以交接。</p>${Object.entries(editor.definition[direction]).map(([name, type]) => `<div class="studio-port-row" data-port-row="${direction}"><input aria-label="${direction === "inputs" ? "接收" : "交付"}名称" value="${esc(name)}" maxlength="64" required><select aria-label="${direction === "inputs" ? "接收" : "交付"}内容类型">${options(catalog.types.filter((item) => editor.definition.kind !== "merge" || item === findingsType).filter((item) => direction !== "outputs" || ![diffType, "parsed-diff@1"].includes(item)).map((item) => [item, typeLabel(item)]), type)}</select><button type="button" class="link-button" data-remove-port>移除</button></div>`).join("")}${button(`add-${direction}`, direction === "inputs" ? "添加接收入口" : "添加交付结果")}</fieldset>`;
  }

  function renderEditor() {
    const dialog = $("#studio-agent-dialog"), definition = editor.definition, config = definition.config, playbook = studioPlaybook(config, definition.name);
    dialog.innerHTML = `<form id="studio-agent-form"><div class="panel-head"><h3 id="studio-agent-title">${editor.id ? "编辑 Agent" : "创建 Agent"}</h3><button class="link-button" type="button" id="studio-close-editor">关闭</button></div>
      <p class="workflow-note">${editor.id ? `草稿 r${editor.revision}。已发布版本不会被编辑覆盖。` : "定义它做什么、接收什么、交付什么。发布后可直接加入当前画布。"}</p>
      <div class="studio-form-row"><label>Agent 名称<input name="agent_name" value="${esc(definition.name)}" maxlength="100" required></label><label>执行方式<select name="kind">${options(Object.entries(labels), definition.kind)}</select></label></div>
      ${definition.kind === "rules" ? `<fieldset><legend>内置规则</legend><div class="studio-checks">${catalog.rules.map((rule) => `<label class="check"><input type="checkbox" name="rule" value="${esc(rule.id)}" ${config.rules.includes(rule.id) ? "checked" : ""}><span>${esc(rule.title)}</span></label>`).join("")}</div></fieldset>
        <fieldset><legend>自定义业务规则</legend><p class="workflow-note">仅匹配 Diff 新增行中的字面文本，不执行正则表达式或代码。</p><div id="studio-literal-checks">${config.checks.map((check) => `<div class="studio-literal-check">${["contains", "rule_id", "title", "explanation", "fix", "test"].map((key) => `<label>${({ contains: "匹配文本", rule_id: "规则标识", title: "问题标题", explanation: "原因", fix: "修复建议", test: "验证方法" })[key]}<input data-check-field="${key}" value="${esc(check[key])}" required></label>`).join("")}<label>严重性<select data-check-field="severity">${options([["low", "低"], ["medium", "中"], ["high", "高"], ["critical", "严重"]], check.severity)}</select></label><button type="button" class="link-button" data-remove-check>移除规则</button></div>`).join("")}</div>${button("add-check", "添加业务规则")}</fieldset>` : ""}
      ${definition.kind === "llm" ? `<fieldset><legend>Agent Playbook</legend><p class="workflow-note">身份、目标和准则会随 Agent 版本固化；输入输出契约由下方端口生成，不能用文字绕过。</p><label>身份<input name="playbook_identity" value="${esc(playbook.identity)}" maxlength="200" required></label><label>审查目标<textarea name="playbook_objective" rows="3" maxlength="2000" required>${esc(playbook.objective)}</textarea></label><label>执行准则<textarea name="playbook_instructions" rows="7" maxlength="12000">${esc(playbook.instructions)}</textarea></label></fieldset><div class="studio-form-row"><label>模型<select name="model">${options(catalog.models.length ? catalog.models.map((model) => [model.model, `${model.provider} / ${model.model}`]) : [["", "尚未配置模型，暂不能发布或运行"]], config.model)}</select></label><label>最大输出 Token<input type="number" name="max_output_tokens" min="1" max="4096" value="${config.max_output_tokens}" required></label></div><fieldset><legend>只读工具权限</legend>${catalog.tools.map((tool) => `<label class="check"><input name="tool" type="checkbox" value="${tool}" ${config.tools.includes(tool) ? "checked" : ""}><span>${tool === "local-rules" ? "内置规则扫描" : "Diff 文件与行数摘要"}</span></label>`).join("")}<p class="workflow-note">选中的工具在调用模型前执行；不能访问未连入的 Diff，不开放 Shell 或任意网络地址。</p></fieldset>` : ""}
      ${definition.kind === "rules" ? '<p class="studio-contract">接收代码变更 → 执行所选规则 → 交付问题、位置与修复建议</p>' : definition.kind === "merge" ? `${portEditor("inputs")}<p class="studio-contract">接收各路检查结果 → 去重合并 → 交付统一的问题列表</p>` : `<details class="studio-advanced"><summary>自定义交接内容（默认接收代码变更、交付审查发现）</summary>${portEditor("inputs")}${portEditor("outputs")}</details>`}
      <p id="studio-model-note" class="workflow-note" role="status"></p><p id="studio-agent-error" class="studio-error" role="alert"></p><div class="studio-actions"><button class="button secondary" type="submit" value="save" formnovalidate>保存 Agent 草稿</button><button class="button secondary" type="submit" value="publish" aria-describedby="studio-model-note">仅发布到组件库</button><button class="button" type="submit" value="add" aria-describedby="studio-model-note">发布并加入画布</button></div></form>`;
    if (!dialog.open) dialog.showModal();
    const closeEditor = async () => {
      if (busy || (editorDirty && !await confirmAction("放弃尚未保存的 Agent 修改？"))) return;
      editor = null; editorDirty = false; dialog.close();
    };
    dialog.oncancel = (event) => { event.preventDefault(); closeEditor(); };
    $("#studio-close-editor").addEventListener("click", closeEditor);
    const form = $("#studio-agent-form");
    const modelUnavailable = () => definition.kind === "llm" && !catalog.models.some((model) => model.model === $('[name="model"]', form).value);
    const updatePublish = () => {
      const blocked = modelUnavailable();
      for (const value of ["publish", "add"]) $(`button[value="${value}"]`, form).disabled = blocked || !manage();
      $("#studio-model-note").textContent = blocked ? "当前模型不可用。可以先保存草稿；管理员配置模型后刷新，再选择模型发布。" : definition.kind === "llm" ? "模型路由已配置；实际调用仍受仓库策略、预算与供应商可用性约束。" : "";
    };
    if (definition.kind === "llm") $('[name="model"]', form).addEventListener("change", updatePublish);
    updatePublish();
    form.addEventListener("input", () => { editorDirty = true; });
    const collect = () => {
      const values = new FormData(form), value = structuredClone(editor.definition);
      value.name = values.get("agent_name");
      if (value.kind === "rules") {
        value.config = { rules: values.getAll("rule"), checks: $$(".studio-literal-check", form).map((row) => Object.fromEntries($$("[data-check-field]", row).map((input) => [input.dataset.checkField, input.value]))) };
      } else {
        for (const direction of (value.kind === "llm" ? ["inputs", "outputs"] : ["inputs"])) {
          const pairs = $$(`[data-port-row="${direction}"]`, form).map((row) => [$("input", row).value, $("select", row).value]);
          if (new Set(pairs.map(([name]) => name)).size !== pairs.length) throw new Error("端口名称不能重复。");
          value[direction] = Object.fromEntries(pairs);
        }
        if (value.kind === "llm") value.config = { playbook: { identity: values.get("playbook_identity"), objective: values.get("playbook_objective"), instructions: values.get("playbook_instructions") }, model: values.get("model"), max_output_tokens: Number(values.get("max_output_tokens")), tools: values.getAll("tool") };
      }
      return value;
    };
    $('[name="kind"]', form).addEventListener("change", (event) => { const name = $('[name="agent_name"]', form).value; editor.definition = { ...defaultAgent(event.target.value), name }; editorDirty = true; renderEditor(); });
    $$("[data-remove-check], [data-remove-port]", form).forEach((node) => node.addEventListener("click", () => { node.parentElement.remove(); editorDirty = true; }));
    $$("[data-studio-action]", form).forEach((node) => node.addEventListener("click", () => {
      try {
        editor.definition = collect(); editorDirty = true; const action = node.dataset.studioAction;
        if (action === "add-check") editor.definition.config.checks.push({ contains: "", rule_id: "BIZ-POLICY", title: "", explanation: "", fix: "", test: "", severity: "medium" });
        else { const direction = action.slice(4); let index = 1; while (`port_${index}` in editor.definition[direction]) index++; editor.definition[direction][`port_${index}`] = direction === "inputs" && editor.definition.kind === "merge" ? findingsType : "text@1"; }
        renderEditor();
      } catch (error) { $("#studio-agent-error").textContent = error.message; }
    }));
    form.addEventListener("submit", async (event) => {
      event.preventDefault(); if (busy || !manage() || !event.submitter || event.submitter.disabled) return;
      const request = epoch, add = event.submitter.value === "add", publish = add || event.submitter.value === "publish";
      if (publish && modelUnavailable()) { updatePublish(); return; }
      try {
        const definition = collect(); editor.definition = definition; busy = true;
        $$("button, input, textarea, select", form).forEach((node) => { node.disabled = true; });
        const saved = await post("/v1/studio/agents", { ...(editor.id ? { id: editor.id } : {}), revision: editor.revision, definition });
        if (request !== epoch) return;
        editor = saved; editorDirty = false;
        const published = publish ? await post(`/v1/studio/agents/${saved.id}/publish`, { revision: saved.revision }) : null;
        if (request !== epoch) return;
        await refreshCatalog(request); if (request !== epoch) return;
        if (add && published && !available.some((item) => agentKey(item) === `${saved.id}:${published.version}`)) {
          const value = await api(`/v1/studio/agents/${saved.id}/versions/${published.version}`);
          if (request !== epoch) return;
          available.push({ id: saved.id, version: value.version, ...value.definition });
        }
        editor = null;
        const added = add && published && addAgent(`${saved.id}:${published.version}`);
        message(added ? "Agent 已加入画布。请指定输入来源，并确认谁的输出作为最终报告。" : add ? "Agent 已发布到组件库，但未加入画布。请检查节点数量后再添加。" : publish ? "Agent 已发布，可从组件库添加到流程。" : "Agent 草稿已保存，发布后可用于流程。");
        render();
      } catch (error) { if (request === epoch) { renderEditor(); $("#studio-agent-error").textContent = error.message; } }
      finally { if (request === epoch) busy = false; }
    });
    if (!manage()) $$("input, textarea, select, button:not(#studio-close-editor)", form).forEach((node) => { node.disabled = true; });
  }

  function drawEdges() {
    const canvas = $("#studio-canvas"), svg = $(".studio-edges", root);
    if (!canvas || !svg || !flow) return;
    const nodes = new Map($$("[data-graph-node]", canvas).map((node) => [node.dataset.graphNode, node]));
    const bounds = canvas.getBoundingClientRect();
    svg.setAttribute("width", canvas.scrollWidth); svg.setAttribute("height", canvas.scrollHeight);
    const links = flow.definition.steps.flatMap((step) => Object.values(step.sources).filter(Boolean).map((source) => [source.split(".")[0], step.id]));
    if (flow.definition.outputs.verified) links.push([flow.definition.outputs.verified.split(".")[0], "$output"]);
    svg.innerHTML = "";
    for (const [from, to] of links) {
      if (!nodes.has(from) || !nodes.has(to)) continue;
      const a = nodes.get(from).getBoundingClientRect(), b = nodes.get(to).getBoundingClientRect();
      const x1 = a.left + a.width / 2 - bounds.left + canvas.scrollLeft, y1 = a.bottom - bounds.top + canvas.scrollTop;
      const x2 = b.left + b.width / 2 - bounds.left + canvas.scrollLeft, y2 = b.top - bounds.top + canvas.scrollTop;
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", `M ${x1} ${y1} C ${x1} ${y1 + 24}, ${x2} ${y2 - 24}, ${x2} ${y2}`);
      svg.appendChild(path);
    }
  }

  window.addEventListener("resize", drawEdges);
  window.addEventListener("beforeunload", (event) => { if (dirty || editorDirty) { event.preventDefault(); event.returnValue = ""; } });
  window.studio = { load, reset() { epoch++; $$(".studio-confirm [data-cancel]").forEach((button) => button.click()); busy = false; catalog = null; flow = null; editor = null; editorDirty = false; available = []; agents = []; documents = []; pages = {}; selected = null; selectedOutput = null; dirty = false; run = null; notice = ""; trial = { repository: "", diff: "", title: "", spec: "", standards: "", target: "draft", version: "" }; bindingRepository = ""; bindingVersion = ""; bindingSnapshot = null; root.innerHTML = ""; } };
  if (location.hash === "#studio") load();
})();
