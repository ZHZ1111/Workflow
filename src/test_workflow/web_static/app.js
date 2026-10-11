const $ = id => document.getElementById(id);
let bootstrap;
let activeJob = null;
const statuses = {queued:'排队中',running:'运行中',finished:'流程结束',failed:'运行失败',completed:'已完成',partial:'部分完成',needs_review:'需要复核'};
const label = value => statuses[value] || value || '—';
async function request(url, options) {
  const response = await fetch(url, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `请求失败 (${response.status})`);
  return body;
}
function loadSample() {
  const value = bootstrap.samples[$('sample').value];
  $('source').value = value.source;
  $('spec').value = value.spec;
}
function modeHint() {
  $('mode-hint').textContent = $('mode').value === 'offline'
    ? '离线样例使用固定生成候选，pytest 和 coverage 真实运行；编辑输入后请切换 DeepSeek。'
    : 'DeepSeek 将接收源码、规格和反馈；密钥从服务器本地配置读取。';
}
function render(job) {
  $('job-status').textContent = label(job.status);
  $('output-dir').textContent = job.outputDir;
  $('request-error').textContent = job.error || '';
  const state = job.state || {};
  const summary = job.summary || {};
  $('task-status').textContent = label(summary.task_status || state.task_status);
  $('stop-reason').textContent = summary.stop_reason || state.stop_reason || '—';
  $('selected-round').textContent = summary.selected_round || state.selected_round || '—';
  $('calls').textContent = `${summary.external_requests ?? '—'} / ${summary.pytest_processes ?? '—'}`;
  $('events').textContent = job.events.map(e => `${e.seq}. [第 ${e.round ?? '—'} 轮] ${e.node || 'workflow'} · ${e.type}${e.duration_seconds == null ? '' : ` (${e.duration_seconds.toFixed(2)}s)`}`).join('\n') || '等待节点事件…';
  $('messages').textContent = job.messages.length ? JSON.stringify(job.messages, null, 2) : '暂无消息';
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
$('sample').addEventListener('change', loadSample);
$('mode').addEventListener('change', modeHint);
$('run-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (activeJob) return;
  $('submit').disabled = true;
  $('request-error').textContent = '';
  try {
    const payload = {source:$('source').value, spec:$('spec').value, sample:$('sample').value,
      mode:$('mode').value, max_rounds:Number($('max-rounds').value), timeout:Number($('timeout').value)};
    const job = await request('/api/runs', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    activeJob = job.id;
    sessionStorage.setItem('workflowJob', activeJob);
    await poll(activeJob);
  } catch (error) { $('request-error').textContent = error.message; }
  finally { activeJob = null; $('submit').disabled = false; }
});
async function init() {
  try {
    bootstrap = await request('/api/bootstrap');
    const config = bootstrap.config;
    $('config-status').textContent = config.ready ? `DeepSeek · ${config.model}` : 'DeepSeek 未配置 · 可运行离线样例';
    $('max-rounds').value = config.max_rounds;
    $('timeout').value = config.timeout;
    if (!config.ready) $('mode').value = 'offline';
    loadSample(); modeHint();
    const previous = sessionStorage.getItem('workflowJob');
    if (previous) {
      try {
        const job = await request(`/api/runs/${previous}`); render(job);
        if (!['finished','failed'].includes(job.status)) { activeJob = previous; await poll(previous); }
      } catch { sessionStorage.removeItem('workflowJob'); }
    }
    activeJob = null; $('submit').disabled = false;
  } catch (error) { $('request-error').textContent = error.message; }
}
init();
