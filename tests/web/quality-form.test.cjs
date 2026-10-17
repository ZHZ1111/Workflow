const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '../../src/test_workflow/web_static');
const source = fs.readFileSync(path.join(root, 'app.js'), 'utf8');

for (const enabled of [false, true]) {
  test(`form sends quality_review_enabled as boolean ${enabled}`, async () => {
    const elements = new Map();
    function element(id) {
      if (!elements.has(id)) elements.set(id, {value:'',checked:false,disabled:false,textContent:'',listeners:{},
        addEventListener(type, callback) { this.listeners[type] = callback; }});
      return elements.get(id);
    }
    const requests = [];
    const context = vm.createContext({
      document:{getElementById:element,querySelectorAll:()=>[]},
      sessionStorage:{getItem:()=>null,setItem:()=>{},removeItem:()=>{}},
      setTimeout,
      fetch:async (url, options) => {
        requests.push({url,options});
        return {ok:true,json:async()=> url === '/api/bootstrap'
          ? {config:{ready:true,model:'fixed',max_rounds:3,timeout:60,quality_review_enabled:false}}
          : {id:'fixed-run',status:'queued'}};
      },
    });
    vm.runInContext(source, context);
    vm.runInContext('render = () => {}; poll = async () => {};', context);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(element('quality-review').checked, false);
    element('source').value = 'def sample(): return 1';
    element('spec').value = 'returns 1';
    element('quality-review').checked = enabled;
    await element('run-form').listeners.submit({preventDefault(){}});
    const submitted = requests.find(item => item.url === '/api/runs');
    assert.ok(submitted);
    assert.equal(JSON.parse(submitted.options.body).quality_review_enabled, enabled);
    assert.equal(element('request-error').textContent, '');
  });
}

test('quality checkbox is accessible and defaults off in shipped HTML', () => {
  const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
  const input = html.match(/<input id="quality-review"[^>]*>/)[0];
  assert.match(input, /type="checkbox"/);
  assert.match(input, /aria-describedby="quality-review-hint"/);
  assert.doesNotMatch(input, /\bchecked\b/);
  assert.match(html, /for="quality-review"/);
});
