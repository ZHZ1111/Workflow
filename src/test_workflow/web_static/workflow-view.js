/* Read-only projection of the Workflow event journal; it never schedules nodes. */
(function (root) {
  const nodeNames = {input:'输入准备',test_generation:'测试生成 Agent',test_execution:'执行与评估 Agent',
    test_quality_review:'测试质量审查 Agent',feedback:'反馈交接',result_summary:'结果摘要',end:'结束与导出'};
  const statusNames = {idle:'等待',running:'进行中',done:'已完成',review:'需要复核',error:'失败',skipped:'未送审 / 未启动',disabled:'未启用'};
  const actionNames = {add_missing_cases:'补充场景',extend_tests:'补充场景',repair_generated_test:'修复测试',
    repair_tests:'修复测试',request_review:'人工复核',finish:'结束',quality_approved:'质量批准',
    quality_revision_requested:'质量补充',quality_needs_review:'规格复核',quality_no_progress:'质量补充无进展'};
  const isRevision = action => ['add_missing_cases','extend_tests','repair_generated_test','repair_tests'].includes(action);
  function derive(job = {}) {
    const events = job.events || [], messages = job.messages || [], state = job.state || {};
    const round = Math.max(state.current_round || 0, ...events.map(event => event.round || 0));
    const current = events.filter(event => event.round === round);
    const route = [...events].reverse().find(event => event.type === 'route_selected');
    const qualityEnabled = state.config?.quality_review_enabled ?? job.summary?.quality_review_enabled ?? job.quality_review_enabled
      ?? events.some(event => event.node === 'test_quality_review');
    const nodes = Object.fromEntries(Object.keys(nodeNames).map(id => [id, {status:'idle',detail:''}]));
    const edges = new Set();
    const consumed = new Set([...(state.consumed_message_ids || []),
      ...events.filter(event => event.type === 'message_consumed').map(event => event.message_id)]);
    nodes.input.status = events.some(event => event.type === 'workflow_started') ? 'done'
      : ['queued','running'].includes(job.status) ? 'running' : 'idle';
    for (const event of current) {
      const node = nodes[event.node];
      if (!node) continue;
      if (event.type === 'node_started') node.status = 'running';
      if (event.type === 'node_finished') node.status = 'done';
      if (event.type === 'node_failed') node.status = 'error';
      if (event.duration_seconds != null) node.detail = `${event.duration_seconds.toFixed(2)} 秒`;
      if (event.error_code) node.detail = event.error_code;
    }
    if (nodes.test_generation.status !== 'idle') edges.add('input-generation');
    if (nodes.test_execution.status !== 'idle') edges.add('generation-execution');
    const feedback = messages.filter(message => ['execution_feedback','quality_feedback'].includes(message.type));
    const lastFeedback = feedback.at(-1);
    if (lastFeedback) {
      const received = consumed.has(lastFeedback.message_id);
      nodes.feedback = {status:received ? 'done' : 'running',
        detail:`第 ${lastFeedback.round} 轮 → 第 ${lastFeedback.round + 1} 轮 · ${received ? '已消费' : '待消费'}`};
      for (const message of feedback) edges.add(message.type === 'quality_feedback' ? 'quality-feedback' : 'execution-feedback');
      if (received) edges.add('feedback-generation');
    } else if (route && isRevision(route.action)) {
      nodes.feedback = {status:'running',detail:actionNames[route.action]};
      edges.add('execution-feedback');
    }
    const quality = job.summary?.round_details?.find(item => item.round === round)?.quality;
    const assessment = quality?.assessment || state.quality_assessments?.find(item => item.round === round);
    const reviewed = current.some(event => event.node === 'test_quality_review');
    if (!qualityEnabled && !reviewed) nodes.test_quality_review = {status:'disabled',detail:'质量审查未启用'};
    else if (reviewed) {
      edges.add('execution-review');
      if (nodes.test_quality_review.status === 'done' && assessment) {
        const verdict = assessment.decision.verdict;
        nodes.test_quality_review = {status:verdict === 'approve' ? 'done' : 'review',
          detail:`${{approve:'审查批准',request_revision:'仍有质量缺口',request_review:'规格需要复核'}[verdict]} · ${assessment.decision.summary}`};
      }
      if (quality?.evidence_verified === false) nodes.test_quality_review = {status:'error',detail:'审查证据校验失败'};
    } else if (qualityEnabled) nodes.test_quality_review.detail = '基础测试与覆盖达标后送审';
    if (nodes.result_summary.status !== 'idle') edges.add(reviewed ? 'review-summary' : 'execution-summary');
    const error = job.status === 'failed' || state.workflow_status === 'error'
      || events.some(event => event.type === 'workflow_error');
    const ended = error || job.status === 'finished' || events.some(event => event.type === 'workflow_finished');
    const stopped = events.some(event => event.type === 'workflow_stopped');
    if (ended) {
      const conclusion = job.summary?.task_status || state.task_status;
      const approved = job.summary?.latest_quality_approved === true || assessment?.decision?.verdict === 'approve';
      nodes.end = {status:error || conclusion === 'failed' ? 'error'
        : qualityEnabled && conclusion === 'completed' && (!approved || quality?.evidence_verified === false) ? 'review'
        : ['needs_review','partial'].includes(conclusion) ? 'review' : 'done',
        detail:job.summary?.stop_reason || state.stop_reason || job.error || '流程已结束'};
      for (const [id,node] of Object.entries(nodes)) {
        if (node.status === 'running') node.status = error ? 'error' : 'skipped';
        if (node.status === 'idle' && id !== 'feedback') node.status = 'skipped';
      }
      if (nodes.result_summary.status === 'done') edges.add('summary-end');
      if (qualityEnabled && !reviewed) nodes.test_quality_review = {status:'skipped',detail:quality?.reason || '基础未达标或流程提前停止，未送审'};
    } else if (stopped && nodes.result_summary.status !== 'running') {
      nodes.end = {status:'running',detail:'保存报告与最终产物'};
      if (nodes.result_summary.status === 'done') edges.add('summary-end');
    }
    const active = Object.keys(nodes).filter(id => nodes[id].status === 'running');
    const routeText = route ? `${actionNames[route.action] || route.action}${route.stop_reason ? ` · ${route.stop_reason}` : ''}` : '等待评估路由';
    return {round,nodes,edges:[...edges],active,routeText,
      eventSequence:events.at(-1)?.seq || 0,feedbackCount:feedback.length,
      consumedFeedback:feedback.filter(message => consumed.has(message.message_id)).length,
      messages:messages.map(message => ({...message,consumed:consumed.has(message.message_id)}))};
  }
  const api = {derive,nodeNames,statusNames,actionNames};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.WorkflowView = api;
})(typeof window !== 'undefined' ? window : globalThis);
