const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const WorkflowView = require('../../src/test_workflow/web_static/workflow-view.js');
const {derive} = WorkflowView;
const event = (type,node,round=1,extra={}) => ({type,node,round,...extra});
const review = verdict => ({round:1,decision:{verdict,summary:'原规格场景',findings:[],questions:[]},execution_assessment:{path:'evaluations/01/assessment.json'}});
const feedback = {message_id:'qf-1',round:1,type:'quality_feedback',sender:'test_quality_review',receiver:'test_generation',
  payload:{candidate_id:'run:1',reason:'明确规格场景缺口'}};

test('quality remains disabled or awaiting base completion without inventing a call', () => {
  assert.equal(derive({status:'queued'}).nodes.test_quality_review.status,'disabled');
  assert.equal(derive({status:'queued',quality_review_enabled:true}).nodes.test_quality_review.status,'idle');
  const running=derive({status:'running',state:{config:{quality_review_enabled:true}},events:[event('workflow_started'),event('node_started','test_execution')]});
  assert.equal(running.nodes.test_quality_review.status,'idle');
  assert.deepEqual(running.active,['test_execution']);
});
test('review start is highlighted from events even if state still says execution', () => {
  const view=derive({status:'running',state:{current_node:'test_execution',config:{quality_review_enabled:true}},events:[
    event('workflow_started'),event('node_finished','test_execution'),event('node_started','test_quality_review')]});
  assert.deepEqual(view.active,['test_quality_review']);
  assert.ok(view.edges.includes('execution-review'));
});
test('quality revision is pending until its next-round consumption is recorded', () => {
  const job={status:'running',state:{config:{quality_review_enabled:true},quality_assessments:[review('request_revision')]},messages:[feedback],
    events:[event('node_finished','test_quality_review'),event('route_selected','test_quality_review',1,{action:'quality_revision_requested'})]};
  const view=derive(job);
  assert.equal(view.nodes.test_quality_review.status,'review');
  assert.ok(view.edges.includes('quality-feedback') && !view.edges.includes('execution-feedback'));
  assert.equal(view.consumedFeedback,0);
  const next=derive({...job,state:{...job.state,current_round:2,consumed_message_ids:['qf-1']},events:[...job.events,event('node_started','test_generation',2)]});
  assert.equal(next.nodes.test_quality_review.status,'idle');
  assert.equal(next.consumedFeedback,1);
  assert.ok(next.edges.includes('feedback-generation'));
});
test('base stop skips enabled review and uses the direct summary path', () => {
  const view=derive({status:'finished',state:{config:{quality_review_enabled:true},task_status:'needs_review'},
    events:[event('node_finished','test_execution'),event('node_finished','result_summary')]});
  assert.equal(view.nodes.test_quality_review.status,'skipped');
  assert.ok(view.edges.includes('execution-summary') && !view.edges.includes('review-summary'));
});
test('approval, human review and review errors have distinct node states', () => {
  for (const [verdict,status] of [['approve','done'],['request_review','review'],['request_revision','review']]) {
    const view=derive({status:'finished',state:{config:{quality_review_enabled:true},quality_assessments:[review(verdict)],
      task_status:verdict==='approve'?'completed':'partial'},events:[event('node_finished','test_quality_review'),event('node_finished','result_summary')]});
    assert.equal(view.nodes.test_quality_review.status,status);
    assert.ok(view.edges.includes('review-summary'));
  }
  const failed=derive({status:'failed',events:[event('node_failed','test_quality_review',1,{error_code:'review_timeout'})]});
  assert.equal(failed.nodes.test_quality_review.status,'error');
  assert.equal(failed.nodes.test_quality_review.detail,'review_timeout');
});
test('enabled completion without current approval cannot turn the end node green', () => {
  const view=derive({status:'finished',state:{current_round:2,config:{quality_review_enabled:true},task_status:'completed',quality_assessments:[review('approve')]}});
  assert.equal(view.nodes.end.status,'review');
});

async function browser() {
  const elements=new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id,{id,textContent:'',value:'',checked:false,children:[],
      addEventListener(){},replaceChildren(...children){this.children=children;},append(...children){this.children.push(...children);},
      set innerHTML(value){throw new Error('untrusted HTML assignment');}});
    return elements.get(id);
  }
  const sandbox={document:{getElementById:element,querySelectorAll:()=>[],createElement:tag=>element(Symbol(tag))},WorkflowView,
    sessionStorage:{getItem:()=>null},setTimeout,fetch:async()=>({ok:true,json:async()=>({config:{ready:true,model:'fixed',max_rounds:3,timeout:60}})})};
  const context=vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.resolve(__dirname,'../../src/test_workflow/web_static/app.js'),'utf8'),context);
  await new Promise(resolve=>setImmediate(resolve));
  return {element,context};
}

test('quality cards show source evidence, consumption and exported approval as text', async () => {
  const {element,context}=await browser();
  const malicious='<img src=x onerror=globalThis.injected=true>';
  const assessment=review('request_revision');
  assessment.decision.findings=[{blocking:true,category:'missing_boundary',target_functions:['total'],description:malicious,
    spec_quote:'当金额为 0 时返回 0',test_code_quote:'assert total(1)==1',suggested_case:'assert total(0)==0'}];
  context.job={id:'safe',status:'finished',summary:{quality_status:'changes_requested',selected_quality_approved:false,latest_quality_approved:false,
    exported_quality_approved:false,model_usage:{total:{external_requests:null,known_external_requests:2,client_calls:2,token_usage:{total_tokens:null},known_token_usage:{total_tokens:15}}},
    round_details:[{round:1,assessment:{task_status:'completed'},quality:{status:'changes_requested',reason:'缺少场景',assessment,
      assessment_path:'quality_reviews/01/assessment.json',feedback:[{message_id:'qf-1',source_round:1,consumed:true,consumer_round:2,consumer_node:'test_generation'}]}}]}};
  vm.runInContext('render(job)',context);
  const card=element('quality-details').children[0];
  assert.match(card.children[0].textContent,/基础执行：completed/);
  assert.ok(card.children[1].textContent.includes(malicious));
  assert.match(card.children[1].textContent,/第 2 轮 test_generation 已消费/);
  assert.match(card.children[1].textContent,/当金额为 0 时返回 0/);
  assert.match(element('quality-status').textContent,/导出已批准：否/);
  assert.match(element('model-usage').children[0].textContent,/未知（已知 2）/);
  assert.equal(context.injected,undefined);
});
test('live next round retains the previous round quality verdict before final summary', async () => {
  const {element,context}=await browser();
  context.job={id:'live',status:'running',state:{current_round:2,quality_status:'not_run',config:{quality_review_enabled:true},
    agent_state:{history:[{number:1},{number:2}]},quality_assessments:[review('request_revision')],consumed_message_ids:['qf-1']},
    messages:[feedback],events:[event('node_started','test_generation',2)]};
  vm.runInContext('render(job)',context);
  assert.match(element('quality-details').children[0].children[0].textContent,/仍有质量缺口/);
  assert.match(element('quality-details').children[1].children[0].textContent,/尚未审查/);
});
test('old quality summaries do not present generation-only requests as a complete total', async () => {
  const {element,context}=await browser();
  context.job={id:'old',status:'finished',state:{config:{quality_review_enabled:true}},summary:{external_requests:1,pytest_processes:1,round_details:[]}};
  vm.runInContext('render(job)',context);
  assert.equal(element('calls').textContent,'— / 1');
});
