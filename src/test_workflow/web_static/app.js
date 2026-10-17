const $ = id => document.getElementById(id);
let modelReady = false;
let activeJob = null;
let displayedJob = null;
let inspectedFlowNode = null;
const flowNodes = [...document.querySelectorAll('[data-node]')];
const nodeDescriptions = Object.fromEntries(flowNodes.map(node => [node.dataset.node,node.querySelector('.node-detail').textContent]));
const statuses = {queued:'排队中',running:'运行中',finished:'流程结束',failed:'运行失败',completed:'已完成',partial:'部分完成',needs_review:'需要复核'};
const label = value => statuses[value] || value || '—';
async function request(url, options) {
  const response = await fetch(url, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `请求失败 (${response.status})`);
  return body;
}
function render(job) {
  if (displayedJob?.id !== job.id) inspectedFlowNode = null;
  displayedJob = job;
  renderFlow(job);
  $('job-status').textContent = label(job.status);
  $('output-dir').textContent = job.outputDir;
  $('request-error').textContent = job.error || '';
  const state = job.state || {};
  const summary = job.summary || {};
  $('task-status').textContent = label(summary.task_status || state.task_status);
  $('stop-reason').textContent = summary.stop_reason || state.stop_reason || '—';
  $('selected-round').textContent = summary.selected_round || state.selected_round || '—';
  $('calls').textContent = `${summary.external_requests ?? '—'} / ${summary.pytest_processes ?? '—'}`;
  $('events').textContent = (job.events || []).map(e => `${e.seq}. [第 ${e.round ?? '—'} 轮] ${e.node || 'workflow'} · ${e.type}${e.duration_seconds == null ? '' : ` (${e.duration_seconds.toFixed(2)}s)`}`).join('\n') || '等待节点事件…';
  $('messages').textContent = job.messages?.length ? JSON.stringify(job.messages, null, 2) : '暂无消息';
  $('test-code').textContent = job.testCode || '暂无最终测试';
  $('report').textContent = job.reportMarkdown || '报告将在流程结束后生成';
  $('rounds').replaceChildren(...(summary.round_details || []).map(row => {
    const element = document.createElement('div'); element.className = 'round';
    const execution = row.execution;
    const coverage = row.coverage;
    const line = coverage?.line_percent == null ? '不可用' : `${coverage.line_percent}%`;
    const branch = coverage?.total_branches === 0 ? '不适用' : coverage?.branch_percent == null ? '不可用' : `${coverage.branch_percent}%`;
    element.textContent = `第 ${row.round} 轮 · ${execution ? `${execution.passed} 通过 / ${execution.failed} 失败 / ${execution.errors} 错误` : '未执行'} · 行覆盖 ${line} / 分支覆盖 ${branch} · ${row.assessment?.action || row.validation_error || '—'}`;
    return element;
  }));
  const names = {tests:'下载测试',source:'下载源码副本',report:'下载报告',summary:'下载摘要',state:'下载状态'};
  $('downloads').replaceChildren(...Object.entries(job.downloads || {}).map(([name, url]) => {
    const link = document.createElement('a'); link.href = url; link.textContent = names[name]; link.download = ''; return link;
  }));
}
function renderFlow(job) {
  const view = WorkflowView.derive(job);
  $('flow-round').textContent = view.round ? `第 ${view.round} / ${job.state?.config?.max_rounds || $('max-rounds').value} 轮` : label(job.status);
  $('flow-progress').textContent = view.active.length ? `当前：${view.active.map(id => WorkflowView.nodeNames[id]).join('、')}`
    : ['finished','failed'].includes(job.status) ? `流程已结束 · ${label(job.summary?.task_status || job.state?.task_status || job.status)}` : '等待节点启动';
  const selected = inspectedFlowNode || view.active[0] || (['finished','failed'].includes(job.status) ? 'end' : 'input');
  for (const element of flowNodes) {
    const id = element.dataset.node, node = view.nodes[id];
    element.dataset.status = node.status;
    element.querySelector('.node-state').textContent = WorkflowView.statusNames[node.status];
    element.querySelector('.node-detail').textContent = node.detail || nodeDescriptions[id];
    element.classList.toggle('is-selected', id === selected);
    element.setAttribute('aria-pressed', String(id === selected));
    if (node.status === 'running') element.setAttribute('aria-current','step');
    else element.removeAttribute('aria-current');
  }
  for (const edge of document.querySelectorAll('[data-edge]')) edge.classList.toggle('is-traversed',view.edges.includes(edge.dataset.edge));
  $('flow-route').textContent = `路由：${view.routeText} · 事件 #${view.eventSequence}`;
  $('flow-evidence-title').textContent = `${WorkflowView.nodeNames[selected]} · ${WorkflowView.statusNames[view.nodes[selected].status]}`;
  const events = (job.events || []).filter(event => selected === 'end' ? ['workflow_stopped','workflow_finished','workflow_error'].includes(event.type)
    : selected === 'input' ? event.type === 'workflow_started' : event.node === selected && event.round === view.round);
  const related = view.messages.filter(message => selected === 'feedback' ? message.type === 'execution_feedback'
    : message.sender === selected || message.receiver === selected).slice(-2);
  const evidence = events.slice(-3).map(event => `#${event.seq} ${event.type}${event.round ? ` · 第 ${event.round} 轮` : ''}${event.error_code ? ` · ${event.error_code}` : ''}${event.duration_seconds == null ? '' : ` · ${event.duration_seconds.toFixed(2)} 秒`}${event.result_path ? `\n产物：${event.result_path}` : ''}`);
  evidence.push(...related.map(message => `${message.message_id} · 候选 ${message.payload.candidate_id} · ${message.consumed ? '已消费' : '待消费'}`));
  $('flow-evidence').textContent = evidence.join('\n') || '该节点暂无执行证据。';
  $('handoff-count').textContent = `${view.messages.length} 条消息 · 反馈 ${view.consumedFeedback} / ${view.feedbackCount} 已消费`;
  $('handoffs').replaceChildren(...view.messages.map(message => {
    const row = document.createElement('article'); row.className = 'handoff';
    const title = document.createElement('strong');
    title.textContent = `${WorkflowView.nodeNames[message.sender] || message.sender} → ${WorkflowView.nodeNames[message.receiver] || message.receiver}`;
    const status = document.createElement('span'); status.className = 'badge'; status.textContent = message.consumed ? '已消费' : '待消费';
    const detail = document.createElement('p'); detail.className = 'muted';
    const messageType = {execution_feedback:'执行反馈',quality_feedback:'质量反馈',review_ready:'送审交接',candidate_ready:'候选交接'}[message.type] || message.type;
    detail.textContent = `第 ${message.round} 轮 · ${messageType} · ${message.payload.candidate_id}\n${message.payload.reason || ''}`;
    row.append(title,status,detail); return row;
  }));
}
for (const node of flowNodes) node.addEventListener('click', () => {
  inspectedFlowNode = node.dataset.node;
  renderFlow(displayedJob || {});
});
const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
async function poll(identity) {
  while (true) {
    try {
      const job = await request(`/api/runs/${identity}`);
      render(job);
      if (['finished','failed'].includes(job.status)) return;
    } catch (error) {
      $('request-error').textContent = `${error.message}；正在重试读取任务状态…`;
    }
    await delay(1200);
  }
}
$('run-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (activeJob) return;
  $('submit').disabled = true;
  $('request-error').textContent = '';
  try {
    const payload = {source:$('source').value, spec:$('spec').value,
      max_rounds:Number($('max-rounds').value), timeout:Number($('timeout').value),
      quality_review_enabled:$('quality-review').checked};
    const job = await request('/api/runs', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    activeJob = job.id;
    render({...job,events:[],messages:[],outputDir:'正在创建任务目录…'});
    sessionStorage.setItem('workflowJob', activeJob);
    await poll(activeJob);
  } catch (error) { $('request-error').textContent = error.message; }
  finally { activeJob = null; $('submit').disabled = !modelReady; }
});
async function init() {
  try {
    const bootstrap = await request('/api/bootstrap');
    const config = bootstrap.config;
    modelReady = config.ready;
    $('config-status').textContent = config.ready ? `DeepSeek · ${config.model}` : 'DeepSeek 未配置';
    if (!modelReady) $('request-error').textContent = '请在服务器 .env 中配置 DeepSeek 模型与密钥后刷新页面。';
    $('max-rounds').value = config.max_rounds;
    $('timeout').value = config.timeout;
    $('quality-review').checked = config.quality_review_enabled ?? false;
    const previous = sessionStorage.getItem('workflowJob');
    if (previous) {
      try {
        const job = await request(`/api/runs/${previous}`); render(job);
        if (!['finished','failed'].includes(job.status)) { activeJob = previous; await poll(previous); }
      } catch { sessionStorage.removeItem('workflowJob'); }
    }
    activeJob = null; $('submit').disabled = !modelReady;
  } catch (error) { $('request-error').textContent = error.message; }
}
init();
