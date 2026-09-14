/* Runs against the real bundled UI. Open generated dist/browser-test.html locally.
   Synthetic composition events check the guard, not Windows IME compatibility. */
(() => {
  const app = document.getElementById('biomind-workbench');
  const results = [];
  const $ = selector => { const e = app.querySelector(selector); if (!e) throw new Error(`Missing ${selector}`); return e; };
  const click = selector => { const e = $(selector); if (e.disabled) throw new Error(`Disabled ${selector}`); e.click(); };
  const assert = (condition, text) => { if (!condition) throw new Error(text); };
  const input = text => { const e = $('#message-input'); e.value = text; e.dispatchEvent(new Event('input', { bubbles: true })); };
  const scenario = name => { const e = $('#scenario-select'); e.value = name; e.dispatchEvent(new Event('change', { bubbles: true })); };
  const check = (name, fn) => { try { fn(); results.push({ name, passed: true }); } catch (e) { results.push({ name, passed: false, reason: String(e) }); } };
  check('新会话、中文多行、文件引用与组合输入保护', () => {
    click('[data-action="new"]');
    const text = '  水稻样本\n　保留中文与空格\n/stop\n!echo 演示'; input(text);
    const e = $('#message-input'); e.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true }));
    e.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    assert(!app.querySelector('.message-user'), 'composition Enter unexpectedly sent');
    e.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true }));
    const shift = new KeyboardEvent('keydown', { key: 'Enter', shiftKey: true, bubbles: true, cancelable: true });
    e.dispatchEvent(shift); assert(!shift.defaultPrevented, 'Shift+Enter intercepted');
    click('[data-action="files"]'); assert($('#modal').open, 'file dialog not open');
    $('#modal input[value="r1"]').checked = true; click('[data-action="apply-references"]');
    click('[data-action="send"]'); assert($('.message-user .message-text').textContent === text, 'text changed');
    assert($('.message-user .file-chip').textContent.includes('demo_R1'), 'reference missing');
  });
  check('计划展开与两次独立审批', () => {
    const plan = $('.plan-card'); const before = plan.open; click('.plan-card summary'); assert(plan.open !== before, 'plan did not toggle');
    if (!plan.open) click('.plan-card summary');
    click('.plan-card [data-action="approve"]');
    assert($('.conversation-header .status').textContent.includes('等待脚本'), 'plan started execution');
    click('.script-disclosure summary'); assert($('.script-disclosure').open, 'script not expanded');
    click('.approval-card [data-action="approve"]');
    assert($('.conversation-header .status').textContent.includes('运行中'), 'script decision not applied');
  });
  check('会话切换保存草稿', () => {
    input('未发送的中文草稿'); const id = $('.session.selected').dataset.id;
    click('[data-action="new"]'); assert($('#message-input').value === '', 'draft leaked');
    click(`[data-action="session"][data-id="${id}"]`); assert($('#message-input').value === '未发送的中文草稿', 'draft lost');
  });
  check('断线冻结视图、后台推进、重连刷新', () => {
    scenario('running'); click('[data-action="disconnect"]');
    const before = $('.conversation-header .status').textContent;
    click('[data-action="advance"]'); click('[data-action="advance"]'); click('[data-action="advance"]');
    assert($('.conversation-header .status').textContent === before, 'offline view changed');
    assert($('#message-input').value === '', 'unexpected draft'); click('[data-action="connect"]');
    assert(app.querySelector('.result-card'), 'completion missing after reattach');
  });
  check('详情标签、长日志与合成报告预览', () => {
    click('[data-action="tab"][data-tab="logs"]'); click('[data-action="long-logs"]');
    assert(app.querySelectorAll('.log-line').length === 60, 'log window unbounded');
    click('[data-action="more-logs"]'); assert(app.querySelectorAll('.log-line').length === 120, 'earlier logs missing');
    click('[data-action="tab"][data-tab="files"]'); click('.resource-row[data-id="report"]');
    assert($('#modal').open && $('#modal').textContent.includes('合成指标'), 'report not previewed'); click('[data-action="close-modal"]');
  });
  check('失败恢复后重新审批', () => {
    scenario('recovery'); click('[data-action="recover"]');
    assert($('.approval-title').textContent.includes('版本 2'), 'recovery version unchanged');
    click('.approval-card [data-action="approve"]'); assert($('.conversation-header .status').textContent.includes('运行中'), 'recovery did not progress');
  });
  check('停止请求与停止回执分离', () => {
    scenario('running'); click('[data-action="stop"]'); click('[data-action="confirm-stop"]');
    assert($('.conversation-header .status').textContent.includes('已请求停止'), 'stop request collapsed into stopped');
    click('[data-action="advance"]'); assert($('.conversation-header .status').textContent.includes('已停止'), 'stop receipt missing');
  });
  check('切换服务器与详情收起', () => {
    const e = $('#server-select'); e.value = 'sandbox'; e.dispatchEvent(new Event('change', { bubbles: true }));
    assert($('.top-location').textContent.includes('测试服务器'), 'server not switched');
    const before = $('.shell').classList.contains('panel-closed'); click('.topbar [data-action="panel-toggle"]');
    assert($('.shell').classList.contains('panel-closed') !== before, 'panel did not toggle');
  });
  scenario('approval');
  check(`当前视口 ${innerWidth}×${innerHeight} 无页面横向溢出`, () => {
    assert(document.documentElement.scrollWidth <= innerWidth + 1, 'horizontal page overflow');
    assert($('#message-input').getBoundingClientRect().width > 50, 'composer too narrow');
  });
  const failed = results.filter(r => !r.passed).length;
  const panel = document.createElement('dialog'); panel.id = 'browser-test-report';
  const heading = document.createElement('h2'); heading.textContent = `浏览器 DOM 测试：${results.length - failed}/${results.length} 通过`;
  const pre = document.createElement('pre'); pre.style.whiteSpace = 'pre-wrap'; pre.textContent = results.map(r => `${r.passed ? 'PASS' : 'FAIL'} ${r.name}${r.reason ? '\n' + r.reason : ''}`).join('\n');
  const note = document.createElement('p'); note.textContent = '本结果只涵盖当前浏览器和视口。组合事件为合成事件，不代表 Windows 中文输入法已验收。';
  const close = document.createElement('button'); close.textContent = '关闭并继续预览'; close.onclick = () => panel.close();
  panel.append(heading, pre, note, close); app.append(panel); panel.showModal();
  document.title = `${failed ? 'FAIL' : 'PASS'} · BioMind browser smoke`;
  window.__BIOMIND_BROWSER_TEST_RESULTS__ = results;
})();
