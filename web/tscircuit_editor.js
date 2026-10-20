/* Local code editor and upstream previews; candidate state stays in app.js. */
(function () {
  'use strict';
  window.CircuitTscircuitEditor = { mount: function (bridge) {
    const el = id => document.getElementById(id);
    const panel = el('tscircuit-panel');
    if (!panel) return;
    const status = el('tscircuit-status'), code = el('tscircuit-code'), name = el('tscircuit-name');
    let controller = null, last = null;
    function tell(message) { status.textContent = message; }
    function download(filename, text, type) {
      const url = URL.createObjectURL(new Blob([text], {type: type}));
      const link = document.createElement('a'); link.href = url; link.download = filename; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
    async function example() {
      try {
        const response = await fetch('/api/design/tscircuit/example');
        const body = await response.json();
        if (!response.ok) throw new Error(body.error || '示例未载入');
        code.value = body.code; name.value = 'rc.tsx'; tell('RC 示例已载入，可直接本机生成。');
      } catch (error) { tell(error.message); }
    }
    el('open-tscircuit').addEventListener('click', () => {
      panel.hidden = !panel.hidden;
      if (!panel.hidden && !code.value) example();
    });
    el('tscircuit-example').addEventListener('click', example);
    el('tscircuit-close').addEventListener('click', () => { if (controller) controller.abort(); panel.hidden = true; });
    el('tscircuit-cancel').addEventListener('click', () => { if (controller) controller.abort(); });
    el('tscircuit-file-button').addEventListener('click', () => el('tscircuit-file').click());
    el('tscircuit-file').addEventListener('change', async event => {
      const file = event.target.files[0]; event.target.value = '';
      if (file) { code.value = await file.text(); name.value = file.name; tell('代码文件已载入。'); }
    });
    el('tscircuit-download-code').addEventListener('click', () => last && download(last.name, last.code, 'text/plain;charset=utf-8'));
    el('tscircuit-download-json').addEventListener('click', () => last && download(last.name + '.circuit.json', JSON.stringify(last.result.upstream_circuit_json, null, 2), 'application/json'));
    el('tscircuit-download-svg').addEventListener('click', () => last && download(last.name + '.svg', last.result.schematic_svg, 'image/svg+xml'));
    el('tscircuit-generate').addEventListener('click', async () => {
      if (controller) return;
      let ticket;
      try { ticket = bridge.begin(); } catch (error) { tell(error.message); return; }
      controller = new AbortController();
      const signal = controller.signal, submitted = {code: code.value, name: name.value || 'circuit.tsx'};
      el('tscircuit-generate').disabled = true; el('tscircuit-cancel').hidden = false;
      el('tscircuit-preview').hidden = true; el('tscircuit-downloads').hidden = true; el('tscircuit-json-details').hidden = true;
      tell('正在本机运行代码并生成原理图…');
      try {
        const response = await fetch('/api/design/tscircuit', {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(submitted), signal: signal});
        const result = await response.json();
        if (signal.aborted) throw new DOMException('已取消', 'AbortError');
        last = {code: submitted.code, name: submitted.name, result: result};
        if (Array.isArray(result.upstream_circuit_json)) {
          el('tscircuit-json').textContent = JSON.stringify(result.upstream_circuit_json, null, 2);
          el('tscircuit-json-details').hidden = false; el('tscircuit-downloads').hidden = false;
          el('tscircuit-download-svg').disabled = !result.schematic_svg;
        }
        if (result.schematic_svg) {
          el('tscircuit-preview').srcdoc = result.schematic_svg;
          el('tscircuit-preview').hidden = false;
        }
        if (!response.ok || result.status !== 'needs_review') {
          throw new Error((result.issues || []).map(issue => issue.message || issue.code).join('；') || result.error || ('HTTP ' + response.status));
        }
        tell('原理图已生成，正在校验候选…');
        await bridge.review(result, submitted, ticket, signal);
        tell('已生成原理图与电路数据，候选已送入审核。');
      } catch (error) {
        tell(error.name === 'AbortError' ? '已取消生成，当前电路保持原样。' : '未应用候选：' + error.message);
      } finally {
        controller = null; el('tscircuit-generate').disabled = false; el('tscircuit-cancel').hidden = true; bridge.finish();
      }
    });
  }};
})();
