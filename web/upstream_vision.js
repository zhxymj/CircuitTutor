/* Local CircuitVision response display and explicit human correction form.
 * Model output is immutable evidence; only form values create a CircuitJSON.
 */
(function (root, factory) {
  if (typeof module !== 'undefined' && module.exports) module.exports = factory();
  else root.CircuitUpstreamVision = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const COMPONENTS = [
    { type: 'resistor', label: '电阻 R', prefix: 'R', needsValue: true },
    { type: 'capacitor', label: '电容 C', prefix: 'C', needsValue: true },
    { type: 'inductor', label: '电感 L', prefix: 'L', needsValue: true },
    { type: 'battery', label: '明确 DC 电源', prefix: 'V', needsValue: true },
    { type: 'gnd', label: '地 GND', prefix: 'GND', needsValue: false },
  ];

  function clone(value) { return JSON.parse(JSON.stringify(value)); }

  function validateResponse(result) {
    if (!result || result.status !== 'needs_manual_correction' || result.circuit !== null ||
        result.accepted !== false || result.electrical_validated !== false)
      throw new Error('本机识别服务未返回未校验的人工校正结果');
    if (!result.image || !Number.isFinite(result.image.width) || !Number.isFinite(result.image.height) ||
        result.image.width <= 0 || result.image.height <= 0 || !Array.isArray(result.detections))
      throw new Error('本机识别结果缺少图像尺寸或检测框');
    result.detections.forEach(function (detection, index) {
      const box = detection && detection.box;
      if (!box || ![box.x, box.y, box.w, box.h].every(Number.isFinite) || box.w < 0 || box.h < 0)
        throw new Error('第 ' + (index + 1) + ' 个检测框坐标无效');
    });
    const geometry = result.wire_geometry;
    if (geometry) {
      if (geometry.status !== 'geometry_candidates' || geometry.candidate_only !== true ||
          geometry.accepted !== false || geometry.electrical_validated !== false ||
          geometry.width !== result.image.width || geometry.height !== result.image.height ||
          !Array.isArray(geometry.segments) || !Array.isArray(geometry.points))
        throw new Error('导线几何候选不能标记为已通过电气确认');
      geometry.segments.forEach(function (segment) {
        if (![segment.x1, segment.y1, segment.x2, segment.y2].every(Number.isFinite))
          throw new Error('导线几何候选坐标无效');
      });
      geometry.points.forEach(function (point) {
        if (![point.x, point.y].every(Number.isFinite)) throw new Error('导线几何候选点坐标无效');
      });
    }
    return result;
  }

  function createOverlay(imageDataUrl, result) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 ' + result.image.width + ' ' + result.image.height);
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', '元件检测、分割与导线几何候选；尚未电气确认');
    svg.setAttribute('id', 'visionOverlay');
    svg.style.cssText = 'display:block;width:100%;height:auto;max-height:360px;background:#fff';
    const source = document.createElementNS(svg.namespaceURI, 'image');
    source.setAttribute('data-layer', 'source');
    source.setAttribute('href', imageDataUrl);
    source.setAttribute('x', '0'); source.setAttribute('y', '0');
    source.setAttribute('width', String(result.image.width)); source.setAttribute('height', String(result.image.height));
    source.setAttribute('preserveAspectRatio', 'none');
    svg.appendChild(source);
    if (result.segmentation && result.segmentation.status === 'segmented' && result.segmentation.mask_data_url) {
      const mask = document.createElementNS(svg.namespaceURI, 'image');
      mask.setAttribute('data-layer', 'mask'); mask.setAttribute('href', result.segmentation.mask_data_url);
      mask.setAttribute('x', '0'); mask.setAttribute('y', '0');
      mask.setAttribute('width', String(result.segmentation.width || result.image.width));
      mask.setAttribute('height', String(result.segmentation.height || result.image.height));
      mask.setAttribute('preserveAspectRatio', 'none'); mask.setAttribute('opacity', '0.28');
      svg.appendChild(mask);
    }
    const geometry = result.wire_geometry;
    if (geometry) {
      const group = document.createElementNS(svg.namespaceURI, 'g');
      group.setAttribute('data-layer', 'wire-geometry');
      const title = document.createElementNS(svg.namespaceURI, 'title');
      title.textContent = '蓝线与黄色点为导线几何候选，尚未电气确认；交叉处不能据此判定连接。';
      if (geometry.truncated) title.textContent += '导线候选已截取，需人工补充。';
      group.appendChild(title);
      geometry.segments.forEach(function (segment) {
        const line = document.createElementNS(svg.namespaceURI, 'line');
        ['x1', 'y1', 'x2', 'y2'].forEach(key => line.setAttribute(key, String(segment[key])));
        line.setAttribute('data-segment-id', String(segment.id));
        line.setAttribute('stroke', '#0284c7'); line.setAttribute('stroke-width', '2');
        line.setAttribute('stroke-dasharray', '6 3'); group.appendChild(line);
      });
      geometry.points.forEach(function (point) {
        const circle = document.createElementNS(svg.namespaceURI, 'circle');
        circle.setAttribute('cx', String(point.x)); circle.setAttribute('cy', String(point.y));
        circle.setAttribute('r', '3'); circle.setAttribute('fill', '#facc15');
        circle.setAttribute('stroke', '#854d0e'); circle.setAttribute('data-point-id', String(point.id));
        group.appendChild(circle);
      });
      svg.appendChild(group);
    }
    result.detections.forEach(function (detection, index) {
      const box = document.createElementNS(svg.namespaceURI, 'rect');
      box.setAttribute('data-detection-index', String(index));
      box.setAttribute('x', String(detection.box.x)); box.setAttribute('y', String(detection.box.y));
      box.setAttribute('width', String(detection.box.w)); box.setAttribute('height', String(detection.box.h));
      box.setAttribute('fill', 'none'); box.setAttribute('stroke', '#e11d48'); box.setAttribute('stroke-width', '2');
      const title = document.createElementNS(svg.namespaceURI, 'title');
      title.textContent = (detection.class_name || '未知类别') + ' · ' + Number(detection.score || 0).toFixed(3);
      box.appendChild(title); svg.appendChild(box);
    });
    return svg;
  }

  function pinNames(symbols, type) {
    return symbols && typeof symbols.pinsOf === 'function' ? symbols.pinsOf(type) : [];
  }

  function appendPinInputs(row, symbols, type) {
    const host = row.querySelector('[data-role="pins"]');
    host.replaceChildren();
    pinNames(symbols, type).forEach(function (pin) {
      const label = document.createElement('label');
      label.textContent = '引脚 ' + pin + ' 网络';
      const input = document.createElement('input');
      input.type = 'text'; input.autocomplete = 'off'; input.placeholder = '人工填写网络名';
      input.setAttribute('data-pin', pin);
      label.appendChild(input); host.appendChild(label);
    });
  }

  function readCorrections(container) {
    return Array.from(container.querySelectorAll('[data-role="correction-row"][data-detection-index]')).map(function (row) {
      const index = Number(row.getAttribute('data-detection-index'));
      const excluded = row.querySelector('[data-role="exclude"]').checked;
      const type = row.querySelector('[data-role="type"]').value;
      const value = row.querySelector('[data-role="value"]').value.trim();
      const pins = {};
      row.querySelectorAll('[data-pin]').forEach(function (input) {
        pins[input.getAttribute('data-pin')] = input.value.trim();
      });
      return { index: index, added_manually: row.getAttribute('data-added-manually') === 'true',
        excluded: excluded, type: type, value: value, pins: pins };
    });
  }

  function correctionErrors(result, corrections, symbols) {
    const errors = [];
    const byIndex = new Map(corrections.map(c => [c.index, c]));
    result.detections.forEach(function (_, index) {
      if (!byIndex.has(index)) errors.push('检测框 ' + (index + 1) + ' 未处理');
    });
    if (byIndex.size !== corrections.length) errors.push('校正项编号重复');
    corrections.forEach(function (correction) {
      const index = correction.index;
      if (!Number.isInteger(index) || index < 0 ||
          (index >= result.detections.length && correction.added_manually !== true) ||
          (index < result.detections.length && correction.added_manually === true)) {
        errors.push('校正项来源无效'); return;
      }
      if (correction.excluded) return;
      const component = COMPONENTS.find(item => item.type === correction.type);
      if (!component) { errors.push('检测框 ' + (index + 1) + ' 需要人工选择器件类型或明确排除'); return; }
      if (component.needsValue && !correction.value)
        errors.push('检测框 ' + (index + 1) + ' 需要人工填写参数值');
      const pins = pinNames(symbols, correction.type);
      if (!pins.length) { errors.push('检测框 ' + (index + 1) + ' 的器件符号或引脚不受支持'); return; }
      pins.forEach(function (pin) {
        if (!correction.pins[pin]) errors.push('检测框 ' + (index + 1) + ' 的引脚 ' + pin + ' 需要人工分配网络');
      });
      Object.keys(correction.pins).forEach(function (pin) {
        if (!pins.includes(pin)) errors.push('校正项 ' + (index + 1) + ' 的引脚 ' + pin + ' 不受支持');
      });
    });
    if (!corrections.some(c => !c.excluded)) errors.push('至少需要一个明确映射的器件');
    return errors;
  }

  function buildCandidate(result, corrections, symbols) {
    const errors = correctionErrors(result, corrections, symbols);
    if (errors.length) return { errors: errors };
    const counters = Object.create(null), parts = [], refsByNet = new Map(), correctionRows = [];
    corrections.forEach(function (correction) {
      const index = correction.index;
      const detection = result.detections[index];
      const evidenceRow = detection ? { detection_index: index, class_id: detection.class_id,
        class_name: detection.class_name, score: detection.score } :
        { detection_index: null, added_manually: true, correction_index: index, source: 'user_added_missing_component' };
      if (correction.excluded) {
        correctionRows.push(Object.assign(evidenceRow, { excluded: true, reason: 'user_marked_false_positive' }));
        return;
      }
      const component = COMPONENTS.find(item => item.type === correction.type);
      const idNumber = counters[component.prefix] = (counters[component.prefix] || 0) + 1;
      const id = component.prefix + idNumber;
      const box = detection ? detection.box : {x: result.image.width / 2, y: result.image.height / 2 + index * 25};
      const part = { id: id, type: component.type, left: Math.round(box.x * 2), top: Math.round(box.y * 2), rotate: 0, attrs: {} };
      if (component.needsValue) part.attrs.value = correction.value;
      parts.push(part);
      Object.entries(correction.pins).forEach(function (entry) {
        const pin = entry[0], net = entry[1].trim();
        if (!refsByNet.has(net)) refsByNet.set(net, []);
        refsByNet.get(net).push(id + ':' + pin);
      });
      correctionRows.push(Object.assign(evidenceRow, { excluded: false, type: component.type,
        value: correction.value || null, pins: Object.assign({}, correction.pins) }));
    });
    const connections = [], unconnectedPins = [];
    refsByNet.forEach(function (refs) {
      if (refs.length === 1) unconnectedPins.push(refs[0]);
      for (let i = 1; i < refs.length; i++) connections.push([refs[0], refs[i], 'blue', []]);
    });
    const circuit = { version: 2, title: 'CircuitVision 人工校正候选', parts: parts, connections: connections,
      provenance: { origin: 'image', backend: 'circuitvision', source_id: result.image.source_name,
        recognition_sha256: result.image.sha256 } };
    const correction = { schema_version: 1, source_name: result.image.source_name,
      raw_image_sha256: result.image.sha256, backend: 'circuitvision', detections: correctionRows,
      topology_source: 'manual_pin_to_net_mapping', unconnected_pins: unconnectedPins, circuit: clone(circuit) };
    return { circuit: circuit, correction: correction,
      issues: unconnectedPins.length ? [{ code: 'TOPOLOGY_UNCONNECTED_PIN',
        message: '以下引脚目前只映射到单端网络，统一门禁仍需检查是否悬空：' + unconnectedPins.join('、') }] : [] };
  }

  function render(container, imageDataUrl, result, symbols, callbacks) {
    container.replaceChildren();
    container.hidden = false;
    const section = document.createElement('section');
    section.id = 'visionCorrection';
    section.setAttribute('aria-label', 'CircuitVision 识别结果与人工校正');
    const heading = document.createElement('h4'); heading.textContent = '本机 CircuitVision 识别结果'; section.appendChild(heading);
    const note = document.createElement('p');
    note.textContent = '红框与彩色遮罩是元件和前景候选；蓝色虚线与黄色点是导线几何候选，尚未电气确认。交叉处和引脚连接仍需人工核对。逐项补充器件类型、参数和每个引脚所属网络。';
    section.appendChild(note);
    if (result.wire_geometry && result.wire_geometry.truncated) {
      const truncation = document.createElement('p');
      truncation.textContent = '导线候选已截取，需人工补充';
      section.appendChild(truncation);
    }
    const overlay = createOverlay(imageDataUrl, result); section.appendChild(overlay);
    const issueList = document.createElement('ul'); issueList.id = 'visionIssues';
    (result.issues || []).forEach(function (issue) {
      const li = document.createElement('li'); li.textContent = issue.message || issue.code || '需要人工复核'; issueList.appendChild(li);
    });
    section.appendChild(issueList);
    const rowsHost = document.createElement('div'); rowsHost.id = 'visionCorrectionRows';
    function appendRow(detection, index) {
      const row = document.createElement('fieldset'); row.setAttribute('data-role', 'correction-row');
      row.setAttribute('data-detection-index', String(index));
      if (!detection) row.setAttribute('data-added-manually', 'true');
      const legend = document.createElement('legend');
      legend.textContent = detection ? '检测框 ' + (index + 1) + ' · ' + (detection.class_name || '未知类别') +
        ' · 置信度 ' + Number(detection.score || 0).toFixed(3) : '人工补充漏检元件（不属于原始检测）';
      row.appendChild(legend);
      const typeLabel = document.createElement('label'); typeLabel.textContent = '人工确认器件类型';
      const select = document.createElement('select'); select.setAttribute('data-role', 'type');
      const placeholder = document.createElement('option'); placeholder.value = ''; placeholder.textContent = '请选择类型…';
      select.appendChild(placeholder);
      COMPONENTS.forEach(function (component) {
        const option = document.createElement('option'); option.value = component.type; option.textContent = component.label;
        select.appendChild(option);
      });
      typeLabel.appendChild(select); row.appendChild(typeLabel);
      const valueLabel = document.createElement('label'); valueLabel.textContent = '人工确认参数值';
      const valueInput = document.createElement('input'); valueInput.type = 'text'; valueInput.setAttribute('data-role', 'value');
      valueInput.placeholder = '未知时请查阅原图/料号资料后填写'; valueLabel.appendChild(valueInput); row.appendChild(valueLabel);
      const pins = document.createElement('div'); pins.setAttribute('data-role', 'pins'); row.appendChild(pins);
      const excludeLabel = document.createElement('label');
      const exclude = document.createElement('input'); exclude.type = 'checkbox'; exclude.setAttribute('data-role', 'exclude');
      excludeLabel.appendChild(exclude); excludeLabel.appendChild(document.createTextNode('明确标记为误检并排除'));
      row.appendChild(excludeLabel);
      if (!detection) {
        const remove = document.createElement('button'); remove.type = 'button';
        remove.textContent = '删除补充项'; remove.setAttribute('data-role', 'remove-manual');
        remove.addEventListener('click', function () { row.remove(); refresh(); }); row.appendChild(remove);
      }
      rowsHost.appendChild(row);
    }
    result.detections.forEach(appendRow);
    section.appendChild(rowsHost);
    let nextManualIndex = result.detections.length;
    const add = document.createElement('button'); add.type = 'button'; add.id = 'visionAddMissing';
    add.textContent = '补充漏检元件';
    add.addEventListener('click', function () { appendRow(null, nextManualIndex++); refresh(); });
    section.appendChild(add);
    const ratings = document.createElement('p'); ratings.className = 'vision-rating-warning';
    ratings.textContent = '器件耐压、功率等额定值未知；请按实际料号资料核对，模型检测分数不表示电气可信度。';
    section.appendChild(ratings);
    const actions = document.createElement('div'); actions.className = 'vision-actions';
    const rawButton = document.createElement('button'); rawButton.type = 'button'; rawButton.id = 'visionDownloadRaw';
    rawButton.textContent = '下载原始模型识别证据'; rawButton.addEventListener('click', callbacks.onDownloadRaw); actions.appendChild(rawButton);
    const inputButton = document.createElement('button'); inputButton.type = 'button'; inputButton.id = 'visionDownloadInput';
    inputButton.textContent = '下载模型实际输入图片'; inputButton.addEventListener('click', callbacks.onDownloadInput); actions.appendChild(inputButton);
    const build = document.createElement('button'); build.type = 'button'; build.id = 'visionBuild';
    build.textContent = '生成校正候选并校验'; build.disabled = true; build.addEventListener('click', function () {
      callbacks.onBuild(readCorrections(section));
    }); actions.appendChild(build);
    const cancel = document.createElement('button'); cancel.type = 'button'; cancel.id = 'visionCancel';
    cancel.textContent = '取消'; cancel.addEventListener('click', callbacks.onCancel); actions.appendChild(cancel);
    section.appendChild(actions);
    container.appendChild(section);

    function refresh() {
      const corrections = readCorrections(section);
      build.disabled = correctionErrors(result, corrections, symbols).length > 0;
    }
    rowsHost.addEventListener('change', function (event) {
      const target = event.target;
      const row = target.closest('[data-detection-index]');
      if (!row) return;
      if (target.matches('[data-role="type"]')) appendPinInputs(row, symbols, target.value);
      const isExcluded = row.querySelector('[data-role="exclude"]').checked;
      ['type', 'value'].forEach(function (role) { row.querySelector('[data-role="' + role + '"]').disabled = isExcluded; });
      row.querySelectorAll('[data-pin]').forEach(input => { input.disabled = isExcluded; });
      refresh();
    });
    rowsHost.addEventListener('input', refresh);
    return { section: section, overlay: overlay, refresh: refresh };
  }

  return { COMPONENTS: COMPONENTS, validateResponse: validateResponse, createOverlay: createOverlay,
    readCorrections: readCorrections, correctionErrors: correctionErrors,
    buildCandidate: buildCandidate, render: render };
});
