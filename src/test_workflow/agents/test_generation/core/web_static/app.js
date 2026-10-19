const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const state = {
  running: false,
  pollTimer: null,
  lastTraceSize: 0,
  reportMarkdown: null,
};

const labels = {
  completed: "已完成",
  partial: "部分完成",
  needs_review: "需要复核",
  failed: "运行失败",
  coverage_target_met: "覆盖目标已达到",
  round_limit_reached: "已达到轮次上限",
  no_progress: "连续轮次没有提升",
  suspected_source_defect: "发现疑似源码缺陷",
  ambiguous_specification: "行为规格存在歧义",
  execution_timeout: "执行超时",
  unrecoverable_error: "遇到不可恢复错误",
};

const actionLabels = {
  analyze_source: "分析源码与函数关系",
  generate_tests: "生成首轮测试",
  run_tests: "执行 pytest 与覆盖率采集",
  repair_generated_test: "修复不可执行的候选",
  add_missing_cases: "依据覆盖缺口补充场景",
  request_review: "请求人工复核",
  finish: "完成本次运行",
};

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("show"), 2600);
}

function lineNumbers(input, gutter) {
  const count = Math.max(1, input.value.split("\n").length);
  gutter.textContent = Array.from({ length: count }, (_, index) => index + 1).join("\n");
  gutter.scrollTop = input.scrollTop;
}

function setupEditors() {
  $$(".editor-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      $$(".editor-tab").forEach((item) => {
        const active = item === tab;
        item.classList.toggle("active", active);
        item.setAttribute("aria-selected", String(active));
      });
      $$(".editor-frame").forEach((panel) => panel.classList.toggle("active", panel.id === `${tab.dataset.panel}-panel`));
      $("#file-name").textContent = tab.dataset.panel === "source" ? "subject.py" : "specification.md";
    });
  });

  [["#source-input", "#source-lines"], ["#spec-input", "#spec-lines"]].forEach(([inputSelector, gutterSelector]) => {
    const input = $(inputSelector);
    const gutter = $(gutterSelector);
    input.addEventListener("input", () => lineNumbers(input, gutter));
    input.addEventListener("scroll", () => { gutter.scrollTop = input.scrollTop; });
    input.addEventListener("keydown", (event) => {
      if (event.key !== "Tab") return;
      event.preventDefault();
      const start = input.selectionStart;
      input.setRangeText("    ", start, input.selectionEnd, "end");
      lineNumbers(input, gutter);
    });
  });
}

async function requestJson(url, options) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "请求失败");
  return data;
}

function resetResult() {
  $("#empty-state").classList.remove("hidden");
  $("#result-content").classList.add("hidden");
  setStatus("idle", "待运行");
  state.lastTraceSize = 0;
  state.reportMarkdown = null;
  $("#report-section").classList.add("hidden");
  closeReport();
}

function setStatus(kind, text) {
  const badge = $("#run-status");
  badge.className = `status-badge ${kind}`;
  badge.textContent = text;
}

function setRunning(running) {
  state.running = running;
  $("#run-button").disabled = running;
  $("#run-button").classList.toggle("running", running);
  $("#run-label").textContent = running ? "Agent 运行中" : "开始生成测试";
}

function percent(value) {
  return typeof value === "number" ? `${value.toFixed(value % 1 ? 1 : 0)}%` : "—";
}

function latestCandidate(runState) {
  if (!runState?.history?.length) return null;
  const selected = runState.best_available_round;
  return selected ? runState.history[selected - 1] : runState.history.at(-1);
}

function renderMetrics(data) {
  const runState = data.state;
  const summary = data.summary;
  const candidate = latestCandidate(runState);
  const coverage = candidate?.coverage;
  const summaryMetrics = summary?.metrics;
  const coverageStatus = coverage?.status || summaryMetrics?.coverage_status;
  const coveredLines = coverage?.covered_lines ?? summaryMetrics?.covered_lines;
  const totalLines = coverage?.total_lines ?? summaryMetrics?.total_lines;
  const coveredBranches = coverage?.covered_branches ?? summaryMetrics?.covered_branches;
  const totalBranches = coverage?.total_branches ?? summaryMetrics?.total_branches;
  const linePercent = coverage?.line_percent ?? summaryMetrics?.line_percent;
  const branchPercent = coverage?.branch_percent ?? summaryMetrics?.branch_percent;
  const decisionStatus = summary?.status || (data.status === "failed" ? "failed" : null);
  $("#metric-status").textContent = labels[decisionStatus] || (data.status === "running" ? "执行中" : "准备中");
  $("#metric-reason").textContent = labels[summary?.stop_reason] || summary?.reason || "Agent 正在处理";
  $("#metric-line").textContent = percent(linePercent);
  $("#metric-line-detail").textContent = coverageStatus === "available" ? `${coveredLines} / ${totalLines} 行` : "等待覆盖率数据";
  $("#metric-branch").textContent = totalBranches === 0 ? "N/A" : percent(branchPercent);
  $("#metric-branch-detail").textContent = coverageStatus === "available" ? (totalBranches ? `${coveredBranches} / ${totalBranches} 分支` : "当前函数没有分支") : "等待覆盖率数据";
  $("#metric-rounds").textContent = runState ? `${runState.current_round} / ${runState.request.max_rounds}` : "—";
  $("#metric-api").textContent = runState ? `${runState.api_calls} 次模型请求` : "正在初始化";
}

function traceTitle(event) {
  if (event.phase === "decision") return actionLabels[event.action] || "Agent 作出下一步决策";
  if (event.phase === "run_started") return "创建独立运行空间";
  if (event.phase === "run_finished") return "运行结束并保存结果";
  if (event.phase === "tool_started") return `开始：${event.tool || "工具"}`;
  if (event.phase === "tool_finished") return `完成：${event.tool || "工具"}`;
  if (event.phase === "tool_failed") return `失败：${event.tool || "工具"}`;
  if (event.phase?.includes("model")) return "调用模型生成候选";
  return event.phase || "处理任务";
}

function renderTrace(events) {
  const visible = events.filter((event) => ["decision", "run_started", "run_finished", "tool_finished", "tool_failed"].includes(event.phase));
  $("#trace-count").textContent = `${visible.length} 个事件`;
  const list = $("#trace-list");
  list.replaceChildren(...visible.map((event) => {
    const item = document.createElement("li");
    item.className = "trace-item";
    const title = document.createElement("b");
    title.textContent = traceTitle(event);
    const reason = document.createElement("span");
    const round = event.round_number ? `第 ${event.round_number} 轮 · ` : "";
    reason.textContent = `${round}${event.reason || "完成"}`;
    item.append(title, reason);
    return item;
  }));
  if (visible.length > state.lastTraceSize) {
    list.scrollTop = list.scrollHeight;
    state.lastTraceSize = visible.length;
  }
}

function renderIssues(issues = []) {
  const section = $("#issues-section");
  section.classList.toggle("hidden", !issues.length);
  $("#issue-count").textContent = issues.length ? `${issues.length} 项` : "";
  $("#issues-list").replaceChildren(...issues.map((issue) => {
    const card = document.createElement("div");
    card.className = "issue";
    const title = document.createElement("strong");
    title.textContent = labels[issue.kind] || issue.kind;
    const body = document.createElement("p");
    body.textContent = issue.summary;
    card.append(title, body);
    return card;
  }));
}

function renderRun(data) {
  $("#empty-state").classList.add("hidden");
  $("#result-content").classList.remove("hidden");
  renderMetrics(data);
  renderTrace(data.trace || []);
  renderIssues(data.state?.issues || []);
  const testSection = $("#test-section");
  testSection.classList.toggle("hidden", !data.testCode);
  $("#test-code").textContent = data.testCode || "";
  state.reportMarkdown = data.reportMarkdown || null;
  $("#report-section").classList.toggle("hidden", !state.reportMarkdown);
  if (data.status === "completed") setStatus("completed", labels[data.summary?.status] || "已完成");
  else if (data.status === "failed") setStatus("failed", "运行失败");
  else setStatus("running", data.status === "preparing" ? "准备中" : "运行中");
}

function openReport() {
  if (!state.reportMarkdown) {
    showToast("报告仍在生成，请稍候");
    return;
  }
  $("#report-content").textContent = state.reportMarkdown;
  $("#report-modal").classList.remove("hidden");
  document.body.classList.add("modal-open");
  $("#close-report-button").focus();
}

function closeReport() {
  $("#report-modal").classList.add("hidden");
  document.body.classList.remove("modal-open");
}

async function pollRun(jobId) {
  try {
    const data = await requestJson(`/api/runs/${jobId}`);
    renderRun(data);
    if (["completed", "failed"].includes(data.status)) {
      setRunning(false);
      if (data.status === "failed") showToast(data.error || data.summary?.reason || "运行失败");
      else showToast("运行完成，结果已保存到 outputs/web");
      return;
    }
    state.pollTimer = window.setTimeout(() => pollRun(jobId), 1200);
  } catch (error) {
    setRunning(false);
    setStatus("failed", "连接中断");
    showToast(error.message);
  }
}

async function startRun() {
  if (state.running) return;
  const source = $("#source-input").value;
  const spec = $("#spec-input").value;
  if (!source.trim()) {
    showToast("请先填写或选择一份 Python 源码");
    return;
  }
  if (!spec.trim()) {
    showToast("请填写行为规格");
    $("#spec-tab").click();
    return;
  }
  setRunning(true);
  $("#empty-state").classList.add("hidden");
  $("#result-content").classList.remove("hidden");
  setStatus("running", "正在创建任务");
  try {
    const job = await requestJson("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source,
        spec,
      }),
    });
    pollRun(job.id);
  } catch (error) {
    setRunning(false);
    setStatus("failed", "无法启动");
    showToast(error.message);
  }
}

async function bootstrap() {
  setupEditors();
  $("#run-button").addEventListener("click", startRun);
  $("#reset-button").addEventListener("click", () => {
    if (state.running) return;
    window.clearTimeout(state.pollTimer);
    $("#source-input").value = "";
    $("#spec-input").value = "";
    lineNumbers($("#source-input"), $("#source-lines"));
    lineNumbers($("#spec-input"), $("#spec-lines"));
    resetResult();
  });
  $("#copy-button").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("#test-code").textContent);
      showToast("测试代码已复制");
    } catch {
      showToast("复制失败，请手动选择代码");
    }
  });
  $("#view-report-button").addEventListener("click", openReport);
  $("#close-report-button").addEventListener("click", closeReport);
  $("#report-modal").addEventListener("click", (event) => {
    if (event.target === $("#report-modal")) closeReport();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("#report-modal").classList.contains("hidden")) closeReport();
  });

  try {
    const data = await requestJson("/api/bootstrap");
    const config = $("#config-status");
    config.textContent = data.config.ready ? `${data.config.model} 已配置` : "模型配置未就绪";
    config.className = `config-status ${data.config.ready ? "ready" : "warning"}`;
  } catch (error) {
    showToast(error.message);
  }
}

bootstrap();
