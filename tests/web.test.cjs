const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

test("handoffs escape metadata and keep report, workflow and fix responses tied to the current task", async () => {
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
    };
  };
  const elements = new Map();
  const query = (selector) => {
    if (!elements.has(selector)) elements.set(selector, element());
    return elements.get(selector);
  };
  const requests = [];
  const response = (data, status = 200) => ({
    ok: status < 400, status, headers: { get: () => "application/json" }, json: async () => data,
  });
  const context = vm.createContext({
    document: { querySelector: query, querySelectorAll: () => [], createElement: element },
    localStorage: { getItem: () => "", setItem() {}, removeItem() {} },
    location: { hash: "#tasks" }, history: { replaceState() {} },
    window: { addEventListener() {}, scrollTo() {} }, setTimeout, clearTimeout,
    fetch(path, options) {
      if (!path.startsWith("/v1/tasks/")) return Promise.resolve(response({}));
      return new Promise((resolve) => requests.push({ path, options, resolve }));
    },
  });
  vm.runInContext(readFileSync(join(__dirname, "../web/app.js"), "utf8"), context);
  const reply = (path, data, status) => {
    const index = requests.findIndex((request) => request.path === path);
    assert.notEqual(index, -1, `request exists: ${path}`);
    requests.splice(index, 1)[0].resolve(response(data, status));
  };
  const report = (id) => ({ id, report: {}, pull_request: 1, input: { head_sha: "test" } });
  const workflow = {
    availability: "recorded", task_state: "CANCELLED", workflow: { name: "policy <script>" },
    steps: ["completed", "running", "failed", "pending"].map((status, index) => ({
      id: `step${index}`, agent_id: "<img src=x>", status, attempt: 2,
      inputs: { findings: "findings@1" }, sources: { findings: "upstream.findings" },
      outputs: { findings: "findings@1" }, blocked_by: status === "pending" ? ["step2"] : [],
      error: status === "failed" ? "ref <script>" : null,
      private_payload: "must not render arbitrary fields",
    })),
  };
  const markup = context.workflowMarkup(workflow);
  assert.match(markup, /1 \/ 4 个节点完成/);
  for (const expected of ["任务：已取消", "不代表 Worker 在线", "已派发", "等待上游：step2", "findings@1", "upstream.findings", "尝试 2 次", "&lt;script&gt;", "&lt;img src=x&gt;"]) {
    assert.ok(markup.includes(expected), expected);
  }
  assert.doesNotMatch(markup, /<script>|<img|must not render/);
  assert.match(context.workflowMarkup({ availability: "pruned" }), /保留策略清理/);
  assert.match(context.workflowMarkup({ availability: "not_recorded" }), /尚无交接记录/);
  assert.throws(() => context.workflowMarkup({ availability: "invalid" }), /格式异常/);

  const old = context.openTask("old");
  const current = context.openTask("new");
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
  reply("/v1/tasks/new", report("new"));
  reply("/v1/tasks/new/workflow", { error: "unavailable" }, 503);
  await refresh;
  assert.match(query("#task-workflow").textContent, /加载失败/);
  assert.equal(query("#task-report").textContent, currentReport);
  assert.equal(query("#task-workflow").attributes["aria-busy"], "false");
  assert.equal(query("#create-fix").classList.contains("hidden"), false);
  assert.match(query("#toast").textContent, /部分数据未能刷新/);

  const fixing = query("#create-fix").events.click();
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
  assert.equal(query("#task-report").textContent, "missing");
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
