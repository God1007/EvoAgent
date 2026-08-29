function proofPayload(entries, reproduction, regression) {
  if (!entries.length || entries.length > 32) throw new Error("请提供 1–32 个文件。");
  const paths = new Set(), original = [], patched = [];
  for (const file of entries) {
    if (!file.path || paths.has(file.path)) throw new Error("文件路径不能为空或重复。");
    if (file.original === null && file.patched === null) throw new Error("文件至少需要存在于一个版本中。");
    paths.add(file.path);
    if (file.original !== null) original.push([file.path, file.original]);
    if (file.patched !== null) patched.push([file.path, file.patched]);
  }
  if (!reproduction.trim()) throw new Error("请填写问题复现命令。");
  return { original: Object.fromEntries(original), patched: Object.fromEntries(patched), reproduction_command: reproduction, regression_command: regression };
}

function proofMarkup(result) {
  const level = result.evidence_level;
  if (![1, 2, 3, 4].includes(level) || !Array.isArray(result.steps)) throw new Error("验证结果格式异常，请重试或联系管理员。");
  const labels = { 1: "尚未证实问题", 2: "问题已复现，修复尚未证实", 3: "修复已验证，回归尚未通过", 4: "修复与回归均已通过" };
  const steps = [["reproduce-on-original", "修复前复现", "failed"], ["reproduce-on-patched", "修复后复现", "passed"], ["regression-on-patched", "修复后回归", "passed"]];
  let measured = 1;
  if (result.steps.length > 3 || result.steps.some((item, index) => item.step !== steps[index][0] || !["passed", "failed", "timeout", "error"].includes(item.status) || (index < result.steps.length - 1 && item.status !== steps[index][2]))) throw new Error("验证步骤不完整，不能确认结论。");
  for (let index = 0; index < result.steps.length; index++) {
    if (result.steps[index].status !== steps[index][2]) break;
    measured++;
  }
  if (level !== measured) throw new Error("验证结论与执行步骤不一致，不能作为证据。");
  const details = steps.map(([key, title, expected], index) => {
    const step = result.steps.find((item) => item.step === key);
    const status = step?.status, confirmed = status === expected;
    const message = ({ passed: "测试通过", failed: "测试失败", timeout: "执行超时，无法判断", error: "执行未完成，无法判断" })[status] || "未执行";
    const note = key === "reproduce-on-original" ? "此步需要测试失败，才能确认问题确实存在。" : key === "regression-on-patched" ? "需要填写回归命令，且测试通过，才可达到 L4。" : "必须使用与修复前相同的复现命令。";
    return `<article class="proof-step"><span class="proof-step-number">0${index + 1}</span><h4>${title}</h4><span class="status ${confirmed ? "state-success" : "status-neutral"}">${message}</span><p class="workflow-note">${note}</p>${step && Number.isFinite(step.duration_seconds) ? `<p class="workflow-note">耗时 ${step.duration_seconds.toFixed(2)} 秒</p>` : ""}${step?.detail && ["passed", "failed"].includes(status) ? `<details><summary>查看测试输出</summary><pre class="evidence-code"><code>${escapeHtml(step.detail)}</code></pre></details>` : ""}</article>`;
  }).join("");
  return `<div class="proof-verdict"><span class="status ${level === 4 ? "state-success" : "status-neutral"}">L${level}</span><h4>${labels[level]}</h4><p>仅验证本次提交的文件和命令，不代表整个项目无缺陷；不会自动修改 GitHub。</p></div><div class="proof-steps">${details}</div>${result.patch ? `<details class="proof-patch"><summary>查看本次代码差异</summary>${artifactValueMarkup(result.patch, "unified-diff@1")}</details>` : ""}`;
}

(() => {
  const form = $("#proof-form");
  if (!form) return;
  const list = $("#proof-files"), target = $("#proof-result"), fields = $("#proof-fields"), submit = $("#proof-submit");
  let generation = 0, running = false;

  function capabilities(caps) {
    setButtonAvailable(submit, caps?.proof === true && !running);
    $("#proof-example").disabled = running;
    $("#proof-access-note").textContent = !caps ? "正在确认执行环境与权限，请登录或刷新。" : caps.proof ? "隔离执行器已连接。依次检查复现、修复及回归；执行器不可用时不会回退到宿主机。" : "当前账号没有修复权限，或隔离执行器尚未配置/暂不可用。可以编辑文件，但不能执行。";
  }

  function addFile(file = { path: "", original: "", patched: "" }) {
    if (list.children.length >= 32) { toast("编辑器最多支持 32 个文件。"); return; }
    const row = document.createElement("div");
    row.className = "proof-file";
    row.innerHTML = `<div class="proof-file-head"><label>文件相对路径<input data-proof-path value="${escapeHtml(file.path)}" placeholder="app.py" maxlength="500" required></label><button class="link-button" type="button" data-proof-remove>移除此文件</button></div><div class="proof-editors">${[["original", "修复前"], ["patched", "修复后"]].map(([key, label]) => `<div><label class="proof-file-toggle"><input type="checkbox" data-proof-exists="${key}" ${file[key] === null ? "" : "checked"}>${label}存在此文件</label><textarea data-proof-content="${key}" aria-label="${label}代码" rows="8" spellcheck="false" ${file[key] === null ? "disabled" : ""}>${escapeHtml(file[key] || "")}</textarea></div>`).join("")}</div>`;
    list.appendChild(row);
    return row;
  }

  function reset() {
    generation++; running = false;
    form.reset(); fields.disabled = false; list.replaceChildren(); addFile();
    setButtonBusy(submit, false);
    target.classList.add("empty"); target.textContent = "运行后展示复现、修复与回归结果。";
    target.setAttribute("aria-busy", "false");
    capabilities(consoleCapabilities);
  }

  function invalidate() {
    if (running) return;
    generation++;
    target.classList.add("empty"); target.textContent = "输入已变化，请重新验证；旧结果已清除。";
  }

  $("#proof-add-file").addEventListener("click", () => { if (!running) { invalidate(); $("[data-proof-path]", addFile() || list.lastElementChild)?.focus(); } });
  list.addEventListener("click", (event) => {
    if (running || !event.target.closest("[data-proof-remove]")) return;
    event.target.closest(".proof-file").remove();
    invalidate();
    $("#proof-add-file").focus();
  });
  form.addEventListener("input", invalidate);
  form.addEventListener("change", invalidate);
  list.addEventListener("change", (event) => {
    const key = event.target.dataset.proofExists;
    if (key === "original" || key === "patched") $(`[data-proof-content="${key}"]`, event.target.closest(".proof-file")).disabled = !event.target.checked;
  });
  $("#proof-example").addEventListener("click", async () => {
    if (running) return;
    const session = authEpoch, current = generation;
    if (!await confirmConsoleAction("将用本地演示样例替换当前文件和命令，不会立即执行。是否继续？", () => session === authEpoch && current === generation && !running)) return;
    invalidate();
    list.replaceChildren();
    addFile({ path: "app.py", original: "def clamp(value):\n    return min(value, 10)\n", patched: "def clamp(value):\n    return max(0, min(value, 10))\n" });
    $('[name="reproduction_command"]', form).value = "python -c 'from app import clamp; assert clamp(-1) == 0'";
    $('[name="regression_command"]', form).value = "python -c 'from app import clamp; assert [clamp(v) for v in (0, 5, 20)] == [0, 5, 10]'";
    $("[data-proof-path]", list).focus();
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (running || consoleCapabilities?.proof !== true) return;
    const session = authEpoch, current = ++generation;
    try {
      const files = $$(".proof-file", list).map((row) => Object.fromEntries([["path", $("[data-proof-path]", row).value], ...["original", "patched"].map((key) => [key, $(`[data-proof-exists="${key}"]`, row).checked ? $(`[data-proof-content="${key}"]`, row).value : null])]));
      const body = proofPayload(files, $('[name="reproduction_command"]', form).value, $('[name="regression_command"]', form).value);
      running = true; fields.disabled = true; capabilities(consoleCapabilities);
      setButtonBusy(submit, true, "正在隔离验证…");
      target.classList.add("empty"); target.textContent = "正在执行三个证据阶段；复现失败后才会验证修复，修复通过后才会执行回归。";
      target.setAttribute("aria-busy", "true");
      const result = await api("/v1/proofs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (session !== authEpoch || current !== generation) return;
      target.innerHTML = proofMarkup(result); target.classList.remove("empty");
    } catch (error) {
      if (session === authEpoch && current === generation) target.textContent = `${error.message} 本次未取得完整证据，不会自动重试。`;
    } finally {
      if (session === authEpoch && current === generation) {
        running = false; fields.disabled = false;
        setButtonBusy(submit, false); capabilities(consoleCapabilities);
        target.setAttribute("aria-busy", "false");
      }
    }
  });
  window.proof = { reset, capabilities };
  reset();
})();
