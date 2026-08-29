const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

test("Studio lays out dependencies and keeps every handoff wired when renaming or deleting", () => {
  const context = vm.createContext({ $: () => null, window: { addEventListener() {} }, location: { hash: "" } });
  vm.runInContext(readFileSync(join(__dirname, "../web/studio.js"), "utf8"), context);
  const definition = {
    steps: [
      { id: "report", sources: { a: "security.findings", b: "business.findings" } },
      { id: "business", sources: { diff: "$input.diff" } },
      { id: "security", sources: { diff: "$input.diff" } },
    ],
    outputs: { verified: "report.findings" },
  };
  const ids = (steps) => JSON.parse(JSON.stringify(context.studioGraphLevels(steps).map((level) => level.map((step) => step.id))));
  assert.deepEqual(ids(definition.steps), [["business", "security"], ["report"]]);
  context.studioRenameStep(definition, "security", "checks");
  assert.equal(definition.steps[0].sources.a, "checks.findings");
  context.studioRenameStep(definition, "report", "final");
  assert.equal(definition.outputs.verified, "final.findings");
  const unchanged = JSON.stringify(definition);
  for (const invalid of ["business", "<script>", "a.b", ""]) assert.throws(() => context.studioRenameStep(definition, "checks", invalid));
  assert.equal(JSON.stringify(definition), unchanged);
  context.studioRemoveStep(definition, "checks");
  assert.equal(definition.steps[0].sources.a, "");
  assert.equal(definition.steps[0].sources.b, "business.findings");
  context.studioRemoveStep(definition, "final");
  assert.equal(definition.outputs.verified, "");
  assert.deepEqual(ids([{ id: "loop", sources: { a: "loop.value" } }]), [["loop"]]);
  assert.deepEqual(ids([{ id: "missing", sources: { a: "unknown.value" } }]), [["missing"]]);
  const choices = [["current-model", "Current model"]];
  const options = (value) => JSON.parse(JSON.stringify(context.studioOptionItems(choices, value)));
  assert.deepEqual(options("current-model"), choices);
  assert.deepEqual(options("previous-model"), [["previous-model", "previous-model（当前不可用）"], ...choices]);
  assert.deepEqual(options(""), [["", "请选择"], ...choices]);
  assert.equal(choices.length, 1, "unavailable selections must not change the available catalog");
  for (const port of ["constructor", "toString", "diff"]) assert.equal(context.studioInputSource({ sources: {} }, port), "");
  assert.equal(context.studioInputSource({ sources: { constructor: "upstream.text" } }, "constructor"), "upstream.text");
  assert.deepEqual(
    JSON.parse(JSON.stringify(context.studioPlaybook({ playbook: { identity: "安全审查", objective: "检查风险", instructions: "只看新增行" } }, "ignored"))),
    { identity: "安全审查", objective: "检查风险", instructions: "只看新增行" },
  );
  assert.deepEqual(
    JSON.parse(JSON.stringify(context.studioPlaybook({ prompt: "legacy prompt" }, "旧 Agent"))),
    { identity: "旧 Agent", objective: "legacy prompt", instructions: "" },
  );

  const wired = { steps: [
    { id: "scan", agent: "one", version: 1, sources: { diff: "$input.diff" } },
    { id: "report", agent: "merge", version: 1, sources: { security: "scan.findings" } },
  ], outputs: { verified: "scan.findings" } };
  assert.equal(context.studioCanConnect(wired.steps, "scan.findings", "report"), true);
  assert.equal(context.studioCanConnect(wired.steps, "report.findings", "scan"), false);
  assert.equal(context.studioCanConnect(wired.steps, "scan.findings", "scan"), false);
  assert.equal(context.studioCanConnect([{ id: "loop", sources: { a: "loop.text" } }], "loop.text", "elsewhere"), true, "bad drafts must not hang the connection picker");
  const previous = { inputs: { diff: "unified-diff@1" }, outputs: { findings: "review-findings@1" } };
  context.studioReplaceAgent(wired, "scan", previous, { ...previous, id: "two", version: 2 });
  assert.equal(wired.steps[0].agent, "two");
  assert.equal(wired.steps[0].version, 2);
  assert.equal(wired.steps[0].sources.diff, "$input.diff");
  assert.equal(wired.steps[1].sources.security, "scan.findings");
  assert.equal(wired.outputs.verified, "scan.findings");
  context.studioReplaceAgent(wired, "scan", previous, { id: "text-agent", version: 1, inputs: { diff: "text@1", context: "text@1" }, outputs: { findings: "text@1" } });
  assert.equal(wired.steps[0].sources.diff, "");
  assert.equal(wired.steps[0].sources.context, "");
  assert.equal(wired.steps[1].sources.security, "");
  assert.equal(wired.outputs.verified, "");
});

test("Studio preserves drafts and trials exactly the selected draft or published snapshot", async () => {
  const elements = new Map();
  let bindingFormActive = false;
  const node = () => ({
    innerHTML: "", textContent: "", value: "", disabled: false, events: {},
    classList: { toggle() {} }, setAttribute() {}, close() {}, remove() {},
    focus() { elements.set("focused", this); },
    addEventListener(name, callback) { this.events[name] = callback; },
    showModal() { this.open = true; },
  });
  const query = (selector, root) => {
    if (root && (selector === "#studio-run-form" || (selector === "#studio-bind-form" && !bindingFormActive))) return null;
    if (!elements.has(selector)) elements.set(selector, node());
    return elements.get(selector);
  };
  const flow = (id) => ({ id, revision: 1, active_version: id === "saved" ? 2 : null, definition: { name: id, steps: [], outputs: { verified: "" } } });
  let catalogUnavailable = false, catalogDenied = false, missingStatus = 404, publicationStatus = 200, holdPublication = null, holdCatalog = null, catalogAgents = [], catalogModels = [], catalogTemplates = [], catalogAgentRecipes = [];
  let bindingStatus = 200, binding = { workflow_id: "saved", version: 1, revision: 7, name: "original" };
  let paged = false, pageFailure = false, pageVersionFailure = false, holdPage = null, addKeys = [];
  const reads = [], writes = [], tasks = new Map(), opened = [];
  const actionNode = (name) => { const item = query(`[action=${name}]`); item.dataset = { studioAction: name }; return item; };
  const addNode = (key) => { const item = query(`[add=${key}]`); item.dataset = { addAgent: key }; return item; };
  const context = vm.createContext({
    $: query, $$: (selector) => selector === "[data-studio-action]" ? ["refresh-run", "open-run", "load-binding", "unbind", "more-agents", "more-workflows", "create-llm", "use-template-dual-axis-review", "use-agent-recipe-feedback-loop"].map(actionNode) : selector === "[data-add-agent]" ? addKeys.map(addNode) : [], currentRole: "admin", accessToken: "fixture",
    escapeHtml: (value) => String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;"), requestAnimationFrame() {}, structuredClone,
    window: { addEventListener() {} }, location: { hash: "" },
    document: { createElement: node, body: { appendChild() {} } },
    FormData: class { constructor(form) { return new Map(Object.entries(form.values)); } },
    portLabels: { diff: "代码变更", context: "审查上下文", evidence: "仓库影响证据", findings: "发现的问题" },
    stateLabels: { PENDING: "等待执行", SUCCESS: "已完成" }, openTask(id) { opened.push(id); },
    confirmConsoleAction: async (_text, current) => current(),
    taskState: (task) => task.state,
    submitReview(body, scope) {
      assert.equal(scope, "studio");
      return context.api("/v1/reviews?async=true", { method: "POST", body: JSON.stringify(body) });
    },
    async api(path, options = {}) {
      if (options.method === "POST") {
        const body = JSON.parse(options.body);
        writes.push({ path, body });
        if (path === "/v1/studio/binding") {
          if (bindingStatus !== 200) throw Object.assign(new Error("private-route-metadata"), { status: bindingStatus });
          assert.equal(body.revision, binding.revision);
          binding = { workflow_id: body.workflow_id, version: body.version, revision: body.revision + 1, name: "published" };
          return { binding };
        }
        if (path === "/v1/studio/workflows") return { ...flow(body.id), revision: body.revision + 1, definition: body.definition };
        assert.equal(path, "/v1/reviews?async=true");
        const id = `trial-${tasks.size + 1}`;
        tasks.set(id, { id, state: "SUCCESS", repository: body.repository, workflow: { ...body.workflow, name: "actual server snapshot" } });
        return { task_id: id, state: "PENDING" };
      }
      reads.push(path);
      if (path.startsWith("/v1/studio/agents?cursor=")) {
        if (holdPage) await holdPage;
        if (pageFailure) throw new Error("page unavailable");
        return { documents: [{ id: "page-agent", name: "updated draft title", active_version: 1 }, { id: "old-agent", name: "older <img>", active_version: 1 }], next_cursor: null };
      }
      if (path.startsWith("/v1/studio/workflows?cursor=")) return { documents: [{ id: "old-flow", name: "old flow" }], next_cursor: null };
      if (/^\/v1\/studio\/agents\/(page-agent|old-agent)\/versions\/1$/.test(path)) {
        if (pageVersionFailure) throw new Error("page version unavailable");
        return { version: 1, definition: { name: "published <img>", kind: "rules", inputs: {}, outputs: { findings: "review-findings@1" } } };
      }
      if (path.startsWith("/v1/studio/binding?")) return { binding };
      if (path.startsWith("/v1/tasks/")) return tasks.get(path.split("/").at(-1));
      if (/^\/v1\/studio\/workflows\/.+\/versions\//.test(path)) {
        if (holdPublication) await holdPublication;
        if (publicationStatus !== 200) throw Object.assign(new Error("publication unavailable"), { status: publicationStatus });
        return { name: "published <img>", version: 1 };
      }
      if (path === "/v1/studio/catalog") {
        if (holdCatalog) await holdCatalog;
        if (catalogDenied) throw Object.assign(new Error("private-role-detail"), { status: 403 });
        if (catalogUnavailable) throw new Error("catalog unavailable");
        return { types: [], inputs: {}, rules: [], tools: [], models: catalogModels, builtins: [], agent_recipes: catalogAgentRecipes, templates: catalogTemplates };
      }
      if (path === "/v1/studio/agents") return { documents: catalogAgents, next_cursor: paged ? "agent-private-cursor" : null };
      if (path === "/v1/studio/workflows") return { documents: ["saved", "other", "missing"].map((id) => ({ id, name: id })), next_cursor: paged ? "flow-private-cursor" : null };
      if (path.includes("/versions/")) throw Object.assign(new Error("version unavailable"), { status: missingStatus });
      const id = path.split("/").at(-1), value = flow(id);
      if (id === "missing") value.definition.steps = [{ id: "reference", agent: "unavailable", version: 1, sources: {} }];
      return value;
    },
  });
  vm.runInContext(readFileSync(join(__dirname, "../web/studio.js"), "utf8"), context);
  const switchTo = async (value) => {
    await query("#studio-flow-picker").events.change({ target: { value } });
    await new Promise(setImmediate); // Flush the event handler's asynchronous action.
  };
  await context.window.studio.load();
  query("#studio-run-target").events.change();
  assert.equal(elements.get("focused"), query("#studio-run-target"), "switching the trial target retains keyboard focus");
  await switchTo("saved");
  query("#studio-name").events.input({ target: { value: "my unsaved changes" } });
  catalogUnavailable = true;
  await switchTo("other");
  assert.match(query("#studio-root").innerHTML, /my unsaved changes/);
  assert.match(query("#studio-root").innerHTML, /有未保存修改/);
  assert.match(query("#studio-root").innerHTML, /catalog unavailable/);
  catalogUnavailable = false;
  missingStatus = 403;
  await switchTo("missing");
  assert.match(query("#studio-root").innerHTML, /my unsaved changes/);
  missingStatus = 404;
  await switchTo("missing");
  assert.match(query("#studio-root").innerHTML, /缺失的 Agent/);
  assert.doesNotMatch(query("#studio-root").innerHTML, /my unsaved changes/);

  await switchTo("saved");
  query("#studio-name").events.input({ target: { value: "unsaved next draft" } });
  const submit = async (target, version = "1") => {
    query("#studio-run-form").events.submit({ preventDefault() {}, target: { values: { target, version, repository: "demo/repo", diff: "fixture diff" } } });
    await new Promise(setImmediate);
  };
  await submit("published");
  assert.deepEqual(writes, [{ path: "/v1/reviews?async=true", body: { repository: "demo/repo", diff: "fixture diff", workflow: { id: "saved", version: 1 } } }]);
  assert.match(query("#studio-root").innerHTML, /有未保存修改/);
  assert.match(query("#studio-root").innerHTML, /最近一次试运行：published &lt;img&gt; · 发布 v1 · demo\/repo/);
  assert.doesNotMatch(query("#studio-root").innerHTML, /published <img>/);
  query("#studio-name").events.input({ target: { value: "even newer unsaved draft" } });
  await actionNode("refresh-run").events.click();
  await new Promise(setImmediate);
  assert.match(query("#studio-root").innerHTML, /最近一次试运行：actual server snapshot · 发布 v1/);
  assert.match(query("#studio-root").innerHTML, /even newer unsaved draft/);
  await actionNode("open-run").events.click();
  assert.deepEqual(opened, ["trial-1"]);
  for (const version of ["", "0", "1.5", "2147483648"]) await submit("published", version);
  for (publicationStatus of [404, 403, 503]) {
    await submit("published", "9");
    if (publicationStatus === 404) assert.match(query("#studio-root").innerHTML, /发布版本不存在或当前账号不可见/);
  }
  assert.equal(writes.length, 1, "failed publication reads must not save or fall back to a draft trial");
  assert.match(query("#studio-root").innerHTML, /有未保存修改/);
  assert.match(query("#studio-root").innerHTML, /最近一次试运行：actual server snapshot · 发布 v1/);
  publicationStatus = 200;
  await submit("draft");
  assert.equal(writes[1].path, "/v1/studio/workflows");
  assert.equal(writes[1].body.definition.name, "even newer unsaved draft");
  assert.deepEqual(writes[2].body.workflow, { id: "saved", draft_revision: 2 });
  assert.match(query("#studio-root").innerHTML, /最近一次试运行：even newer unsaved draft · 草稿 r2/);
  assert.doesNotMatch(query("#studio-root").innerHTML, /有未保存修改/);
  await submit("draft");
  assert.equal(writes[3].path, "/v1/reviews?async=true", "unchanged retries must not save and advance the draft revision");
  assert.deepEqual(writes[3].body, writes[2].body);
  let releasePublication;
  holdPublication = new Promise((resolve) => { releasePublication = resolve; });
  await submit("published");
  context.window.studio.reset();
  releasePublication();
  await new Promise(setImmediate);
  assert.equal(writes.length, 4, "logout must invalidate a pending version read before task submission");
  assert.equal(query("#studio-root").innerHTML, "");
  let releaseCatalog;
  holdCatalog = new Promise((resolve) => { releaseCatalog = resolve; });
  catalogAgents = [{ id: "old-session-agent", active_version: 1 }];
  const staleLoad = context.window.studio.load();
  context.window.studio.reset();
  releaseCatalog();
  await staleLoad;
  assert.ok(!reads.some((path) => path.includes("old-session-agent")), "an expired catalog read must not fetch agent definitions in the next session");

  holdCatalog = null; holdPublication = null; catalogAgents = [];
  await context.window.studio.load();
  await switchTo("saved");
  query("#studio-bind-form").values = { repository: "demo/repo", version: "2" };
  bindingFormActive = true;
  const loadBinding = async () => {
    await actionNode("load-binding").events.click();
    await new Promise(setImmediate);
  };
  const submitBinding = async () => {
    query("#studio-bind-form").events.submit({ preventDefault() {} });
    await new Promise(setImmediate);
  };
  await loadBinding();
  bindingStatus = 403;
  await submitBinding();
  assert.equal(binding.version, 1);
  assert.match(query("#studio-root").innerHTML, /切换未执行.*模型策略.*重新读取/);
  assert.doesNotMatch(query("#studio-root").innerHTML, /private-route-metadata|已切换到/);
  assert.equal(writes.at(-1).body.revision, 7);
  const deniedWrites = writes.length;
  bindingStatus = 200;
  await submitBinding();
  assert.equal(writes.length, deniedWrites, "a rejected switch must reread the current binding before retrying");
  await loadBinding();
  await submitBinding();
  assert.equal(binding.version, 2);
  assert.equal(binding.revision, 8);
  assert.match(query("#studio-root").innerHTML, /已切换到published · v2/);

  paged = true;
  catalogAgents = [{ id: "page-agent", name: "page one", active_version: 1 }];
  addKeys = ["old-agent:1"];
  await context.window.studio.load();
  query("#studio-name").events.input({ target: { value: "unsaved pagination draft" } });
  query("#studio-step-name").value = "unapplied_node_name";
  const beforePagingWrites = writes.length;
  for (const failure of ["page", "version", "none"]) {
    pageFailure = failure === "page"; pageVersionFailure = failure === "version";
    await actionNode("more-agents").events.click();
    assert.match(query("#studio-root").innerHTML, /unsaved pagination draft/);
    assert.match(query("#studio-root").innerHTML, /有未保存修改/);
    assert.equal(query("#studio-step-name").value, "unapplied_node_name");
    if (failure !== "none") {
      assert.doesNotMatch(query("#studio-root").innerHTML, /data-edit-agent="old-agent"/);
      assert.match(query("#studio-root").innerHTML, /data-studio-action="more-agents"/);
    }
  }
  const pagedHtml = query("#studio-root").innerHTML;
  assert.equal((pagedHtml.match(/data-edit-agent="page-agent"/g) || []).length, 1);
  assert.match(pagedHtml, /older &lt;img&gt;/);
  assert.doesNotMatch(pagedHtml, /older <img>|private-cursor|data-studio-action="more-agents"/);
  assert.equal(elements.get("focused"), query('[data-edit-agent="old-agent"]'));
  assert.deepEqual(reads.filter((path) => path.includes("agents?cursor=")), Array(3).fill("/v1/studio/agents?cursor=agent-private-cursor"), "failed pages must retry the same cursor");
  await addNode("old-agent:1").events.click();
  assert.match(query("#studio-root").innerHTML, /published &lt;img&gt;/);
  assert.match(query("#studio-root").innerHTML, /1 个节点/);
  await actionNode("more-workflows").events.click();
  assert.match(query("#studio-root").innerHTML, /old flow/);
  assert.match(query("#studio-root").innerHTML, /1 个节点/);
  assert.match(query("#studio-root").innerHTML, /unsaved pagination draft/);
  assert.equal(writes.length, beforePagingWrites, "pagination must not save, publish or change bindings");
  await switchTo("old-flow");
  assert.match(query("#studio-root").innerHTML, /value="old-flow" selected>old-flow/);
  assert.doesNotMatch(query("#studio-root").innerHTML, /old-flow（当前不可用）/);

  let releasePage;
  holdPage = new Promise((resolve) => { releasePage = resolve; });
  const latePage = actionNode("more-agents").events.click();
  context.window.studio.reset();
  paged = false; catalogAgents = []; addKeys = [];
  await context.window.studio.load();
  const beforeLatePage = reads.length;
  releasePage();
  await latePage;
  holdPage = null;
  assert.equal(reads.length, beforeLatePage, "an old session's page must not load published definitions");
  assert.doesNotMatch(query("#studio-root").innerHTML, /data-edit-agent="old-agent"|private-cursor/);

  for (const role of ["auditor", "maintainer"]) {
    context.currentRole = role;
    const before = reads.length;
    await context.window.studio.load();
    assert.equal(reads.length, before, "read-only roles must not fetch authored definitions");
    assert.match(query("#studio-root").innerHTML, /管理员权限.*任务中心/);
    assert.doesNotMatch(query("#studio-root").innerHTML, /studio-agent-dialog|studio-flow-picker/);
  }
  context.currentRole = "admin";
  holdCatalog = new Promise((resolve) => { releaseCatalog = resolve; });
  const pendingCatalog = context.window.studio.load();
  context.currentRole = "auditor";
  await context.window.studio.load();
  releaseCatalog();
  await pendingCatalog;
  holdCatalog = null;
  assert.match(query("#studio-root").innerHTML, /管理员权限.*任务中心/);
  assert.doesNotMatch(query("#studio-root").innerHTML, /studio-agent-dialog|studio-flow-picker/);
  context.currentRole = "admin"; // The browser's cached role can outlive a server-side demotion.
  await context.window.studio.load();
  assert.match(query("#studio-root").innerHTML, /studio-flow-picker/);
  catalogDenied = true;
  await context.window.studio.load();
  assert.match(query("#studio-root").innerHTML, /管理员权限.*任务中心/);
  assert.doesNotMatch(query("#studio-root").innerHTML, /studio-agent-dialog|studio-flow-picker|private-role-detail/);
  catalogDenied = false;
  await context.window.studio.load();
  catalogTemplates = [{
    id: "dual-axis-review", name: "双轴 PR 审查", description: "规范与需求并行",
    available: false, reason: "配置模型路由后可用",
    definition: { name: "双轴测试", steps: [{ id: "standards", agent: "standards-review", version: 1, sources: { diff: "$input.diff", context: "$input.context", evidence: "$input.evidence" } }], outputs: { verified: "standards.findings" } },
  }];
  context.window.studio.reset();
  await context.window.studio.load();
  assert.match(query("#studio-root").innerHTML, /use-template-dual-axis-review" disabled.*双轴 PR 审查.*配置模型路由后可用/s);
  const writesBeforeTemplate = writes.length;
  await actionNode("use-template-dual-axis-review").events.click();
  assert.doesNotMatch(query("#studio-root").innerHTML, /value="双轴测试"/);
  catalogTemplates[0].available = true;
  context.window.studio.reset();
  await context.window.studio.load();
  await actionNode("use-template-dual-axis-review").events.click();
  assert.match(query("#studio-root").innerHTML, /value="双轴测试"/);
  assert.match(query("#studio-root").innerHTML, /1 个节点/);
  assert.equal(writes.length, writesBeforeTemplate, "using a template changes only the unsaved canvas");

  catalogAgentRecipes = [{
    id: "feedback-loop", name: "回归与验证审查", description: "检查可信验证路径",
    definition: { name: "回归验收 Agent", kind: "llm", inputs: { diff: "unified-diff@1" }, outputs: { findings: "review-findings@1" }, config: { playbook: { identity: "回归审查员", objective: "检查精确失败信号", instructions: "通过公共接口验证" }, model: "", tools: [], max_output_tokens: 2048 } },
  }];
  context.window.studio.reset();
  await context.window.studio.load();
  const writesBeforeRecipe = writes.length;
  await actionNode("use-agent-recipe-feedback-loop").events.click();
  assert.match(query("#studio-agent-dialog").innerHTML, /回归验收 Agent/);
  assert.match(query("#studio-agent-dialog").innerHTML, /检查精确失败信号/);
  assert.equal(writes.length, writesBeforeRecipe, "using a recipe only opens an editable unsaved Agent");

  await actionNode("create-llm").events.click();
  await new Promise(setImmediate);
  for (const value of ["publish", "add"]) assert.equal(query(`button[value="${value}"]`).disabled, true);
  assert.equal(query('button[value="save"]').disabled, false, "missing models still allow authoring a draft");
  assert.match(query("#studio-model-note").textContent, /模型不可用.*保存草稿/);
  const writesBefore = writes.length;
  // Even a synthetic submit cannot save or publish an unavailable model.
  for (const value of ["publish", "add"]) await query("#studio-agent-form").events.submit({ preventDefault() {}, submitter: { value } });
  assert.equal(writes.length, writesBefore);
  catalogModels = [{ model: "configured-model", provider: "fixture" }];
  context.window.studio.reset();
  await context.window.studio.load();
  await actionNode("create-llm").events.click();
  await new Promise(setImmediate);
  query('[name="model"]').value = "removed-model";
  query('[name="model"]').events.change();
  assert.equal(query('button[value="publish"]').disabled, true);
  query('[name="model"]').value = "configured-model";
  query('[name="model"]').events.change();
  assert.equal(query('button[value="publish"]').disabled, false);
});

test("readable outputs omit internal metadata, escape content and remain tied to the current task", async () => {
  // Only app.js's DOM surface; real layout is checked in the browser.
  const element = () => {
    const classes = new Set();
    let html = "";
    return {
      dataset: {}, events: {}, attributes: {}, disabled: false,
      get innerHTML() { return html; },
      set innerHTML(value) { html = String(value); },
      get textContent() { return html; },
      set textContent(value) {
        html = String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
      },
      classList: {
        add: (name) => classes.add(name), remove: (name) => classes.delete(name),
        contains: (name) => classes.has(name),
        toggle(name, on) { if (on) classes.add(name); else classes.delete(name); },
      },
      addEventListener(name, callback) { this.events[name] = callback; },
      setAttribute(name, value) { this.attributes[name] = value; },
      querySelectorAll: () => [],
      querySelector: (selector) => query(selector),
      reset() {}, focus() {},
    };
  };
  const elements = new Map();
  const query = (selector) => {
    if (!elements.has(selector)) elements.set(selector, element());
    return elements.get(selector);
  };
  const requests = [];
  let transitions = 0;
  const response = (data, status = 200) => ({
    ok: status < 400, status, headers: { get: () => "application/json" }, json: async () => data,
  });
  const context = vm.createContext({
    document: { querySelector: query, querySelectorAll: () => [], createElement: element, startViewTransition(callback) { transitions += 1; callback(); } },
    localStorage: { getItem: () => "", setItem() {}, removeItem() {} },
    location: { hash: "#tasks" }, history: { replaceState() {} },
    window: { addEventListener() {}, scrollTo() {} }, setTimeout, clearTimeout,
    fetch(path, options) {
      if (!path.startsWith("/v1/tasks/") && !path.startsWith("/v1/studio/")) return Promise.resolve(response({}));
      return new Promise((resolve) => requests.push({ path, options, resolve }));
    },
  });
  vm.runInContext(readFileSync(join(__dirname, "../web/app.js"), "utf8"), context);
  assert.equal(transitions, 1, "supported browsers use the native page transition");
  context.window.matchMedia = () => ({ matches: true });
  context.show("overview");
  assert.equal(transitions, 1, "reduced motion skips the page transition");
  const reply = (path, data, status) => {
    const index = requests.findIndex((request) => request.path === path);
    assert.notEqual(index, -1, `request exists: ${path}`);
    requests.splice(index, 1)[0].resolve(response(data, status));
  };
  const report = (id) => ({ id, repository: id, report: {}, pull_request: 1, can_fix: true });
  const workflow = {
    availability: "recorded", task_state: "CANCELLED", workflow: { name: "policy <script>" },
    steps: ["completed", "running", "failed", "pending"].map((status, index) => ({
      id: `step${index}`, agent_id: "<img src=x>", status, attempt: 2,
      inputs: { findings: "findings@1" }, sources: { findings: "upstream.findings" },
      outputs: { findings: "findings@1" }, blocked_by: status === "pending" ? ["step2"] : [],
      duration_ms: index === 0 ? 1500 : null, updated_at: index === 0 ? "2026-08-29T00:00:00Z" : null,
      error: status === "failed" ? "ref <script>" : null,
      private_payload: "must not render arbitrary fields", input_sha256: "secret-digest", idempotency_key: "secret-key",
    })),
  };
  const markup = context.workflowMarkup(workflow, { workflow: { version: 2, name: "policy <script>", steps: Object.fromEntries(workflow.steps.map((step) => [step.id, "Agent <img src=x>"])) } });
  assert.match(markup, /1 \/ 4 个步骤完成/);
  for (const expected of ["任务：已取消", "不代表 Worker 在线", "已派发", "等待上游：Agent", "发布版本 v2", "尝试 2 次", "耗时 1.5 秒", "&lt;script&gt;", "&lt;img src=x&gt;"]) {
    assert.ok(markup.includes(expected), expected);
  }
  assert.doesNotMatch(markup, /<script>|<img|must not render|secret-key|secret-digest|findings@1|幂等键|输入摘要/);
  assert.match(context.workflowMarkup({ availability: "pruned" }), /保留策略清理/);
  assert.match(context.workflowMarkup({ availability: "not_recorded" }), /尚无交接记录/);
  assert.throws(() => context.workflowMarkup({ availability: "invalid" }), /格式异常/);

  const finding = { title: "Dynamic <script>", path: "app.py", line: 12, severity: "critical", explanation: "User input is executed.", evidence: "eval(value)", fix: "Use a parser.", test: "Reject malicious input.", fingerprint: "private-fingerprint" };
  const pretty = context.reportMarkup({
    state: "SUCCESS", repository: "demo/repo", input: { prompt: "private-prompt", digest: "private-digest" }, trace: ["private-trace"],
    report: { findings: [finding], files_reviewed: ["app.py"], risk: "high", unknown: "private-extra" },
  });
  for (const expected of ["发现 1 个待关注问题", "第 12 行", "User input is executed.", "eval(value)", "Use a parser.", "Reject malicious input.", "Dynamic &lt;script&gt;"]) assert.ok(pretty.includes(expected), expected);
  assert.doesNotMatch(pretty, /private-|<script>|fingerprint|prompt|digest/);
  assert.match(context.reportMarkup({ state: "SUCCESS", report: { findings: [] } }), /本次未发现问题/);
  assert.match(context.reportMarkup({ state: "FAILED" }), /本次审查未完成/);
  assert.match(context.reportMarkup({ state: "FAILED", retrying: true }), /正在重试审查/);
  assert.match(context.reportMarkup({ state: "EXECUTING", cancel_requested: true }), /正在取消审查/);
  assert.match(context.workflowMarkup(workflow, { state: "FAILED", retrying: true }), /任务：重试中/);
  assert.match(context.reportMarkup({ state: "SUCCESS", delivery_pending: true, report: { findings: [] } }), /回写尚未确认完成/);
  assert.doesNotMatch(context.reportMarkup({ state: "FAILED" }), /本次未发现问题/);
  assert.match(context.reportMarkup({ state: "CANCELLED" }), /本次审查已取消/);
  assert.match(context.reportMarkup({ state: "SUCCESS", report: {} }), /报告暂不可用/);
  const artifact = context.artifactMarkup({ status: "completed", inputs: { diff: "+<img src=x>" }, outputs: { findings: [finding], hidden: "private-value" }, output_sha256: "private-hash" }, { inputs: { diff: "unified-diff@1" }, outputs: { findings: "review-findings@1" } });
  assert.match(artifact, /收到的内容/);
  assert.match(artifact, /交付的结果/);
  assert.match(artifact, /&lt;img src=x&gt;/);
  assert.doesNotMatch(artifact, /<img|private-|output_sha256/);
  const contextArtifact = context.artifactValueMarkup({
    origin: "github-webhook", title: "PR <script>", spec: "Must isolate input.", standards: "No eval.", truncated: true,
    metadata: { token: "private-token" },
  }, "review-context@1");
  for (const expected of ["PR &lt;script&gt;", "来源：GitHub PR", "内容已按安全上限截断", "需求 / Spec", "项目规范", "Must isolate input.", "No eval."]) assert.ok(contextArtifact.includes(expected), expected);
  assert.doesNotMatch(contextArtifact, /<script>|private-token|metadata/);
  const evidenceArtifact = context.artifactValueMarkup({
    origin: "github-archive", status: "partial", revision: "abcdef1234567890", indexed_files: 12, indexed_bytes: 4096,
    changed_paths: ["app.py"], changed_symbols: ["app.run"], impacted_symbols: ["worker.call<script>"], importing_files: ["worker.py"], truncated: true,
    metadata: { token: "private-token" },
  }, "repository-evidence@1");
  for (const expected of ["已建立部分仓库影响证据", "GitHub 固定版本归档", "abcdef123456", "12 个 Python 文件", "4,096 字节", "变更文件", "app.py", "受影响符号", "worker.call&lt;script&gt;", "已按安全上限截断"]) assert.ok(evidenceArtifact.includes(expected), expected);
  assert.doesNotMatch(evidenceArtifact, /abcdef1234567890|<script>|private-token|metadata/);
  const unavailableEvidence = context.artifactValueMarkup({ origin: "github-archive", status: "unavailable", revision: "abc", indexed_files: 0, indexed_bytes: 0 }, "repository-evidence@1");
  assert.match(unavailableEvidence, /仓库影响证据不可用.*仅使用已提交的 Diff/s);
  const rewiredStep = {
    inputs: { security: "review-findings@1", business: "review-findings@1", diff: "unified-diff@1" },
    outputs: { findings: "review-findings@1" },
    sources: { security: "general.findings", business: "scanner.findings", diff: "$input.diff" },
  };
  const rewired = context.artifactMarkup({
    status: "completed", inputs: { security: [{ ...finding, severity: "low" }], business: [finding], diff: "+value" }, outputs: { findings: [finding] },
  }, rewiredStep, { general: "一般意见", scanner: "安全审查 <img src=x>" });
  assert.match(rewired, /来自 一般意见 · 发现的问题<\/strong>.*接收为 安全检查结果.*severity-low/s);
  assert.match(rewired, /来自 安全审查 &lt;img src=x&gt; · 发现的问题<\/strong>.*接收为 业务检查结果.*severity-critical/s);
  assert.match(rewired, /来自 原始变更 · 代码变更/);
  assert.doesNotMatch(rewired, /<img|scanner\.findings|general\.findings|private-|fingerprint/);
  assert.doesNotMatch(context.artifactValueMarkup({ prompt: "private-value" }, "unknown@1"), /private-value|prompt/);
  assert.match(context.evaluationMarkup({ decision: "approved", version: { version: 3, prompt: "private-prompt" }, candidate: { score: 0.8 }, baseline: { score: 0.7 } }), /等待发布审批/);
  const auditHtml = context.auditRows([
    { actor: "alice <img>", action: "task.resume", resource: "task <script>", created_at: "2026-08-30T00:00:00Z", detail: "private-detail" },
    { actor: "system", action: "constructor", resource: "safe", created_at: "2026-08-30T00:00:00Z" },
  ]);
  for (const expected of ["续跑任务", "alice &lt;img&gt;", "task &lt;script&gt;", "系统操作", "constructor"]) assert.ok(auditHtml.includes(expected), expected);
  assert.doesNotMatch(auditHtml, /<img>|<script>|private-detail|function Object/);
  const outboxHtml = context.outboxRows([{ id: "review:task-1", attempts: 4, updated_at: "2026-08-30T00:00:00Z", last_error: "private-error", payload: "private-payload" }]);
  assert.match(outboxHtml, /结果回写等待重新派发.*review:task-1.*已尝试 4 次.*重新派发/s);
  assert.doesNotMatch(outboxHtml, /private-error|private-payload/);
  const deadHtml = context.deadLetterRows([{ task_id: "task-1", attempt: 3, failed_at: 1, error: "private-error", payload: "private-payload" }]);
  assert.match(deadHtml, /task-1.*第 3 次投递失败.*查看任务/s);
  assert.doesNotMatch(deadHtml, /private-error|private-payload/);

  // Only public codes are displayable; old APIs/proxies must not leak their bodies.
  for (const [data, status, expected] of [
    [{ error_code: "draft_conflict", error: "private-prompt" }, 409, /草稿.*变化/],
    [{ error_code: "invalid_workflow" }, 400, /连线/],
    [{ error: "private-provider-metadata" }, 403, /权限|策略/],
    [{ error: { detail: "private-credential" } }, 400, /输入/],
    [{ error_code: "constructor", error: "private-value" }, 500, /服务/],
  ]) {
    const pending = context.api("/v1/tasks/error");
    reply("/v1/tasks/error", data, status);
    await assert.rejects(pending, (error) => {
      assert.equal(error.status, status);
      assert.match(error.message, expected);
      assert.doesNotMatch(error.message, /private-|\[object Object\]/);
      return true;
    });
  }
  for (const malformed of [
    { ok: false, status: 503, headers: { get: () => "text/html" }, text: async () => "private-proxy-page" },
    { ok: false, status: 503, headers: { get: () => "application/json" }, json: async () => { throw new SyntaxError("private-response-fragment"); } },
  ]) {
    const pending = context.api("/v1/tasks/error");
    requests.shift().resolve(malformed);
    await assert.rejects(pending, (error) => error.status === 503 && /服务/.test(error.message) && !/private-/.test(error.message));
  }

  const workflowId = "a".repeat(32);
  for (const invalid of ["draft", `${workflowId}:0`, `${workflowId}:1.5`, `${workflowId}:2147483648`, '<img>:1']) assert.throws(() => context.reviewWorkflowSelection(invalid));
  assert.equal(context.reviewWorkflowSelection(""), null);
  const loadCatalog = async (version) => {
    const pending = context.loadReviewWorkflows();
    reply("/v1/studio/workflows", { documents: [{ id: workflowId, name: "unpublished name", active_version: version }, { id: "b".repeat(32), name: "draft only", active_version: null }], next_cursor: "next-page" });
    await new Promise(setImmediate);
    reply(`/v1/studio/workflows/${workflowId}/versions/${version}`, { name: "Published <script>", version });
    await pending;
  };
  await loadCatalog(1);
  assert.match(query("#review-workflow").innerHTML, /Published &lt;script&gt; · v1/);
  assert.doesNotMatch(query("#review-workflow").innerHTML, /unpublished name|draft only|<script>/);
  query("#review-workflow").value = `${workflowId}:1`;
  await loadCatalog(2);
  assert.equal(query("#review-workflow").value, `${workflowId}:1`, "a catalog refresh must not silently select a newer version");
  assert.match(query("#review-workflow").innerHTML, /v1.*v2/);
  const beforePage = query("#review-workflow").innerHTML;
  const failedPage = context.loadReviewWorkflows(true);
  reply("/v1/studio/workflows?cursor=next-page", {}, 503);
  assert.equal(await failedPage, false);
  assert.equal(query("#review-workflow").innerHTML, beforePage);
  assert.equal(query("#review-workflow").value, `${workflowId}:1`);
  const latePage = context.loadReviewWorkflows(true);
  context.resetReviewSubmission();
  reply("/v1/studio/workflows?cursor=next-page", { documents: [{ id: workflowId, active_version: 3 }] });
  await latePage;
  assert.doesNotMatch(query("#review-workflow").innerHTML, /Published|v1|v2|v3/);
  assert.equal(requests.length, 0, "expired page responses must not fetch publications for another session");

  const old = context.openTask("old");
  const current = context.openTask("new");
  assert.ok(requests.every((request) => request.options.headers["X-EvoAgent-View"] === "console"));
  assert.ok(query("#create-fix").classList.contains("hidden"));
  reply("/v1/tasks/new", report("new"));
  reply("/v1/tasks/new/workflow", workflow);
  assert.equal(await current, true);
  const currentReport = query("#task-report").textContent;
  const currentWorkflow = query("#task-workflow").innerHTML;
  reply("/v1/tasks/old", report("old"));
  reply("/v1/tasks/old/workflow", { availability: "pruned" });
  assert.equal(await old, false);
  assert.equal(query("#task-report").textContent, currentReport);
  assert.equal(query("#task-workflow").innerHTML, currentWorkflow);

  // Endpoint failures are independent; refresh retries both panes.
  const refresh = query("#refresh").events.click();
  await new Promise(setImmediate);
  reply("/v1/tasks/new", report("new"));
  reply("/v1/tasks/new/workflow", { error: "unavailable" }, 503);
  await refresh;
  assert.match(query("#task-workflow").textContent, /加载失败/);
  assert.equal(query("#task-report").textContent, currentReport);
  assert.equal(query("#task-workflow").attributes["aria-busy"], "false");
  assert.equal(query("#create-fix").classList.contains("hidden"), false);
  assert.match(query("#toast").textContent, /部分数据未能刷新/);

  context.confirmConsoleAction = async () => true; // Dialog behavior is covered by task controls below.
  const fixing = query("#create-fix").events.click();
  await new Promise(setImmediate);
  assert.equal(requests[0].path, "/v1/tasks/new/fix");
  assert.equal(requests[0].options.method, "POST");
  const next = context.openTask("next");
  await query("#create-fix").events.click();
  assert.equal(requests.filter((request) => request.path.endsWith("/fix")).length, 1);
  reply("/v1/tasks/next", { error: "missing" }, 404);
  reply("/v1/tasks/next/workflow", { availability: "not_recorded" });
  assert.equal(await next, false);
  reply("/v1/tasks/new/fix", { branch: "old-task-branch" });
  await fixing;
  assert.match(query("#task-report").textContent, /不存在|无权访问/);
  assert.ok(query("#create-fix").classList.contains("hidden"));

  // Reselecting the same ID or logging out also invalidates earlier reads.
  const first = context.openTask("same");
  const second = context.openTask("same");
  reply("/v1/tasks/same", report("stale"));
  reply("/v1/tasks/same/workflow", workflow);
  assert.equal(await first, false);
  assert.match(query("#task-report").textContent, /正在加载/);
  query("#logout").events.click();
  reply("/v1/tasks/same", report("late"));
  reply("/v1/tasks/same/workflow", workflow);
  assert.equal(await second, false);
  assert.equal(query("#task-report").textContent, "请选择一个任务。");
  assert.equal(query("#task-workflow").textContent, "请选择一个任务。");
  assert.equal(query("#task-workflow").attributes["aria-busy"], "false");
  assert.equal(requests.length, 0);
  vm.runInContext("clearTimeout(toastTimer)", context);
});

test("review submissions recover lost acknowledgements without storing Diff or crossing sessions", async (t) => {
  const receipts = new Map(), pending = [], sent = [];
  let awaitingRequest = null, storageBlocked = false;
  const response = (body, status = 200) => ({ ok: status < 400, status, headers: { get: () => "application/json" }, json: async () => body });
  const nextRequest = () => pending.length ? Promise.resolve(pending.shift()) : new Promise((resolve) => { awaitingRequest = resolve; });
  const boot = () => {
    const nodes = new Map();
    const persisted = new Map([["evoagent_token", "account-a-token"], ["evoagent_role", "admin"]]);
    const element = () => {
      const classes = new Set();
      let html = "";
      return {
        dataset: {}, fields: {}, events: {}, disabled: false,
        get innerHTML() { return html; }, set innerHTML(value) { html = String(value); },
        get textContent() { return html; }, set textContent(value) { html = String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;"); },
        classList: { add: (key) => classes.add(key), remove: (key) => classes.delete(key), contains: (key) => classes.has(key), toggle(key, on) { if (on) classes.add(key); else classes.delete(key); } },
        addEventListener(key, callback) { this.events[key] = callback; }, setAttribute() {},
        querySelector: (selector) => query(selector), querySelectorAll: () => [], reset() { this.fields = {}; }, focus() {},
      };
    };
    const query = (selector) => { if (!nodes.has(selector)) nodes.set(selector, element()); return nodes.get(selector); };
    const crypto = require("node:crypto").webcrypto;
    const context = vm.createContext({
      document: { querySelector: query, querySelectorAll: () => [], createElement: element },
      localStorage: { getItem: (key) => persisted.get(key) ?? null, setItem: (key, value) => persisted.set(key, value), removeItem: (key) => persisted.delete(key) },
      sessionStorage: {
        getItem(key) { if (storageBlocked) throw new Error("storage denied"); return receipts.get(key) ?? null; },
        setItem(key, value) { if (storageBlocked) throw new Error("storage denied"); receipts.set(key, value); },
        removeItem(key) { receipts.delete(key); },
      },
      window: { crypto, addEventListener() {}, scrollTo() {} }, location: { hash: "#review" }, history: { replaceState() {} },
      FormData: class { constructor(form) { return new Map(Object.entries(form.fields)); } },
      TextEncoder, AbortSignal, setTimeout, clearTimeout,
      fetch(path, options) {
        if (path === "/v1/auth/login") return Promise.resolve(response({ access_token: "account-b-token", role: "admin" }));
        if (path !== "/v1/reviews?async=true") return Promise.resolve(response({ stats: {}, tasks: [] }));
        return new Promise((resolve, reject) => {
          const request = { path, options, resolve, reject };
          sent.push(request);
          if (awaitingRequest) { const notify = awaitingRequest; awaitingRequest = null; notify(request); }
          else pending.push(request);
        });
      },
    });
    vm.runInContext(readFileSync(join(__dirname, "../web/app.js"), "utf8"), context);
    t.after(() => vm.runInContext("clearTimeout(toastTimer)", context));
    return { context, query };
  };
  let page = boot();
  const body = { repository: "private/repo", diff: "PRIVATE-DIFF", workflow: { id: "a".repeat(32), version: 1 } };
  const first = page.context.submitReview(body, "studio");
  const lost = await nextRequest(), key = lost.options.headers["Idempotency-Key"];
  assert.match(key, /^[a-f0-9-]{36}$/);
  assert.equal(lost.options.headers["X-EvoAgent-View"], "console");
  assert.ok(lost.options.signal instanceof AbortSignal);
  lost.reject(new TypeError("connection closed after server commit"));
  await assert.rejects(first, /暂时无法确认/);
  const stored = receipts.get("evoagent_pending_trial");
  assert.deepEqual(Object.keys(JSON.parse(stored)).sort(), ["fingerprint", "key"]);
  assert.doesNotMatch(stored, /PRIVATE-DIFF|private\/repo|account-a-token|workflow/);

  page = boot(); // Same tab storage after a reload; no request body was retained.
  const retry = page.context.submitReview(body, "studio");
  const retried = await nextRequest();
  assert.equal(retried.options.headers["Idempotency-Key"], key);
  retried.resolve(response({ task_id: "original", state: "SUCCESS", replayed: true }));
  assert.equal((await retry).task_id, "original");
  assert.equal(receipts.size, 0);
  const again = page.context.submitReview(body, "studio");
  const fresh = await nextRequest();
  assert.notEqual(fresh.options.headers["Idempotency-Key"], key, "a confirmed new run is a new intent");
  fresh.resolve(response({ task_id: "fresh", state: "PENDING", replayed: false }));
  await again;

  const malformed = page.context.submitReview(body, "studio");
  const malformedReply = await nextRequest();
  malformedReply.resolve(response({ unexpected: "not an acknowledgement" }));
  await assert.rejects(malformed, /暂时无法确认/);
  const unavailable = page.context.submitReview(body, "studio");
  const unavailableReply = await nextRequest();
  assert.equal(unavailableReply.options.headers["Idempotency-Key"], malformedReply.options.headers["Idempotency-Key"]);
  unavailableReply.resolve(response({ error: "upstream unavailable" }, 503));
  await assert.rejects(unavailable, /暂时无法确认/);
  const changed = page.context.submitReview({ ...body, workflow: { ...body.workflow, version: 2 } }, "studio");
  const changedRequest = await nextRequest();
  assert.notEqual(changedRequest.options.headers["Idempotency-Key"], malformedReply.options.headers["Idempotency-Key"]);
  changedRequest.resolve(response({ task_id: "version-2", state: "PENDING" }));
  await changed;
  const beforeDeniedStorage = sent.length;
  storageBlocked = true;
  await assert.rejects(page.context.submitReview(body, "manual"), /本次请求尚未发出/);
  storageBlocked = false;
  receipts.set("evoagent_pending_review", "0");
  await assert.rejects(page.context.submitReview(body, "manual"), /本次请求尚未发出/);
  receipts.delete("evoagent_pending_review");
  assert.equal(sent.length, beforeDeniedStorage);

  for (const status of [200, 401]) {
    page = boot();
    const form = page.query("#review-form"), button = page.query('button[type="submit"]');
    form.fields = { repository: "old-account/repo", diff: "old-private-diff", workflow: `${"a".repeat(32)}:2`, title: "Context title", spec: "Required behavior", standards: "No dynamic execution" };
    const oldSubmission = form.events.submit({ preventDefault() {}, currentTarget: form });
    const oldRequest = await nextRequest();
    const oldBody = JSON.parse(oldRequest.options.body);
    assert.deepEqual(oldBody.workflow, { id: "a".repeat(32), version: 2 }, "the manual form must submit the chosen immutable version");
    assert.deepEqual(oldBody.context, { title: "Context title", spec: "Required behavior", standards: "No dynamic execution" });
    await form.events.submit({ preventDefault() {}, currentTarget: form });
    assert.equal(sent.at(-1), oldRequest, "busy forms must not start another request");
    page.query("#logout").events.click();
    assert.deepEqual(form.fields, {});
    const loginForm = page.query("#login-form");
    loginForm.fields = { username: "new-user", password: "new-password" };
    await loginForm.events.submit({ preventDefault() {}, currentTarget: loginForm });
    form.fields = { repository: "new-account/repo", diff: "new-diff" };
    const newSubmission = form.events.submit({ preventDefault() {}, currentTarget: form });
    const newRequest = await nextRequest();
    assert.notEqual(newRequest.options.headers["Idempotency-Key"], oldRequest.options.headers["Idempotency-Key"]);
    oldRequest.resolve(response(status === 200 ? { task_id: "old-task", state: "SUCCESS" } : { error: "old session expired" }, status));
    await oldSubmission;
    assert.equal(button.disabled, true, "a late old response must not re-enable the new submission");
    assert.equal(page.query("#review-result").textContent, "正在提交审查任务…");
    assert.ok(page.query("#login-overlay").classList.contains("hidden"));
    assert.equal(JSON.parse(receipts.get("evoagent_pending_review")).key, newRequest.options.headers["Idempotency-Key"]);
    newRequest.resolve(response({ task_id: "new-task", state: "PENDING" }));
    await newSubmission;
    assert.match(page.query("#review-result").innerHTML, /new-account\/repo/);
    assert.doesNotMatch(page.query("#review-result").innerHTML, /old-account|old-private-diff/);
  }

  let finishDigest;
  page.context.window.crypto = { randomUUID: require("node:crypto").randomUUID, subtle: { digest: () => new Promise((resolve) => { finishDigest = resolve; }) } };
  const digestPending = page.context.submitReview(body, "manual");
  const sentBeforeLogout = sent.length;
  page.query("#logout").events.click();
  finishDigest(new ArrayBuffer(32));
  await assert.rejects(digestPending, /登录状态已变化/);
  assert.equal(sent.length, sentBeforeLogout, "logout during hashing must prevent sending under a new identity");
  assert.equal(receipts.size, 0);
});

test("task controls confirm intent, preserve targets and distinguish review retries from delivery", async (t) => {
  const nodes = new Map(), dialogs = new Set(), requests = [], credentials = new Map();
  let created = 0;
  const element = (key) => {
    const classes = new Set(); let html = "";
    return {
      dataset: {}, events: {}, disabled: false,
      get innerHTML() { return html; }, set innerHTML(value) { html = String(value); },
      get textContent() { return html; }, set textContent(value) { html = String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;"); },
      classList: { add: (name) => classes.add(name), remove: (name) => classes.delete(name), contains: (name) => classes.has(name), toggle(name, on) { if (on) classes.add(name); else classes.delete(name); } },
      addEventListener(name, fn) { this.events[name] = fn; }, setAttribute() {}, reset() {}, focus() {},
      querySelector: (selector) => query(`${key} ${selector}`), querySelectorAll: () => [],
      showModal() { dialogs.add(this); }, close() { dialogs.delete(this); }, remove() { dialogs.delete(this); },
      click() { return this.events.click?.(); },
    };
  };
  const query = (key) => { if (!nodes.has(key)) nodes.set(key, element(key)); return nodes.get(key); };
  const response = (value, status = 200) => ({ ok: status < 400, status, headers: { get: () => "application/json" }, json: async () => value });
  const context = vm.createContext({
    document: {
      querySelector: query,
      querySelectorAll: (selector) => selector === ".studio-confirm [data-cancel]" ? [...dialogs].map((dialog) => dialog.querySelector("[data-cancel]")) : [],
      createElement: (tag) => element(`${tag}-${++created}`), body: { appendChild() {} },
    },
    localStorage: { getItem: (key) => credentials.get(key) ?? "", removeItem: (key) => credentials.delete(key) }, sessionStorage: { removeItem() {} },
    window: { addEventListener() {}, scrollTo() {} }, location: { hash: "#tasks" }, history: { replaceState() {} },
    AbortSignal, setTimeout, clearTimeout,
    fetch: (path, options) => path.startsWith("/v1/tasks/")
      ? new Promise((resolve, reject) => requests.push({ path, options, resolve, reject }))
      : Promise.resolve(response({ tasks: [] })),
  });
  vm.runInContext(readFileSync(join(__dirname, "../web/app.js"), "utf8"), context);
  t.after(() => vm.runInContext("clearTimeout(toastTimer)", context));
  const take = (path) => { const index = requests.findIndex((item) => item.path === path); assert.notEqual(index, -1, path); return requests.splice(index, 1)[0]; };
  const flush = () => new Promise(setImmediate);
  const task = (id, state, extra = {}) => ({ id, repository: id, state, can_resume: state === "FAILED", can_cancel: state === "PENDING", ...extra });
  const read = (id, value) => {
    take(`/v1/tasks/${id}`).resolve(response(value));
    take(`/v1/tasks/${id}/workflow`).resolve(response({ availability: "not_recorded" }));
  };
  const open = async (id, value) => { const pending = context.openTask(id); read(id, value); await pending; };
  const confirm = async () => {
    assert.equal(dialogs.size, 1);
    [...dialogs][0].querySelector("[data-confirm]").click();
    await flush();
  };
  const finish = async (operation, id, value, after) => {
    const request = take(`/v1/tasks/${id}/${operation}`);
    assert.equal(request.options.method, "POST");
    assert.equal(request.options.body, "{}");
    assert.equal(request.options.headers["X-EvoAgent-View"], "console");
    assert.ok(request.options.signal instanceof AbortSignal);
    request.resolve(response(value)); await flush(); read(id, after);
  };
  await open("failed", task("failed", "FAILED"));
  let operation = query("#resume-task").click();
  assert.equal(requests.length, 0, "opening confirmation must not send an operation");
  await query("#resume-task").click();
  assert.equal(dialogs.size, 1, "double-click must not create a second confirmation");
  [...dialogs][0].querySelector("[data-cancel]").click(); await operation;
  assert.equal(requests.length, 0);
  operation = query("#resume-task").click(); await confirm();
  await finish("resume", "failed", { already_active: true, state: "PENDING" }, task("failed", "FAILED", { retrying: true, can_resume: false, can_cancel: true }));
  await operation;
  assert.match(query("#task-control-result").textContent, /未重复派发/);
  assert.match(query("#task-report").innerHTML, /正在重试审查/);
  operation = query("#cancel-task").click(); await confirm();
  await finish("cancel", "failed", { accepted: true, cancel_requested: true }, task("failed", "EXECUTING", { cancel_requested: true }));
  await operation;
  assert.match(query("#task-report").innerHTML, /正在取消审查/);
  assert.ok(query("#task-controls").classList.contains("hidden"));

  await open("delivery", task("delivery", "SUCCESS", { can_resume: true, delivery_pending: true, report: { findings: [] } }));
  assert.equal(query("#resume-task").textContent, "重试结果回写");
  operation = query("#resume-task").click();
  assert.match([...dialogs][0].innerHTML, /只处理已有报告/);
  await confirm();
  await finish("resume", "delivery", { delivery_resumed: true, state: "SUCCESS" }, task("delivery", "SUCCESS", { report: { findings: [] } }));
  await operation;
  assert.match(query("#task-control-result").textContent, /不会重新审查代码/);

  await open("repair", task("repair", "SUCCESS", { report: { findings: [] }, can_fix: true }));
  operation = query("#create-fix").click();
  await query("#create-fix").click();
  assert.equal(dialogs.size, 1);
  assert.match([...dialogs][0].innerHTML, /GitHub.*分支/);
  assert.equal(requests.length, 0, "repair publication requires confirmation");
  [...dialogs][0].querySelector("[data-cancel]").click(); await operation;
  assert.equal(query("#create-fix").disabled, false);
  operation = query("#create-fix").click(); await confirm();
  const oldFix = take("/v1/tasks/repair/fix");
  await open("blocked-repair", task("blocked-repair", "SUCCESS", { report: { findings: [] }, can_fix: false, fix_blocker: "sandbox" }));
  oldFix.resolve(response({ branch: "old-branch" })); await operation;
  assert.equal(query("#create-fix").disabled, true, "old repair finally cannot unlock the current task");
  assert.match(query("#fix-note").textContent, /隔离修复环境/);
  await query("#create-fix").click();
  assert.equal(dialogs.size, 0);
  assert.equal(requests.length, 0);

  credentials.set("evoagent_token", "fixture");
  vm.runInContext("accessToken = observedToken = 'fixture'; currentRole = 'auditor'", context);
  await open("read-only", task("read-only", "FAILED"));
  await query("#resume-task").click();
  assert.equal(dialogs.size, 0);
  assert.equal(requests.length, 0);
  vm.runInContext("currentRole = 'maintainer'", context);
  await open("unconfirmed", task("unconfirmed", "FAILED"));
  operation = query("#resume-task").click(); await confirm();
  take("/v1/tasks/unconfirmed/resume").reject(new TypeError("acknowledgement lost"));
  await operation;
  assert.match(query("#task-control-result").textContent, /先刷新任务状态/);
  assert.equal(requests.length, 0, "unknown acknowledgements must not automatically retry");
  assert.equal(query("#resume-task").disabled, false);

  operation = query("#resume-task").click();
  await open("replacement", task("replacement", "FAILED"));
  await operation;
  assert.equal(dialogs.size, 0, "switching tasks closes the old confirmation");
  assert.equal(requests.length, 0);
  const old = query("#resume-task").click(); await confirm();
  const oldRequest = take("/v1/tasks/replacement/resume");
  await open("new", task("new", "FAILED"));
  operation = query("#resume-task").click(); await confirm();
  oldRequest.resolve(response({ resumed: true, state: "PENDING" })); await old;
  assert.equal(query("#resume-task").disabled, true);
  assert.equal(query("#task-control-result").textContent, "正在提交操作…");
  assert.equal(requests.length, 1, "old acknowledgement must not refresh another selection");
  await finish("resume", "new", { resumed: true, state: "PENDING" }, task("new", "PENDING"));
  await operation;

  operation = query("#cancel-task").click(); await confirm();
  const pendingCancel = take("/v1/tasks/new/cancel");
  query("#logout").click();
  pendingCancel.resolve(response({ accepted: true, cancel_requested: true }));
  await operation;
  assert.equal(query("#task-control-result").textContent, "");
  assert.ok(query("#task-controls").classList.contains("hidden"));
  assert.equal(requests.length, 0, "logout invalidates operation follow-up reads");
});

test("the whole console clears private data and ignores reads, writes and logins from an expired session", async (t) => {
  const nodes = new Map(), requests = [], redirects = [], events = {};
  const stored = new Map([["evoagent_token", "token-a"], ["evoagent_role", "platform_admin"]]);
  const element = (key) => {
    const classes = new Set();
    let html = "";
    return {
      fields: {}, events: {}, dataset: {}, disabled: false, inert: false,
      get innerHTML() { return html; }, set innerHTML(value) { html = String(value); },
      get textContent() { return html; }, set textContent(value) { html = String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;"); },
      classList: { add: (name) => classes.add(name), remove: (name) => classes.delete(name), contains: (name) => classes.has(name), toggle(name, on) { if (on) classes.add(name); else classes.delete(name); } },
      addEventListener(name, fn) { this.events[name] = fn; }, setAttribute() {}, focus() {},
      querySelector: (selector) => query(`${key} ${selector}`), querySelectorAll: () => [], reset() { this.fields = {}; },
    };
  };
  const query = (key) => { if (!nodes.has(key)) nodes.set(key, element(key)); return nodes.get(key); };
  const localStorage = { getItem: (key) => stored.get(key) ?? null, setItem: (key, value) => stored.set(key, value), removeItem: (key) => stored.delete(key) };
  const context = vm.createContext({
    document: { querySelector: query, querySelectorAll: () => [], createElement: () => element("") },
    localStorage, sessionStorage: { removeItem() {} }, location: { hash: "#overview" }, history: { replaceState() {} },
    window: { addEventListener(name, fn) { events[name] = fn; }, scrollTo() {}, location: { assign: (url) => redirects.push(url) } },
    FormData: class { constructor(form) { return new Map(Object.entries(form.fields)); } }, setTimeout, clearTimeout,
    fetch: (path, options) => new Promise((resolve, reject) => requests.push({ path, options, resolve, reject })),
  });
  vm.runInContext(readFileSync(join(__dirname, "../web/app.js"), "utf8"), context);
  t.after(() => vm.runInContext("clearTimeout(toastTimer)", context));
  const take = (path) => {
    const index = requests.findIndex((request) => request.path === path);
    assert.notEqual(index, -1, `pending request: ${path}`);
    return requests.splice(index, 1)[0];
  };
  const reply = (request, value, status = 200) => request.resolve({ ok: status < 400, status, headers: { get: () => "application/json" }, json: async () => value });
  const flush = () => new Promise(setImmediate);
  const payload = (name) => ({
    capabilities: { role: "platform_admin", review: true, manage: true, audit: true, platform: true, github_install_configured: true }, ready: true,
    stats: {}, tasks: [{ id: name, repository: name, state: "SUCCESS" }],
    skills: [{ name, description: name, version: 1 }], cases: [{ category: name }], runs: [],
    decision: "deferred", version: { version: name === "old-tenant-private" ? 17 : 29 },
    url: `https://example.invalid/${name}`,
  });
  const submit = (id) => query(id).events.submit({ preventDefault() {}, currentTarget: query(id) });
  for (const key of ['#review-form button[type="submit"]', '#evolution-form button[type="submit"]', '#install-github', '#auto-evolve']) assert.equal(query(key).disabled, true, `${key} fails closed before status is known`);
  await Promise.all([submit("#review-form"), submit("#evolution-form"), query("#install-github").events.click(), query("#auto-evolve").events.click()]);
  assert.equal(requests.length, 1, "unknown capability state must never trigger a POST");
  const initialDashboard = take("/api/dashboard");
  const deniedDashboard = context.loadDashboard();
  reply(take("/api/dashboard"), { capabilities: { role: "auditor", review: false, manage: false, audit: true, platform: false } });
  await deniedDashboard;
  const auditOnly = context.loadOperations();
  reply(take("/api/audit?limit=50"), { events: [{ actor: "reader", action: "task.resume", resource: "task-a", created_at: "2026-08-30T00:00:00Z" }] });
  await auditOnly;
  assert.match(query("#audit-list").innerHTML, /续跑任务.*reader.*task-a/s);
  assert.match(query("#outbox-list").textContent, /仅管理员/);
  assert.equal(requests.length, 0, "an auditor must not request capacity or delivery internals");
  reply(initialDashboard, payload("old-tenant-private"));
  await flush();
  assert.equal(query('#review-form button[type="submit"]').disabled, true, "late capabilities cannot undo newer restrictions");
  assert.match(query("#review-access-note").textContent, /仅可查看/);
  assert.match(query("#github-status").textContent, /管理员权限/);
  const unconfigured = context.loadDashboard();
  reply(take("/api/dashboard"), { capabilities: { ...payload("").capabilities, github_install_configured: false } });
  await unconfigured;
  assert.equal(query("#install-github").disabled, true);
  assert.match(query("#github-status").textContent, /尚未完成.*配置/);
  context.setButtonBusy(query("#install-github"), true, "working");
  context.setButtonBusy(query("#install-github"), false);
  assert.equal(query("#install-github").disabled, true, "finishing a request cannot unlock an unavailable action");
  const unavailable = context.loadFailures();
  for (const request of requests.splice(0)) reply(request, { ready: false });
  await unavailable;
  assert.equal(query("#auto-evolve").disabled, true);
  assert.equal(query('#evolution-form button[type="submit"]').disabled, true);
  const oldReady = context.loadFailures(), oldReadyRequests = requests.splice(0);
  const failedReady = context.loadFailures();
  for (const request of requests.splice(0)) reply(request, { error_code: "unavailable" }, 503);
  await failedReady;
  for (const request of oldReadyRequests) reply(request, payload("old-tenant-private"));
  await oldReady;
  assert.equal(query("#auto-evolve").disabled, true, "late ready responses cannot erase a newer read failure");
  const configured = context.loadDashboard();
  reply(take("/api/dashboard"), payload("old-tenant-private"));
  await configured;
  assert.match(query("#recent-tasks").textContent, /old-tenant-private/);
  const operations = context.loadOperations();
  reply(take("/api/audit?limit=50"), { events: [{ actor: "admin", action: "outbox.replay", resource: "review:task-1", created_at: "2026-08-30T00:00:00Z" }] });
  reply(take("/api/outbox?status=dead&limit=50"), { messages: [{ id: "review:task-1", attempts: 5, updated_at: "2026-08-30T00:00:00Z" }] });
  reply(take("/api/queue/dead-letters?limit=50"), { messages: [{ task_id: "task-2", attempt: 3, failed_at: 1 }] });
  reply(take("/api/tenant-review-capacity"), { enabled: true, max_active_reviews: 4, active_reviews: 2, available: 2, saturated: false });
  assert.equal(await operations, true);
  assert.match(query("#operations-stats").innerHTML, /活跃审查.*2.*容量上限.*4.*结果回写死信.*1.*任务执行死信.*1/s);
  assert.match(query("#outbox-list").innerHTML, /review:task-1.*重新派发/s);
  assert.match(query("#queue-dead-list").innerHTML, /task-2.*查看任务/s);
  query("#evolution-form").fields = { skill_name: "old-skill", prompt: "old-private-prompt" };
  query("#login-form").fields = { username: "old-user", password: "old-password" };
  const ready = context.loadFailures();
  for (const request of requests.splice(0)) reply(request, payload("old-tenant-private"));
  await ready;
  const oldWrites = [
    query("#install-github").events.click(),
    query("#auto-evolve").events.click(),
    query("#evolution-form").events.submit({ preventDefault() {}, currentTarget: query("#evolution-form") }),
  ];
  const oldReads = [context.loadDashboard(), context.loadTasks(), context.loadSkills(), context.loadFailures()];
  const oldRequests = requests.splice(0);
  query("#logout").events.click();
  for (const key of ["#stats", "#recent-tasks", "#all-tasks", "#skill-list", "#evolution-status", "#failure-list", "#operations-stats", "#audit-list", "#outbox-list", "#queue-dead-list", "#evolution-result", "#toast"]) {
    assert.doesNotMatch(query(key).textContent, /old-tenant-private/, `${key} clears synchronously on logout`);
  }
  assert.deepEqual(query("#evolution-form").fields, {});
  assert.deepEqual(query("#login-form").fields, {});
  assert.equal(query(".app-shell").inert, true);
  const loginForm = query("#login-form");
  loginForm.fields = { username: "new-user", password: "new-password" };
  const login = loginForm.events.submit({ preventDefault() {}, currentTarget: loginForm });
  reply(take("/v1/auth/login"), { access_token: "token-b", role: "platform_admin" });
  await flush();
  reply(take("/api/dashboard"), payload("new-tenant-public"));
  await login;
  await flush();
  assert.equal(query(".app-shell").inert, false);
  const newMarkup = query("#recent-tasks").innerHTML;
  const newReady = context.loadFailures();
  for (const request of requests.splice(0)) reply(request, payload("new-tenant-public"));
  await newReady;
  const newWrite = query("#auto-evolve").events.click();
  const newRequest = take("/v1/evolution/auto");
  for (const request of oldRequests) reply(request, payload("old-tenant-private"));
  await Promise.all([...oldReads, ...oldWrites]);
  assert.equal(query("#recent-tasks").innerHTML, newMarkup);
  for (const key of ["#all-tasks", "#skill-list", "#failure-list", "#evolution-result", "#toast"]) assert.doesNotMatch(query(key).textContent, /old-tenant-private|版本 17/);
  assert.equal(query("#auto-evolve").disabled, true, "old writes must not unlock a new session's action");
  assert.equal(redirects.length, 0, "a late installation response must not redirect a different account");
  assert.equal(requests.length, 0, "stale writes must not start follow-up reads as the new account");
  reply(newRequest, payload("new-tenant-public"));
  await newWrite;
  for (const request of requests.splice(0)) reply(request, payload("new-tenant-public"));
  await flush();
  assert.match(query("#evolution-result").textContent, /版本 29/);

  const beginLogin = () => {
    loginForm.fields = { username: "next-user", password: "next-password" };
    return loginForm.events.submit({ preventDefault() {}, currentTarget: loginForm });
  };
  const signIn = async (token) => {
    const attempt = beginLogin();
    reply(take("/v1/auth/login"), { access_token: token, role: "platform_admin" });
    await attempt;
    reply(take("/api/dashboard"), payload("new-tenant-public"));
    await flush();
  };
  const failedReads = [context.loadDashboard(), context.loadTasks(), context.loadSkills(), context.loadFailures(), query("#refresh").events.click()];
  const failedRequests = requests.splice(0);
  query("#logout").events.click();
  const currentLogin = beginLogin(), currentLoginRequest = take("/v1/auth/login");
  failedRequests.forEach((request, index) => {
    if (index % 2) request.reject(new Error("old-tenant-private transport failure"));
    else reply(request, { error: "old-tenant-private failure" }, index === 0 ? 401 : 503);
  });
  await Promise.all(failedReads);
  assert.equal(query('#login-form button[type="submit"]').disabled, true);
  assert.equal(loginForm.fields.username, "next-user", "late 401s must not reset an in-flight login");
  assert.equal(query("#toast").textContent, "");
  reply(currentLoginRequest, { access_token: "token-c", role: "platform_admin" });
  await currentLogin;
  reply(take("/api/dashboard"), payload("new-tenant-public"));
  await flush();

  const lateSkill = context.loadSkills(), skillRequest = take("/api/skills");
  const expired = context.loadTasks();
  take("/api/tasks").resolve({
    ok: false, status: 401, headers: { get: () => "application/json" },
    json: async () => { throw new SyntaxError("private-expired-response"); },
  });
  assert.equal(await expired, false);
  assert.equal(stored.has("evoagent_token"), false);
  assert.equal(query(".app-shell").inert, true);
  assert.match(query("#login-error").textContent, /登录已失效/);
  reply(skillRequest, payload("old-tenant-private"));
  await lateSkill;
  assert.doesNotMatch(query("#skill-list").textContent, /old-tenant-private/);
  await assert.rejects(context.api("/v1/evolution/auto", { method: "POST" }), /重新登录/);
  assert.equal(requests.length, 0, "a signed-out tab must not fall back to an anonymous write");
  const incorrect = beginLogin();
  reply(take("/v1/auth/login"), { error: "incorrect password" }, 401);
  await incorrect;
  assert.equal(loginForm.fields.username, "next-user");
  assert.match(query("#login-error").textContent, /登录未成功.*账号.*密码/);

  const abandoned = beginLogin(), abandonedRequest = take("/v1/auth/login");
  stored.set("evoagent_token", "other-tab-token");
  events.storage({ storageArea: localStorage, key: "evoagent_token" });
  assert.deepEqual(loginForm.fields, {});
  assert.match(query("#login-error").textContent, /其他页面/);
  reply(abandonedRequest, { access_token: "abandoned-token", role: "platform_admin" });
  await abandoned;
  assert.equal(stored.get("evoagent_token"), "other-tab-token", "late login success must not overwrite a different tab's account");
  assert.equal(query(".app-shell").inert, true);
  assert.equal(requests.length, 0);

  for (const event of ["focus", "pageshow", "storage"]) {
    await signIn(`fresh-${event}`);
    events.storage({ storageArea: localStorage, key: "unrelated-preference" });
    assert.equal(query(".app-shell").inert, false);
    stored.clear();
    events[event]({ storageArea: localStorage, key: null });
    assert.equal(query(".app-shell").inert, true, `${event} detects shared sign-out`);
    assert.doesNotMatch(query("#recent-tasks").textContent, /new-tenant-public/);
  }
  await signIn("last-session");
  const racing = context.loadDashboard(), racingRequest = take("/api/dashboard");
  stored.set("evoagent_token", "newer-account-token"); // Before the storage event is dispatched.
  reply(racingRequest, { error: "old session expired" }, 401);
  await racing;
  assert.equal(stored.get("evoagent_token"), "newer-account-token");
  assert.equal(query(".app-shell").inert, true);
  assert.equal(requests.length, 0);
  await signIn("permission-check");
  const forbidden = context.api("/v1/evolution/status");
  reply(take("/v1/evolution/status"), { error: "permission denied" }, 403);
  await assert.rejects(forbidden, /权限.*策略/);
  assert.equal(stored.get("evoagent_token"), "permission-check", "permission denial is not session expiry");
  assert.equal(query(".app-shell").inert, false);
});

test("repository governance reads safe output, sends CAS versions and preserves stale edits", async (t) => {
  const nodes = new Map(), requests = [], credentials = new Map([
    ["evoagent_token", "admin-token"], ["evoagent_role", "platform_admin"],
  ]);
  let policyInputs = [];
  const element = (key) => {
    const classes = new Set(); let html = "";
    return {
      value: "", checked: false, fields: {}, events: {}, dataset: {}, disabled: false, inert: false,
      get innerHTML() { return html; }, set innerHTML(value) { html = String(value); },
      get textContent() { return html; }, set textContent(value) { html = String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;"); },
      classList: { add: (name) => classes.add(name), remove: (name) => classes.delete(name), contains: (name) => classes.has(name), toggle(name, on) { if (on) classes.add(name); else classes.delete(name); } },
      addEventListener(name, fn) { this.events[name] = fn; }, setAttribute() {}, focus() {}, reset() { this.fields = {}; },
      querySelector: (selector) => query(`${key} ${selector}`),
      querySelectorAll: (selector) => selector.startsWith("[data-policy-group=")
        ? policyInputs.filter((input) => selector.includes(input.dataset.policyGroup)) : [],
    };
  };
  const query = (key) => { if (!nodes.has(key)) nodes.set(key, element(key)); return nodes.get(key); };
  const response = (value, status = 200) => ({ ok: status < 400, status, headers: { get: () => "application/json" }, json: async () => value });
  const context = vm.createContext({
    document: { querySelector: query, querySelectorAll: () => [], createElement: () => element("created") },
    localStorage: { getItem: (key) => credentials.get(key) ?? "", setItem: (key, value) => credentials.set(key, value), removeItem: (key) => credentials.delete(key) },
    sessionStorage: { removeItem() {} }, location: { hash: "#governance" }, history: { replaceState() {} },
    window: { addEventListener() {}, scrollTo() {}, location: { assign() {} } },
    FormData: class { constructor(form) { return new Map(Object.entries(form.fields)); } },
    AbortSignal, setTimeout, clearTimeout,
    fetch: (path, options) => new Promise((resolve, reject) => requests.push({ path, options, resolve, reject })),
  });
  vm.runInContext(readFileSync(join(__dirname, "../web/app.js"), "utf8"), context);
  vm.runInContext("confirmConsoleAction = async (_message, current) => current()", context);
  t.after(() => vm.runInContext("clearTimeout(toastTimer)", context));
  const take = (path) => {
    const index = requests.findIndex((request) => request.path === path);
    assert.notEqual(index, -1, `pending request: ${path}`);
    return requests.splice(index, 1)[0];
  };
  const reply = (request, value, status = 200) => request.resolve(response(value, status));
  const flush = () => new Promise(setImmediate);
  const policy = (version, enabled = true) => ({
    repository: "org/repo", version, source: version ? "configured" : "legacy-grant",
    policy: {
      enabled, auto_fix: false, post_review_comments: true,
      allowed_reviewers: ["review", "retired<img>"], allowed_fix_rules: ["SEC-YAML-LOAD"],
      allowed_llm_providers: [], allowed_llm_models: [], llm_region: null, max_diff_bytes: 4096,
    },
    history: version ? [{ version, actor: "admin<script>", created_at: "2026-08-30T00:00:00Z", policy: { secret: "private-history" } }] : [],
    available_reviewers: ["review"], available_fix_rules: ["SEC-YAML-LOAD"],
    tenant_id: "private-tenant", metadata: { secret: "private-metadata" },
  });

  reply(take("/api/dashboard"), { capabilities: { role: "platform_admin", review: true, manage: true, audit: true, platform: true }, stats: {}, tasks: [] });
  await flush();
  const repository = query("#policy-repository");
  repository.value = "ORG/Repo";
  repository.events.input();
  let pending = query("#policy-load").events.click();
  reply(take("/v1/repository-policies?repository=org%2Frepo"), policy(1));
  assert.equal(await pending, true);
  assert.match(query("#policy-summary").innerHTML, /org\/repo.*已配置策略.*4,096 字节/s);
  assert.match(query("#policy-reviewers").innerHTML, /retired&lt;img&gt;.*当前部署已不可用/s);
  assert.match(query("#policy-history").innerHTML, /admin&lt;script&gt;/);
  for (const target of ["#policy-summary", "#policy-reviewers", "#policy-history"]) {
    assert.doesNotMatch(query(target).innerHTML, /private-|<img>|<script>|\{"/);
  }

  const form = query("#policy-form");
  query('#policy-form [name="enabled"]').checked = false;
  query('#policy-form [name="auto_fix"]').checked = true;
  query('#policy-form [name="post_review_comments"]').checked = false;
  query('#policy-form [name="max_diff_bytes"]').value = "8192";
  query('#policy-form [name="allowed_llm_providers"]').value = "local, local, openai";
  query('#policy-form [name="allowed_llm_models"]').value = "model-a";
  query('#policy-form [name="llm_region"]').value = "cn-north-1";
  policyInputs = [
    { checked: true, dataset: { policyGroup: "allowed_reviewers", policyValue: encodeURIComponent("review") } },
    { checked: true, dataset: { policyGroup: "allowed_fix_rules", policyValue: encodeURIComponent("SEC-YAML-LOAD") } },
  ];
  pending = form.events.submit({ preventDefault() {}, currentTarget: form });
  await flush();
  let write = take("/v1/repository-policies");
  const body = JSON.parse(write.options.body);
  assert.equal(body.expected_version, 1);
  assert.deepEqual(body.policy.allowed_llm_providers, ["local", "openai"]);
  assert.deepEqual(body.policy.allowed_reviewers, ["review"]);
  reply(write, { error_code: "policy_conflict" }, 409);
  await pending;
  assert.match(query("#policy-result").textContent, /其他管理员.*(?:未|没有)保存.*重新读取/);
  assert.equal(query("#policy-fields").disabled, true);
  assert.equal(query('#policy-form [name="max_diff_bytes"]').value, "8192", "conflict must preserve the visible edit");

  pending = query("#policy-load").events.click();
  reply(take("/v1/repository-policies?repository=org%2Frepo"), policy(2));
  await pending;
  query('#policy-form [name="max_diff_bytes"]').value = "9000";
  pending = form.events.submit({ preventDefault() {}, currentTarget: form });
  await flush();
  write = take("/v1/repository-policies");
  assert.equal(JSON.parse(write.options.body).expected_version, 2);
  reply(write, { repository: "org/repo", version: 3, policy: {} }, 201);
  await flush();
  reply(take("/v1/repository-policies?repository=org%2Frepo"), policy(3, false));
  await pending;
  assert.match(query("#policy-result").textContent, /v3 已保存/);
  assert.match(query("#policy-summary").innerHTML, /审查<\/b><span>已关闭/);
  assert.equal(requests.length, 0);

  query("#logout").events.click();
  assert.match(query("#policy-summary").textContent, /读取后展示/);
  assert.doesNotMatch(query("#policy-summary").textContent, /org\/repo|private/);
  assert.equal(query("#policy-fields").disabled, true);
});

test("Proof displays evidence stages without leaking metadata or treating incomplete results as L4", () => {
  const escapeHtml = (value) => String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
  const context = vm.createContext({ $: () => null, escapeHtml, artifactValueMarkup: escapeHtml });
  vm.runInContext(readFileSync(join(__dirname, "../web/proof.js"), "utf8"), context);
  const payload = context.proofPayload([
    { path: "removed.py", original: "old", patched: null },
    { path: "added.py", original: null, patched: "new" },
    { path: "__proto__", original: "before", patched: "after" },
  ], "test", "regression");
  assert.deepEqual(JSON.parse(JSON.stringify(payload)), JSON.parse('{"original":{"removed.py":"old","__proto__":"before"},"patched":{"added.py":"new","__proto__":"after"},"reproduction_command":"test","regression_command":"regression"}'));
  for (const files of [[], Array(33).fill({}), [{ path: "a", original: null, patched: null }], [{ path: "", original: "", patched: "" }], [{ path: "a" }, { path: "a" }]]) {
    assert.throws(() => context.proofPayload(files, "test", ""));
  }
  assert.throws(() => context.proofPayload([{ path: "a", original: "", patched: "" }], " ", ""));
  const steps = ["reproduce-on-original", "reproduce-on-patched", "regression-on-patched"].map((step, index) => ({ step, status: index ? "passed" : "failed", duration_seconds: 0.1, detail: "<script>untrusted()</script>", attestation: { request_sha256: "private-fingerprint" } }));
  for (let level = 1; level <= 4; level++) {
    const html = context.proofMarkup({ evidence_level: level, steps: steps.slice(0, level - 1), patch: "<img>" });
    assert.match(html, new RegExp(`L${level}`));
    assert.doesNotMatch(html, /<script>|<img>|private-fingerprint|attestation/);
    if (level > 1) assert.match(html, /state-success\">测试失败/, "failing on the original is expected evidence");
  }
  for (const invalid of [{ evidence_level: 4, steps: [] }, { evidence_level: 1, steps: [steps[1]] }, { evidence_level: 1, steps: [{ ...steps[0], status: "passed" }, steps[1]] }]) assert.throws(() => context.proofMarkup(invalid));
  const inconclusive = context.proofMarkup({ evidence_level: 1, steps: [{ ...steps[0], status: "error", detail: "private-diagnostic" }] });
  assert.match(inconclusive, /无法判断/);
  assert.doesNotMatch(inconclusive, /private-diagnostic|state-success/);
});

test("Proof submits once, invalidates edited evidence and discards results after sign-out", async () => {
  const nodes = new Map(), requests = [];
  const element = () => ({
    children: [], fields: new Map(), events: {}, value: "", checked: true, disabled: false, textContent: "", innerHTML: "",
    classList: { add() {}, remove() {} }, reset() {}, focus() {}, setAttribute() {},
    addEventListener(name, fn) { this.events[name] = fn; },
    appendChild(child) { this.children.push(child); }, replaceChildren() { this.children = []; },
  });
  const query = (key, root) => {
    const collection = root ? root.fields : nodes;
    if (!collection.has(key)) collection.set(key, element());
    return collection.get(key);
  };
  const context = vm.createContext({
    $: query, $$: (_key, root) => root.children, window: {}, document: { createElement: element },
    authEpoch: 0, consoleCapabilities: { proof: true }, escapeHtml: String, artifactValueMarkup: String,
    setButtonAvailable: (button, available) => { button.disabled = !available; }, setButtonBusy() {}, toast() {},
    confirmConsoleAction: async (_text, current) => current(),
    api: (path, options) => new Promise((resolve, reject) => requests.push({ path, options, resolve, reject })),
  });
  vm.runInContext(readFileSync(join(__dirname, "../web/proof.js"), "utf8"), context);
  const form = query("#proof-form"), files = query("#proof-files"), output = query("#proof-result");
  const fill = () => {
    query("[data-proof-path]", files.children[0]).value = "app.py";
    query('[data-proof-content="original"]', files.children[0]).value = "before";
    query('[data-proof-content="patched"]', files.children[0]).value = "after";
    query('[name="reproduction_command"]', form).value = "test";
  };
  fill();
  let pending = form.events.submit({ preventDefault() {} });
  await form.events.submit({ preventDefault() {} });
  assert.equal(requests.length, 1, "double submit cannot launch a second job");
  assert.equal(query("#proof-fields").disabled, true);
  assert.equal(requests[0].path, "/v1/proofs");
  assert.equal(requests[0].options.method, "POST");
  assert.deepEqual(JSON.parse(requests[0].options.body).patched, { "app.py": "after" });
  requests.shift().resolve({ evidence_level: 1, steps: [] }); await pending;
  assert.match(output.innerHTML, /L1/);
  assert.equal(query("#proof-fields").disabled, false);
  form.events.input();
  assert.match(output.textContent, /旧结果已清除/);
  pending = form.events.submit({ preventDefault() {} });
  const old = requests.shift();
  context.authEpoch++; context.consoleCapabilities = null;
  context.window.proof.reset();
  const resetOutput = output.innerHTML;
  old.resolve({ evidence_level: 1, steps: [] }); await pending;
  assert.equal(output.innerHTML, resetOutput, "an old account cannot restore its result");
  assert.equal(query("#proof-submit").disabled, true);
  assert.equal(files.children.length, 1);
  assert.equal(query("[data-proof-path]", files.children[0]).value, "");
  assert.equal(requests.length, 0);
});
