const {test} = require('node:test');
const assert = require('node:assert/strict');
const {derive} = require('../../src/test_workflow/web_static/workflow-view.js');
const event = (type,node,round=1,extra={}) => ({type,node,round,...extra});
const feedback = {message_id:'feedback-1',round:1,type:'execution_feedback',sender:'test_execution',
  receiver:'test_generation',payload:{candidate_id:'run:1',reason:'coverage below target'}};

test('queued job shows input preparation without claiming Agent execution', () => {
  const view=derive({status:'queued'});
  assert.deepEqual(view.active,['input']);
  assert.equal(view.nodes.test_generation.status,'idle');
  assert.deepEqual(view.edges,[]);
});
test('event journal highlights active execution despite an older state snapshot', () => {
  const view=derive({status:'running',state:{current_round:1,current_node:'test_generation'},events:[
    event('workflow_started'),event('node_finished','test_generation'),event('node_started','test_execution',1,{seq:8})]});
  assert.deepEqual(view.active,['test_execution']);
  assert.equal(view.nodes.test_generation.status,'done');
  assert.equal(view.eventSequence,8);
  assert.ok(view.edges.includes('generation-execution'));
});
test('feedback remains pending until a real consumption record exists', () => {
  const view=derive({status:'running',events:[event('route_selected',null,1,{action:'add_missing_cases'})],messages:[feedback]});
  assert.equal(view.nodes.feedback.status,'running');
  assert.equal(view.feedbackCount,1);
  assert.equal(view.consumedFeedback,0);
  assert.ok(!view.edges.includes('feedback-generation'));
});
test('second round resets execution while preserving consumed feedback', () => {
  const view=derive({status:'running',state:{current_round:2},messages:[feedback],events:[
    event('node_finished','test_execution'),event('message_consumed','test_generation',2,{message_id:'feedback-1'}),
    event('node_started','test_generation',2)]});
  assert.equal(view.round,2);
  assert.equal(view.nodes.test_generation.status,'running');
  assert.equal(view.nodes.test_execution.status,'idle');
  assert.equal(view.nodes.feedback.status,'done');
  assert.equal(view.consumedFeedback,1);
});
test('state snapshot also provides persisted consumption evidence', () => {
  const view=derive({messages:[feedback],state:{consumed_message_ids:['feedback-1']}});
  assert.equal(view.messages[0].consumed,true);
});
test('successful export highlights completion only after the workflow finishes', () => {
  const view=derive({status:'finished',summary:{task_status:'completed',stop_reason:'coverage_target_met'},events:[
    event('workflow_started'),event('node_finished','test_generation'),event('node_finished','test_execution'),
    event('workflow_stopped'),event('node_finished','result_summary'),event('workflow_finished')]});
  for(const id of ['input','test_generation','test_execution','result_summary','end']) assert.equal(view.nodes[id].status,'done');
  assert.equal(view.nodes.feedback.status,'idle');
  assert.deepEqual(view.active,[]);
  assert.ok(view.edges.includes('summary-end'));
});
test('needs_review is distinct from successful business completion', () => {
  const view=derive({status:'finished',state:{task_status:'needs_review',stop_reason:'ambiguous_specification'}});
  assert.equal(view.nodes.end.status,'review');
  assert.equal(view.nodes.end.detail,'ambiguous_specification');
  assert.equal(view.nodes.result_summary.status,'skipped');
});
test('partial result does not turn the final node green', () => {
  assert.equal(derive({status:'finished',summary:{task_status:'partial'}}).nodes.end.status,'review');
});
test('node failure preserves its error and leaves later execution unstarted', () => {
  const view=derive({status:'failed',events:[event('node_started','test_generation'),
    event('node_failed','test_generation',1,{error_code:'model_error'}),event('workflow_error')]});
  assert.equal(view.nodes.test_generation.status,'error');
  assert.equal(view.nodes.test_generation.detail,'model_error');
  assert.equal(view.nodes.test_execution.status,'skipped');
  assert.equal(view.nodes.end.status,'error');
});
test('server failure without state is rendered without inventing completed nodes', () => {
  const view=derive({status:'failed',error:'Unable to create task',events:[],messages:[]});
  assert.equal(view.nodes.input.status,'skipped');
  assert.equal(view.nodes.end.status,'error');
  assert.equal(view.nodes.end.detail,'Unable to create task');
});
test('summary may still be running after the business loop has stopped', () => {
  const view=derive({status:'running',events:[event('workflow_stopped'),event('node_started','result_summary')]});
  assert.equal(view.nodes.result_summary.status,'running');
  assert.equal(view.nodes.end.status,'idle');
});
test('report export is a running state, not premature completion', () => {
  const view=derive({status:'running',events:[event('workflow_stopped'),event('node_finished','result_summary')]});
  assert.equal(view.nodes.end.status,'running');
  assert.ok(view.edges.includes('summary-end'));
});
test('a new job clears any prior success or feedback display', () => {
  const view=derive({status:'queued',events:[],messages:[]});
  assert.equal(view.nodes.end.status,'idle');
  assert.equal(view.feedbackCount,0);
});
