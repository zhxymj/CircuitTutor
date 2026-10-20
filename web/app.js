/* ============================================================
 * app.js —— 智绘电路演示页逻辑（自 index.html 内联脚本外提）
 * ------------------------------------------------------------
 * 职责：页面状态管理 + SSE 事件流消费 + 工具条/属性面板接线 +
 *       导出 SVG / 保存与载入工程。
 * 依赖（index.html 按序先加载）：symbols.js / wire_geometry.js /
 *       layout.js / render.js / interact.js（全局 CircuitRender、
 *       CircuitInteract 等）。
 * 外提依据：页面 JS 与 HTML 分离——编辑器 lint/高亮/可测性更好，且该文件
 *       可复用服务端静态缓存的零磁盘读路径。逻辑与原内联脚本逐字节一致。
 * ============================================================ */
(function () {
  const form = document.getElementById('form');
  const input = document.getElementById('q');
  const goBtn = document.getElementById('go');
  const editInput = document.getElementById('edit-q');
  const editBtn = document.getElementById('edit-btn');
  const editStatus = document.getElementById('edit-status');
  const revertEditBtn = document.getElementById('revert-edit');
  const editReview = document.getElementById('editReview');
  const editReviewBody = document.getElementById('editReviewBody');
  const editReviewSvg = document.getElementById('editReviewSvg');
  const imageOriginal = document.getElementById('imageOriginal');
  const editAcceptBtn = document.getElementById('editAccept');
  const editDiscardBtn = document.getElementById('editDiscard');
  const stages = document.getElementById('stages');
  const meta = document.getElementById('meta');
  const svgHost = document.getElementById('svg-host');
  const canvasEmpty = document.getElementById('canvasEmpty');
  const propsEl = document.getElementById('props');
  const propsCloseBtn = document.getElementById('propsClose');
  if (propsCloseBtn) {
    propsCloseBtn.addEventListener('click', function () {
      propsEl.style.display = 'none';
      if (ctl && ctl.clearSelection) ctl.clearSelection();
    });
  }
  const undoBtn = document.getElementById('undo');
  const redoBtn = document.getElementById('redo');
  const insertSel = document.getElementById('insert-type');
  const dirtyEl = document.getElementById('dirty');
  const ercBadge = document.getElementById('erc-badge');   // 编辑后 ERC 实时状态
  const exportBtn = document.getElementById('export-svg');
  const bomBtn = document.getElementById('export-bom');
  const spiceBtn = document.getElementById('export-spice');
  const simBtn = document.getElementById('btn-sim');
  const schemesBtn = document.getElementById('btn-schemes');
  const historyBtn = document.getElementById('btn-history');
  const saveHistBtn = document.getElementById('btn-save-history');
  const traceBtn = document.getElementById('btn-trace');
  const tracePanel = document.getElementById('tracePanel');
  const traceCloseBtn = document.getElementById('traceClose');
  const traceBody = document.getElementById('traceBody');
  const simKindSel = document.getElementById('sim-kind');
  const schemesPanel = document.getElementById('schemesPanel');
  const schemesBody = document.getElementById('schemesBody');
  const schemesClose = document.getElementById('schemesClose');
  const historyPanel = document.getElementById('historyPanel');
  const historyBody = document.getElementById('historyBody');
  const historyClose = document.getElementById('historyClose');
  const verBadge = document.getElementById('verBadge');
  const kicadBtn = document.getElementById('export-kicad');
  const pcbBtn = document.getElementById('btn-pcb');
  const realPcbBtn = document.getElementById('btn-pcb-real');
  const camPcbBtn = document.getElementById('btn-pcb-cam');
  const camPcbFile = document.getElementById('file-pcb-cam');
  const saveBtn = document.getElementById('save-json');
  const loadBtn = document.getElementById('load-json');
  const fileInput = document.getElementById('file-json');
  const imageLoadBtn = document.getElementById('load-image');
  const imageFileInput = document.getElementById('file-image');
  const imageRecognitionMode = document.getElementById('image-recognition-mode');
  const visionCorrectionHost = document.getElementById('visionCorrectionHost');
  const visionCancelRequestBtn = document.getElementById('cancel-vision-request');
  const externalLoadBtn = document.getElementById('load-external');
  const externalFileInput = document.getElementById('file-external');
  const externalSource = document.getElementById('external-source');
  const clarifyEl = document.getElementById('clarify');
  const wireBanner = document.getElementById('wireBanner');
  const wireCancelBtn = document.getElementById('wireCancel');
  const precisionCard = document.getElementById('precisionCard');
  const routingNotice = document.createElement('div');
  routingNotice.id = 'routing-notice';
  routingNotice.className = 'wb-validation-summary';
  routingNotice.setAttribute('role', 'status');
  routingNotice.setAttribute('aria-live', 'polite');
  routingNotice.hidden = true;
  const checksBody = document.getElementById('workbench-checks-body');
  checksBody.insertBefore(routingNotice, checksBody.firstChild);
  const ercPanel = document.getElementById('ercPanel');
  const ercPanelBody = document.getElementById('ercPanelBody');
  const ercPanelClose = document.getElementById('ercPanelClose');
  const moreExamplesBtn = document.getElementById('moreExamples');
  const examplesExtra = document.getElementById('examplesExtra');
  const schThemeSel = document.getElementById('sch-theme');
  const progressWrap = document.getElementById('progressWrap');
  const progressFill = document.getElementById('progressFill');
  const progressLabel = document.getElementById('progressLabel');
  let schTheme = 'kicad';
  // 多模型选择（级联面板）
  const modelBtn = document.getElementById('modelBtn');
  const modelPanel = document.getElementById('modelPanel');
  const mpProviders = document.getElementById('mp-providers');
  const mpModels = document.getElementById('mp-models');
  const mpKeyrow = document.getElementById('mp-keyrow');
  const mpKeylabel = document.getElementById('mp-keylabel');
  const mpKey = document.getElementById('mp-key');
  const mpApply = document.getElementById('mp-apply');
  const mpCustom = document.getElementById('mp-custom');
  const mpCName = document.getElementById('mp-c-name');
  const mpCModel = document.getElementById('mp-c-model');
  const mpCUrl = document.getElementById('mp-c-url');
  const mpCKey = document.getElementById('mp-c-key');
  const mpCApply = document.getElementById('mp-c-apply');
  const mpStatus = document.getElementById('mp-status');

  // ---------- 电路状态（交互层就地修改它，再触发重渲染） ----------
  let circuit = null;      // 当前 CircuitJSON
  let editBusy = false;    // AI 增量改图请求在途，防止重复提交
  let pendingEdit = null;  // 服务端候选图，等待人工审核；校验状态单独显示
  let pendingVision = null; // 本机模型的原始结果与人工校正处于独立待处理阶段
  let visionAbortController = null;
  if (visionCancelRequestBtn) visionCancelRequestBtn.addEventListener('click', function () {
    if (visionAbortController) visionAbortController.abort();
  });
  // 最近一次成功应用的 AI 改图：用于「撤回更改」按钮与状态提示。
  // priorSnapshot 为改图前 CircuitJSON；apply 后 seedUndo 进撤销栈。
  let lastAiEdit = null;   // { instruction, priorSnapshot } | null
  function clearLastAiEdit() {
    lastAiEdit = null;
    if (revertEditBtn) revertEditBtn.disabled = true;
  }
  function revertLastAiEdit() {
    if (!circuit || !ctl || !lastAiEdit) return;
    if (JSON.stringify(circuit) !== lastAiEdit.appliedSnapshot) {
      clearLastAiEdit();
      setEditStatus('电路已再次修改，请使用撤销逐步回退', true);
      return;
    }
    if (!ctl.canUndo()) {
      clearLastAiEdit();
      setEditStatus('没有可撤回的修改', true);
      return;
    }
    const instr = lastAiEdit.instruction || '';
    ctl.undo();  // onChange → markDirty + scheduleErc + onRerender → syncHistoryButtons
    clearLastAiEdit();
    setEditStatus(instr ? ('已撤回：' + instr) : '已撤回修改前的电路');
    setEditChanges('已撤回，恢复到 AI 修改之前的电路');
    meta.textContent = '已恢复到 AI 修改之前的电路 · ' + (circuit ? circuitCounts(circuit) : '');
    pulseErcBadge();
  }
  let ctl = null;         // 当前的交互控制器（每次渲染后重建）
  let workbench = null;   // 工程台只读取此处的真实电路与交互控制器。
  let selectedId = null;  // 当前选中的元件 id（跨重渲染保持）
  let lastVB = null;      // 最近一次缩放/平移视图（跨重渲染保持）
  let ercSummary = null;  // 服务端 ERC 终检摘要（生成流程，SSE 'erc' 事件）
  // 编辑后重算 ERC：完整报告（比摘要多 issues[]，渲染层要它画引脚标注）。
  // 生成流程给的是摘要、编辑后重算给的是全量报告，二者合并成同一份状态：
  // ercReport.passed + 计数用于状态栏，ercReport.issues 用于画布标注。
  let ercReport = null;
  let ercTimer = null;    // 防抖句柄（连续拖动/输入只算最后一次）
  let ercSeq = 0;         // 请求序号：丢弃乱序返回的旧响应
  let ercPending = 0;     // 在途请求数（状态栏显示“重算中…”）

  // “已手动修改”标记：交互层任何变更（含撤销/重做）都会触发 onChange。
  // dirty 状态同时驱动 beforeunload——手动改动只存在内存里，
  // 直接关页即丢，关闭/刷新前浏览器弹出确认（无修改时不打扰）
  let dirty = false;
  function markDirty(v) {
    dirty = !!v;
    dirtyEl.textContent = v ? '（已手动修改）' : '';
    if (workbench) workbench.refresh();
  }
  function setMetaFail(shortMsg, detail) {
    if (!meta) return;
    meta.innerHTML = '';
    const span = document.createElement('span');
    span.textContent = shortMsg || '操作失败';
    meta.appendChild(span);
    if (detail) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.textContent = '详情';
      btn.style.cssText = 'margin-left:8px;font-size:12px;padding:2px 8px;border-radius:6px;border:1px solid var(--line);background:var(--panel-2);cursor:pointer';
      const box = document.createElement('div');
      box.hidden = true;
      box.style.cssText = 'margin-top:6px;font-size:12px;color:var(--mut);white-space:pre-wrap';
      box.textContent = String(detail).slice(0, 500);
      btn.addEventListener('click', function () { box.hidden = !box.hidden; });
      meta.appendChild(btn);
      meta.appendChild(box);
    }
  }

  function setEditStatus(text, isError) {
    if (!editStatus) return;
    editStatus.textContent = text || '';
    editStatus.className = isError ? 'err' : '';
  }

  function clearPendingEdit(message) {
    pendingEdit = null;
    pendingVision = null;
    if (editReview) editReview.classList.remove('on');
    if (editReviewBody) editReviewBody.replaceChildren();
    if (editReviewSvg) editReviewSvg.replaceChildren();
    if (imageOriginal) { imageOriginal.replaceChildren(); imageOriginal.hidden = true; }
    if (visionCorrectionHost) { visionCorrectionHost.replaceChildren(); visionCorrectionHost.hidden = true; }
    if (editAcceptBtn) editAcceptBtn.textContent = '接受修改';
    if (message) setEditStatus(message);
    syncHistoryButtons();
  }


  // ---------- 体验层：来源徽章 / ERC 建议 / 示例卡 / 证据 / 导出预览 ----------
  const srcBadge = document.getElementById('srcBadge');
  const editChangesEl = document.getElementById('editChanges');
  const evidencePanel = document.getElementById('evidencePanel');
  const evidenceBody = document.getElementById('evidenceBody');
  const evidenceClose = document.getElementById('evidenceClose');
  const evidenceBtn = document.getElementById('btn-evidence');
  const demoBtn = document.getElementById('btn-demo');
  const kicadModal = document.getElementById('kicadModal');
  const kicadPreview = document.getElementById('kicadPreview');
  const kicadModalClose = document.getElementById('kicadModalClose');
  const kicadModalOk = document.getElementById('kicadModalOk');
  const kicadCopySteps = document.getElementById('kicadCopySteps');
  let demoMode = false;
  try {
    demoMode = /[?&]demo=1\b/.test(location.search) || localStorage.getItem('zht-demo') === '1';
  } catch (e) { demoMode = /[?&]demo=1\b/.test(location.search); }

  // ERC 规则 → 用户可执行建议（message 已含「建议/应」时不重复前缀）
  const RULE_TIP = {
    'LED缺限流电阻': '在 LED 与电源之间串联 220Ω～1kΩ 限流电阻',
    'LED限流电阻过小': '把限流电阻增大到至少 100Ω，常用 220Ω～1kΩ',
    '极性反接': 'LED 长脚(+)接高电位，短脚(-)接低电位；请对调两条连线',
    '电阻功耗超限': '换更大阻值，或改用功率更大的电阻封装（建议 ≥1/4W）',
    '电阻功耗超额定': '换更大阻值，或改用功率更大的电阻封装（建议 ≥1/4W）',
    'LED超额定电流': '增大串联电阻，把电流降到 ≤20mA（指示灯常用 2～10mA）',
    'LED电流偏大': '可适当增大限流电阻，指示灯常用 2～10mA',
    '芯片未供电': '为芯片 VCC/GND 连接电源与地，并就近放置 100nF 去耦电容',
    'IC缺电源': '为芯片 VCC/GND 连接电源与地，并就近放置 100nF 去耦电容',
    'I2C缺上拉': 'SDA/SCL 各接约 4.7kΩ（常用 2.2k～10k）上拉到逻辑电源',
    '电源短路': '去掉把电源与地直接相连的导线或 0Ω 电阻',
    '0Ω电阻短路': '去掉把电源与地直接相连的 0Ω 电阻或导线',
    '悬空输入': '按数据手册连接该引脚；若确为 NC，请标记 no-connect',
    '幻觉引脚': '改用目录内合法元件与引脚名',
    '未知元件': '改用受支持的元件类型',
  };
  function ruleTipOf(it) {
    if (!it) return '';
    const msg = String(it.message || '');
    if (/建议|应串联|应连接|请对调|请检查/.test(msg) && msg.length > 18) {
      // message 已含可操作建议时，仅在无 RULE 命中且缺「建议：」时不强加
    }
    const rule = String(it.rule || '');
    let tip = RULE_TIP[rule] || '';
    if (!tip) {
      const keys = Object.keys(RULE_TIP);
      for (let i = 0; i < keys.length; i++) {
        if (msg.indexOf(keys[i]) >= 0 || rule.indexOf(keys[i]) >= 0) { tip = RULE_TIP[keys[i]]; break; }
      }
    }
    if (!tip) return '';
    if (msg.indexOf('建议') >= 0 && msg.indexOf(tip.slice(0, 6)) >= 0) return '';
    return tip;
  }

  function setSourceBadge(kind, title) {
    if (!srcBadge) return;
    srcBadge.className = 'src-badge';
    if (!kind) { srcBadge.textContent = ''; return; }
    srcBadge.classList.add('on', kind);
    if (kind === 'ai') { srcBadge.textContent = 'AI 完成'; srcBadge.title = title || '在线模型生成并通过门禁'; }
    else if (kind === 'draft') { srcBadge.textContent = '未验证草稿'; srcBadge.title = title || '尚未通过完整官方校验'; }
    else if (kind === 'validated') { srcBadge.textContent = '校验通过'; srcBadge.title = title || '当前电路已通过适用的官方校验门禁'; }
    else if (kind === 'image') { srcBadge.textContent = '图片候选'; srcBadge.title = title || '图片识别候选，已进入人工纠错'; }
    else if (kind === 'template') { srcBadge.textContent = '模板回退'; srcBadge.title = title || '模型失败或未过门禁，已用内置模板，不计入 AI 完成'; }
    else if (kind === 'rejected') { srcBadge.textContent = '已拒绝'; srcBadge.title = title || '请求被拒绝，未生成新电路'; }
  }

  function circuitCounts(c) {
    const parts = (c && c.parts && c.parts.length) || 0;
    const conns = (c && c.connections && c.connections.length) || 0;
    return parts + ' 个元件 / ' + conns + ' 条连线';
  }

  function refreshSourceFromCircuit() {
    const origin = circuit && circuit.provenance && circuit.provenance.origin;
    if (origin === 'image') setSourceBadge('image');
    else if (origin === 'template') setSourceBadge('template');
    else if (origin === 'prompt') setSourceBadge('draft', '电路已修改，需重新运行完整验收');
    else setSourceBadge(null);
  }

  function summarizeChangesText(ch) {
    if (!ch) return '';
    const bits = [];
    if (ch.parts_modified && ch.parts_modified.length)
      bits.push('修改 ' + ch.parts_modified.join('、'));
    if (ch.parts_added && ch.parts_added.length)
      bits.push('新增 ' + ch.parts_added.join('、'));
    if (ch.parts_removed && ch.parts_removed.length)
      bits.push('删除 ' + ch.parts_removed.join('、'));
    if (ch.connections_added) bits.push('连线 +' + ch.connections_added);
    if (ch.connections_removed) bits.push('连线 -' + ch.connections_removed);
    return bits.length ? ('改动摘要：' + bits.join('；')) : '改动摘要：无明显结构变化';
  }

  function setEditChanges(text) {
    if (!editChangesEl) return;
    editChangesEl.textContent = text || '';
  }

  // 审核直接比较候选图与提交时快照，避免服务端摘要漏掉字段变化。
  function describePart(part) {
    const value = part && part.attrs && part.attrs.value;
    return String(part.id) + '（' + String(part.type || '未知类型') +
      (value == null ? '' : '，' + String(value)) + '）';
  }

  function connectionLabel(conn) {
    if (!Array.isArray(conn)) return JSON.stringify(conn);
    const details = [];
    if (conn[2]) details.push('颜色 ' + String(conn[2]));
    if (conn[3] && (Array.isArray(conn[3]) ? conn[3].length : true))
      details.push('路径 ' + JSON.stringify(conn[3]).slice(0, 90));
    return String(conn[0] || '?') + ' ↔ ' + String(conn[1] || '?') +
      (details.length ? '（' + details.join('，') + '）' : '');
  }

  function connectionChanges(before, after) {
    const oldList = (before.connections || []).map(function (c) { return JSON.stringify(c); });
    const newList = (after.connections || []).map(function (c) { return JSON.stringify(c); });
    const oldCount = new Map();
    const newCount = new Map();
    oldList.forEach(function (key) { oldCount.set(key, (oldCount.get(key) || 0) + 1); });
    newList.forEach(function (key) { newCount.set(key, (newCount.get(key) || 0) + 1); });
    const added = [], removed = [];
    newCount.forEach(function (count, key) {
      for (let i = 0; i < count - (oldCount.get(key) || 0); i++) added.push(JSON.parse(key));
    });
    oldCount.forEach(function (count, key) {
      for (let i = 0; i < count - (newCount.get(key) || 0); i++) removed.push(JSON.parse(key));
    });
    return { added: added, removed: removed };
  }

  function buildEditDiff(before, after) {
    const oldParts = new Map((before.parts || []).map(function (p) { return [p.id, p]; }));
    const newParts = new Map((after.parts || []).map(function (p) { return [p.id, p]; }));
    const partsAdded = [], partsRemoved = [], partsModified = [];
    newParts.forEach(function (part, id) {
      if (!oldParts.has(id)) partsAdded.push(describePart(part));
      else if (JSON.stringify(oldParts.get(id)) !== JSON.stringify(part)) {
        const oldPart = oldParts.get(id);
        const fields = [];
        const keys = new Set(Object.keys(oldPart).concat(Object.keys(part)));
        keys.forEach(function (key) {
          if (JSON.stringify(oldPart[key]) === JSON.stringify(part[key])) return;
          if (key === 'attrs') {
            const prev = oldPart.attrs || {}, next = part.attrs || {};
            new Set(Object.keys(prev).concat(Object.keys(next))).forEach(function (attr) {
              if (JSON.stringify(prev[attr]) !== JSON.stringify(next[attr]))
                fields.push(attr + '：' + String(prev[attr] == null ? '空' : prev[attr]) +
                  ' → ' + String(next[attr] == null ? '空' : next[attr]));
            });
          } else if (key !== 'id') {
            fields.push(key + '：' + String(oldPart[key] == null ? '空' : oldPart[key]) +
              ' → ' + String(part[key] == null ? '空' : part[key]));
          }
        });
        partsModified.push(String(id) + '：' + (fields.join('；') || '内容变化'));
      }
    });
    oldParts.forEach(function (part, id) {
      if (!newParts.has(id)) partsRemoved.push(describePart(part));
    });
    return { partsAdded: partsAdded, partsRemoved: partsRemoved,
             partsModified: partsModified, connections: connectionChanges(before, after) };
  }

  function normalizeEditErc(rawErc) {
    rawErc = rawErc || {};
    function ercCount(arrayKey, numberKey) {
      return Array.isArray(rawErc[arrayKey])
        ? rawErc[arrayKey].length : Number(rawErc[numberKey]) || 0;
    }
    return {
      passed: !!rawErc.passed,
      fatal: ercCount('fatal_errors', 'fatal'),
      major: ercCount('major_errors', 'major'),
      minor: ercCount('minor_errors', 'minor'),
      warning: ercCount('warnings', 'warning'),
      failure_count: Number(rawErc.failure_count) || 0,
      issues: Array.isArray(rawErc.issues) ? rawErc.issues : [],
    };
  }

  function appendReviewGroup(title, items, kind) {
    if (!editReviewBody) return;
    const section = document.createElement('section');
    section.className = 'edit-review-group ' + (kind || '');
    const h = document.createElement('h5');
    h.textContent = title + '（' + items.length + '）';
    section.appendChild(h);
    if (!items.length) {
      const empty = document.createElement('p');
      empty.textContent = '无';
      section.appendChild(empty);
    } else {
      const list = document.createElement('ul');
      items.forEach(function (item) {
        const li = document.createElement('li');
        li.textContent = item;
        list.appendChild(li);
      });
      section.appendChild(list);
    }
    editReviewBody.appendChild(section);
  }

  function focusReviewIssue(pins) {
    if (!editReviewSvg || !Array.isArray(pins)) return;
    editReviewSvg.querySelectorAll('g.review-focused').forEach(function (g) {
      g.classList.remove('review-focused');
    });
    const ids = new Set(pins.map(function (pin) { return String(pin).split(':')[0]; }));
    let first = null;
    editReviewSvg.querySelectorAll('g[data-part]').forEach(function (g) {
      if (!ids.has(g.getAttribute('data-part'))) return;
      g.classList.add('review-focused');
      if (!first) first = g;
    });
    if (first && first.scrollIntoView) first.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  function showEditReview(candidate, originalCircuit, originalSnapshot, instruction) {
    const normalized = normalizeEditErc(candidate.erc || candidate.erc_report);
    pendingEdit = { circuit: candidate.circuit, originalCircuit: originalCircuit,
      originalSnapshot: originalSnapshot, instruction: instruction, erc: normalized,
      changes: candidate.changes, validation: candidate.validation || null,
      origin: candidate.origin || 'ai', imageRecognition: candidate.imageRecognition || null,
      imageCorrection: candidate.imageCorrection || null, imageOverlay: candidate.imageOverlay || null };
    if (editReviewBody) {
      editReviewBody.replaceChildren();
      const diff = buildEditDiff(JSON.parse(originalSnapshot) || { parts: [], connections: [] }, candidate.circuit);
      appendReviewGroup('新增器件', diff.partsAdded, 'added');
      appendReviewGroup('修改器件', diff.partsModified, 'modified');
      appendReviewGroup('删除器件', diff.partsRemoved, 'removed');
      appendReviewGroup('新增连线', diff.connections.added.map(connectionLabel), 'added');
      appendReviewGroup('删除连线', diff.connections.removed.map(connectionLabel), 'removed');
      const issues = document.createElement('section');
      issues.className = 'edit-review-group issues';
      const h = document.createElement('h5');
      h.textContent = '候选图 ERC（致命 ' + normalized.fatal + ' · 严重 ' + normalized.major +
        ' · 提示 ' + normalized.minor + ' · 警告 ' + normalized.warning + '）';
      issues.appendChild(h);
      if (!normalized.issues.length) {
        const p = document.createElement('p');
        p.textContent = candidate.erc || candidate.erc_report
          ? '未发现问题' : '尚无 ERC 结果，请检查校验门禁';
        issues.appendChild(p);
      }
      normalized.issues.forEach(function (issue) {
        const row = document.createElement('button');
        row.type = 'button';
        row.className = 'review-issue ' + String(issue.severity || '');
        const pins = Array.isArray(issue.pins) ? issue.pins : [];
        row.textContent = (issue.rule || issue.severity || '问题') + '：' +
          (issue.message || '') + (pins.length ? ' · 位置：' + pins.join(' / ') : ' · 位置未提供');
        row.disabled = !pins.length;
        if (pins.length) row.addEventListener('click', function () { focusReviewIssue(pins); });
        issues.appendChild(row);
      });
      editReviewBody.appendChild(issues);
      if (Array.isArray(candidate.imageIssues)) {
        appendReviewGroup('图片识别待确认项', candidate.imageIssues.map(function (item) {
          return item.message || item.code || '未确认';
        }), 'issues');
      }
      if (Array.isArray(candidate.externalIssues)) {
        appendReviewGroup('外部结果待确认项', candidate.externalIssues.map(function (item) {
          return (item.path ? item.path + '：' : '') + (item.message || item.code || '未确认');
        }), 'issues');
      }
      if (candidate.externalEvidence) {
        const evidence = candidate.externalEvidence;
        const evidenceButton = document.createElement('button');
        evidenceButton.type = 'button';
        evidenceButton.textContent = '下载原始外部文件';
        evidenceButton.addEventListener('click', function () {
          download(evidence.name, new Blob([evidence.content], { type: 'text/plain;charset=utf-8' }));
        });
        editReviewBody.appendChild(evidenceButton);
      }
      if (candidate.externalConverted) {
        const converted = candidate.externalConverted;
        const convertedButton = document.createElement('button');
        convertedButton.type = 'button';
        convertedButton.textContent = '下载转换后的 CircuitJSON';
        convertedButton.addEventListener('click', function () {
          download(converted.name, new Blob([JSON.stringify(converted.circuit, null, 2)],
            { type: 'application/json;charset=utf-8' }));
        });
        editReviewBody.appendChild(convertedButton);
      }
      if (candidate.imageRecognition) {
        const recognition = candidate.imageRecognition;
        appendReviewGroup('原始模型检测框与评分', (recognition.detections || []).map(function (item, index) {
          const label = item.id || ('检测框 ' + (index + 1));
          const type = item.class_name || item.type || '未知类型';
          const meaning = item.scoreMeaning || '模型置信度；不是电气正确性';
          return label + '：' + type + ' · 评分 ' + item.score + ' · ' + meaning;
        }));
        const words = recognition.ocr && recognition.ocr.words;
        const texts = Array.isArray(words) ? words : (recognition.texts || []);
        appendReviewGroup('原始识别文字（数值需人工填写）', texts.slice(0, 200).map(function (item) {
          return String(item.text || '') + (Number.isFinite(item.confidence) ? ' · OCR 置信度 ' + item.confidence.toFixed(1) + '/100' : '');
        }));
        const evidenceButton = document.createElement('button');
        evidenceButton.type = 'button';
        evidenceButton.textContent = '下载原始模型识别证据';
        evidenceButton.addEventListener('click', function () {
          download('图片原始识别证据.json', new Blob([JSON.stringify(recognition, null, 2)], { type: 'application/json' }));
        });
        editReviewBody.appendChild(evidenceButton);
      }
      if (candidate.imageCorrection) {
        const correctionButton = document.createElement('button');
        correctionButton.type = 'button'; correctionButton.textContent = '下载人工校正证据';
        correctionButton.addEventListener('click', function () {
          download('图片人工校正记录.json', new Blob([JSON.stringify(candidate.imageCorrection, null, 2)],
            { type: 'application/json;charset=utf-8' }));
        });
        editReviewBody.appendChild(correctionButton);
      }
      if (candidate.imageValidationEvidence) {
        const validationEvidenceButton = document.createElement('button');
        validationEvidenceButton.type = 'button'; validationEvidenceButton.textContent = '下载图片电气校验报告';
        validationEvidenceButton.addEventListener('click', function () {
          download('图片电气校验报告.json', new Blob([JSON.stringify(candidate.imageValidationEvidence, null, 2)],
            { type: 'application/json;charset=utf-8' }));
        });
        editReviewBody.appendChild(validationEvidenceButton);
      }
      if (candidate.validation) {
        const gate = candidate.validation;
        const blockers = (gate.blocking_gates || []).map(function (key) {
          const item = gate.gates && gate.gates[key] || {};
          return key + '：' + (item.reason || item.status || '未通过');
        });
        appendReviewGroup(gate.accepted ? '官方校验通过' : '官方校验未完成或未通过',
          blockers, gate.accepted ? 'added' : 'issues');
      }
    }
    if (editReviewSvg) {
      const out = CircuitRender.renderCircuit(candidate.circuit, {
        title: '候选电路预览', theme: schTheme, erc: normalized,
        layout: (candidate.circuit.parts || []).every(function (p) {
          return Number.isFinite(Number(p.left)) && Number.isFinite(Number(p.top));
        }) ? 'keep' : undefined,
      });
      editReviewSvg.innerHTML = out.svg;
    }
    if (editReview) editReview.classList.add('on');
    if (editAcceptBtn) editAcceptBtn.textContent = candidate.validation && candidate.validation.accepted
      ? '接受修改' : '应用未验证草稿';
    setEditStatus(candidate.validation && candidate.validation.accepted
      ? '候选图已通过官方校验，请审核后接受'
      : '候选图仅供草稿编辑；官方校验未完成或未通过');
    syncHistoryButtons();
  }

  function pulseErcBadge() {
    if (!ercBadge) return;
    ercBadge.classList.remove('pulse');
    void ercBadge.offsetWidth;
    ercBadge.classList.add('pulse');
  }

  function applyDemoMode(on) {
    demoMode = !!on;
    try { localStorage.setItem('zht-demo', demoMode ? '1' : '0'); } catch (e) { /* ignore */ }
    document.body.classList.toggle('demo-mode', demoMode);
    if (modelBtn) modelBtn.style.display = demoMode ? 'none' : '';
    if (modelPanel && demoMode) modelPanel.style.display = 'none';
    if (mpKey) { mpKey.value = ''; mpKey.disabled = demoMode; }
    if (mpCKey) { mpCKey.value = ''; mpCKey.disabled = demoMode; }
    if (demoBtn) demoBtn.textContent = demoMode ? '退出演示模式' : '演示模式';
  }

  function openEvidence() {
    if (!evidencePanel || !evidenceBody) return;
    evidencePanel.classList.add('on');
    if (workbench) workbench.showEvidence();
  }

  function closeEvidence() {
    if (evidencePanel) evidencePanel.classList.remove('on');
  }

  function showKicadModal() {
    if (!kicadModal) return;
    kicadModal.classList.add('on');
    if (kicadPreview) {
      const svg = svgHost && svgHost.querySelector('svg');
      if (svg) {
        kicadPreview.innerHTML = '';
        const clone = svg.cloneNode(true);
        clone.removeAttribute('style');
        kicadPreview.appendChild(clone);
        const note = document.createElement('div');
        note.className = 'km-note';
        note.textContent = '网页预览（当前画布），KiCad 工程以 zip 内文件为准';
        kicadPreview.appendChild(note);
      } else {
        kicadPreview.textContent = '（暂无画布预览，请查看 zip 内 PREVIEW.svg）';
      }
    }
  }

  function closeKicadModal() {
    if (kicadModal) kicadModal.classList.remove('on');
  }

  const KICAD_STEPS = [
    '1. 解压下载的 zip，进入工程文件夹',
    '2. 双击 OPEN_IN_KICAD.bat，或 KiCad →「打开工程」选择 *.kicad_pro',
    '3. 项目管理器双击 *.kicad_sch；空白时按 Home/适合页面',
    '注意：不要用「文件→导入」；可先看包内 PREVIEW.svg',
  ].join('\n');

  if (evidenceBtn) evidenceBtn.addEventListener('click', openEvidence);
  if (evidenceClose) evidenceClose.addEventListener('click', closeEvidence);
  if (demoBtn) demoBtn.addEventListener('click', function () { applyDemoMode(!demoMode); });
  if (kicadModalClose) kicadModalClose.addEventListener('click', closeKicadModal);
  if (kicadModalOk) kicadModalOk.addEventListener('click', closeKicadModal);
  if (kicadCopySteps) {
    kicadCopySteps.addEventListener('click', function () {
      const text = KICAD_STEPS;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () {
          kicadCopySteps.textContent = '已复制';
          setTimeout(function () { kicadCopySteps.textContent = '复制打开步骤'; }, 1500);
        }).catch(function () {
          window.prompt('请手动复制：', text);
        });
      } else {
        window.prompt('请手动复制：', text);
      }
    });
  }

  // 空状态示例卡：填入需求并生成
  (function bindEmptyExamples() {
    const box = document.getElementById('emptyExamples');
    if (!box) return;
    box.addEventListener('click', function (e) {
      const card = e.target.closest ? e.target.closest('.ee-card') : null;
      if (!card) return;
      const q = card.getAttribute('data-q') || card.textContent.trim();
      if (input) input.value = q;
      if (goBtn) {
        goBtn.classList.add('hint');
        setTimeout(function () { goBtn.classList.remove('hint'); }, 1200);
      }
      if (form && form.requestSubmit) form.requestSubmit();
      else if (goBtn) goBtn.click();
    });
  })();

  if (demoMode) applyDemoMode(true);


  window.addEventListener('beforeunload', function (e) {
    if (!dirty || !circuit) return;
    e.preventDefault();
    e.returnValue = '';   // Chrome 需要 returnValue 才会弹确认框
  });
  // 撤销/重做按钮可用态：历史栈挂在 circuit 对象上、跨重渲染保留，
  // 每次渲染后（也是每次变更/撤销后）刷新一次
  function syncHistoryButtons() {
    const has = !!circuit;
    exportBtn.disabled = !has;   // 导出/保存都要先有电路
    if (bomBtn) bomBtn.disabled = !has;
    if (spiceBtn) spiceBtn.disabled = !has;
    if (simBtn) simBtn.disabled = !has;
    if (simKindSel) simKindSel.disabled = !has;
    if (schemesBtn) schemesBtn.disabled = !has;
    if (saveHistBtn) saveHistBtn.disabled = !has;
    if (editBtn) {
      editBtn.disabled = !circuit;
      if (editBusy || pendingEdit) editBtn.disabled = true;
    }
    saveBtn.disabled = !has;
    kicadBtn.disabled = !has;
    if (pcbBtn) pcbBtn.disabled = !has;
    if (realPcbBtn) realPcbBtn.disabled = !has;
    if (camPcbBtn) camPcbBtn.disabled = !has;
    if (revertEditBtn) {
      revertEditBtn.disabled = !(lastAiEdit && ctl && ctl.canUndo());
    }
    if (!ctl) { undoBtn.disabled = true; redoBtn.disabled = true; return; }
    undoBtn.disabled = !ctl.canUndo();
    redoBtn.disabled = !ctl.canRedo();
  }

  // ---------- 编辑后重算 ERC（POST /api/erc） ----------
  // 为什么需要：ERC 原先只在生成流水线末尾算一次，用户在画布上拖动/
  // 改值/删除/新增元件后就再也不重算——「设计规则检查」与实际电路脱节。
  // 这里在每次编辑提交后防抖送回服务端重算，把最新 issues 画回画布。
  //
  // 纯计算、零 token 成本，故不限流；但仍做三件事保证不打扰用户：
  //   1) 防抖 300ms —— 连续拖动只在停手后算一次；
  //   2) 序号守卫   —— 慢响应被后发的快响应超越时丢弃（避免旧结果覆盖新结果）；
  //   3) 静默失败   —— 服务端不可用时保留上一次结果，不弹错、不清空画布。
  function ercStatusText() {
    if (ercPending > 0) return 'ERC 重算中…';
    if (!ercReport) return '';
    const r = ercReport;
    if (r.passed && !r.warning) return 'ERC 全部通过 ✔';
    return 'ERC：致命 ' + r.fatal + ' · 严重 ' + r.major +
      ' · 提示 ' + r.minor + ' · 警告 ' + r.warning;
  }

  function ercStatusClass() {
    if (ercPending > 0 || !ercReport) return '';
    if (ercReport.passed && !ercReport.warning) return 'ok';
    if (ercReport.fatal || ercReport.major) return 'bad';
    return 'warn';
  }

  function refreshErcBadge() {
    if (!ercBadge) return;
    const txt = ercStatusText();
    ercBadge.textContent = txt;
    ercBadge.className = 'erc-badge ' + ercStatusClass();
    ercBadge.title = ercReport
      ? (ercReport.issues && ercReport.issues.length
         ? '共 ' + ercReport.issues.length + ' 条检查意见，点击查看明细'
         : '设计规则检查未发现问题')
      : '';
    if (ercPanel && ercPanel.classList.contains('on')) renderErcPanel();
  }

  function refreshErcBadgePulse() {
    refreshErcBadge();
    pulseErcBadge();
  }

  function renderErcPanel() {
    if (!ercPanelBody) return;
    ercPanelBody.innerHTML = '';
    if (!ercReport) {
      const p = document.createElement('div');
      p.className = 'empty';
      p.textContent = '尚未生成或编辑电路，暂无检查结果。';
      ercPanelBody.appendChild(p);
      return;
    }
    const issues = ercReport.issues || [];
    if (!issues.length) {
      const p = document.createElement('div');
      p.className = 'empty';
      p.textContent = '设计规则检查未发现问题 ✔';
      ercPanelBody.appendChild(p);
      return;
    }
    // 按严重级别分组：fatal → major → minor → warning（高严重度在前）
    const SEV_META = {
      fatal:   { label: '致命', icon: '❌', cls: 'g-fatal' },
      major:   { label: '严重', icon: '⚠️', cls: 'g-major' },
      minor:   { label: '提示', icon: 'ℹ️', cls: 'g-minor' },
      warning: { label: '警告', icon: '💡', cls: 'g-warning' },
    };
    const order = ['fatal', 'major', 'minor', 'warning'];
    const groups = { fatal: [], major: [], minor: [], warning: [], other: [] };
    issues.forEach(function (it) {
      const sev = it && it.severity;
      if (groups[sev]) groups[sev].push(it);
      else groups.other.push(it);
    });
    order.concat(['other']).forEach(function (sev) {
      const list = groups[sev];
      if (!list || !list.length) return;
      const meta = SEV_META[sev] || { label: sev || '其他', icon: 'ℹ️', cls: 'g-minor' };
      const g = document.createElement('div');
      g.className = 'erc-group ' + meta.cls;
      const head = document.createElement('div');
      head.className = 'erc-group-head';
      const headIcon = document.createElement('span');
      headIcon.textContent = meta.icon;
      const headLabel = document.createElement('span');
      headLabel.textContent = meta.label;
      const count = document.createElement('span');
      count.className = 'count';
      count.textContent = list.length + ' 条';
      head.appendChild(headIcon); head.appendChild(headLabel); head.appendChild(count);
      g.appendChild(head);
      list.forEach(function (it) {
        g.appendChild(buildErcIssueRow(it, meta.icon));
      });
      ercPanelBody.appendChild(g);
    });
  }

  function buildErcIssueRow(it, icon) {
    const d = document.createElement('div');
    d.className = 'issue ' + (it.severity || '') +
      (it.pins && it.pins.length ? ' has-pins' : '');
    const ic = document.createElement('span');
    ic.className = 'icon';
    ic.textContent = icon || '';
    ic.setAttribute('aria-hidden', 'true');
    const body = document.createElement('div');
    body.className = 'body';
    if (it.rule) {
      const rule = document.createElement('span');
      rule.className = 'rule';
      rule.textContent = it.rule + '：';
      body.appendChild(rule);
    }
    body.appendChild(document.createTextNode(it.message || ''));
    if (it.pins && it.pins.length) {
      const hint = document.createElement('span');
      hint.className = 'pins-hint';
      hint.textContent = '点击定位元件 · ' + it.pins.slice(0, 4).join(' / ') +
        (it.pins.length > 4 ? ' …' : '');
      body.appendChild(hint);
      d.title = '点击在画布上高亮相关元件';
      d.addEventListener('click', function () { highlightErcPins(it.pins); });
    }
    const tip = ruleTipOf(it);
    if (tip) {
      const tipEl = document.createElement('div');
      tipEl.className = 'erc-tip';
      tipEl.textContent = '建议：' + tip;
      body.appendChild(tipEl);
    }
    const sev = it && it.severity;
    if (sev === 'fatal' || sev === 'major') {
      const acts = document.createElement('div');
      acts.className = 'erc-actions';
      if (it.pins && it.pins.length) {
        const bLoc = document.createElement('button');
        bLoc.type = 'button';
        bLoc.textContent = '定位';
        bLoc.addEventListener('click', function (ev) {
          ev.stopPropagation();
          highlightErcPins(it.pins);
        });
        acts.appendChild(bLoc);
      }
      const bTip = document.createElement('button');
      bTip.type = 'button';
      bTip.textContent = '查看建议';
      bTip.addEventListener('click', function (ev) {
        ev.stopPropagation();
        const t = body.querySelector('.erc-tip');
        if (t) t.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        toggleErcPanel(true);
      });
      acts.appendChild(bTip);
      if (lastAiEdit && ctl && ctl.canUndo && ctl.canUndo()) {
        const bRev = document.createElement('button');
        bRev.type = 'button';
        bRev.textContent = '撤回更改';
        bRev.addEventListener('click', function (ev) {
          ev.stopPropagation();
          revertLastAiEdit();
        });
        acts.appendChild(bRev);
      }
      body.appendChild(acts);
    }
    d.appendChild(ic);
    d.appendChild(body);
    return d;
  }

  // 点击 ERC 条目 → 在画布上选中/高亮对应元件（取 pins 里第一个位号）
  function highlightErcPins(pins) {
    if (!pins || !pins.length || !ctl) return;
    const partId = String(pins[0]).split(':')[0];
    if (!partId) return;
    if (ctl.select) ctl.select(partId);
    const esc = String(partId).replace(/["\\\]]/g, '');
    const g = svgHost && svgHost.querySelector('g[data-part="' + esc + '"]');
    if (g && g.scrollIntoView) {
      try { g.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); }
      catch (e) { g.scrollIntoView(); }
    }
  }

  function toggleErcPanel(open) {
    if (!ercPanel) return;
    const show = open == null ? !ercPanel.classList.contains('on') : !!open;
    ercPanel.classList.toggle('on', show);
    if (show) renderErcPanel();
  }

  function updatePrecisionCard(simData) {
    if (!precisionCard) return;
    const notes = [];
    if (simData && simData.led_currents && simData.led_currents.length) {
      simData.led_currents.forEach(function (led) {
        const st = led.status === 'ok' ? '正常' : (led.status === 'warn' ? '偏大' : (led.status === 'danger' ? '超额定' : String(led.status || '')));
        notes.push((led.led || 'LED') + ' 电流约 ' + (led.i_ma != null ? Number(led.i_ma).toFixed(1) + 'mA' : '—') +
          (led.resistor ? '（' + led.resistor + '）' : '') +
          ' · 电源 ' + (led.vcc != null ? led.vcc + 'V' : '—') + ' · ' + st +
          '（工程估算，不能替代器件手册）');
      });
    }
    if (simData && simData.vcc != null) {
      notes.unshift('电源电压：' + simData.vcc + 'V（估算）');
    }
    (circuit && circuit.parts || []).forEach(function (p) {
      if (p && p.attrs && p.attrs.value_source) {
        notes.push(String(p.id) + '（' + (p.attrs.value || '') + '）：' + String(p.attrs.value_source));
      }
    });
    if (!notes.length) {
      precisionCard.classList.remove('on');
      precisionCard.innerHTML = '';
      return;
    }
    let html = '<div class="pc-title">参数精确化（欧姆定律 · E12）</div><ul>';
    notes.forEach(function (n) { html += '<li></li>'; });
    // 用 text 节点填，避免转义问题
    precisionCard.innerHTML = '<div class="pc-title">参数计算结果</div><ul></ul>';
    const ul = precisionCard.querySelector('ul');
    notes.forEach(function (n) {
      const li = document.createElement('li');
      li.textContent = n;
      ul.appendChild(li);
    });
    precisionCard.classList.add('on');
  }

  function setWireBanner(on, fromRef) {
    if (!wireBanner) return;
    wireBanner.classList.toggle('on', !!on);
    const canvasEl = document.getElementById('canvas');
    if (canvasEl) canvasEl.classList.toggle('wiring-mode', !!on);
    if (on && fromRef) {
      const sp = wireBanner.querySelector('span');
      if (sp) sp.textContent = '正在连线：起点 ' + fromRef + ' —— 再单击另一个引脚完成，或按 ESC 取消';
    }
  }

  function fetchSimulateSummary() {
    if (!circuit) return;
    const original = circuit, snapshot = JSON.stringify(circuit);
    fetch('/api/simulate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ circuit: circuit }),
    }).then(function (r) {
      return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status)); return j; });
    }).then(function (j) {
      if (circuit !== original || JSON.stringify(circuit) !== snapshot) return;
      window.__lastSim = j;
      updatePrecisionCard(j);
      if (j && j.blocks && j.blocks.length) {
        const bl = j.blocks.map(function (b) { return (b.title || b.key) + '：' + (b.explain || ''); }).join(' ｜ ');
        if (precisionCard && precisionCard.classList.contains('on')) {
          const bd = document.createElement('div');
          bd.className = 'prec-line';
          bd.textContent = '电路块：' + bl;
          precisionCard.appendChild(bd);
        }
      }
    }).catch(function (e) { /* 静默：仿真摘要失败不打断编辑 */ });
  }

  function recomputeErc() {
    if (!circuit) return;
    const mySeq = ++ercSeq;
    ercPending++;
    refreshErcBadge();
    const body = JSON.stringify({ circuit: circuit });
    fetch('/api/erc', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body,
    }).then(function (resp) {
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      return resp.json();
    }).then(function (d) {
      // 序号守卫：只接受最新一次请求的结果（乱序响应直接丢弃）
      if (mySeq !== ercSeq) return;
      ercReport = {
        passed: !!d.passed,
        fatal: Number(d.fatal_errors && d.fatal_errors.length) || 0,
        major: Number(d.major_errors && d.major_errors.length) || 0,
        minor: Number(d.minor_errors && d.minor_errors.length) || 0,
        warning: Number(d.warnings && d.warnings.length) || 0,
        failure_count: Number(d.failure_count) || 0,
        issues: Array.isArray(d.issues) ? d.issues : [],
      };
      // 摘要与全量报告合并：SSE 用 fatal/major/... 四个计数，
      // 这里把全量报告折算成同形状，保证 done 事件里那套文案口径一致
      ercSummary = {
        passed: ercReport.passed, fatal: ercReport.fatal,
        major: ercReport.major, minor: ercReport.minor,
        warning: ercReport.warning,
      };
      renderAll({ layout: 'keep' });   // 重渲染：把 ERC 标注画回画布
    }).catch(function () {
      // 静默失败：保留上一次结果（离线编辑时不清空既有标注）
      if (mySeq !== ercSeq) return;
      meta.textContent = 'ERC 重算失败（服务未响应）——已保留上一次结果';
    }).then(function () {
      if (mySeq !== ercSeq) return;
      ercPending = Math.max(0, ercPending - 1);
      refreshErcBadge();
    });
  }

  function scheduleErc() {
    if (ercTimer) clearTimeout(ercTimer);
    ercTimer = setTimeout(function () { ercTimer = null; recomputeErc(); }, 300);
  }

  let validationTimer = null;
  let validationSeq = 0;
  let validationBusy = false;
  let validationError = '';
  function scheduleValidation(source, delay) {
    if (validationTimer) clearTimeout(validationTimer);
    const seq = ++validationSeq;
    window.__lastValidation = null;
    validationBusy = true;
    validationError = '';
    if (workbench) workbench.refresh();
    validationTimer = setTimeout(async function () {
      validationTimer = null;
      if (!circuit) return;
      const original = circuit, snapshot = JSON.stringify(circuit);
      const keepAiBadge = srcBadge && srcBadge.classList.contains('ai');
      try {
        const response = await fetch('/api/validate', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ circuit: JSON.parse(snapshot), source: source || 'manual' }),
        });
        const report = await response.json();
        if (!response.ok) throw new Error(report.error || ('HTTP ' + response.status));
        if (seq !== validationSeq || circuit !== original || JSON.stringify(circuit) !== snapshot) return;
        window.__lastValidation = report;
        if (report.accepted) setSourceBadge(keepAiBadge ? 'ai' : 'validated');
        else {
          refreshSourceFromCircuit();
          if (!circuit.provenance || !circuit.provenance.origin) setSourceBadge('draft');
        }
        setEditStatus(report.accepted ? '当前电路已重新通过官方校验' :
          '当前电路仍为草稿：' + (report.blocking_gates || []).join('、'));
      } catch (error) {
        if (seq !== validationSeq || circuit !== original || JSON.stringify(circuit) !== snapshot) return;
        window.__lastValidation = null;
        refreshSourceFromCircuit();
        if (!circuit.provenance || !circuit.provenance.origin) setSourceBadge('draft');
        setEditStatus('官方校验未完成：' + error.message, true);
        validationError = error.message || String(error);
      } finally {
        if (seq === validationSeq) {
          validationBusy = false;
          if (workbench) workbench.refresh();
        }
      }
    }, delay == null ? 1500 : delay);
  }

  // ---------- 渲染入口：SSE 结果与交互层重渲染共用 ----------
  function savedLayoutMode(savedCircuit) {
    const parts = savedCircuit && savedCircuit.parts;
    return Array.isArray(parts) && parts.length > 0 && parts.every(function (part) {
      return part && Number.isFinite(part.left) && Number.isFinite(part.top);
    }) ? 'keep' : undefined;
  }

  function renderAll(extra) {
    if (!circuit) return;
    extra = extra || {};
    if (extra.fresh) {
      selectedId = null; lastVB = null;
      if (validationTimer) { clearTimeout(validationTimer); validationTimer = null; }
      validationSeq++;
      validationBusy = false; validationError = ''; window.__lastValidation = null;
      window.__lastSim = null;
      precisionCard.classList.remove('on');
      precisionCard.replaceChildren();
      markDirty(false);
    } // 新电路：清空选中/视图/修改标记与旧电路的校验。
    if (ctl && !extra.fresh) {                              // 记住当前视图/选中，重渲染后还原
      lastVB = ctl.getViewBox();
      selectedId = ctl.getSelected();
    }
    const out = CircuitRender.renderCircuit(circuit, {
      title: '智绘电路',
      theme: schTheme,
      // 传入最近一次 ERC 结果：渲染层用它把致命/严重问题标注到对应引脚上。
      // 生成时由 SSE 摘要升级为全量报告后填充，编辑后由 /api/erc 刷新；
      // 未算过时为 null（渲染层按“无检查结果”处理，不画标注）。
      erc: ercReport,
      layout: extra.layout, // 交互修改后用 'keep' 保持用户布局，走线自动重排
    });
    svgHost.innerHTML = out.svg;
    if (canvasEmpty) canvasEmpty.classList.add('hidden');
    if (out.warnings && out.warnings.length)
      meta.textContent = '渲染提示：' + out.warnings.join('；');
    routingNotice.textContent = (out.warnings || []).length ? '布线与图形提示：' + out.warnings.join('；') : '';
    routingNotice.hidden = !(out.warnings || []).length;
    if (!routingNotice.hidden) {
      checksBody.hidden = false;
      document.getElementById('workbench-checks-toggle').setAttribute('aria-expanded', 'true');
    }
    const svg = svgHost.querySelector('svg');
    if (svg && window.CircuitInteract) {
      ctl = CircuitInteract.attach(svg, circuit, {
        panel: propsEl,        // 属性面板容器（由本页提供）
        selectedId: selectedId,
        viewBox: lastVB,
        onChange: function () {
          window.__lastValidation = null;
          refreshSourceFromCircuit();
          if (pendingEdit) clearPendingEdit('当前电路已修改，候选图已失效，请重新提交修改');
          if (lastAiEdit) clearLastAiEdit();
          markDirty(true);
          updatePrecisionCard();
          fetchSimulateSummary();
          // 交互层已就地改 circuit（拖动/改值/删除/插入/连线/撤销/重做）
          // → 防抖送回服务端重算 ERC，让检查结果跟着电路走
          scheduleErc();
          scheduleValidation('manual');
        },
        onRerender: function () { renderAll({ layout: 'keep' }); },
        onWireChange: function (on, fromRef) { setWireBanner(on, fromRef); },
      });
    } else {
      ctl = null;
    }
    syncHistoryButtons();
    if (workbench) workbench.refresh();
    if (extra.fresh) fetchSimulateSummary();
  }

  // 工具条：撤销 / 重做 / 缩放（历史随 circuit 对象保留，重渲染不丢）
  undoBtn.addEventListener('click', function () { if (ctl) ctl.undo(); });
  redoBtn.addEventListener('click', function () { if (ctl) ctl.redo(); });
  document.getElementById('zoom-in').addEventListener('click', function () { if (ctl) ctl.zoomIn(); });
  document.getElementById('zoom-out').addEventListener('click', function () { if (ctl) ctl.zoomOut(); });
  document.getElementById('zoom-reset').addEventListener('click', function () {
    if (ctl) ctl.resetView();
  });
  if (schThemeSel) {
    schThemeSel.addEventListener('change', function () {
      schTheme = this.value || 'kicad';
      try { localStorage.setItem('zht-sch-theme', schTheme); } catch (e) { /* ignore */ }
      renderAll({ layout: 'keep' });
    });
    try {
      const saved = localStorage.getItem('zht-sch-theme');
      if (saved && schThemeSel.querySelector('option[value="' + saved + '"]')) {
        schTheme = saved; schThemeSel.value = saved;
      }
    } catch (e) { /* ignore */ }
  }
  if (ercBadge) {
    ercBadge.addEventListener('click', function () { toggleErcPanel(); });
    ercBadge.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleErcPanel(); }
    });
  }
  if (ercPanelClose) ercPanelClose.addEventListener('click', function () { toggleErcPanel(false); });
  if (wireCancelBtn) wireCancelBtn.addEventListener('click', function () {
    if (ctl && ctl.cancelWire) ctl.cancelWire();
    setWireBanner(false);
  });
  if (moreExamplesBtn && examplesExtra) {
    moreExamplesBtn.addEventListener('click', function () {
      const open = examplesExtra.hasAttribute('hidden');
      if (open) examplesExtra.removeAttribute('hidden');
      else examplesExtra.setAttribute('hidden', '');
      moreExamplesBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
      moreExamplesBtn.textContent = open ? '收起示例' : '更多示例';
    });
  }

  // ---------- 插入元件下拉：从 PART_INFO 动态生成（P1-2） ----------
  // 原先 index.html 里硬编码 10 个 option，而前端符号库实有 38 种元件
  // （PART_INFO 与 component_catalog.CATALOG / frontend_manifest.json 同源）
  // —— 用户明明能渲染的元件却插不进来。改为按分组从 PART_INFO 生成，
  // 数据源唯一，新增元件只改 catalog + symbols 两处即可自动出现在下拉里。
  const INSERT_GROUPS = [
    ['无源元件', ['resistor', 'capacitor', 'electrolytic', 'inductor', 'potentiometer', 'crystal', 'fuse',
                  'ferrite_bead', 'varistor']],
    ['二极管', ['led', 'diode', 'zener', 'schottky', 'photodiode', 'tvs']],
    ['三极管 / 场效应管', ['npn', 'pnp', 'nmos', 'pmos', 'phototransistor', 'scr']],
    ['运算放大器', ['opamp', 'opamp_inverting', 'opamp_noninverting',
                    'opamp_comparator', 'opamp_follower']],
    ['电源与接地', ['battery', 'vcc', 'gnd', 'ac_source', 'regulator_7805']],
    ['集成电路', ['ne555', 'optocoupler']],
    ['机电与执行器', ['buzzer', 'buzzer_passive', 'speaker', 'relay', 'dc_motor', 'servo',
                      'bridge_rectifier']],
    ['开关与接插件', ['switch', 'pushbutton', 'screw_terminal']],
    ['传感元件', ['photocell', 'thermistor']],
    ['测量仪表', ['ammeter', 'voltmeter']],
    ['官方库导入', ['capacitor_trim', 'capacitor_variable', 'capacitor_feedthrough', 'crystal_gnd', 'circuit_breaker_1p', 'circuit_breaker_2p', 'circuit_breaker_3p', 'capacitor_network', 'antenna_dipole', 'battery_cell_1v5']],
    ['电容电感细分', ['capacitor_ceramic', 'capacitor_film', 'capacitor_tantalum', 'resistor_network', 'inductor_toroid', 'transformer']],
    ['二极管扩展', ['led_rgb', 'diode_rectifier', 'triac', 'diac', 'ir_led', 'varactor']],
    ['功率半导体', ['igbt', 'ujt', 'phototriac']],
    ['数字/模拟 IC', ['ic_and', 'ic_or', 'ic_not', 'ic_xor', 'ic_nand', 'ic_nor', 'ic_buffer', 'flipflop_d', 'counter_4bit', 'shiftreg_8bit', 'lm393', 'lm324', 'ne556', 'cd4017', 'atmega328', 'esp32', 'max7219']],
    ['传感器模块', ['dht11', 'ds18b20', 'i2c_rh_sensor', 'bmp280', 'mpu6050', 'hc_sr04', 'pir', 'mq2', 'sound_sensor', 'ir_receiver', 'hall_sensor', 'load_cell', 'microphone', 'optointerrupter']],
    ['显示模块', ['sevenseg', 'lcd1602', 'oled_ssd1306', 'tm1637']],
    ['执行器', ['stepper', 'solenoid', 'heater', 'buzzer_magnetic', 'speaker_8ohm']],
    ['电源与接口', ['lm317', 'buck_module', 'boost_module', 'lipo_battery', 'solar_cell', 'usb_port', 'barrel_jack',
                     'ldo_3v3', 'ldo_1v8', 'charge_pump', 'voltage_monitor', 'power_sequencer']],
    ['开关与接插件扩展', ['fuse_resettable', 'conn_header', 'switch_toggle', 'switch_dip', 'switch_reed', 'connector_screw2', 'potentiometer_trim', 'antenna', 'jumper_wire',
                          'uart_header', 'spi_header', 'i2c_header', 'usb_c']],
    ['通信与保护器件', ['can_transceiver', 'rs485_transceiver', 'tvs_array', 'esd_diode',
                        'common_mode_choke', 'gas_discharge_tube', 'resettable_fuse_1206']],
    ['精密模拟与数据转换', ['instrumentation_amp', 'current_sense_amp', 'digital_pot', 'dac_8bit', 'adc_8bit']],
    ['执行器扩展', ['buzzer_magnetic_5v', 'relay_solid_state', 'optocoupler_ac']],
  ];

  function buildInsertOptions() {
    if (!insertSel || !CircuitInteract || !CircuitInteract.PART_INFO) return;
    const info = CircuitInteract.PART_INFO;
    const listed = {};   // 已分组的类型，最后补漏用
    insertSel.innerHTML = '';
    const ph = document.createElement('option');
    ph.value = ''; ph.textContent = '选择类型…';
    insertSel.appendChild(ph);
    INSERT_GROUPS.forEach(function (pair) {
      const group = document.createElement('optgroup');
      group.label = pair[0];
      let n = 0;
      pair[1].forEach(function (type) {
        const d = info[type];
        if (!d) return;                     // 表里没有的类型跳过（防手误写错 key）
        listed[type] = 1;
        const op = document.createElement('option');
        op.value = type;
        // 中文名 + 位号前缀（位号前缀帮用户预判插入后会分到什么编号）
        op.textContent = d.name + '（' + d.ref + '）';
        group.appendChild(op); n++;
      });
      if (n) insertSel.appendChild(group);
    });
    // 补漏组：PART_INFO 里有、但上面分组没列到的类型（新增元件时忘记归组
    // 也不会从此消失在下拉里——这是「数据源唯一」的兜底保证）
    const rest = Object.keys(info).filter(t => !listed[t]);
    if (rest.length) {
      const g2 = document.createElement('optgroup');
      g2.label = '其他';
      rest.forEach(function (type) {
        const op = document.createElement('option');
        op.value = type;
        op.textContent = info[type].name + '（' + info[type].ref + '）';
        g2.appendChild(op);
      });
      insertSel.appendChild(g2);
    }
  }
  buildInsertOptions();

  // 插入元件：选中类型即在当前视图中心插入（interact.js 自动分配位号、
  // 带默认参数、插入后自动选中；新元件初始悬空，需手动接线——
  // 引脚连线已支持，插入后即时提示连线操作）
  insertSel.addEventListener('change', function () {
    const type = this.value;
    this.value = ''; // 回到占位项，便于连续插入同类型元件
    if (!type || !ctl) return;
    const p = ctl.insertPart(type);
    if (!p) { meta.textContent = '不支持的元件类型：' + type; return; }
    const info = CircuitInteract.partInfo(type);
    meta.textContent = '已插入 ' + p.id + '（' + ((info && info.name) || type) +
      '）· 该元件悬空——单击其引脚端点开始连线，再单击另一引脚完成（ESC 取消）';
  });

  const STAGE_NAME = {
    clarify: '确认你的意图', model: '调用模型', start: '收到需求',
    extract: '识别元件', components: '确认元件', plan: '理解并规划电路',
    generate: '正在画电路图', repair_struct: '修复电路结构', repair_erc: '修复电气规则',
    autofix: '自动修复可确定的问题', precision: '工程参数估算', simulate: '准仿真',
    ratings: '额定值检查', erc: '检查电气规则（防接错/烧管）',
    structure: '检查元件与连线是否合法', kicad: '准备工程与导出信息',
    done: '完成', fallback: '已使用内置模板（不算 AI 完成）', rejected: '请求被拒绝',
  };

  // 生成进度可视化：已知阶段序列（plan→generate→erc→autofix→precision→simulate→done）
  // 未知/中间修复阶段不回退进度条，只推进。ratings 挂在 simulate 后、done 前。
  const PROGRESS_STAGES = [
    'plan', 'generate', 'erc', 'repair_erc',
    'autofix', 'precision', 'simulate', 'ratings', 'done',
  ];
  let progressPct = 0;

  function resetProgress() {
    progressPct = 0;
    if (!progressWrap) return;
    progressWrap.classList.add('on');
    progressWrap.classList.remove('done');
    if (progressFill) progressFill.style.width = '0%';
    if (progressLabel) progressLabel.textContent = '准备中…';
    progressWrap.setAttribute('aria-valuenow', '0');
  }

  function hideProgress() {
    if (!progressWrap) return;
    progressWrap.classList.remove('on');
  }

  function updateProgress(stage) {
    if (!progressWrap || !progressFill) return;
    const idx = PROGRESS_STAGES.indexOf(stage);
    if (idx < 0) return;
    const pct = Math.round(((idx + 1) / PROGRESS_STAGES.length) * 100);
    if (pct < progressPct) return;   // 只前进不后退（修复类阶段可能重复出现）
    progressPct = pct;
    progressFill.style.width = pct + '%';
    progressWrap.setAttribute('aria-valuenow', String(pct));
    if (progressLabel) {
      progressLabel.textContent = (STAGE_NAME[stage] || stage) + ' · ' + pct + '%';
    }
    if (stage === 'done' || pct >= 100) {
      progressWrap.classList.add('done');
      if (progressLabel) progressLabel.textContent = '完成 · 100%';
    }
  }

  function addStage(stage, detail, cls) {
    const li = document.createElement('li');
    if (cls) li.className = cls;
    const since = ((performance.now() - t0) / 1000).toFixed(1);
    li.innerHTML = '<span class="t">+' + since + 's</span>' +
      '<span class="tag">' + (STAGE_NAME[stage] || stage) + '</span>' +
      '<span></span>';
    li.lastElementChild.textContent = detail || '';
    stages.appendChild(li);
    updateProgress(stage);
  }

  // ---------- 多模型选择（级联：提供商 → 模型 → Key/自定义端点） ----------
  // 状态模型：
  //   llmSel = {kind:'env'}                            服务端 env Key（DeepSeek 默认）
  //          | {kind:'preset', provider, model, label} 内置提供商 + 用户自己的 Key
  //          | {kind:'custom', name, base_url, model}  自定义 OpenAI 兼容端点
  // Key 存 localStorage（lsqc_llm_keys，仅存本浏览器）；选择存 lsqc_llm_sel；
  // 自定义端点列表存 lsqc_llm_customs。cfg id 不持久化——每次生成前
  // ensureCfg() 重新 POST /api/llm_config 注册（服务端内存 TTL 自动管理）。
  let providerCatalog = null;   // /api/providers 拉取一次缓存
  let panelProvider = null;     // 面板中当前点选的一级提供商 id
  let llmSel = loadLlmSel();

  function loadJSON(k, dft) {
    try { const v = JSON.parse(localStorage.getItem(k)); return v == null ? dft : v; }
    catch (e) { return dft; }
  }
  function loadLlmSel() {
    const s = loadJSON('lsqc_llm_sel', { kind: 'env' });
    return (s && s.kind) ? s : { kind: 'env' };
  }
  function saveLlmSel() { localStorage.setItem('lsqc_llm_sel', JSON.stringify(llmSel)); }
  function llmKeys() { return loadJSON('lsqc_llm_keys', {}); }
  function setLlmKey(name, v) {
    const ks = llmKeys(); ks[name] = v;
    localStorage.setItem('lsqc_llm_keys', JSON.stringify(ks));
  }
  function customConfigs() { return loadJSON('lsqc_llm_customs', []); }
  function saveCustomConfigs(list) {
    localStorage.setItem('lsqc_llm_customs', JSON.stringify(list));
  }

  function currentModelLabel() {
    if (llmSel.kind === 'preset') return llmSel.label || llmSel.model;
    if (llmSel.kind === 'custom') return '自定义·' + (llmSel.name || llmSel.model);
    // DeepSeek 默认：服务端 env Key 已配置，或本浏览器曾在面板保存过 Key
    // → 视为已配置，按钮只显示模型名（不带「未配」注记）
    if (providerCatalog)
      return (providerCatalog.deepseek_env_key || llmKeys()['deepseek'])
        ? 'DeepSeek' : 'DeepSeek（未配Key·模板兜底）';
    return 'DeepSeek';
  }
  function syncModelBtn() { modelBtn.textContent = '模型：' + currentModelLabel(); }

  function setMpStatus(text, isErr) {
    mpStatus.innerHTML = '';
    const span = document.createElement('span');
    if (isErr) span.className = 'err';
    span.textContent = text;
    mpStatus.appendChild(span);
  }

  function ensureProviders(cb) {
    if (providerCatalog) return cb();
    fetch('/api/providers').then(function (r) { return r.json(); })
      .then(function (d) { providerCatalog = d; syncModelBtn(); cb(); })
      .catch(function () { setMpStatus('无法加载提供商列表（/api/providers）', true); });
  }

  // 一级：点选提供商 → 刷新二级模型行与输入区
  function selectProvider(pid) {
    panelProvider = pid;
    mpCustom.style.display = 'none';
    mpKeyrow.style.display = 'none';
    mpModels.innerHTML = '';
    if (pid === '__custom_new__') {
      mpCustom.style.display = 'block';
      setMpStatus('填写自定义 OpenAI 兼容端点后「保存并选用」。');
      renderProviderRow();
      return;
    }
    const cust = customConfigs().find(function (c) { return c.name === pid; });
    if (cust) {   // 已保存的自定义端点：直接选用
      llmSel = { kind: 'custom', name: cust.name, base_url: cust.base_url, model: cust.model };
      saveLlmSel(); syncModelBtn(); renderProviderRow();
      setMpStatus('已选用：' + cust.name + '（' + cust.model + ' @ ' + cust.base_url + '）');
      return;
    }
    const p = providerCatalog.providers.find(function (x) { return x.id === pid; });
    if (!p) return;
    // DeepSeek：服务端 env Key 或本浏览器保存的 Key 任一已配 → 免输 Key 直接选用
    if (p.id === 'deepseek' &&
        (providerCatalog.deepseek_env_key || llmKeys()['deepseek'])) {
      llmSel = { kind: 'env' };
      saveLlmSel(); syncModelBtn(); renderProviderRow();
      setMpStatus(providerCatalog.deepseek_env_key
        ? '已选用：DeepSeek（服务端环境变量供 Key）。亦可改用自己的 Key——点击下方模型。'
        : '已选用：DeepSeek（使用本浏览器保存的 Key）。');
    } else {
      setMpStatus('请选择模型，然后填写你自己的 ' + p.name + ' API Key。');
    }
    p.models.forEach(function (m) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'mp-btn' + (llmSel.kind === 'preset' && llmSel.provider === p.id &&
        llmSel.model === m.id ? ' sel' : '');
      b.textContent = m.label;
      b.addEventListener('click', function () {
        mpKeylabel.textContent = p.name + ' API Key（仅存本浏览器）';
        mpKey.value = llmKeys()[p.id] || '';
        mpKeyrow.style.display = 'flex';
        mpApply.onclick = function () { applyPreset(p, m); };
        mpModels.querySelectorAll('.mp-btn').forEach(function (x) { x.classList.remove('sel'); });
        b.classList.add('sel');
      });
      mpModels.appendChild(b);
    });
    renderProviderRow();
  }

  function applyPreset(p, m) {
    const key = mpKey.value.trim();
    if (!key) { setMpStatus('请填写 ' + p.name + ' 的 API Key', true); return; }
    setLlmKey(p.id, key);
    llmSel = { kind: 'preset', provider: p.id, model: m.id, label: p.name + '·' + m.id };
    saveLlmSel(); syncModelBtn();
    setMpStatus('已选用：' + llmSel.label + '。生成时将使用该模型。');
  }

  function applyCustom() {
    const name = mpCName.value.trim(), model = mpCModel.value.trim(),
      url = mpCUrl.value.trim(), key = mpCKey.value.trim();
    if (!name || !model || !url) { setMpStatus('名称 / 模型名 / base_url 均为必填', true); return; }
    if (!key) { setMpStatus('请填写 API Key（本地端点可填任意非空串，如 ollama）', true); return; }
    const list = customConfigs().filter(function (c) { return c.name !== name; });
    list.push({ name: name, base_url: url, model: model });
    saveCustomConfigs(list);
    setLlmKey('custom:' + name, key);
    llmSel = { kind: 'custom', name: name, base_url: url, model: model };
    saveLlmSel(); syncModelBtn(); renderProviderRow();
    mpCustom.style.display = 'none';
    setMpStatus('已选用：' + name + '（' + model + ' @ ' + url + '）');
  }

  function deleteCustom(name) {
    saveCustomConfigs(customConfigs().filter(function (c) { return c.name !== name; }));
    if (llmSel.kind === 'custom' && llmSel.name === name) {
      llmSel = { kind: 'env' }; saveLlmSel(); syncModelBtn();
    }
    renderProviderRow();
    setMpStatus('已删除自定义端点：' + name);
  }

  function renderProviderRow() {
    if (!providerCatalog) return;
    mpProviders.innerHTML = '';
    providerCatalog.providers.forEach(function (p) {
      if (p.custom) return;   // “自定义…”在末尾单独渲染
      const b = document.createElement('button');
      b.type = 'button';
      const active = (p.id === 'deepseek' && llmSel.kind === 'env') ||
        (llmSel.kind === 'preset' && llmSel.provider === p.id) ||
        panelProvider === p.id;
      b.className = 'mp-btn' + (active ? ' sel' : '');
      b.textContent = p.name;
      b.addEventListener('click', function () { selectProvider(p.id); });
      mpProviders.appendChild(b);
    });
    customConfigs().forEach(function (c) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'mp-btn' + (llmSel.kind === 'custom' && llmSel.name === c.name ? ' sel' : '');
      b.textContent = '⚙ ' + c.name;
      b.addEventListener('click', function () { selectProvider(c.name); });
      mpProviders.appendChild(b);
      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'mp-del';
      del.textContent = '×';
      del.title = '删除该自定义端点';
      del.addEventListener('click', function () { deleteCustom(c.name); });
      mpProviders.appendChild(del);
    });
    const add = document.createElement('button');
    add.type = 'button';
    add.className = 'mp-btn';
    add.textContent = '＋ 自定义端点…';
    add.addEventListener('click', function () { selectProvider('__custom_new__'); });
    mpProviders.appendChild(add);
  }

  modelBtn.addEventListener('click', function () {
    const show = modelPanel.style.display === 'none';
    modelPanel.style.display = show ? 'block' : 'none';
    modelBtn.classList.toggle('on', show);
    if (show) ensureProviders(function () {
      renderProviderRow();
      setMpStatus('当前：' + currentModelLabel());
    });
  });
  mpCApply.addEventListener('click', applyCustom);
  syncModelBtn();
  // 页面加载即拉取提供商目录一次：按钮注记与 ensureCfg 的 env Key 状态即时准确
  ensureProviders(function () {});

  // 生成前注册当前选择 → cfg id（Key 不进 URL；服务端只存内存）
  // env 模式无需注册，直接回 ''。失败时 errCb —— 调用方决定回退或中止。
  // DeepSeek 默认模型：优先取服务端预设表第一项（预设表是唯一权威，
  // 模型升级时只改 llm_providers.py 即可）。兜底字面量必须与服务端
  // DeepSeekClient.DEFAULT_MODEL 保持一致 —— 目录拉取失败（离线/接口异常）
  // 时仍能注册到正确的模型名，避免回落到已下线或过期的型号。
  function deepseekDefaultModel() {
    const p = providerCatalog && providerCatalog.providers &&
      providerCatalog.providers.find(function (x) { return x.id === 'deepseek'; });
    return (p && p.models && p.models[0] && p.models[0].id) || 'deepseek-flash';
  }
  function registerCfg(body, okCb, errCb) {
    fetch('/api/llm_config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(function (r) {
      return r.json().then(function (d) {
        if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
        okCb(d.cfg);
      });
    }).catch(function (e) { errCb(e); });
  }
  function ensureCfg(okCb, errCb) {
    // env（DeepSeek 默认）：服务端 env Key 已配 → 后端直接用默认配置；
    // 未配但本浏览器保存过 DeepSeek Key → 用保存的 Key 注册（行为与按钮注记一致）
    if (llmSel.kind === 'env') {
      const envSaved = llmKeys()['deepseek'];
      if (!providerCatalog || providerCatalog.deepseek_env_key || !envSaved)
        return okCb('');
      return registerCfg({ provider: 'deepseek', model: deepseekDefaultModel(),
                           api_key: envSaved, label: 'DeepSeek' }, okCb, errCb);
    }
    let body;
    if (llmSel.kind === 'preset') {
      body = { provider: llmSel.provider, model: llmSel.model,
               api_key: llmKeys()[llmSel.provider] || '', label: llmSel.label };
    } else {
      body = { provider: 'custom', base_url: llmSel.base_url, model: llmSel.model,
               api_key: llmKeys()['custom:' + llmSel.name] || '', label: llmSel.name };
    }
    if (!body.api_key)
      return errCb(new Error('请点击「模型」按钮填写 ' +
        (llmSel.label || llmSel.name) + ' 的 API Key'));
    registerCfg(body, okCb, errCb);
  }

  // ---------- AI 增量改图 ----------
  // 请求始终携带当前完整电路和 ensureCfg 注册得到的 cfg。旧电路只读地
  // 放进请求体；服务端返回合法候选后先供人工审核，接受时才提交到原
  // 历史栈。失败路径不调用 renderAll，保证用户画布可继续编辑。
  function acceptPendingEdit() {
    if (!pendingEdit) return;
    const review = pendingEdit;
    if (circuit !== review.originalCircuit ||
        JSON.stringify(circuit) !== review.originalSnapshot) {
      clearPendingEdit('当前电路已变化，候选图未应用，请重新提交修改');
      return;
    }
    if ((!ctl || !ctl.applyCircuit) && review.originalCircuit !== null) {
      clearPendingEdit('画布尚未就绪，候选图未应用');
      return;
    }
    clearPendingEdit();
    ercSeq++; // 旧电路在途 ERC 响应不得覆盖候选图的检查结果。
    if (ctl && ctl.applyCircuit) ctl.applyCircuit(review.circuit);
    else circuit = review.circuit; // 空画布的首份图片候选，尚无历史控制器。
    const rawErc = review.erc;
    ercReport = {
      passed: rawErc.passed,
      fatal: rawErc.fatal,
      major: rawErc.major,
      minor: rawErc.minor,
      warning: rawErc.warning,
      failure_count: rawErc.failure_count,
      issues: rawErc.issues,
    };
    ercSummary = ercReport ? {
      passed: ercReport.passed,
      fatal: ercReport.fatal,
      major: ercReport.major,
      minor: ercReport.minor,
      warning: ercReport.warning,
    } : null;
    ercPending = 0;
    markDirty(true);
    refreshErcBadge();
    renderAll({ layout: 'keep' });
    if (review.origin === 'ai' && review.originalCircuit) {
      lastAiEdit = { instruction: review.instruction,
        priorSnapshot: review.originalSnapshot, appliedSnapshot: JSON.stringify(circuit) };
    } else clearLastAiEdit();
    setEditChanges(summarizeChangesText(review.changes));
    updatePrecisionCard();
    scheduleErc();
    scheduleValidation(review.origin === 'image' ? 'image' :
      (review.origin === 'external' ? 'import' : 'ai'));
    const undoHint = review.origin === 'ai' && review.originalCircuit
      ? ' · 可「撤回更改」' : (review.originalCircuit ? ' · 可用 Ctrl+Z 撤销' : ' · 请继续人工纠错');
    setEditStatus((review.validation && review.validation.accepted
      ? '修改已应用并通过官方校验' : '未验证草稿已应用') + undoHint);
    meta.textContent = (review.validation && review.validation.accepted
      ? '修改已应用 · ' : '未验证草稿已应用 · ') + circuitCounts(circuit) +
      (review.originalCircuit ? '（可使用 Ctrl+Z 撤销）' : '（可插入器件、改值和修改连线）');
    setSourceBadge(review.origin === 'image' ? 'image' :
      (review.validation && review.validation.accepted ? (review.origin === 'external' ? 'validated' : 'ai') : 'draft'),
      review.origin === 'image' ? '图片识别后人工纠错草稿' :
        (review.origin === 'external' ? '外部结果经人工审核应用' : '增量改图'));
    pulseErcBadge();
    syncHistoryButtons();
  }

  function submitCircuitEdit() {
    if (!circuit || editBusy || pendingEdit || !editInput) return;
    const instruction = editInput.value.trim();
    if (!instruction) {
      setEditStatus('请输入修改内容', true);
      return;
    }
    const originalCircuit = circuit;
    // 交互层会原地修改 circuit；快照既固定本次请求输入，也用于识别
    // 请求等待期间发生的拖动、改值、连线等就地编辑。
    const originalSnapshot = JSON.stringify(originalCircuit);
    const requestCircuit = JSON.parse(originalSnapshot);
    editBusy = true;
    setEditStatus('修改中…');
    syncHistoryButtons();

    function failEdit(err) {
      // 不在这里赋值 circuit 或重绘：失败必须保留原电路引用和画布。
      if (pendingEdit) clearPendingEdit();
      setEditStatus('修改失败：' + (err && err.message ? err.message : '服务未响应'), true);
      setEditChanges('');
      setMetaFail('增量改图失败，已保留当前电路', err && (err.message || String(err)));
    }

    ensureCfg(function (cfg) {
      fetch('/api/edit', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ circuit: requestCircuit, instruction: instruction, cfg: cfg }),
      }).then(function (resp) {
        return resp.json().catch(function () { return {}; }).then(function (d) {
          if (!resp.ok) throw new Error(d.error || ('HTTP ' + resp.status));
          return d;
        });
      }).then(function (d) {
        if (!d || !d.circuit || !Array.isArray(d.circuit.parts) ||
            !Array.isArray(d.circuit.connections)) {
          throw new Error('服务未返回有效 CircuitJSON');
        }
        if (circuit !== originalCircuit || JSON.stringify(circuit) !== originalSnapshot) {
          throw new Error('当前电路已发生变化，未应用过期修改');
        }
        // 候选图与 ERC 留在审核面板中；接受之前画布和编辑历史都不变。
        showEditReview(d, originalCircuit, originalSnapshot, instruction);
      }).catch(function (e) { failEdit(e); }).then(function () {
        editBusy = false;
        syncHistoryButtons();
      });
    }, function (err) {
      // cfg 注册失败同样不触碰 circuit，且不回退到另一套模型配置。
      failEdit(err);
      editBusy = false;
      syncHistoryButtons();
    });
  }
  if (editBtn) editBtn.addEventListener('click', submitCircuitEdit);
  if (editAcceptBtn) editAcceptBtn.addEventListener('click', acceptPendingEdit);
  if (editDiscardBtn) editDiscardBtn.addEventListener('click', function () {
    clearPendingEdit('已放弃候选修改，当前电路保持原样');
  });
  if (revertEditBtn) revertEditBtn.addEventListener('click', revertLastAiEdit);
  if (editInput) editInput.addEventListener('keydown', function (ev) {
    if ((ev.ctrlKey || ev.metaKey) && ev.key === 'Enter') submitCircuitEdit();
  });

  let t0 = 0;

  // 生成中实时耗时：LLM 规划/生成动辄数十秒，进度条之外在状态栏
  // 跳动“生成中… X.Xs”让等待有脉搏（done/onerror 时停止并被最终文案覆盖）
  let elapsedTimer = null;
  function startTicker() {
    stopTicker();
    elapsedTimer = setInterval(function () {
      meta.textContent = '生成中… ' +
        ((performance.now() - t0) / 1000).toFixed(1) + 's';
    }, 100);
  }
  function stopTicker() {
    if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null; }
  }

  // ---------- 需求澄清：生成前先判歧，命中则列出理解方式供用户确认 ----------
  function hideClarify() {
    if (!clarifyEl) return; // 防御：容器缺失时静默跳过，绝不阻断生成主流程
    clarifyEl.style.display = 'none';
    clarifyEl.innerHTML = '';
  }

  function setGoLoading(on, label) {
    if (!goBtn) return;
    const spin = goBtn.querySelector('.spin');
    if (on) {
      goBtn.classList.add('loading');
      goBtn.disabled = true;
      if (spin) spin.hidden = false;
      goBtn.dataset.label = goBtn.dataset.label || goBtn.textContent.trim();
      goBtn.childNodes.forEach(function (n) {
        if (n.nodeType === 3) n.textContent = label || '生成中…';
      });
      // text node 可能被 spin 干扰：直接写 aria
      goBtn.setAttribute('aria-busy', 'true');
    } else {
      goBtn.classList.remove('loading');
      goBtn.disabled = false;
      if (spin) spin.hidden = true;
      const old = goBtn.dataset.label || '生成电路';
      goBtn.childNodes.forEach(function (n) {
        if (n.nodeType === 3) n.textContent = old;
      });
      goBtn.removeAttribute('aria-busy');
    }
  }

  function run() {
    const q = input.value.trim();
    if (!q) return;
    stopTicker();           // 清掉可能残留的耗时计时器（重复提交保护）
    stages.innerHTML = '';
    meta.textContent = '';
    // 澄清、拒绝或网络失败均不产生新电路；收到 circuit 事件后才替换工程。
    clearPendingEdit();
    syncHistoryButtons();
    hideClarify();
    resetProgress();
    setGoLoading(true, '生成中…');
    t0 = performance.now();
    addStage('clarify', '需求分析中（歧义检测）…');

    // 多模型：先注册当前选择拿到 cfg id（env 模式直接回 ''）；
    // 注册失败诚实提示并回退服务端默认模型，演示不中断
    ensureCfg(function (cfg) {
      runClarify(q, cfg);
    }, function (e) {
      addStage('fallback', '模型配置失败：' + e.message +
        ' —— 已回退服务端默认模型', 'fallback');
      runClarify(q, '');
    });
  }

  function runClarify(q, cfg) {
    fetch('/api/clarify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: q, cfg: cfg }),
    }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }).then(function (res) {
      if (res && res.status === 'clarify' &&
          Array.isArray(res.questions) && res.questions.length) {
        setGoLoading(false);   // 澄清期间允许改需求重新提交
        showClarify(q, res.questions, res.source, cfg);
      } else {
        addStage('clarify', '需求清晰，直接生成');
        startGenerate(q, null, cfg);
      }
    }).catch(function () {
      // 判歧接口失败绝不阻塞生成（判歧是增强，不是门禁）
      addStage('clarify', '歧义检测不可用，直接生成', 'fallback');
      startGenerate(q, null, cfg);
    });
  }

  function showClarify(q, questions, source, cfg) {
    if (!clarifyEl) return; // 防御：容器缺失时降级（用户可直接改需求重新提交）
    addStage('clarify', '需求有 ' + questions.length + ' 处需要确认（判歧：' +
      (source === 'llm' ? 'LLM' : '本地规则') + '），请确认后开始生成');
    clarifyEl.innerHTML = '';
    const title = document.createElement('div');
    title.className = 'cq-title';
    title.textContent = '您的需求存在多种理解方式，请确认后再生成' +
      '（未作答的问题保持默认，不做任何假设）：';
    clarifyEl.appendChild(title);

    questions.forEach(function (qu, i) {
      const item = document.createElement('div');
      item.className = 'cq-item';
      const qd = document.createElement('div');
      qd.className = 'cq-q';
      qd.textContent = (i + 1) + '. ' + qu.question;
      item.appendChild(qd);
      if (qu.reason) {
        const rd = document.createElement('div');
        rd.className = 'cq-reason';
        rd.textContent = '为什么要问：' + qu.reason;
        item.appendChild(rd);
      }
      const opts = document.createElement('div');
      opts.className = 'cq-opts';
      (qu.options || []).forEach(function (opt) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'cq-opt';
        b.textContent = opt;
        b.addEventListener('click', function () {
          opts.querySelectorAll('.cq-opt').forEach(function (x) {
            x.classList.remove('sel');
          });
          b.classList.add('sel');
        });
        opts.appendChild(b);
      });
      item.appendChild(opts);
      const free = document.createElement('input');
      free.className = 'cq-free';
      free.placeholder = '其他（自行填写，优先于上方选项；留空 = 此问题不作答）';
      item.appendChild(free);
      clarifyEl.appendChild(item);
    });

    const actions = document.createElement('div');
    actions.className = 'cq-actions';
    const ok = document.createElement('button');
    ok.id = 'cq-ok';
    ok.type = 'button';
    ok.textContent = '确认并生成';
    ok.addEventListener('click', function () {
      const qa = collectQa(questions);
      hideClarify();
      addStage('clarify', qa.length
        ? '已并入 ' + qa.length + ' 条澄清答复' : '未作答，按原需求生成');
      startGenerate(q, qa, cfg);
    });
    const skip = document.createElement('button');
    skip.id = 'cq-skip';
    skip.type = 'button';
    skip.textContent = '跳过，直接生成';
    skip.addEventListener('click', function () {
      hideClarify();
      addStage('clarify', '已跳过澄清，按原需求生成');
      startGenerate(q, null, cfg);
    });
    actions.appendChild(ok);
    actions.appendChild(skip);
    clarifyEl.appendChild(actions);
    clarifyEl.style.display = 'block';
    meta.textContent = '请确认以上问题（也可以修改需求后重新提交）';
  }

  function collectQa(questions) {
    // 收集答复：自由输入优先于选项；未作答的问题不进入合并（不擅自假设）
    const items = clarifyEl.querySelectorAll('.cq-item');
    const qa = [];
    questions.forEach(function (qu, i) {
      const item = items[i];
      if (!item) return;
      const free = item.querySelector('.cq-free');
      const sel = item.querySelector('.cq-opt.sel');
      const answer = (free && free.value.trim()) || (sel ? sel.textContent : '');
      if (answer) qa.push({ question: qu.question, answer: answer });
    });
    return qa;
  }

  function startGenerate(q, qaPairs, cfg) {
    setGoLoading(true, '生成中…');
    addStage('start', '请求已发出：' + q);
    startTicker();   // 状态栏实时耗时（done/onerror 停止并覆盖）
    let url = '/api/generate?q=' + encodeURIComponent(q);
    if (qaPairs && qaPairs.length)
      url += '&c=' + encodeURIComponent(JSON.stringify(qaPairs));
    if (cfg)   // 多模型：/api/llm_config 注册的 cfg id（Key 不进 URL）
      url += '&cfg=' + encodeURIComponent(cfg);

    // fallback 事件的“前置拒绝”reason 经此传给 done（rejected
    // 请求不产生电路，done 需要区分文案；SSE 事件间无共享状态）
    let rejectReason = '';
    let rejectQuestions = [];

    const es = new EventSource(url);
    let generatedSnapshot = null;

    es.addEventListener('stage', function (ev) {
      const d = JSON.parse(ev.data);
      addStage(d.stage, d.detail);
    });

    es.addEventListener('circuit', function (ev) {
      // 电路 JSON 存进页面变量：交互层直接修改它并触发重渲染
      circuit = JSON.parse(ev.data);
      clearPendingEdit();
      clearLastAiEdit();   // 新生成电路：与上一轮 AI 改图无关
      if (validationTimer) { clearTimeout(validationTimer); validationTimer = null; }
      validationSeq++; // 旧工程排队/在途的校验不得覆盖新生成电路的来源状态。
      // 新电路：清空上一轮的 ERC 结果与在途请求（序号自增使旧响应全部失效），
      // 否则旧电路的标注会短暂画到新电路上
      ercReport = null;
      ercSummary = null;
      ercSeq++;
      ercPending = 0;
      refreshErcBadge();
      renderAll({ fresh: true });
      generatedSnapshot = JSON.stringify(circuit);
      if (ctl && ctl.resetView) ctl.resetView();
      updatePrecisionCard();
      setEditChanges('');
      meta.textContent = '已载入电路：' + circuitCounts(circuit) + ' · ERC 见工具栏徽章';
      pulseErcBadge();
    });

    es.addEventListener('schemes', function (ev) {
      try {
        const d = JSON.parse(ev.data);
        // 工业级：生成后自动展示拓扑草案（不打断主流程）
        if (d && d.schemes && d.schemes.length) {
          // 先只记录到 meta，面板待用户点「多方案」时展开（避免生成过程被打断）
          window.__lastSchemes = d.schemes;
          meta.textContent = '（已匹配 ' + d.schemes.length + ' 个拓扑草案，点「多方案」查看）';
        }
      } catch (e) { /* ignore */ }
    });

    es.addEventListener('erc', function (ev) {
      // 服务端 ERC 终检摘要（SSE 事件只带 4 个计数，不带 issues[]）。
      // 摘要用于 done 文案；画布标注需要 issues[]，故随后主动向 /api/erc
      // 拉一次全量报告——同一条计算口径，不重复实现规则。
      try {
        const d = JSON.parse(ev.data);
        ercSummary = {
          passed: !!d.passed,
          fatal: Number(d.fatal) || 0, major: Number(d.major) || 0,
          minor: Number(d.minor) || 0, warning: Number(d.warning) || 0,
        };
      } catch (e) { ercSummary = null; }
      updateProgress('erc');
      // circuit 事件已先行到达（流水线先送 circuit 再送 erc），此处可安全重算
      if (circuit) recomputeErc();
    });

    es.addEventListener('fallback', function (ev) {
      const d = JSON.parse(ev.data);
      if (d.rejected) {
        rejectReason = d.reason || '';
        rejectQuestions = Array.isArray(d.questions) ? d.questions : [];
        setSourceBadge('rejected', d.reason);
        addStage('rejected', '请求被拒绝：' + d.reason + '（当前未生成新电路）', 'fallback');
        return;
      }
      const tpl = d.template ? '（模板：' + d.template + '）' : '';
      setSourceBadge('template', d.reason || '');
      addStage('fallback', '已使用内置模板，非 AI 生成成功' + tpl + ' —— ' + (d.reason || ''), 'fallback');
    });

    es.addEventListener('validation', function (ev) {
      try {
        const report = JSON.parse(ev.data);
        if (generatedSnapshot !== JSON.stringify(circuit)) return;
        window.__lastValidation = report;
        if (workbench) workbench.refresh();
        addStage('validation', report.accepted ? '官方验收门禁通过' :
          '候选尚未通过官方验收：' + (report.blocking_gates || []).join('、'),
          report.accepted ? 'done' : 'fallback');
      } catch (err) { /* 下一次手动校验仍可重新取得报告 */ }
    });

    es.addEventListener('done', function (ev) {
      const d = JSON.parse(ev.data);
      stopTicker();
      updateProgress('done');
      // 捕获 request_id：链路面板「最近一次」据此查询
      if (d.request_id) window.__lastRid = d.request_id;
      const total = ((performance.now() - t0) / 1000).toFixed(1);
      function ercBit() {
        if (!ercSummary) return '';
        return ercSummary.passed && !ercSummary.warning
          ? ' · ERC 全部通过 ✔'
          : ' · ERC：致命 ' + ercSummary.fatal + ' · 严重 ' + ercSummary.major +
            ' · 提示 ' + ercSummary.minor + ' · 警告 ' + ercSummary.warning;
      }
      if (d.ok) {
        const accepted = d.accepted && generatedSnapshot === JSON.stringify(circuit);
        setSourceBadge(accepted ? 'ai' : 'draft');
        meta.textContent = (accepted ? '电路已生成并通过官方验收 · ' : 'AI 草稿已生成，官方验收未通过或未完成 · ') +
          circuitCounts(circuit) + ercBit() +
          ' · 可导出 KiCad（本次 LLM ' + (d.budget_used || 0) + '/' + (d.budget_max || 0) + ' 次，' + total + 's）';
        pulseErcBadge();
      } else if (d.rejected) {
        if (rejectQuestions.length) showClarify(q, rejectQuestions, 'rule', cfg);
        setSourceBadge('rejected');
        meta.textContent = '请求被拒绝：' + (rejectReason || '请求被拒绝') +
          '，当前未生成新电路' + (circuit ? '，原工程和未保存修改已保留' : '') +
          '（总耗时 ' + total + 's）';
      } else {
        setSourceBadge('template');
        let txt = '已使用内置模板，非 AI 生成成功';
        if (circuit) txt += ' · ' + circuitCounts(circuit);
        meta.textContent = txt + ercBit() + ' · 总耗时 ' + total + 's';
        pulseErcBadge();
      }
      es.close();
      setGoLoading(false);
    });

    es.onerror = function () {
      es.close();
      stopTicker();
      if (goBtn.classList.contains('loading') || goBtn.disabled) {
        setMetaFail('连接中断（服务是否已停止？）', 'EventSource onerror；请确认本地服务仍在运行');
        setGoLoading(false);
      }
    };
  }

  // ---------- 导出 SVG / 保存工程 / 载入工程 ----------
  function download(name, blob) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
  }
  function stamp() {
    const d = new Date();
    const p = function (n) { return String(n).padStart(2, '0'); };
    return '' + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) +
      '-' + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
  }

  exportBtn.addEventListener('click', function () {
    const svg = svgHost.querySelector('svg');
    if (!svg) return;
    const text = new XMLSerializer().serializeToString(svg);
    download('智绘电路-' + stamp() + '.svg',
             new Blob(['<?xml version="1.0" encoding="UTF-8"?>\n', text],
                      { type: 'image/svg+xml;charset=utf-8' }));
    meta.textContent = '已导出 SVG（可用浏览器/Inkscape 打开）';
  });

  if (bomBtn) {
    bomBtn.addEventListener('click', function () {
      if (!circuit) return;
      meta.textContent = '正在生成 BOM…';
      fetch('/api/bom', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ circuit: circuit, format: 'csv' }),
      }).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.text();
      }).then(function (text) {
        download('智绘电路-BOM-' + stamp() + '.csv',
                 new Blob([text], { type: 'text/csv;charset=utf-8' }));
        meta.textContent = '已导出 BOM（CSV，可用 Excel 打开）';
      }).catch(function (e) {
        meta.textContent = '导出 BOM 失败: ' + e.message;
      });
    });
  }

  if (spiceBtn) {
    spiceBtn.addEventListener('click', function () {
      if (!circuit) return;
      meta.textContent = '正在生成 SPICE 网表…';
      fetch('/api/spice', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ circuit: circuit, name: 'circuit', format: 'cir' }),
      }).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.text();
      }).then(function (text) {
        download('智绘电路-SPICE-' + stamp() + '.cir',
                 new Blob([text], { type: 'text/plain;charset=utf-8' }));
        meta.textContent = '已导出 SPICE 网表（可用 ngspice / LTspice 打开）';
      }).catch(function (e) {
        meta.textContent = '导出 SPICE 失败: ' + e.message;
      });
    });
  }

  if (simBtn) {
    simBtn.addEventListener('click', function () {
      if (!circuit) return;
      const analyzedCircuit = circuit, analyzedSnapshot = JSON.stringify(circuit);
      const kind = (simKindSel && simKindSel.value) || 'op';
      const kindLabel = { op: '直流工作点', ac: '交流扫频', tran: '瞬态', dc: 'DC 扫描' }[kind] || kind;
      meta.textContent = '正在 SPICE 仿真（' + kindLabel + '）…';
      simBtn.disabled = true;
      fetch('/api/spice_analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ circuit: circuit, kind: kind }),
      }).then(function (r) {
        return r.json().then(function (j) {
          if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
          return j;
        });
      }).then(function (j) {
        if (circuit !== analyzedCircuit || JSON.stringify(circuit) !== analyzedSnapshot) return;
        const items = (j.analytic && j.analytic.items) || [];
        const engine = j.engine === 'ngspice' ? 'ngspice 引擎' : '内置计算引擎';
        let html = '<div class="pc-title">SPICE 仿真 · ' + kindLabel + '（' + engine + '）</div>';

        // 解析解条目
        const bits = [];
        items.forEach(function (it) {
          if (it.kind === 'led_current') bits.push(it.led + ' ≈ ' + it.i_ma + ' mA（电阻功耗 ' + it.p_mw + ' mW）');
          else if (it.kind === 'divider') bits.push('分压中点 ≈ ' + it.vout + ' V');
          else if (it.kind === 'rc_time_constant') bits.push('τ ≈ ' + it.tau_ms + ' ms' + (it.fc_hz ? '，fc ≈ ' + it.fc_hz + ' Hz' : ''));
          else if (it.kind === 'linear_regulator') bits.push(it.reg + ' 压差 ' + it.dropout + ' V，@100mA 功耗 ' + it.p_w_at_100ma + ' W');
        });
        if (bits.length) {
          html += '<ul>' + bits.map(function (b) { return '<li>' + b + '</li>'; }).join('') + '</ul>';
        }

        // AC 幅频表 + 曲线（kind=ac 时）
        if (kind === 'ac' && j.ac_response && j.ac_response.points && j.ac_response.points.length) {
          const ac = j.ac_response;
          const topo = ac.topology === 'highpass' ? '高通' : '低通';
          html += '<div class="ac-note">一阶 RC ' + topo + ' · fc ≈ ' + ac.fc_hz + ' Hz · R=' + ac.r + 'Ω C=' + ac.c + 'F</div>';
          const chart = buildAcChart(ac);
          if (chart) html += chart;
          html += '<table class="ac-tbl"><thead><tr><th>频率</th><th>增益(dB)</th><th>|H|</th></tr></thead><tbody>';
          ac.points.forEach(function (p) {
            html += '<tr><td>' + formatFreq(p.f_hz) + '</td><td>' + p.gain_db + '</td><td>' + p.gain + '</td></tr>';
          });
          html += '</tbody></table>';
        }

        // ngspice 节点电压（kind=op 且有结果时）
        if (kind === 'op' && j.node_voltages && Object.keys(j.node_voltages).length) {
          html += '<ul>';
          Object.keys(j.node_voltages).forEach(function (n) {
            html += '<li>V(' + n + ') = ' + j.node_voltages[n].toFixed(3) + ' V</li>';
          });
          html += '</ul>';
        }

        // 网表下载链接
        if (j.deck) {
          html += '<div class="ac-note"><a href="#" id="dl-deck">下载 .cir 网表</a>（可用 ngspice / LTspice 打开）</div>';
        }

        if (precisionCard) {
          precisionCard.innerHTML = html;
          precisionCard.classList.add('on');
          const dlLink = precisionCard.querySelector('#dl-deck');
          if (dlLink) {
            dlLink.addEventListener('click', function (e) {
              e.preventDefault();
              download('智绘电路-' + kind + '-' + stamp() + '.cir',
                       new Blob([j.deck], { type: 'text/plain;charset=utf-8' }));
            });
          }
        }
        meta.textContent = '仿真(' + engine + ')：' + kindLabel + ' 完成';
      }).catch(function (e) {
        if (circuit !== analyzedCircuit || JSON.stringify(circuit) !== analyzedSnapshot) return;
        meta.textContent = 'SPICE 仿真失败: ' + e.message;
      }).then(function () {
        simBtn.disabled = !circuit;
      });
    });
  }

  function formatFreq(hz) {
    if (hz >= 1e6) return (hz / 1e6).toFixed(hz % 1e6 ? 2 : 0) + ' MHz';
    if (hz >= 1e3) return (hz / 1e3).toFixed(hz % 1e3 ? 2 : 0) + ' kHz';
    if (hz >= 1) return hz.toFixed(hz % 1 ? 1 : 0) + ' Hz';
    return hz.toFixed(2) + ' Hz';
  }

  // AC 幅频曲线：内联 SVG（零外部库）。X 轴对数频率，Y 轴增益 dB。
  // 颜色走 CSS 变量（类名 .ac-curve / .ac-fc 等在 index.html 定义），暗色自动适配。
  function buildAcChart(ac) {
    const pts = (ac && ac.points) || [];
    if (pts.length < 2) return '';
    const W = 480, H = 180;
    const padL = 44, padR = 14, padT = 14, padB = 30;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;
    const f0 = Number(pts[0].f_hz) || 1;
    const f1 = Number(pts[pts.length - 1].f_hz) || f0 * 10;
    const logF0 = Math.log10(Math.max(f0, 1e-12));
    const logF1 = Math.log10(Math.max(f1, f0 * 1.01));
    let gMin = Infinity, gMax = -Infinity;
    pts.forEach(function (p) {
      const g = Number(p.gain_db);
      if (!isFinite(g)) return;
      if (g < gMin) gMin = g;
      if (g > gMax) gMax = g;
    });
    if (!isFinite(gMin) || !isFinite(gMax)) return '';
    if (gMax - gMin < 1) { gMax += 1; gMin -= 1; }
    const yPad = (gMax - gMin) * 0.08;
    gMin -= yPad; gMax += yPad;
    function xOf(f) {
      const t = (Math.log10(Math.max(f, 1e-12)) - logF0) / (logF1 - logF0 || 1);
      return padL + Math.max(0, Math.min(1, t)) * plotW;
    }
    function yOf(g) {
      const t = (g - gMin) / (gMax - gMin || 1);
      return padT + (1 - Math.max(0, Math.min(1, t))) * plotH;
    }
    let d = '';
    pts.forEach(function (p, i) {
      const x = xOf(p.f_hz).toFixed(1);
      const y = yOf(Number(p.gain_db)).toFixed(1);
      d += (i === 0 ? 'M' : 'L') + x + ' ' + y;
    });
    // 网格与坐标标签：横轴 1/2/5 十进制频点，纵轴 0dB 参考线
    const grids = [];
    const labels = [];
    const decades = Math.max(1, Math.round(logF1 - logF0));
    const step = decades > 5 ? 2 : 1;
    for (let i = 0; i <= decades; i += step) {
      const f = Math.pow(10, logF0 + i);
      const x = xOf(f).toFixed(1);
      grids.push('<line class="ac-grid" x1="' + x + '" y1="' + padT + '" x2="' + x + '" y2="' + (padT + plotH) + '"/>');
      labels.push('<text x="' + x + '" y="' + (padT + plotH + 14) + '" text-anchor="middle">' + formatFreq(f) + '</text>');
    }
    if (gMin <= 0 && gMax >= 0) {
      const y0 = yOf(0).toFixed(1);
      grids.push('<line class="ac-grid" x1="' + padL + '" y1="' + y0 + '" x2="' + (padL + plotW) + '" y2="' + y0 + '"/>');
      labels.push('<text x="' + (padL - 6) + '" y="' + (Number(y0) + 3) + '" text-anchor="end">0 dB</text>');
    }
    // 纵轴两端标签
    labels.push('<text x="' + (padL - 6) + '" y="' + (padT + 8) + '" text-anchor="end">' + gMax.toFixed(0) + '</text>');
    labels.push('<text x="' + (padL - 6) + '" y="' + (padT + plotH) + '" text-anchor="end">' + gMin.toFixed(0) + '</text>');
    // fc 垂线
    let fcMark = '';
    const fc = Number(ac.fc_hz);
    if (isFinite(fc) && fc > 0 && fc >= f0 && fc <= f1) {
      const fx = xOf(fc).toFixed(1);
      fcMark = '<line class="ac-fc" x1="' + fx + '" y1="' + padT + '" x2="' + fx + '" y2="' + (padT + plotH) + '"/>' +
        '<text x="' + fx + '" y="' + (padT + 10) + '" text-anchor="middle" style="fill:var(--ok)">fc</text>';
    }
    const dots = pts.map(function (p) {
      return '<circle class="ac-dot" cx="' + xOf(p.f_hz).toFixed(1) + '" cy="' +
        yOf(Number(p.gain_db)).toFixed(1) + '" r="2.2"/>';
    }).join('');
    return '<div class="ac-chart" role="img" aria-label="幅频响应曲线">' +
      '<svg viewBox="0 0 ' + W + ' ' + H + '" xmlns="http://www.w3.org/2000/svg">' +
      '<rect x="' + padL + '" y="' + padT + '" width="' + plotW + '" height="' + plotH + '" fill="none" class="ac-axis"/>' +
      grids.join('') +
      '<path class="ac-curve" d="' + d + '"/>' +
      dots + fcMark +
      labels.join('') +
      '<text x="' + (padL + plotW / 2) + '" y="' + (H - 2) + '" text-anchor="middle">频率（对数）</text>' +
      '</svg>' +
      '<div class="ac-legend">频率 vs 增益(dB)' + (fc ? ' · 绿虚线为 -3dB 截止频率 fc' : '') + '</div>' +
      '</div>';
  }

  kicadBtn.addEventListener('click', function () {
    if (!circuit) return;
    const name = String(circuit.title || circuit.name || 'circuit');
    meta.textContent = '正在生成 KiCad 工程…';
    fetch('/api/kicad', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name, circuit: circuit }),
    }).then(function (r) {
      if (!r.ok) {
        return r.json().then(function (j) {
          throw new Error(j.error || ('HTTP ' + r.status));
        });
      }
      return r.blob();
    }).then(function (blob) {
      download('zhihui-kicad-' + stamp() + '.zip', blob);
      meta.textContent = '已导出 KiCad 工程包。解压后双击 OPEN_IN_KICAD.bat（或打开工程选 .kicad_pro），不要用「文件→导入」。';
      showKicadModal();
    }).catch(function (e) {
      setMetaFail('导出 KiCad 失败', e && (e.message || String(e)));
    });
  });

  if (pcbBtn) {
    pcbBtn.addEventListener('click', function () {
      if (!circuit) return;
      const name = String(circuit.title || circuit.name || 'circuit');
      meta.textContent = '正在生成 PCB 预布局…';
      fetch('/api/pcb', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name, circuit: circuit }),
      }).then(function (r) {
        if (!r.ok) {
          return r.json().then(function (j) {
            throw new Error(j.error || ('HTTP ' + r.status));
          });
        }
        return r.blob();
      }).then(function (blob) {
        download('智绘电路-PCB-' + stamp() + '.kicad_pcb', blob);
        meta.textContent = '已导出 PCB 布局草稿（用 KiCad 打开 .kicad_pcb 可查看元件位置）';
      }).catch(function (e) {
        meta.textContent = 'PCB 导出失败: ' + e.message;
      });
    });
  }

  if (realPcbBtn) realPcbBtn.addEventListener('click', async function () {
    if (!circuit) return;
    const original = circuit, snapshot = JSON.stringify(circuit);
    realPcbBtn.disabled = true;
    meta.textContent = '正在核对真实封装与焊盘网络…';
    try {
      const response = await fetch('/api/pcb/candidate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ circuit: JSON.parse(snapshot), name: 'candidate' }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || ('HTTP ' + response.status));
      if (circuit !== original || JSON.stringify(circuit) !== snapshot) throw new Error('当前电路已变化，请重新生成');
      window.__lastPcbAudit = result;
      download('未布线候选-' + stamp() + '.kicad_pcb', new Blob([result.board_text], { type: 'text/plain' }));
      meta.textContent = '真实封装候选已导出，尚未布线和 DRC 校验；焊盘审计 ' + (result.pad_audit || []).length + ' 项';
    } catch (error) { meta.textContent = '真实 PCB 候选未生成：' + error.message; }
    finally { realPcbBtn.disabled = !circuit; }
  });
  if (camPcbBtn && camPcbFile) {
    camPcbBtn.addEventListener('click', function () { if (circuit) { camPcbFile.value = ''; camPcbFile.click(); } });
    camPcbFile.addEventListener('change', async function () {
      const file = camPcbFile.files && camPcbFile.files[0];
      if (!file || !circuit) return;
      const original = circuit, snapshot = JSON.stringify(circuit);
      camPcbBtn.disabled = true;
      meta.textContent = '正在校验源原理图、真实焊盘和 PCB DRC…';
      try {
        if (file.size > 2 * 1024 * 1024) throw new Error('板文件超过 2 MB，请使用范围内的小型双层板');
        const response = await fetch('/api/pcb/manufacture', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ circuit: JSON.parse(snapshot), board_text: await file.text() }),
        });
        if (!response.ok) {
          const failure = await response.json();
          window.__lastPcbManufacturing = failure;
          throw new Error(failure.error || ('HTTP ' + response.status));
        }
        const blob = await response.blob();
        if (circuit !== original || JSON.stringify(circuit) !== snapshot) throw new Error('当前电路已变化，校验包对应旧电路，请重新提交');
        download('待工程师及板厂复核-' + stamp() + '.zip', blob);
        meta.textContent = '制造候选包已导出，包含校验证据；请交电子工程师与板厂复核';
      } catch (error) { meta.textContent = '未导出制造候选包：' + error.message; }
      finally { camPcbBtn.disabled = !circuit; }
    });
  }

  saveBtn.addEventListener('click', function () {
    if (!circuit) return;
    const doc = { app: 'CircuitTutor', version: 1,
                  savedAt: new Date().toISOString(), circuit: circuit };
    download('智绘电路工程-' + stamp() + '.json',
             new Blob([JSON.stringify(doc, null, 2)],
                      { type: 'application/json;charset=utf-8' }));
    meta.textContent = '工程已保存（含元件与连线，可再次载入继续编辑）';
  });

  loadBtn.addEventListener('click', function () { fileInput.click(); });
  fileInput.addEventListener('change', function () {
    const file = fileInput.files && fileInput.files[0];
    fileInput.value = '';   // 允许连续载入同一文件
    if (!file) return;
    const reader = new FileReader();
    reader.onload = function () {
      try {
        const doc = JSON.parse(reader.result);
        const c = doc && doc.circuit ? doc.circuit : doc;   // 兼容裸 CircuitJSON
        if (!c || !Array.isArray(c.parts) || !Array.isArray(c.connections))
          throw new Error('缺少 parts / connections 数组');
        circuit = c;
        refreshSourceFromCircuit();
        clearPendingEdit();
        clearLastAiEdit();   // 载入工程：撤回目标已不存在
        renderAll({ fresh: true, layout: savedLayoutMode(circuit) });
        scheduleErc();
        scheduleValidation('import');
        meta.textContent = '已载入工程：' + file.name +
          '（' + c.parts.length + ' 个元件 / ' + c.connections.length + ' 条连线）';
      } catch (err) {
        meta.textContent = '载入失败：' + err.message + '（请选择本工具导出的工程 JSON 文件）';
      }
    };
    reader.readAsText(file, 'utf-8');
  });

  if (imageLoadBtn && imageFileInput) {
    let ocrScriptPromise = null;
    function loadOcrScript() {
      if (window.Tesseract) return Promise.resolve(window.Tesseract);
      if (!ocrScriptPromise) ocrScriptPromise = new Promise(function (resolve, reject) {
        const script = document.createElement('script');
        script.src = '/static/vision/tesseract.min.js';
        const timer = setTimeout(function () { script.remove(); reject(new Error('OCR 加载超时')); }, 8000);
        script.onload = function () { clearTimeout(timer); resolve(window.Tesseract); };
        script.onerror = function () { clearTimeout(timer); reject(new Error('本机 OCR 依赖不可用')); };
        document.head.appendChild(script);
      }).catch(function (error) { ocrScriptPromise = null; throw error; });
      return ocrScriptPromise;
    }
    function drawSvgPrimitives(primitives) {
      const xs = [], ys = [];
      primitives.forEach(function (p) {
        [p.x, p.x1, p.x2].forEach(function (v) { if (Number.isFinite(v)) xs.push(v); });
        [p.y, p.y1, p.y2].forEach(function (v) { if (Number.isFinite(v)) ys.push(v); });
      });
      const minX = Math.min(0, ...xs), minY = Math.min(0, ...ys);
      const maxX = Math.max(100, ...xs), maxY = Math.max(100, ...ys);
      const scale = Math.min(1, 1000 / (maxX - minX + 20), 600 / (maxY - minY + 20));
      const canvas = document.createElement('canvas');
      canvas.width = Math.max(1, Math.ceil((maxX - minX + 20) * scale));
      canvas.height = Math.max(1, Math.ceil((maxY - minY + 20) * scale));
      const ctx = canvas.getContext('2d');
      if (!ctx) return canvas;
      ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.scale(scale, scale); ctx.translate(10 - minX, 10 - minY);
      ctx.strokeStyle = '#222'; ctx.fillStyle = '#222'; ctx.lineWidth = 1.5;
      primitives.forEach(function (p) {
        ctx.beginPath();
        if (p.kind === 'line') { ctx.moveTo(p.x1, p.y1); ctx.lineTo(p.x2, p.y2); ctx.stroke(); }
        else if (p.kind === 'rect') ctx.strokeRect(p.x, p.y, p.width, p.height);
        else if (p.kind === 'circle' && p.r >= 0) { ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2); p.filled ? ctx.fill() : ctx.stroke(); }
        else if (p.kind === 'text') { ctx.font = '12px sans-serif'; ctx.fillText(p.text, p.x, p.y); }
      });
      return canvas;
    }
    async function sha256Bytes(bytes) {
      const digest = await crypto.subtle.digest('SHA-256', bytes);
      return Array.from(new Uint8Array(digest), function (byte) { return byte.toString(16).padStart(2, '0'); }).join('');
    }
    function dataUrlBytes(dataUrl) {
      const encoded = dataUrl.slice(dataUrl.indexOf(',') + 1);
      const binary = atob(encoded), bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      return bytes;
    }
    async function circuitVisionRequestImage(file) {
      if (!window.CircuitUpstreamVision) throw new Error('本机识别界面模块未加载');
      if (!/^image\/(png|jpeg)$/.test(file.type)) throw new Error('本机 CircuitVision 当前仅支持 PNG 或 JPEG 图片');
      if (file.size > 10 * 1024 * 1024) throw new Error('图片文件超过 10 MB');
      if (editBusy || pendingEdit) throw new Error('当前有候选审核或图片识别正在进行');
      const originalCircuit = circuit;
      const originalSnapshot = JSON.stringify(circuit);
      editBusy = true;
      visionAbortController = new AbortController();
      imageLoadBtn.disabled = true;
      if (externalLoadBtn) externalLoadBtn.disabled = true;
      if (visionCancelRequestBtn) visionCancelRequestBtn.hidden = false;
      setEditStatus('正在使用本机 CircuitVision 识别图片…');
      syncHistoryButtons();
      let bitmap = null;
      try {
        bitmap = await createImageBitmap(file);
        if (bitmap.width * bitmap.height > 100000000) throw new Error('图片超过 1 亿像素，请先裁剪');
        let scale = Math.min(1, 1600 / Math.max(bitmap.width, bitmap.height));
        let canvas, imageDataUrl;
        for (let attempt = 0; attempt < 8; attempt++) {
          canvas = document.createElement('canvas');
          canvas.width = Math.max(1, Math.round(bitmap.width * scale));
          canvas.height = Math.max(1, Math.round(bitmap.height * scale));
          const ctx = canvas.getContext('2d');
          ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, canvas.width, canvas.height);
          ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
          imageDataUrl = canvas.toDataURL('image/png');
          if (imageDataUrl.length <= 9.5 * 1024 * 1024) break;
          scale *= 0.82;
        }
        if (!imageDataUrl || imageDataUrl.length > 9.5 * 1024 * 1024)
          throw new Error('缩放后的图片仍超过本机识别请求大小限制');
        const inputPngBytes = dataUrlBytes(imageDataUrl);
        const normalization = {
          source_file: file.name, source_mime: file.type, source_size_bytes: file.size,
          source_sha256: await sha256Bytes(await file.arrayBuffer()),
          source_width_after_exif: bitmap.width, source_height_after_exif: bitmap.height,
          output_mime: 'image/png', output_width: canvas.width, output_height: canvas.height,
          scale_x: canvas.width / bitmap.width, scale_y: canvas.height / bitmap.height,
          exif_orientation_applied_by_browser_decode: true,
          input_png_sha256: await sha256Bytes(inputPngBytes),
        };
        if (visionAbortController.signal.aborted) throw new DOMException('已取消', 'AbortError');
        if (circuit !== originalCircuit || JSON.stringify(circuit) !== originalSnapshot)
          throw new Error('图片准备期间当前电路已变化，请重新开始识别');
        const response = await fetch('/api/image/analyze', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ backend: 'circuitvision', image: imageDataUrl, source_name: file.name }),
          signal: visionAbortController.signal,
        });
        let result;
        try { result = await response.json(); }
        catch (_) { throw new Error('本机识别服务返回了无法读取的响应'); }
        if (!response.ok) throw new Error(result.error || ('HTTP ' + response.status));
        CircuitUpstreamVision.validateResponse(result);
        if (result.image.sha256 !== normalization.input_png_sha256)
          throw new Error('识别服务返回的图像 SHA-256 与实际提交的 PNG 不一致');
        if (circuit !== originalCircuit || JSON.stringify(circuit) !== originalSnapshot)
          throw new Error('识别期间当前电路已变化；结果已丢弃');
        const raw = JSON.parse(JSON.stringify(result));
        pendingVision = { raw: raw, imageDataUrl: imageDataUrl, originalCircuit: originalCircuit,
          originalSnapshot: originalSnapshot, fileName: file.name, normalization: normalization };
        CircuitUpstreamVision.render(visionCorrectionHost, imageDataUrl, raw, CircuitSymbols, {
          onBuild: submitVisionCorrection,
          onCancel: function () {
            pendingVision = null;
            visionCorrectionHost.replaceChildren(); visionCorrectionHost.hidden = true;
            setEditStatus('已取消本机识别校正；当前电路保持原样');
          },
          onDownloadRaw: function () {
            download('CircuitVision-原始识别-' + file.name + '.json',
              new Blob([JSON.stringify(raw, null, 2)], { type: 'application/json;charset=utf-8' }));
          },
          onDownloadInput: function () {
            download('CircuitVision-模型输入-' + file.name.replace(/\.[^.]+$/, '') + '.png',
              new Blob([inputPngBytes], { type: 'image/png' }));
          },
        });
        meta.textContent = '本机 CircuitVision 原始结果：' + file.name + ' · ' + raw.detections.length +
          ' 个检测框；类型、参数和拓扑尚未人工确认';
        setEditStatus('模型结果仅作人工校正依据；尚未形成电路或执行电气校验');
      } finally {
        if (bitmap) bitmap.close();
        visionAbortController = null;
        if (visionCancelRequestBtn) visionCancelRequestBtn.hidden = true;
        editBusy = false;
        imageLoadBtn.disabled = false;
        if (externalLoadBtn) externalLoadBtn.disabled = false;
        syncHistoryButtons();
      }
    }
    async function submitVisionCorrection(corrections) {
      if (!pendingVision || editBusy) return;
      const vision = pendingVision;
      if (circuit !== vision.originalCircuit || JSON.stringify(circuit) !== vision.originalSnapshot) {
        setEditStatus('当前电路已变化；请取消本次识别并重新开始', true);
        return;
      }
      const built = CircuitUpstreamVision.buildCandidate(vision.raw, corrections, CircuitSymbols);
      if (built.errors && built.errors.length) {
        setEditStatus(built.errors.join('；'), true);
        return;
      }
      built.correction.normalization = Object.assign({}, vision.normalization);
      editBusy = true;
      visionAbortController = new AbortController();
      const buildButton = visionCorrectionHost.querySelector('#visionBuild');
      if (buildButton) buildButton.disabled = true;
      if (visionCancelRequestBtn) visionCancelRequestBtn.hidden = false;
      setEditStatus('正在校验人工校正候选…');
      syncHistoryButtons();
      const imageIssues = (vision.raw.issues || []).map(function (issue) {
        return { code: issue.code, message: issue.message };
      });
      imageIssues.push({ code: 'RATINGS_UNKNOWN',
        message: '器件耐压、功率等额定值仍需依据具体料号核对；本机图像识别不提供额定值证明' });
      (built.issues || []).forEach(function (issue) { imageIssues.push(issue); });
      let validation = null;
      try {
        const validationResponse = await fetch('/api/validate', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ circuit: built.circuit, source: 'image' }),
          signal: visionAbortController.signal,
        });
        let validationBody;
        try { validationBody = await validationResponse.json(); }
        catch (_) { throw new Error('校验服务返回了无法读取的响应'); }
        if (!validationResponse.ok) throw new Error(validationBody.error || ('HTTP ' + validationResponse.status));
        validation = validationBody;
      } catch (error) {
        if (error && error.name === 'AbortError') {
          setEditStatus('已取消候选校验；当前电路保持原样', true);
          editBusy = false; visionAbortController = null;
          if (visionCancelRequestBtn) visionCancelRequestBtn.hidden = true;
          if (buildButton) buildButton.disabled = false;
          syncHistoryButtons();
          return;
        }
        imageIssues.push({ code: 'VALIDATION_UNAVAILABLE',
          message: '校验服务不可用：' + (error.message || String(error)) });
      }
      if (circuit !== vision.originalCircuit || JSON.stringify(circuit) !== vision.originalSnapshot) {
        setEditStatus('校验期间当前电路已变化；候选结果已丢弃', true);
        editBusy = false; visionAbortController = null;
        if (visionCancelRequestBtn) visionCancelRequestBtn.hidden = true;
        syncHistoryButtons();
        return;
      }
      const ercGate = validation && validation.gates && validation.gates.erc;
      const counts = ercGate && ercGate.evidence && ercGate.evidence.counts || {};
      const imageErc = ercGate && ercGate.status !== 'not_run'
        ? Object.assign({ issues: ercGate.issues || [], passed: ercGate.status === 'pass' }, counts) : null;
      const overlay = CircuitUpstreamVision.createOverlay(vision.imageDataUrl, vision.raw);
      visionCorrectionHost.replaceChildren(); visionCorrectionHost.hidden = true;
      showEditReview({ circuit: built.circuit, erc: imageErc, changes: {}, validation: validation,
        imageIssues: imageIssues, origin: 'image', imageRecognition: vision.raw,
        imageCorrection: built.correction,
        imageValidationEvidence: { source: 'image', response: validation, issues: imageIssues },
        imageOverlay: overlay },
        vision.originalCircuit, vision.originalSnapshot, '本机 CircuitVision 图片人工校正：' + vision.fileName);
      if (imageOriginal) {
        imageOriginal.replaceChildren(overlay.cloneNode(true)); imageOriginal.hidden = false;
      }
      meta.textContent = '人工校正候选：' + vision.fileName + ' · ' + circuitCounts(built.circuit) +
        ' · 模型原始结果与人工映射分别记录';
      editBusy = false;
      visionAbortController = null;
      if (visionCancelRequestBtn) visionCancelRequestBtn.hidden = true;
      syncHistoryButtons();
    }
    imageLoadBtn.addEventListener('click', function () { imageFileInput.click(); });
    imageFileInput.addEventListener('change', async function () {
      const file = imageFileInput.files && imageFileInput.files[0];
      imageFileInput.value = '';
      if (!file || editBusy) return;
      if (imageRecognitionMode && imageRecognitionMode.value === 'circuitvision') {
        try { await circuitVisionRequestImage(file); }
        catch (err) {
          const cancelled = err && err.name === 'AbortError';
          setEditStatus(cancelled ? '已取消本机识别；当前电路保持原样' :
            '本机 CircuitVision 识别失败：' + (err.message || String(err)), !cancelled);
          meta.textContent = cancelled ? '已取消本次识别，当前电路保持原样' : '识别失败，当前电路保持原样';
        }
        return;
      }
      editBusy = true;
      imageLoadBtn.disabled = true;
      setEditStatus('图片识别与校验中…');
      syncHistoryButtons();
      const originalCircuit = circuit;
      const originalSnapshot = JSON.stringify(circuit);
      try {
        if (!window.CircuitImageRecognition) throw new Error('图片识别模块未加载');
        if (file.size > 10 * 1024 * 1024) throw new Error('图片文件超过 10 MB');
        let proposal, originalCanvas;
        const options = { sourceId: file.name, title: '图片识别候选' };
        if (/\.svg$/i.test(file.name) || file.type === 'image/svg+xml') {
          const svgText = await file.text();
          proposal = CircuitImageRecognition.recognizeSvg(svgText, options);
          originalCanvas = drawSvgPrimitives(CircuitImageRecognition.svgToPrimitives(svgText));
        } else if (/^image\/(png|jpeg)$/.test(file.type)) {
          if (typeof createImageBitmap !== 'function') throw new Error('当前浏览器不能读取该图片');
          const bitmap = await createImageBitmap(file);
          try {
            if (bitmap.width * bitmap.height > 100000000)
              throw new Error('图片超过 1 亿像素，请先裁剪');
            const imageScale = Math.min(1, 1600 / Math.max(bitmap.width, bitmap.height));
            const canvas = document.createElement('canvas');
            canvas.width = Math.max(1, Math.round(bitmap.width * imageScale));
            canvas.height = Math.max(1, Math.round(bitmap.height * imageScale));
            const ctx = canvas.getContext('2d', { willReadFrequently: true });
            if (!ctx) throw new Error('无法创建图片读取画布');
            ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
            originalCanvas = canvas;
            const imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
            let ocrAdapter = null;
            try {
              const Tesseract = await loadOcrScript();
              ocrAdapter = await CircuitImageRecognition.createTesseractAdapter(Tesseract, {
                langPath: location.origin + '/static/vision/lang',
                corePath: location.origin + '/static/vision/core',
                workerPath: location.origin + '/static/vision/worker.min.js',
                cacheMethod: 'none', language: 'eng', psm: 11,
              });
              proposal = await CircuitImageRecognition.recognizeRasterWithOcr(imageData, canvas, ocrAdapter, options);
            } catch (error) {
              proposal = CircuitImageRecognition.recognizeRaster(imageData, options);
              proposal.issues.push({ code: 'OCR_UNAVAILABLE', message: '文字识别未完成：' + error.message });
            } finally { if (ocrAdapter) await ocrAdapter.terminate(); }
            proposal.normalization = { source_file: file.name, original_width: bitmap.width,
              original_height: bitmap.height, width: canvas.width, height: canvas.height, scale: imageScale };
            if (imageScale < 1) proposal.issues.push({ code: 'IMAGE_RESIZED',
              message: '图片按比例缩小至 ' + canvas.width + '×' + canvas.height + '，识别坐标对应缩小后的图；请对照原文件核对细节' });
          } finally { bitmap.close(); }
        } else throw new Error('仅支持 SVG、PNG 或 JPEG 图片');
        if (circuit !== originalCircuit || JSON.stringify(circuit) !== originalSnapshot)
          throw new Error('识别期间当前电路已变化，请重新导入');
        let validation = null;
        const imageIssues = (proposal.issues || []).slice();
        try {
          const response = await fetch('/api/validate', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ circuit: proposal.circuit, source: 'image' }),
          });
          const body = await response.json();
          if (!response.ok) throw new Error(body.error || ('HTTP ' + response.status));
          validation = body;
        } catch (err) {
          imageIssues.push({ code: 'VALIDATION_UNAVAILABLE',
            message: '校验服务不可用：' + (err.message || String(err)) });
        }
        if (circuit !== originalCircuit || JSON.stringify(circuit) !== originalSnapshot)
          throw new Error('校验期间当前电路已变化，请重新导入');
        if (pendingEdit) clearPendingEdit();
        const ercGate = validation && validation.gates && validation.gates.erc;
        const ercCounts = ercGate && ercGate.evidence && ercGate.evidence.counts || {};
        const imageErc = ercGate && ercGate.status !== 'not_run'
          ? Object.assign({ issues: ercGate.issues || [], passed: ercGate.status === 'pass' },
              ercCounts) : null;
        showEditReview({ circuit: proposal.circuit, erc: imageErc, changes: {},
          validation: validation, imageIssues: imageIssues, origin: 'image',
          imageRecognition: { method: proposal.method, detections: proposal.detections,
            topology: proposal.topology, texts: proposal.texts, ocr: proposal.ocr, normalization: proposal.normalization } },
          originalCircuit, originalSnapshot, '图片识别：' + file.name);
        if (imageOriginal && originalCanvas) {
          imageOriginal.replaceChildren(originalCanvas);
          imageOriginal.hidden = false;
        }
        meta.textContent = '图片识别候选：' + file.name + ' · ' +
          circuitCounts(proposal.circuit) + ' · 请核对每个器件、文字和连接';
      } catch (err) {
        setEditStatus('图片识别失败：' + (err.message || String(err)), true);
      } finally {
        editBusy = false;
        imageLoadBtn.disabled = false;
        syncHistoryButtons();
      }
    });
  }


  // ---------- 多方案 / 工程历史 ----------
  function showSchemes(list) {
    if (!schemesPanel || !schemesBody) return;
    schemesBody.innerHTML = '';
    if (!list || !list.length) {
      schemesBody.innerHTML = '<div style="font-size:12px;color:var(--mut)">未找到匹配的拓扑草案</div>';
      schemesPanel.classList.add('on');
      return;
    }
    list.forEach(function (s) {
      const card = document.createElement('div');
      card.className = 'scheme-card';
      const left = document.createElement('div');
      left.style.flex = '1';
      const t = document.createElement('div');
      t.className = 'sc-title';
      t.textContent = s.title || s.key;
      const r = document.createElement('div');
      r.className = 'sc-reason';
      r.textContent = (s.reason || '') + (s.score ? ' · 分 ' + s.score : '');
      const sc = document.createElement('div');
      sc.className = 'sc-score';
      sc.textContent = '元件 ' + ((s.circuit && s.circuit.parts) || []).length +
        ' · 连线 ' + ((s.circuit && s.circuit.connections) || []).length;
      left.appendChild(t); left.appendChild(r); left.appendChild(sc);
      const btn = document.createElement('button');
      btn.className = 'sc-use';
      btn.type = 'button';
      btn.textContent = '选用';
      btn.addEventListener('click', function () {
        circuit = s.circuit;
        clearPendingEdit();
        clearLastAiEdit();   // 载入拓扑草案

        renderAll({ fresh: true });
        schemesPanel.classList.remove('on');
        meta.textContent = '已载入方案：' + (s.title || s.key);
        updatePrecisionCard();
        scheduleErc();
      });
      card.appendChild(left); card.appendChild(btn);
      schemesBody.appendChild(card);
    });
    schemesPanel.classList.add('on');
  }

  function loadSchemes(q) {
    // 优先用生成时缓存的草案（工业级：点按钮即看，不再等一次请求）
    if (window.__lastSchemes && window.__lastSchemes.length) {
      // 需要完整 circuit 才能「选用」，缓存的是精简字段，故仍请求一次完整数据
    }
    fetch('/api/schemes', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: q }),
    }).then(function (r) { return r.json(); }).then(function (d) {
      showSchemes(d.schemes || []);
      meta.textContent = '已列出 ' + ((d.schemes || []).length) + ' 个拓扑草案';
    }).catch(function (e) {
      meta.textContent = '多方案失败: ' + e.message;
    });
  }
  if (schemesBtn) {
    schemesBtn.addEventListener('click', function () {
      const q = input.value.trim();
      if (!q) return;
      meta.textContent = '正在匹配拓扑草案…';
      loadSchemes(q);
    });
  }
  if (schemesClose) schemesClose.addEventListener('click', function () {
    schemesPanel.classList.remove('on');
  });

  function refreshHistory() {
    if (!historyPanel || !historyBody) return;
    fetch('/api/history').then(function (r) { return r.json(); }).then(function (d) {
      historyBody.innerHTML = '';
      const items = d.items || [];
      if (!items.length) {
        historyBody.innerHTML = '<div style="font-size:12px;color:var(--mut)">暂无历史，点「存档」保存当前电路</div>';
      } else {
        items.forEach(function (it) {
          const row = document.createElement('div');
          row.className = 'hist-item';
          const title = document.createElement('span');
          title.className = 'h-title';
          title.textContent = it.title || '未命名';
          const meta2 = document.createElement('span');
          meta2.className = 'h-meta';
          meta2.textContent = (it.saved_at || '') + ' · ' + it.parts + '件';
          const open = document.createElement('button');
          open.type = 'button';
          open.textContent = '打开';
          open.addEventListener('click', function () {
            fetch('/api/history?id=' + encodeURIComponent(it.id)).then(function (r) {
              return r.json();
            }).then(function (rec) {
              if (rec && rec.circuit) {
                circuit = rec.circuit;
        clearPendingEdit();
        clearLastAiEdit();   // 载入历史工程

                renderAll({ fresh: true, layout: savedLayoutMode(circuit) });
                historyPanel.classList.remove('on');
                meta.textContent = '已载入历史：' + (it.title || it.id);
                updatePrecisionCard();
                scheduleErc();
              }
            }).catch(function (e) {
              meta.textContent = '载入失败: ' + e.message;
            });
          });
          const del = document.createElement('button');
          del.type = 'button';
          del.className = 'del';
          del.textContent = '删除';
          del.addEventListener('click', function () {
            fetch('/api/history?id=' + encodeURIComponent(it.id), { method: 'DELETE' })
              .then(function () { refreshHistory(); })
              .catch(function () {});
          });
          row.appendChild(title); row.appendChild(meta2);
          row.appendChild(open); row.appendChild(del);
          historyBody.appendChild(row);
        });
      }
      historyPanel.classList.add('on');
    }).catch(function (e) {
      meta.textContent = '历史列表失败: ' + e.message;
    });
  }
  if (historyBtn) historyBtn.addEventListener('click', refreshHistory);
  if (historyClose) historyClose.addEventListener('click', function () {
    historyPanel.classList.remove('on');
  });

  // ---------- LLM 链路日志面板 ----------
  function toggleTracePanel(force) {
    if (!tracePanel) return;
    const show = force !== undefined ? force : !tracePanel.classList.contains('on');
    tracePanel.classList.toggle('on', show);
    if (show) refreshTrace();
  }
  function renderTraceEvents(rid, events) {
    if (!traceBody) return;
    const row = document.createElement('div');
    row.className = 'tr-rid';
    row.textContent = 'rid=' + rid + '（' + events.length + ' 条事件）';
    row.title = '点击查看该 rid 的完整事件';
    row.addEventListener('click', function () {
      fetch('/api/trace?rid=' + encodeURIComponent(rid))
        .then(function (r) { return r.json(); })
        .then(function (d) {
          traceBody.innerHTML = '';
          renderTraceEvents(rid, d.events || []);
        })
        .catch(function (e) {
          meta.textContent = '查询失败: ' + e.message;
        });
    });
    traceBody.appendChild(row);
    events.forEach(function (e) {
      const div = document.createElement('div');
      div.className = 'tr-ev' + (e.level === 'error' ? ' error' : '');
      const span = document.createElement('span');
      span.className = 'tr-span';
      span.textContent = e.span || '';
      div.appendChild(span);
      div.appendChild(document.createTextNode(e.msg || ''));
      if (e.duration_ms !== undefined) {
        const ms = document.createElement('span');
        ms.className = 'tr-ms';
        ms.textContent = e.duration_ms + 'ms';
        div.appendChild(ms);
      }
      traceBody.appendChild(div);
    });
  }
  function refreshTrace() {
    if (!traceBody) return;
    traceBody.innerHTML = '<div style="color:var(--mut)">加载中…</div>';
    const rid = window.__lastRid;
    const url = rid ? '/api/trace?rid=' + encodeURIComponent(rid) : '/api/trace?n=5';
    fetch(url).then(function (r) { return r.json(); }).then(function (d) {
      traceBody.innerHTML = '';
      if (d.events && d.request_id) {
        renderTraceEvents(d.request_id, d.events);
      } else if (d.latest) {
        const rids = Object.keys(d.latest);
        if (!rids.length) {
          traceBody.innerHTML = '<div style="color:var(--mut)">暂无记录，先生成一次电路</div>';
        } else {
          rids.forEach(function (rid2) {
            renderTraceEvents(rid2, d.latest[rid2] || []);
          });
        }
      } else {
        traceBody.innerHTML = '<div style="color:var(--mut)">暂无记录</div>';
      }
    }).catch(function (e) {
      traceBody.innerHTML = '<div style="color:var(--danger)">查询失败: ' + e.message + '</div>';
    });
  }
  if (traceBtn) traceBtn.addEventListener('click', function () { toggleTracePanel(); });
  if (traceCloseBtn) traceCloseBtn.addEventListener('click', function () { toggleTracePanel(false); });
  if (saveHistBtn) {
    saveHistBtn.addEventListener('click', function () {
      if (!circuit) return;
      meta.textContent = '正在保存…';
      fetch('/api/history', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ circuit: circuit, title: (circuit.title || '未命名电路'), query: input.value.trim() }),
      }).then(function (r) { return r.json(); }).then(function (rec) {
        meta.textContent = '已存档：' + (rec.title || rec.id);
        if (historyPanel && historyPanel.classList.contains('on')) refreshHistory();
      }).catch(function (e) {
        meta.textContent = '存档失败: ' + e.message;
      });
    });
  }

  // 版本徽章
  fetch('/api/health').then(function (r) { return r.json(); }).then(function (d) {
    if (verBadge && d.version) verBadge.textContent = 'v' + d.version;
  }).catch(function () {});


  form.addEventListener('submit', function (e) { e.preventDefault(); run(); });

  // ---------- 主题切换：跟随系统 / 浅色 / 深色 三态 ----------
  // 首屏套用已在 index.html 的 <head> 内联脚本完成（防 FOUC）；此处只做
  // 用户后续交互：下拉变更 → 写 data-theme + 存 localStorage。
  // 选择存 localStorage('zht-theme')，刷新后由首屏脚本恢复，保持用户偏好。
  (function initTheme() {
    var sel = document.getElementById('theme');
    if (!sel) return;
    var cur = 'system';
    try { cur = localStorage.getItem('zht-theme') || 'system'; } catch (e) { cur = 'system'; }
    sel.value = cur;
    sel.addEventListener('change', function () {
      var v = sel.value;
      if (v !== 'light' && v !== 'dark' && v !== 'system') v = 'system';
      document.documentElement.setAttribute('data-theme', v);
      try { localStorage.setItem('zht-theme', v); } catch (e) { /* 隐私模式：仅本次生效 */ }
    });
  })();

  // ---------- CircuitVision / HCD / tscircuit 外部结果导入 ----------
  // 转换结果先经统一校验，再交给现有候选审核；服务端结果不直接改动画布。
  if (externalLoadBtn && externalFileInput && externalSource) {
    externalLoadBtn.addEventListener('click', function () {
      if (editBusy || pendingEdit) return;
      externalFileInput.click();
    });
    externalFileInput.addEventListener('change', async function () {
      const file = externalFileInput.files && externalFileInput.files[0];
      externalFileInput.value = '';
      if (!file || editBusy || pendingEdit) return;
      const source = externalSource.value;
      if (['circuitvision_spice', 'hcd', 'tscircuit'].indexOf(source) < 0) {
        setEditStatus('请选择支持的外部结果格式', true);
        return;
      }
      const originalCircuit = circuit;
      const originalSnapshot = JSON.stringify(circuit);
      const labels = {
        circuitvision_spice: 'CircuitVision · SPICE 网表',
        hcd: 'HCD · JSON 识别结果',
        tscircuit: 'tscircuit · 独立 Circuit JSON 格式',
      };
      editBusy = true;
      externalLoadBtn.disabled = true;
      if (imageLoadBtn) imageLoadBtn.disabled = true;
      setEditStatus('正在导入并转换 ' + file.name + '…');
      try {
        const content = await file.text();
        if (circuit !== originalCircuit || JSON.stringify(circuit) !== originalSnapshot)
          throw new Error('导入期间当前电路已变化，请重新选择文件');
        const response = await fetch('/api/import/external', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ source: source, content: content, name: file.name }),
        });
        let result;
        try { result = await response.json(); }
        catch (_) { throw new Error('导入服务返回了无法读取的响应'); }
        if (circuit !== originalCircuit || JSON.stringify(circuit) !== originalSnapshot)
          throw new Error('转换期间当前电路已变化，候选结果已丢弃');
        if (response.status === 422) {
          const issues = Array.isArray(result.issues) ? result.issues : [];
          const detail = issues.map(function (issue) {
            return (issue.path ? issue.path + '：' : '') + (issue.message || issue.code || '转换未完成');
          }).join('；');
          setEditStatus(detail || '外部结果缺少必要信息，已拒绝导入', true);
          return;
        }
        if (!response.ok) throw new Error(result.error || ('HTTP ' + response.status));
        const proposal = result.circuit;
        if (!proposal || proposal.version !== 2 || !Array.isArray(proposal.parts) ||
            !Array.isArray(proposal.connections))
          throw new Error('导入服务未返回有效的项目 CircuitJSON');

        const issues = Array.isArray(result.issues) ? result.issues.slice() : [];
        issues.unshift({ code: 'EXTERNAL_REVIEW_REQUIRED',
          message: labels[source] + ' 已转换为项目 CircuitJSON；请核对原始连接和器件参数' });
        let validation = null;
        try {
          const validationResponse = await fetch('/api/validate', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ circuit: proposal, source: 'import' }),
          });
          const validationBody = await validationResponse.json();
          if (!validationResponse.ok)
            throw new Error(validationBody.error || ('HTTP ' + validationResponse.status));
          validation = validationBody;
        } catch (error) {
          issues.push({ code: 'VALIDATION_UNAVAILABLE',
            message: '校验服务不可用：' + (error.message || String(error)) });
        }
        if (circuit !== originalCircuit || JSON.stringify(circuit) !== originalSnapshot)
          throw new Error('校验期间当前电路已变化，候选结果已丢弃');

        const ercGate = validation && validation.gates && validation.gates.erc;
        const counts = ercGate && ercGate.evidence && ercGate.evidence.counts || {};
        const importedErc = ercGate && ercGate.status !== 'not_run'
          ? Object.assign({ issues: ercGate.issues || [], passed: ercGate.status === 'pass' }, counts)
          : null;
        showEditReview({ circuit: proposal, erc: importedErc, changes: {},
          validation: validation, externalIssues: issues, origin: 'external',
          externalEvidence: { name: file.name, content: content },
          externalConverted: { name: file.name.replace(/\.[^.]+$/, '') + '.circuitjson.json', circuit: proposal } },
          originalCircuit, originalSnapshot, '外部导入：' + labels[source] + ' · ' + file.name);
        meta.textContent = '外部结果候选：' + file.name + ' · ' + circuitCounts(proposal) +
          ' · 已转换为项目 CircuitJSON，待人工审核';
      } catch (error) {
        if (circuit === originalCircuit && JSON.stringify(circuit) === originalSnapshot)
          setEditStatus('外部导入失败：' + (error.message || String(error)), true);
      } finally {
        editBusy = false;
        externalLoadBtn.disabled = false;
        if (imageLoadBtn) imageLoadBtn.disabled = false;
        syncHistoryButtons();
      }
    });
  }

  if (window.CircuitTscircuitEditor) window.CircuitTscircuitEditor.mount({
    begin: function () {
      if (editBusy || pendingEdit || pendingVision) throw new Error('请先完成当前候选审核或识别。');
      editBusy = true;
      return { originalCircuit: circuit, originalSnapshot: JSON.stringify(circuit) };
    },
    finish: function () { editBusy = false; syncHistoryButtons(); },
    review: async function (result, submitted, ticket, signal) {
      function unchanged() {
        if (signal.aborted) throw new DOMException('已取消', 'AbortError');
        if (circuit !== ticket.originalCircuit || JSON.stringify(circuit) !== ticket.originalSnapshot)
          throw new Error('当前电路已变化，请重新生成候选。');
      }
      unchanged();
      const issues = (result.issues || []).slice();
      let validation = null;
      try {
        const response = await fetch('/api/validate', {method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({circuit: result.circuit, source:'import'}), signal:signal});
        const body = await response.json();
        if (!response.ok) throw new Error(body.error || ('HTTP ' + response.status));
        validation = body;
      } catch (error) {
        if (signal.aborted) throw error;
        issues.push({code:'VALIDATION_UNAVAILABLE', message:'校验服务不可用：' + error.message});
      }
      unchanged();
      const gate = validation && validation.gates && validation.gates.erc;
      const counts = gate && gate.evidence && gate.evidence.counts || {};
      const erc = gate && gate.status !== 'not_run'
        ? Object.assign({issues:gate.issues || [], passed:gate.status === 'pass'}, counts) : null;
      showEditReview({circuit:result.circuit, erc:erc, changes:{}, validation:validation, origin:'external', externalIssues:issues,
        externalEvidence:{name:submitted.name,content:submitted.code},
        externalConverted:{name:submitted.name+'.project.json',circuit:result.circuit}},
        ticket.originalCircuit, ticket.originalSnapshot, '代码建电路：' + submitted.name);
      meta.textContent = '代码建电路候选：' + submitted.name + ' · ' + circuitCounts(result.circuit) + ' · 待人工审核';
    }
  });

  // ---------- 模板分类浏览（多方案面板扩展） ----------
  const TEMPLATE_CAT_ORDER = ["电源", "指示与显示", "传感", "驱动与功率", "接口与保护", "定时与逻辑", "模拟与滤波", "其他"];
  function groupTemplatesByCategory(list) {
    const g = {};
    (list || []).forEach(function (item) {
      const cat = (item && item.category) || '其他';
      if (!g[cat]) g[cat] = [];
      g[cat].push(item);
    });
    return g;
  }
  // 若后续挂载 /api/templates 可填充；当前 schemes 面板在展示时按 category 分组
  window.__TEMPLATE_CAT_ORDER = TEMPLATE_CAT_ORDER;
  window.__groupTemplatesByCategory = groupTemplatesByCategory;

  if (window.CircuitWorkbench) workbench = window.CircuitWorkbench.mount({
    getCircuit: function () { return circuit; },
    getController: function () { return ctl; },
    isDirty: function () { return dirty; },
    getValidationState: function () {
      return { report: window.__lastValidation || null, busy: validationBusy, error: validationError };
    },
    validate: function () { if (circuit) scheduleValidation('manual', 0); },
  });

  // ---------- 示例提示词 chips：点选即填入并触发生成 ----------
  var examplesEl = document.getElementById('examples');
  if (examplesEl) {
    examplesEl.addEventListener('click', function (e) {
      var chip = e.target.closest ? e.target.closest('.chip') : null;
      if (!chip) return;
      input.value = chip.getAttribute('data-q') || chip.textContent;
      if (form.requestSubmit) form.requestSubmit(); // 触发 submit → run()
      else goBtn.click();                           // 旧浏览器兜底
    });
  }
})();
