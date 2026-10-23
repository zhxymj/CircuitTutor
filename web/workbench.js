/* Engineering workbench chrome. Circuit state and commands remain owned by app.js. */
(function (root) {
  'use strict';
  function mount(api) {
    const $ = id => document.getElementById(id);
    if (!$('workbench-shell')) return null;
    const left = $('workbench-project'), right = $('workbench-inspector');
    const leftToggle = $('workbench-left-toggle'), rightToggle = $('workbench-right-toggle');
    const exportToggle = $('workbench-export-toggle'), exportMenu = $('workbench-export-menu');
    const checksToggle = $('workbench-checks-toggle'), checksBody = $('workbench-checks-body');
    const smallScreen = matchMedia('(max-width:760px)'), narrowScreen = matchMedia('(max-width:1080px)');
    /* 模块尺寸可调：左/右列宽与底部抽屉高度写入 CSS 变量并记忆在 localStorage
       （与主题同一前缀）。只影响布局尺寸，不触碰电路状态与既有命令路径；
       窄屏由 CSS 隐藏分隔条，桌面端（>1080px）才可拖拽。 */
    const shell = $('workbench-shell');
    const splitterEls = { left: document.querySelector('[data-splitter="left"]'), right: document.querySelector('[data-splitter="right"]'), drawer: document.querySelector('[data-splitter="drawer"]') };
    const LAYOUT_KEY = 'zht-layout', LAYOUT_VARS = { left: '--wb-left-w', right: '--wb-right-w', drawer: '--wb-drawer-h' };
    const LAYOUT_MIN = { left: 190, right: 220, drawer: 90 }, LAYOUT_MAX = { left: 480, right: 560 };
    const layout = { left: null, right: null, drawer: null };
    function panelSize(which) {
      const el = which === 'left' ? left : which === 'right' ? right : checksBody;
      const rect = el.getBoundingClientRect();
      return which === 'drawer' ? rect.height : rect.width;
    }
    function clampPanel(which, value) {
      if (which === 'drawer') return Math.max(LAYOUT_MIN.drawer, Math.min(Math.round(value), Math.max(LAYOUT_MIN.drawer, Math.round(shell.clientHeight * 0.6))));
      const rail = shell.querySelector('.wb-rail').getBoundingClientRect().width;
      const other = which === 'left' ? panelSize('right') : panelSize('left');
      const max = Math.max(LAYOUT_MIN[which], Math.min(LAYOUT_MAX[which], shell.clientWidth - rail - other - 320));
      return Math.max(LAYOUT_MIN[which], Math.min(Math.round(value), max));
    }
    function applyLayout() {
      Object.keys(LAYOUT_VARS).forEach(key => {
        if (layout[key] == null) shell.style.removeProperty(LAYOUT_VARS[key]);
        else shell.style.setProperty(LAYOUT_VARS[key], layout[key] + 'px');
      });
    }
    function saveLayout() { try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout)); } catch (error) { /* 隐私模式禁用存储时忽略 */ } }
    function dragSplitter(which, event) {
      if (event.button != null && event.button !== 0) return;
      event.preventDefault();
      if (which === 'drawer' && checksBody.hidden) setChecks(true);
      const axis = which === 'drawer' ? 'clientY' : 'clientX', start = event[axis], startSize = panelSize(which);
      function move(moveEvent) {
        const delta = moveEvent[axis] - start;
        layout[which] = clampPanel(which, which === 'right' ? startSize - delta : startSize + delta);
        applyLayout();
      }
      function finish() {
        document.removeEventListener('pointermove', move); document.removeEventListener('pointerup', finish);
        document.removeEventListener('pointercancel', finish); saveLayout();
      }
      document.addEventListener('pointermove', move); document.addEventListener('pointerup', finish);
      document.addEventListener('pointercancel', finish);
    }
    Object.keys(splitterEls).forEach(which => {
      const el = splitterEls[which]; if (!el) return;
      el.addEventListener('pointerdown', event => dragSplitter(which, event));
      el.addEventListener('dblclick', () => { layout[which] = null; applyLayout(); saveLayout(); });
      el.addEventListener('keydown', event => {
        const shrink = which === 'drawer' ? 'ArrowUp' : 'ArrowLeft', grow = which === 'drawer' ? 'ArrowDown' : 'ArrowRight';
        if (event.key === 'Home') { event.preventDefault(); layout[which] = null; applyLayout(); saveLayout(); return; }
        if (event.key !== shrink && event.key !== grow) return;
        event.preventDefault();
        const current = layout[which] == null ? panelSize(which) : layout[which];
        layout[which] = clampPanel(which, current + (event.key === shrink ? -16 : 16)); applyLayout(); saveLayout();
      });
    });
    try {
      const saved = JSON.parse(localStorage.getItem(LAYOUT_KEY)) || {};
      Object.keys(LAYOUT_VARS).forEach(key => { const value = Number(saved[key]); if (Number.isFinite(value) && value > 0) layout[key] = value; });
    } catch (error) { /* 隐私模式禁用 localStorage 时保持响应式默认值 */ }
    applyLayout();
    addEventListener('resize', () => {
      let changed = false;
      Object.keys(LAYOUT_VARS).forEach(key => {
        if (layout[key] == null) return;
        const clamped = clampPanel(key, layout[key]);
        if (clamped !== layout[key]) { layout[key] = clamped; changed = true; }
      });
      if (changed) applyLayout();
    });
    /* 教学模式（教育方式）：只读当前工程状态，给关键术语加解释气泡、给学习
       路径高亮当前步骤、给门禁结果补“为什么查这项”。默认关闭并记忆在
       localStorage（键 zht-teach）；关闭时对既有行为零影响，也不改电路/校验/
       导出结果。术语表与说明都是本地静态内容，无外部请求。 */
    const TEACH_KEY = 'zht-teach';
    const teachToggle = $('teachMode'), teachPath = $('teachPath'), teachOnboard = $('teachOnboard');
    const TEACH_TERMS = [
      ['#workbench-validate > span', 'validation'],
      ['#workbench-validate-inspector', 'validation'],
      ['[data-inspector-tab="properties"]', 'properties'],
      ['[data-inspector-tab="validation"]', 'validation'],
      ['#workbench-checks-toggle > strong', 'checks'],
      ['.wb-checks-intro', 'checks'],
      ['#erc-badge', 'erc'],
      ['#workbench-requirements > .wb-section-label', 'requirement'],
      ['#workbench-components > .wb-section-label', 'catalog'],
      ['.wb-menu-group[data-export-group="design"] > h3', 'export_design'],
      ['#export-svg', 'svg'],
      ['#export-bom', 'bom'],
      ['#export-kicad', 'kicad'],
      ['.wb-menu-group[data-export-group="simulation"] > h3', 'spice'],
      ['.wb-menu-group[data-export-group="pcb"] > h3', 'pcb']
    ];
    const TEACH_GLOSSARY = {
      requirement:{term:'需求 · REQUIREMENT',text:'用中文写清电源轨、器件型号与连接关系。缺供电、缺 I2C 地址等条件会先向你澄清，不会替你猜。'},
      catalog:{term:'元件目录 · CATALOG',text:'共 152 类器件。点一下即插入画布中央，插入后需要自己把引脚连起来。'},
      properties:{term:'元件属性 · PROPERTIES',text:'读取当前工程里的真实参数（位号、参数值、旋转、MPN、封装）。修改进入撤销栈，可随时撤销。'},
      validation:{term:'统一门禁 · VALIDATION',text:'结构 / 电气规则 / 额定值 / SPICE / KiCad 官方 ERC 与网表逐项检查。未运行记为“未完成”，不会用“看起来没问题”代替结论。'},
      checks:{term:'检查与过程 · CHECKS',text:'记录生成过程的每一步、规则检查明细与工程估算，用来说明“为什么得到这个结果”。'},
      erc:{term:'电气规则检查 · ERC',text:'用确定性规则查短路、悬空、极性反接、LED 缺限流电阻、缺去耦电容等；显示的是当前电路的真实运行结果。'},
      spice:{term:'SPICE 仿真 · SPICE',text:'用真实仿真算工作点或瞬态波形，属于独立于 AI 的外部复验，比“工程估算”更强。'},
      bom:{term:'物料清单 · BOM',text:'位号、型号 / MPN、数量与备注，可直接对照采购或贴片。'},
      kicad:{term:'KiCad 工程 · KICAD',text:'导出可直接打开的完整 KiCad 工程，再用官方 ERC / DRC 复验；解压后双击 OPEN_IN_KICAD.bat。'},
      svg:{term:'矢量图 · SVG',text:'导出的矢量原理图，适合贴进报告、论文或 PPT。'},
      pcb:{term:'制造候选 · PCB',text:'预布局 / 真实 PCB / 制造文件都只是候选；制造始终 release=false，需要电子工程师与板厂复核。'},
      export_design:{term:'原理图与物料',text:'这一组导出的是给人看的图纸与料单：SVG 矢量图、BOM 清单与 KiCad 工程。'}
    };
    const TEACH_GATE_WHY = {
      structure:'先确认电路结构合法：必需器件是否齐全、连接是否成立、有没有明显矛盾。',
      erc:'再用确定性电气规则排查短路、悬空、极性、限流与去耦等常见错误。',
      ratings:'核对元件额定值是否覆盖实际工作电压、电流与功耗，避免选型超限。',
      kicad:'交给 KiCad 官方 ERC 复验，独立于本工具自身的规则。',
      kicad_cli:'官方 CLI 不可用时会记为“未完成”，不会当成通过。',
      official_erc:'由 KiCad 计算的官方 ERC 结论，不是本工具的估算。',
      official_netlist:'把导出网表与当前电路逐引脚比对，防止导出后连接漂移。',
      ngspice:'用 ngspice 真实跑一遍仿真，验证工作点或瞬态行为。',
      spice:'检查生成的 SPICE 网表是否可用、是否真的运行过。',
      spice_deck:'生成的 SPICE 网表本身是否合法、能否被解析，这是仿真前的最低要求。',
      spice_runtime:'网表能否被 ngspice 真正跑起来；运行时错误会在这里暴露，而不是被估算掩盖。',
      kicad_export:'能否导出成可直接打开的 KiCad 工程，用于官方复验与留档。',
      simulation:'看仿真结果是否满足需求（例如 LED 电流是否在额定范围）。',
      pinmap:'核对引脚与封装的映射是否完整，避免“有器件却无焊盘”。',
      manufacturing:'复核制造文件与工艺规则；这一步不构成生产放行。'
    };
    /* 教学模式 · 元件教学卡：选中元件时在属性面板底部补“这是什么 / 为什么需要 /
       常见错误”，再加上当前电路已运行门禁里与该器件相关的真实问题。
       静态说明只用于常见器件，未覆盖的类型给出诚实的通用解释，不编造结论。 */
    const TEACH_PART_NOTES = {
      resistor:{what:'限流 / 分压元件：把电流或电压限制在需要的大小。',why:'LED 必须串限流电阻，否则电流失控会烧灯；上拉 / 下拉也靠电阻。',mistakes:'漏接或阻值算错（LED 电流超出额定）、功耗超限发热。'},
      capacitor:{what:'储能与滤波元件，隔直通交。',why:'电源入口与每个芯片供电脚附近都要去耦，抑制瞬态电压跌落。',mistakes:'漏放去耦电容；电解 / 钽电容极性接反。'},
      electrolytic:{what:'电解电容：容量大且有极性。',why:'常用于电源滤波与储能。',mistakes:'正负极接反、耐压不足。'},
      led:{what:'发光二极管：单向导通的指示灯。',why:'用来直观显示供电或运行状态。',mistakes:'不串限流电阻、极性接反、多颗并联共用一个限流电阻。'},
      diode:{what:'整流二极管：只允许单向导通。',why:'用于防反接与整流。',mistakes:'方向接反、耐压或电流不足。'},
      schottky:{what:'肖特基二极管：正向压降更低的二极管。',why:'低压整流与防反接时损耗更小。',mistakes:'反向漏电与耐压未核对。'},
      zener:{what:'齐纳二极管：反向击穿时起稳压/限幅作用。',why:'用于简易稳压与限幅保护。',mistakes:'缺限流电阻、稳压值选错。'},
      npn:{what:'NPN 三极管：小电流控制大电流的开关/放大器件。',why:'常用于驱动继电器、蜂鸣器、LED 等负载。',mistakes:'基极限流电阻缺失、发射极/集电极接反、感性负载缺续流二极管。'},
      pnp:{what:'PNP 三极管：高边开关常用。',why:'用于电源正极侧的通断控制。',mistakes:'基极电平逻辑接反、缺少上拉。'},
      nmos:{what:'N 沟道 MOSFET：电压控制的开关管。',why:'导通电阻小，适合较大电流负载。',mistakes:'栅极无下拉导致误导通、栅极驱动电压不足。'},
      pmos:{what:'P 沟道 MOSFET：高边开关常用。',why:'用于电源正极侧通断。',mistakes:'栅极逻辑接反、缺少栅极电阻。'},
      battery:{what:'电源：给电路提供能量。',why:'所有器件都要供电，电压需与器件额定值匹配。',mistakes:'未标电压、极性接反、供电轨与器件不匹配。'},
      vcc:{what:'电源符号：标记某个网络是正电源。',why:'把供电网络标清楚，便于阅读与检查。',mistakes:'与真实供电轨同名不同压，或缺失供电网络。'},
      gnd:{what:'地符号：所有信号与电流的参考点。',why:'没有公共地，电路无法形成回路，也无法判断电平。',mistakes:'地不连通、信号地与大电流地混在一起。'},
      opamp:{what:'运算放大器：差分放大与信号调理核心。',why:'用于放大、比较、缓冲等模拟功能。',mistakes:'电源脚未接、输入超出共模范围、缺少反馈网络。'},
      ne555:{what:'555 定时器：经典振荡/延时芯片。',why:'做闪烁、延时、脉冲输出等教学与实用电路。',mistakes:'复位脚悬空、控制脚缺去耦电容、阻容取值算错。'},
      regulator_7805:{what:'线性稳压器：把输入电压稳定到固定输出。',why:'给需要稳定电压的器件供电。',mistakes:'输入/输出缺电容、压差过大导致过热。'},
      crystal:{what:'晶振：提供精确时钟。',why:'MCU 与通信芯片需要稳定时钟源。',mistakes:'负载电容缺失、走线过长。'},
      switch:{what:'开关：手动通断电路。',why:'用于上电控制或模式选择。',mistakes:'引脚接错导致常通/常断、未做消抖。'},
      pushbutton:{what:'按键：瞬时通断。',why:'用于复位或用户输入。',mistakes:'缺上拉/下拉，悬空时电平不定。'},
      potentiometer:{what:'电位器：可调电阻。',why:'用于分压调压、调节灵敏度。',mistakes:'三脚接反导致调节方向相反。'},
      inductor:{what:'电感：储能与滤波。',why:'用于开关电源储能与 EMI 抑制。',mistakes:'饱和电流不足、与电容参数不匹配。'},
      buzzer:{what:'蜂鸣器：发声器件。',why:'用于提示音与报警。',mistakes:'有源/无源类型选错、缺少驱动三极管或续流。'},
      atmega328:{what:'ATmega328P：常见 8 位 MCU。',why:'做主控，读传感器并驱动负载。',mistakes:'去耦电容缺失、复位与时钟未接、I2C 地址未声明。'},
      esp32:{what:'ESP32：带无线功能的 MCU。',why:'做带 WiFi / 蓝牙的采集与控制。',mistakes:'供电电流不足、启动脚被拉低、去耦缺失。'},
      i2c_header:{what:'I2C 连接器：引出 SDA / SCL / 电源 / 地。',why:'把子板或传感器接到主控。',mistakes:'SDA 与 SCL 接反、缺上拉、供电轨接错。'},
      dht11:{what:'DHT11：数字温湿度传感器。',why:'采集环境温湿度。',mistakes:'缺上拉、供电电压不符、数据线过长。'},
      i2c_rh_sensor:{what:'I2C 温湿度传感器：通过 I2C 读温湿度。',why:'用于环境监测子板。',mistakes:'I2C 地址未声明、缺上拉、去耦缺失。'},
      bmp280:{what:'BMP280：气压与温度传感器。',why:'用于气压 / 高度测量。',mistakes:'I2C 地址冲突、缺上拉。'},
      mpu6050:{what:'MPU6050：六轴姿态传感器。',why:'用于姿态与运动检测。',mistakes:'中断脚接错、供电不稳导致读数漂移。'},
      fuse:{what:'保险丝：过流保护。',why:'异常电流时切断，保护后级电路。',mistakes:'额定电流选得过大，失去保护意义。'}
    };
    function teachPartGroup(type) {
      const option = document.querySelector('#insert-type option[value="' + type + '"]');
      const group = option && option.closest('optgroup');
      return group ? group.label : '';
    }
    function teachPartIssues(part, state) {
      const gates = (state.report && state.report.gates) || {}, found = [];
      Object.keys(gates).forEach(key => (Array.isArray(gates[key].issues) ? gates[key].issues : []).forEach(issue => {
        const pins = issue && Array.isArray(issue.pins) ? issue.pins : [];
        const text = typeof issue === 'string' ? issue : (issue && (issue.message || issue.code) || '');
        if (text && pins.some(pin => typeof pin === 'string' && pin.split(':')[0] === part.id) && !found.includes(text)) found.push(text);
      }));
      return found.slice(0, 4);
    }
    function teachPartCard(part, state) {
      const notes = TEACH_PART_NOTES[part.type] || null;
      const info = root.CircuitInteract && root.CircuitInteract.PART_INFO ? root.CircuitInteract.PART_INFO[part.type] : null;
      const group = teachPartGroup(part.type), name = info ? info.name : part.type;
      const card = document.createElement('div'); card.className = 'wb-teach-part';
      const head = document.createElement('h4'); head.textContent = '教学卡 · TEACHING CARD'; card.appendChild(head);
      const meta = document.createElement('p'); meta.className = 'wb-teach-part-meta';
      meta.textContent = [name, part.id, group].filter(Boolean).join(' · '); card.appendChild(meta);
      function block(label, text) {
        const section = document.createElement('section'), title = document.createElement('b'), body = document.createElement('p');
        title.textContent = label; body.textContent = text; section.append(title, body); card.appendChild(section);
      }
      block('这是什么', notes ? notes.what : ('这是' + name + (group ? '（' + group + '）' : '') + '；插入后需要为每个引脚接上网络。它的作用要结合具体连接判断。'));
      block('为什么需要', notes ? notes.why : '它的作用取决于连在哪两个网络上：先看它与哪些器件共享网络，再判断是否合理。');
      const section = document.createElement('section'), title = document.createElement('b'), list = document.createElement('ul');
      title.textContent = '常见错误（教学提示）';
      (notes ? [notes.mistakes] : ['引脚悬空或漏接', '极性 / 方向接反', '参数超出额定范围']).forEach(text => {
        const item = document.createElement('li'); item.textContent = text; list.appendChild(item);
      });
      section.append(title, list); card.appendChild(section);
      const found = teachPartIssues(part, state);
      if (found.length) {
        const box = document.createElement('section'), boxTitle = document.createElement('b'), boxList = document.createElement('ul');
        boxTitle.textContent = '当前电路已发现（来自门禁报告）';
        found.forEach(text => { const item = document.createElement('li'); item.textContent = text; boxList.appendChild(item); });
        box.append(boxTitle, boxList); card.appendChild(box);
      }
      return card;
    }
    /* 教学模式 · 瞬态时序图：数据来自后端 /api/spice_transient 解析的真实
       ngspice .tran 输出。这里只做阈值数字化与排版，绝不合成波形：接口返回
       ran=false 时显示“未运行 + 原因”，不画替代图。阈值与每格时长都写在图上
       与下方“运行事实”里，避免把教学图误读成实测或验收结论。 */
    const TIMING_SLOTS = 48;
    const timingSection = $('workbench-timing'), timingStage = $('timingStage'), timingRun = $('timingRun');
    const TIMING_REASONS = {
      ngspice_unavailable: '本机未找到 ngspice',
      no_voltage_nodes: '未从 .op 输出识别到电压节点',
      tran_run_failed: 'ngspice 瞬态运行失败',
      no_transient_table: 'ngspice 未输出 .print tran 波形表'
    };
    function timingMessage(text) {
      if (!timingStage) return;
      timingStage.replaceChildren();
      const el = document.createElement('p'); el.className = 'wb-timing-empty';
      el.textContent = text; timingStage.appendChild(el);
    }
    function timingLevels(series) {
      const lo = Math.min.apply(null, series), hi = Math.max.apply(null, series);
      if (!(hi - lo > 1e-3)) return null;              // 平直无跳变：不画
      const mid = (lo + hi) / 2;
      const stride = Math.max(1, Math.floor(series.length / TIMING_SLOTS));
      const slots = [];
      for (let i = 0; i < series.length && slots.length < TIMING_SLOTS; i += stride) {
        slots.push(series[i] >= mid ? '1' : '0');
      }
      return { wave: slots.join(''), mid: mid, lo: lo, hi: hi };
    }
    function drawTiming(data) {
      if (!timingStage) return;
      timingStage.replaceChildren();
      if (!root.wavedrom || typeof root.wavedrom.renderAny !== 'function') {
        timingMessage('时序渲染器（wavedrom）未加载，无法绘制。'); return;
      }
      const nodes = (data.nodes || []).filter(node => data.series && Array.isArray(data.series[node]));
      const signals = [], skipped = [], THS = [];
      nodes.forEach(node => {
        const lv = timingLevels(data.series[node]);
        if (!lv) { skipped.push(node); return; }
        signals.push({ name: node + ' （阈值 ' + lv.mid.toFixed(3) + ' V）', wave: lv.wave });
        THS.push(node + ' ' + lv.mid.toFixed(3) + 'V');
      });
      if (!signals.length) {
        timingMessage('仿真已运行，但所有节点电压平直（无跳变），故未绘制时序图。'); return;
      }
      const span = (data.time && data.time.length > 1) ? (data.time[data.time.length - 1] - data.time[0]) : 0;
      const wavejson = {
        signal: signals,
        config: { hscale: 1 },
        head: { text: '真实 .tran 输出 · 阈值数字化' },
        foot: { text: '每格 ≈ ' + (span ? (span / TIMING_SLOTS * 1000).toFixed(3) : '?') +
          ' ms · 共 ' + TIMING_SLOTS + ' 格 · .tran 共 ' + String(data.rows) + ' 行' }
      };
      let svg;
      try {
        svg = root.wavedrom.onml.stringify(root.wavedrom.renderAny(0, wavejson, root.wavedrom.waveSkin, false));
      } catch (error) { timingMessage('时序图渲染失败：' + error.message); return; }
      const canvas = document.createElement('div');
      canvas.className = 'wb-timing-canvas'; canvas.innerHTML = svg;
      timingStage.appendChild(canvas);
      const facts = document.createElement('p');
      facts.className = 'wb-timing-facts';
      facts.textContent = '运行事实：engine=' + data.engine + ' · 节点 ' + signals.length + '/' + nodes.length +
        ' · .tran 采样 ' + String(data.rows) + ' 行 · 阈值（节点电压中值）' + THS.join('、') +
        (skipped.length ? ' · 平直未绘制：' + skipped.join('、') : '');
      timingStage.appendChild(facts);
    }
    async function runTiming() {
      const circuit = api.getCircuit();
      if (!circuit) { timingMessage('尚未创建电路，无法运行瞬态仿真。'); return; }
      timingRun.disabled = true;
      timingMessage('正在运行 ngspice .tran …');
      try {
        const response = await fetch('/api/spice_transient', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ circuit: circuit, step: 1e-4, stop: 0.01 })
        });
        const data = await response.json().catch(() => null);
        if (!response.ok || !data) {
          timingMessage('瞬态接口返回错误：' + ((data && data.error) || ('HTTP ' + response.status))); return;
        }
        if (!data.ran) {
          timingMessage('未运行：' + (TIMING_REASONS[data.reason] || data.reason || '未知原因') +
            '。此处不显示替代波形，请以实际仿真或实测为准。'); return;
        }
        drawTiming(data);
      } catch (error) {
        timingMessage('瞬态请求失败：' + error.message);
      } finally {
        timingRun.disabled = false;
      }
    }
    if (timingRun) timingRun.addEventListener('click', runTiming);
    const teachTermEls = [];
    TEACH_TERMS.forEach(pair => { const el = document.querySelector(pair[0]); if (el) { el.dataset.teach = pair[1]; teachTermEls.push(el); } });
    const teachTip = document.createElement('div');
    teachTip.id = 'teachTip'; teachTip.setAttribute('role', 'tooltip'); teachTip.hidden = true;
    document.body.appendChild(teachTip);
    let teaching = false, tipFor = null;
    function hideTeachTip() { teachTip.hidden = true; tipFor = null; }
    function showTeachTip(el) {
      if (!teaching || tipFor === el) return;
      const entry = TEACH_GLOSSARY[el.dataset.teach]; if (!entry) return;
      teachTip.replaceChildren();
      const title = document.createElement('b'); title.textContent = entry.term;
      teachTip.append(title, document.createTextNode(entry.text));
      teachTip.hidden = false; tipFor = el;
      const rect = el.getBoundingClientRect(), box = teachTip.getBoundingClientRect();
      const left = Math.min(Math.max(8, rect.left), Math.max(8, innerWidth - box.width - 8));
      const below = rect.bottom + 8;
      const top = below + box.height > innerHeight - 8 ? Math.max(8, rect.top - box.height - 8) : below;
      teachTip.style.left = left + 'px'; teachTip.style.top = top + 'px';
    }
    function teachStep(state, circuit) {
      if (!circuit) return 1;
      if (!state.report) return 3;
      return state.report.accepted ? 5 : 4;
    }
    function updateTeachPath(state, circuit) {
      if (!teaching || !teachPath) return;
      const current = teachStep(state, circuit);
      teachPath.querySelectorAll('[data-step]').forEach(item => {
        const n = Number(item.dataset.step);
        item.classList.toggle('done', n < current);
        item.classList.toggle('active', n === current);
      });
    }
    function applyTeach(on) {
      teaching = on;
      document.documentElement.setAttribute('data-teach', on ? 'on' : 'off');
      if (teachToggle) teachToggle.checked = on;
      if (teachPath) teachPath.hidden = !on;
      if (teachOnboard) teachOnboard.hidden = !on;
      if (timingSection) {
        timingSection.hidden = !on;
        if (!on && timingStage) timingStage.replaceChildren();   // 关闭时不留上次的图
      }
      teachTermEls.forEach(el => {
        if (on && !el.hasAttribute('tabindex') && !/^(?:BUTTON|A|INPUT|SELECT|TEXTAREA)$/.test(el.tagName)) {
          el.setAttribute('tabindex', '0'); el.dataset.teachTab = '1';
        } else if (!on && el.dataset.teachTab === '1') {
          el.removeAttribute('tabindex'); delete el.dataset.teachTab;
        }
      });
      if (on) updateTeachPath(api.getValidationState(), api.getCircuit());
      else hideTeachTip();
      refreshSelection();
      try { localStorage.setItem(TEACH_KEY, on ? '1' : '0'); } catch (error) { /* 隐私模式禁用存储时忽略 */ }
    }
    if (teachToggle) teachToggle.addEventListener('change', () => applyTeach(teachToggle.checked));
    document.addEventListener('mouseover', event => { const el = event.target && event.target.closest ? event.target.closest('[data-teach]') : null; if (el) showTeachTip(el); });
    document.addEventListener('mouseout', event => { const el = event.target && event.target.closest ? event.target.closest('[data-teach]') : null; if (el && el === tipFor) hideTeachTip(); });
    document.addEventListener('focusin', event => { const el = event.target && event.target.closest ? event.target.closest('[data-teach]') : null; if (el) showTeachTip(el); });
    document.addEventListener('focusout', event => { const el = event.target && event.target.closest ? event.target.closest('[data-teach]') : null; if (el && el === tipFor) hideTeachTip(); });
    document.addEventListener('keydown', event => { if (event.key === 'Escape') hideTeachTip(); });
    addEventListener('scroll', hideTeachTip, true);
    addEventListener('resize', hideTeachTip);
    let teachStored = false;
    try { teachStored = localStorage.getItem(TEACH_KEY) === '1'; } catch (error) { teachStored = false; }
    applyTeach(teachStored);

    const FONT_KEY = 'zht-font-scale';
    const fontSelect = $('fontScale');
    function applyFontScale(value) {
      const next = (value === 'compact' || value === 'large') ? value : 'standard';
      document.documentElement.setAttribute('data-font', next);
      if (fontSelect) fontSelect.value = next;
      try { localStorage.setItem(FONT_KEY, next); } catch (error) { /* 隐私模式禁用存储时忽略 */ }
    }
    if (fontSelect) fontSelect.addEventListener('change', () => applyFontScale(fontSelect.value));
    let fontStored = 'standard';
    try { fontStored = localStorage.getItem(FONT_KEY) || 'standard'; } catch (error) { fontStored = 'standard'; }
    applyFontScale(fontStored);
    const partList = $('workbench-part-list'), insert = $('insert-type'), partRows = [];
    function setDrawer(side, open) {
      const panel = side === 'left' ? left : right, toggle = side === 'left' ? leftToggle : rightToggle;
      panel.classList.toggle('mobile-open', open); toggle.setAttribute('aria-expanded', String(open));
      if (side === 'left' ? smallScreen.matches : narrowScreen.matches) panel.setAttribute('aria-hidden', String(!open));
      else panel.removeAttribute('aria-hidden');
      if (open) {
        const other = side === 'left' ? right : left, otherToggle = side === 'left' ? rightToggle : leftToggle;
        other.classList.remove('mobile-open'); otherToggle.setAttribute('aria-expanded', 'false');
        if (side === 'left' ? narrowScreen.matches : smallScreen.matches) other.setAttribute('aria-hidden', 'true');
      }
      $('workbench-scrim').hidden = !((smallScreen.matches && left.classList.contains('mobile-open')) || (narrowScreen.matches && right.classList.contains('mobile-open')));
    }
    function closeDrawers() { setDrawer('left', false); setDrawer('right', false); }
    function selectTab(name, reveal) {
      document.querySelectorAll('[data-workbench-tab]').forEach(button => {
        const on = button.dataset.workbenchTab === name;
        button.classList.toggle('active', on); button.setAttribute('aria-selected', String(on)); button.tabIndex = on ? 0 : -1;
        $(button.getAttribute('aria-controls')).hidden = !on;
      });
      document.querySelectorAll('[data-workbench-nav]').forEach(button => button.classList.toggle('active', button.dataset.workbenchNav === name));
      if (reveal && smallScreen.matches) setDrawer('left', true);
    }
    function selectInspector(name, reveal) {
      document.querySelectorAll('[data-inspector-tab]').forEach(button => {
        const on = button.dataset.inspectorTab === name;
        button.classList.toggle('active', on); button.setAttribute('aria-selected', String(on)); button.tabIndex = on ? 0 : -1;
        $(button.getAttribute('aria-controls')).hidden = !on;
      });
      if (reveal && narrowScreen.matches) setDrawer('right', true);
    }
    function setChecks(open) { checksBody.hidden = !open; checksToggle.setAttribute('aria-expanded', String(open)); }
    function setExports(open) { exportMenu.hidden = !open; exportToggle.setAttribute('aria-expanded', String(open)); }
    document.querySelectorAll('[data-workbench-tab]').forEach(button => button.addEventListener('click', () => selectTab(button.dataset.workbenchTab, false)));
    document.querySelectorAll('[data-inspector-tab]').forEach(button => button.addEventListener('click', () => selectInspector(button.dataset.inspectorTab, false)));
    document.querySelectorAll('.wb-tabs').forEach(tabs => tabs.addEventListener('keydown', event => {
      if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
      const buttons = Array.from(tabs.querySelectorAll('[role=tab]')), current = buttons.indexOf(document.activeElement);
      if (current < 0) return;
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length-1 : (current+(event.key === 'ArrowRight' ? 1 : -1)+buttons.length)%buttons.length;
      event.preventDefault(); buttons[next].click(); buttons[next].focus();
    }));
    document.querySelectorAll('[data-workbench-nav]').forEach(button => button.addEventListener('click', () => {
      const name = button.dataset.workbenchNav;
      if (name === 'checks') { setChecks(checksBody.hidden); closeDrawers(); return; }
      selectTab(name === 'settings' ? 'files' : name, true);
      if (name === 'settings') $('themeWrap').scrollIntoView({block:'nearest'});
    }));
    leftToggle.addEventListener('click', () => setDrawer('left', !left.classList.contains('mobile-open')));
    rightToggle.addEventListener('click', () => setDrawer('right', !right.classList.contains('mobile-open')));
    $('workbench-scrim').addEventListener('click', closeDrawers);
    exportToggle.addEventListener('click', () => { setExports(exportMenu.hidden); closeDrawers(); });
    checksToggle.addEventListener('click', () => setChecks(checksBody.hidden));
    document.addEventListener('click', event => { if (!event.target.closest('.wb-export-wrap')) setExports(false); });
    document.addEventListener('keydown', event => { if (event.key === 'Escape') { setExports(false); closeDrawers(); } });
    function responsive() { closeDrawers(); }
    smallScreen.addEventListener('change', responsive); narrowScreen.addEventListener('change', responsive);
    // Library symbols and insertion share the existing editor catalog and command path.
    Array.from(insert.querySelectorAll('optgroup')).forEach(group => {
      const heading = document.createElement('h3'); heading.className = 'wb-library-heading'; heading.textContent = group.label; partList.appendChild(heading);
      const rows = [];
      Array.from(group.querySelectorAll('option')).forEach(option => {
        const type = option.value, info = root.CircuitInteract.PART_INFO[type], button = document.createElement('button');
        button.type = 'button'; button.className = 'wb-part-button'; button.dataset.partType = type; button.title = '插入'+info.name;
        const symbol = document.createElement('span'); symbol.className = 'wb-part-symbol';
        const def = root.CircuitSymbols.LIB[type];
        if (def) symbol.innerHTML = '<svg aria-hidden="true" viewBox="-9 -9 '+(def.w+18)+' '+(def.h+18)+'">'+root.CircuitSymbols.renderSymbol(type,0,0,0)+'</svg>';
        else symbol.textContent = info.ref;
        const label = document.createElement('span'); label.className = 'wb-part-name'; label.textContent = info.name;
        const subtitle = document.createElement('small'); subtitle.textContent = info.ref+' · '+type;
        label.appendChild(subtitle); button.append(symbol,label); partList.appendChild(button);
        button.addEventListener('click', () => {
          if (!api.getController()) return;
          insert.value = type; insert.dispatchEvent(new Event('change',{bubbles:true})); selectInspector('properties',false);
          if (smallScreen.matches) closeDrawers(); refresh();
        });
        const row = {button,text:(info.name+' '+info.ref+' '+type+' '+group.label).toLowerCase()}; rows.push(row); partRows.push(row);
      });
      heading._rows = rows;
    });
    const noResults = document.createElement('p'); noResults.className = 'wb-subtle'; noResults.textContent = '没有匹配的元件，请尝试中文名或元件类型。'; noResults.hidden = true; partList.appendChild(noResults);
    $('workbench-library-count').textContent = partRows.length+' 种';
    $('workbench-part-search').addEventListener('input', event => {
      const search = event.target.value.trim().toLowerCase(); let count=0;
      partRows.forEach(row => { row.button.hidden = !row.text.includes(search); if (!row.button.hidden) count++; });
      partList.querySelectorAll('.wb-library-heading').forEach(heading => { heading.hidden = !heading._rows.some(row => !row.button.hidden); }); noResults.hidden = count>0;
    });
    const gateNames = {structure:'电路结构',erc:'电气规则',ratings:'额定值',kicad:'KiCad 官方检查',ngspice:'ngspice 仿真',spice:'SPICE 仿真',simulation:'电路仿真',pinmap:'引脚与封装映射',kicad_cli:'KiCad 官方检查',manufacturing:'制造文件复核',spice_deck:'SPICE 网表',spice_runtime:'SPICE 实际运行',kicad_export:'KiCad 工程导出',official_erc:'KiCad 官方 ERC',official_netlist:'官方网表一致性'};
    const gateStatuses = {pass:'通过',fail:'未通过',incomplete:'未完成',unavailable:'未完成',not_run:'未运行',skipped:'未运行',not_applicable:'不适用',unsupported:'不支持',blocked:'被阻断',error:'错误'};
    function renderValidation(state, hasCircuit, target) {
      const host = target || $('workbench-validation-report'); host.replaceChildren(); const report = state.report;
      const summary = document.createElement('div'); summary.className='wb-validation-summary';
      summary.textContent = state.busy ? '正在运行当前电路的官方校验…' : state.error ? '校验未完成：'+state.error : report ? (report.accepted ? '当前电路已通过适用门禁。' : '当前电路仍需复核，查看各项检查结果。') : (hasCircuit ? '当前电路尚无完整校验结果。点击上方按钮开始检查。' : '创建或载入电路后运行校验。'); host.appendChild(summary);
      if (!report || state.busy) return;
      Object.keys(report.gates||{}).forEach(key => {
        const gate=report.gates[key]||{}, row=document.createElement('div'); row.className='wb-validation-gate'; row.dataset.gate=key; row.dataset.status=gate.status||'not_run';
        const titleRow=document.createElement('div'), title=document.createElement('strong'); title.textContent=gateNames[key]||key;
        const status=document.createElement('span'); status.className='wb-gate-status '+(gate.status||'not_run'); status.textContent=gateStatuses[gate.status]||gate.status||'未运行'; titleRow.append(title,status); row.appendChild(titleRow);
        const messages=[]; if (gate.reason) messages.push(String(gate.reason)); if (gate.message) messages.push(String(gate.message));
        (Array.isArray(gate.issues)?gate.issues:[]).slice(0,6).forEach(issue => messages.push(typeof issue === 'string' ? issue : issue.message||issue.code||'检查项需复核'));
        if (messages.length) { const detail=document.createElement('small'); detail.textContent=messages.join('；'); row.appendChild(detail); }
        if (teaching && TEACH_GATE_WHY[key]) { const why=document.createElement('small'); why.className='wb-gate-why'; why.textContent='为什么查这项：'+TEACH_GATE_WHY[key]; row.appendChild(why); } host.appendChild(row);
      });
    }
    function runValidation() { selectInspector('validation',true); api.validate(); refresh(); }
    $('workbench-validate').addEventListener('click',runValidation); $('workbench-validate-inspector').addEventListener('click',runValidation);
    function showEvidence() {
      const host=$('evidenceBody'), circuit=api.getCircuit(), state=api.getValidationState(); host.replaceChildren();
      function row(text) { const el=document.createElement('div'); el.className='ev-row'; el.textContent=text; host.appendChild(el); }
      if (!circuit) { row('当前尚无工程。创建或载入电路后，查看工程来源与当前门禁报告。'); return; }
      row('当前工程：'+String(circuit.title||circuit.name||'未命名工程'));
      const origin=circuit.provenance&&circuit.provenance.origin;
      const origins={prompt:'中文需求',template:'本地模板',image:'图片识别',external:'外部导入',import:'本地导入',manual:'手工编辑'};
      row('工程来源：'+(origin?(origins[origin]||String(origin)):'工程未记录来源')+
        ($('srcBadge').textContent?' · 当前状态：'+$('srcBadge').textContent:''));
      const notes=circuit.engineering_example;
      if (notes&&typeof notes==='object') {
        [['sensor','传感器原始备注'],['assumptions','工程原始假设'],['limitations','工程原始限制'],['review_required','原始复核标记 review_required']].forEach(pair => {
          if (!Object.prototype.hasOwnProperty.call(notes,pair[0])) return;
          const value=notes[pair[0]];
          row(pair[1]+'：'+(typeof value==='string'?value:JSON.stringify(value,null,2)));
        });
      }
      const unknown=[];
      Object.values(state.report&&state.report.gates||{}).forEach(gate => {
        (Array.isArray(gate.issues)?gate.issues:[]).forEach(issue => {
          const message=typeof issue==='string'?issue:issue.message||issue.code||'';
          if (/未知|缺少|缺失|未提供|未指定|不明确|unknown|missing/i.test(message)&&!unknown.includes(message)) unknown.push(message);
        });
      });
      row('门禁报告中的缺项与未知参数：'+(unknown.length?unknown.join('；'):state.report?'当前报告未单列缺项摘要。请同时核对工程原始备注及各门禁问题。':'尚未完成当前电路的完整检查。'));
      const report=document.createElement('div'); report.id='workbench-evidence-gates'; host.appendChild(report);
      renderValidation(state,true,report);
      row('报告位置：右侧“电路校验”显示同一份当前门禁报告。图片与代码候选的原始结果可在对应候选审核面板查看和下载。');
    }
    function refreshSelection() {
      const ctl=api.getController(), circuit=api.getCircuit(), selected=ctl&&ctl.getSelected();
      const part=selected&&circuit&&(circuit.parts||[]).find(p => p.id===selected);
      $('workbench-selection-empty').hidden=!!part; $('workbench-status-selection').textContent=part?'已选中 '+part.id:'未选中元件';
      $('workbench-selection-caption').textContent=part ? ((circuit.connections||[]).filter(c => Array.isArray(c) && c.slice(0,2).some(ref => typeof ref === 'string' && ref.split(':')[0]===part.id)).length+' 条相关连线') : '';
      const metadata=$('workbench-part-metadata');
      metadata.hidden=!part; metadata.replaceChildren();
      if (part) {
        const title=document.createElement('h3'); title.textContent='器件信息'; metadata.appendChild(title);
        const component=part.component||{};
        [['型号 / MPN','mpn'],['制造商','manufacturer'],['封装','footprint']].forEach(pair => {
          const row=document.createElement('div'), label=document.createElement('span'), value=document.createElement('div');
          row.className='wb-metadata-row'; label.textContent=pair[0];
          value.textContent=component[pair[1]]!=null&&String(component[pair[1]]).trim()?String(component[pair[1]]):'未提供';
          row.append(label,value); metadata.appendChild(row);
        });
        if (teaching) metadata.appendChild(teachPartCard(part, api.getValidationState()));
      }
    }
    function refresh() {
      const circuit=api.getCircuit(), has=!!circuit, title=has?String(circuit.title||circuit.name||'未命名工程'):'未命名工程';
      const parts=has&&Array.isArray(circuit.parts)?circuit.parts.length:0, connections=has&&Array.isArray(circuit.connections)?circuit.connections.length:0;
      $('workbench-title').textContent=title; $('workbench-title').title=title; $('workbench-project-title').textContent=has?title:'当前工程';
      $('workbench-project-summary').textContent=has?parts+' 个元件 · '+connections+' 条连线':'输入需求，开始设计电路';
      $('workbench-document-name').textContent=has?title:'原理图'; $('workbench-canvas-title').textContent=has?title:'原理图工作区';
      $('workbench-canvas-subtitle').textContent=has?parts+' 个元件 · '+connections+' 条连线 · 可直接编辑':'需求生成 · 本地编辑 · 校验与导出';
      $('workbench-status-counts').textContent=parts+' 个元件 · '+connections+' 条连线';
      document.querySelector('.wb-status-format').textContent=has&&circuit.version!=null?'CircuitJSON v'+String(circuit.version):'CircuitJSON';
      $('workbench-save-state').textContent=has?(api.isDirty()?'有未保存的修改':'当前电路就绪'):'尚未创建电路';
      const state=api.getValidationState(); $('workbench-validate').disabled=$('workbench-validate-inspector').disabled=!has||state.busy;
      $('workbench-checks-summary').textContent=state.busy?'校验进行中':state.report?(state.report.accepted?'适用门禁通过':'仍有待复核项'):(has?'查看电气检查与生成过程':'尚未运行检查');
      partRows.forEach(row => { row.button.disabled=!api.getController(); }); renderValidation(state,has); refreshSelection(); updateTeachPath(state,has?circuit:null);
      if ($('evidencePanel').classList.contains('on')) showEvidence();
    }
    new MutationObserver(refreshSelection).observe($('props'),{childList:true,attributes:true,attributeFilter:['style']});
    new MutationObserver(() => { if ($('ercPanel').classList.contains('on')) selectInspector('validation',true); }).observe($('ercPanel'),{attributes:true,attributeFilter:['class']});
    new MutationObserver(() => { if ($('progressWrap').classList.contains('on')&&!$('progressWrap').classList.contains('done')) setChecks(true); }).observe($('progressWrap'),{attributes:true,attributeFilter:['class']});
    new MutationObserver(() => { if (/失败|拒绝|缺少|未完成|出错/.test($('meta').textContent)) setChecks(true); }).observe($('meta'),{childList:true,subtree:true,characterData:true});
    $('svg-host').addEventListener('click',() => { selectInspector('properties',false); refreshSelection(); });
    $('form').addEventListener('submit',() => { selectTab('requirements',false); closeDrawers(); setChecks(true); });
    $('q').addEventListener('keydown',event => { if (event.key==='Enter'&&!event.shiftKey&&!event.isComposing) { event.preventDefault(); $('form').requestSubmit(); } });
    selectTab('requirements',false); selectInspector('properties',false); responsive(); refresh();
    return {refresh,showEvidence};
  }
  root.CircuitWorkbench={mount};
})(window);
