/* ============================================================
 * symbols.js —— 智绘电路元件 SVG 符号库（原生 JS，零依赖，数据驱动）
 * ------------------------------------------------------------
 * 设计要点（符号坐标遵循教科书标准画法，“端口用 side/cx/cy 比例坐标
 * 描述”的单一数据源做法；直接解决开发中“反相运放反馈节点和走线交汇点
 * 对不齐”的问题）：
 *  每个元件只声明一次：符号框 w/h、端口 ports（比例坐标 cx/cy）、符号内部图形 body。
 *  - 渲染：renderSymbol() 按实例位置/旋转生成 <g>；
 *  - 接线：pinWorld() 用【同一份 ports】算出引脚世界坐标给走线模块，
 *    所以“线接在哪里”和“符号画在哪里”永远一致，不会再出现坐标错位。
 * ============================================================ */

(function (root, factory) {
  if (typeof module !== 'undefined' && module.exports) module.exports = factory();
  else root.CircuitSymbols = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // 端口比例坐标：cx/cy ∈ [0,1]，表示引脚在符号框宽/高上的相对位置；side 仅作朝向提示
  function P(id, side, cx, cy) { return { id, side, cx, cy }; }

  // 每个元件：w/h 符号框，ports 端口，ref 位号前缀，body() 返回框内 SVG（原点在框左上角）
  //
  // 符号风格现状（本轮不改以免改动渲染输出）：
  // 项目当前混用两套图形惯例——电阻/光敏电阻/热敏电阻走 ANSI 锯齿折线
  // （我国家庭与北美教材常见），电容/电感/扬声器/电池等走 IEC 矩形与圆弧画法
  // （GB/T 4728 与欧洲惯例）。教学演示中两种画法都有人认得，割裂的代价是
  // 「同一张图上风格不统一」；要真正统一需要给 LIB 加一层 style 变体并在渲染时
  // 按用户偏好切换，属于渲染行为变更（会改变几何基线），留作后续单独立项。
  const LIB = {
    resistor: {
      w: 120, h: 36, ref: 'R',
      ports: [P('1', 'left', 0, .5), P('2', 'right', 1, .5)],
      body: () => `<polyline points="0,18 36,18 40,9 48,27 56,9 64,27 72,9 80,27 84,18 120,18"/>`,
    },
    capacitor: {
      w: 84, h: 40, ref: 'C',
      ports: [P('+', 'left', 0, .5), P('-', 'right', 1, .5)],
      body: () =>
        `<line x1="0" y1="20" x2="38" y2="20"/>` +
        `<line x1="38" y1="8" x2="38" y2="32"/>` +
        `<line x1="46" y1="8" x2="46" y2="32"/>` +
        `<line x1="46" y1="20" x2="84" y2="20"/>`,
    },
    inductor: {
      w: 120, h: 36, ref: 'L',
      ports: [P('1', 'left', 0, .5), P('2', 'right', 1, .5)],
      body: () =>
        `<line x1="0" y1="18" x2="30" y2="18"/>` +
        `<path d="M30 18 a7.5 7.5 0 0 1 15 0 a7.5 7.5 0 0 1 15 0 a7.5 7.5 0 0 1 15 0 a7.5 7.5 0 0 1 15 0" fill="none"/>` +
        `<line x1="90" y1="18" x2="120" y2="18"/>`,
    },
    potentiometer: {
      w: 128, h: 48, ref: 'RV',
      ports: [P('1', 'left', 0, .5), P('2', 'right', 1, .5), P('3', 'top', .5, 0)],
      body: () =>
        `<polyline points="0,24 40,24 44,15 52,33 60,15 68,33 76,15 84,33 88,24 128,24"/>` +
        `<line x1="64" y1="0" x2="64" y2="14"/>` +
        `<path d="M58 14 L70 14 L64 24 Z" fill="currentColor" stroke="none"/>`,
    },
    fuse: {
      w: 96, h: 36, ref: 'F',
      ports: [P('1', 'left', 0, .5), P('2', 'right', 1, .5)],
      body: () =>
        `<line x1="0" y1="18" x2="26" y2="18"/>` +
        `<rect x="26" y="9" width="44" height="18" rx="3" fill="none"/>` +
        `<line x1="70" y1="18" x2="96" y2="18"/>`,
    },
    crystal: {
      w: 88, h: 40, ref: 'Y',
      ports: [P('1', 'left', 0, .5), P('2', 'right', 1, .5)],
      body: () =>
        `<line x1="0" y1="20" x2="30" y2="20"/><line x1="30" y1="8" x2="30" y2="32"/>` +
        `<rect x="38" y="10" width="12" height="20" fill="none"/>` +
        `<line x1="58" y1="8" x2="58" y2="32"/><line x1="58" y1="20" x2="88" y2="20"/>`,
    },
    diode: {
      w: 96, h: 36, ref: 'D',
      ports: [P('A', 'left', 0, .5), P('K', 'right', 1, .5), P('+', 'left', 0, .5), P('-', 'right', 1, .5)],
      body: () =>
        `<line x1="0" y1="18" x2="36" y2="18"/>` +
        `<path d="M36 8 L36 28 L58 18 Z" fill="none"/>` +
        `<line x1="58" y1="8" x2="58" y2="28"/>` +
        `<line x1="58" y1="18" x2="96" y2="18"/>`,
    },
    zener: {
      w: 96, h: 36, ref: 'D',
      ports: [P('A', 'left', 0, .5), P('K', 'right', 1, .5), P('+', 'left', 0, .5), P('-', 'right', 1, .5)],
      body: () =>
        `<line x1="0" y1="18" x2="36" y2="18"/>` +
        `<path d="M36 8 L36 28 L58 18 Z" fill="none"/>` +
        `<path d="M52 8 L58 8 L58 28 L64 28" fill="none"/>` +
        `<line x1="58" y1="18" x2="96" y2="18"/>`,
    },
    led: {
      w: 96, h: 48, ref: 'LED',
      ports: [P('+', 'left', 0, .5), P('-', 'right', 1, .5)],
      body: () =>
        `<line x1="0" y1="24" x2="34" y2="24"/>` +
        `<path d="M34 12 L34 36 L56 24 Z" fill="none"/>` +
        `<line x1="56" y1="12" x2="56" y2="36"/>` +
        `<line x1="56" y1="24" x2="96" y2="24"/>` +
        // 向外的两个发光箭头
        `<line x1="52" y1="14" x2="64" y2="2"/><polyline points="58,2 64,2 64,8" fill="none"/>` +
        `<line x1="60" y1="18" x2="72" y2="6"/><polyline points="66,6 72,6 72,12" fill="none"/>`,
    },
    // 三极管（viewBox 96x96）：base 左、collector 上、emitter 下
    npn: bjt(true), pnp: bjt(false),
    // N 沟道 MOSFET（简化规范画法，竖向，G 左 / D 上 / S 下）
    nmos: {
      w: 64, h: 96, ref: 'Q',
      ports: [P('G', 'left', 0, .5), P('D', 'top', .5, 0), P('S', 'bottom', .5, 1)],
      body: () =>
        `<line x1="0" y1="48" x2="22" y2="48"/>` +      // G 引线
        `<line x1="28" y1="28" x2="28" y2="68"/>` +      // 栅极竖线
        `<line x1="40" y1="28" x2="40" y2="44"/>` +      // D 沟道上半
        `<line x1="40" y1="52" x2="40" y2="68"/>` +      // S 沟道下半
        `<line x1="40" y1="28" x2="40" y2="0"/>` +       // D 引线向上
        `<line x1="40" y1="68" x2="40" y2="96"/>` +      // S 引线向下
        `<line x1="40" y1="68" x2="56" y2="60"/>`,       // 源极箭头（向里=N沟道）
    },
    switch: {
      w: 96, h: 36, ref: 'SW',
      ports: [P('1', 'left', 0, .5), P('2', 'right', 1, .5)],
      body: () =>
        `<line x1="0" y1="18" x2="28" y2="18"/><circle cx="28" cy="18" r="3" fill="currentColor" stroke="none"/>` +
        `<line x1="28" y1="18" x2="62" y2="6"/>` +
        `<circle cx="68" cy="18" r="3" fill="currentColor" stroke="none"/><line x1="68" y1="18" x2="96" y2="18"/>`,
    },
    pushbutton: {
      w: 96, h: 48, ref: 'SW',
      ports: [P('1', 'left', 0, 32 / 48), P('2', 'right', 1, 32 / 48)],
      body: () =>
        `<line x1="0" y1="32" x2="30" y2="32"/><line x1="66" y1="32" x2="96" y2="32"/>` +
        `<line x1="30" y1="32" x2="30" y2="26"/><line x1="66" y1="32" x2="66" y2="26"/>` +
        `<line x1="26" y1="26" x2="70" y2="26"/><line x1="48" y1="26" x2="48" y2="12"/>` +
        `<line x1="38" y1="12" x2="58" y2="12"/>`,
    },
    battery: {
      w: 96, h: 36, ref: 'BAT',
      ports: [P('+', 'left', 0, .5), P('-', 'right', 1, .5)],
      body: () =>
        `<line x1="0" y1="18" x2="34" y2="18"/>` +
        `<line x1="34" y1="6" x2="34" y2="30"/><line x1="42" y1="11" x2="42" y2="25"/>` +
        `<line x1="50" y1="6" x2="50" y2="30"/><line x1="58" y1="11" x2="58" y2="25"/>` +
        `<line x1="58" y1="18" x2="96" y2="18"/>`,
    },
    vcc: { // 电源（箭头朝上的 VCC 标记，单端在底部；位号放侧旁避开中央引线）
      w: 48, h: 40, ref: 'VCC', refAt: 'right',
      ports: [P('1', 'bottom', .5, 1)],
      body: () =>
        `<line x1="24" y1="40" x2="24" y2="20"/>` +
        `<path d="M12 20 L36 20 L24 4 Z" fill="none"/>`,
    },
    gnd: { // 地，单端在顶部；位号放侧旁避开中央引线
      w: 48, h: 48, ref: 'GND', refAt: 'right',
      ports: [P('1', 'top', .5, 0)],
      body: () =>
        `<line x1="24" y1="0" x2="24" y2="20"/>` +
        `<line x1="8" y1="20" x2="40" y2="20"/>` +
        `<line x1="14" y1="30" x2="34" y2="30"/>` +
        `<line x1="20" y1="40" x2="28" y2="40"/>`,
    },
    // 运放：三角符号，同相(+)/反相(-)输入在左，输出在右
    opamp: {
      w: 64, h: 48, ref: 'U',
      ports: [P('+', 'left', 0, .75), P('-', 'left', 0, .25), P('out', 'right', 1, .5)],
      body: () =>
        `<path d="M12,4 V44 L52,24 Z" fill="none"/>` +
        `<line x1="0" y1="36" x2="12" y2="36"/><line x1="0" y1="12" x2="12" y2="12"/>` +
        `<line x1="52" y1="24" x2="64" y2="24"/>` +
        // + / - 标记
        `<text x="16" y="39" font-size="10" fill="currentColor" stroke="none">+</text>` +
        `<text x="16" y="16" font-size="12" fill="currentColor" stroke="none">−</text>`,
    },
    // 蜂鸣器：半圆 + 声波
    buzzer: {
      w: 104, h: 48, ref: 'BZ',
      ports: [P('+', 'left', 0, .5), P('-', 'right', 1, .5)],
      body: () =>
        `<line x1="0" y1="24" x2="28" y2="24"/>` +
        `<path d="M28 8 A16 16 0 0 1 28 40 L28 8 Z" fill="none"/>` +
        `<line x1="28" y1="24" x2="104" y2="24"/>` +
        `<path d="M46 16 A12 12 0 0 1 46 32" fill="none"/>` +
        `<path d="M56 10 A20 20 0 0 1 56 38" fill="none"/>`,
    },
    // 扬声器（IEC 梯形喇叭口 + 声波弧；声波弧避开 y=24 引线通路，上下各一）
    speaker: {
      w: 112, h: 48, ref: 'LS',
      ports: [P('+', 'left', 0, .5), P('-', 'right', 1, .5)],
      body: () =>
        `<line x1="0" y1="24" x2="24" y2="24"/>` +
        `<path d="M24 6 L50 6 L74 24 L50 42 L24 42 Z" fill="none"/>` +
        `<line x1="74" y1="24" x2="112" y2="24"/>` +
        `<path d="M84 4 A14 14 0 0 1 84 18" fill="none"/>` +
        `<path d="M84 30 A14 14 0 0 1 84 44" fill="none"/>`,
    },
    // ---- 2026-09-09 常用元件扩展批（与 CATALOG/PART_INFO/KiCad 端同步） ----
    // 肖特基二极管：二极管图形 + 阴极杆两端 S 形钩（上钩向右下、下钩向左上）
    schottky: {
      w: 96, h: 36, ref: 'D',
      ports: [P('A', 'left', 0, .5), P('K', 'right', 1, .5), P('+', 'left', 0, .5), P('-', 'right', 1, .5)],
      body: () =>
        `<line x1="0" y1="18" x2="36" y2="18"/>` +
        `<path d="M36 8 L36 28 L58 18 Z" fill="none"/>` +
        `<line x1="58" y1="8" x2="58" y2="28"/>` +
        `<polyline points="64,12 64,8 58,8" fill="none"/>` +
        `<polyline points="52,24 52,28 58,28" fill="none"/>` +
        `<line x1="58" y1="18" x2="96" y2="18"/>`,
    },
    // 光电二极管：二极管图形 + 两个指向管体的箭头（光照进来，与 LED 相反）
    photodiode: {
      w: 96, h: 48, ref: 'D',
      ports: [P('A', 'left', 0, .5), P('K', 'right', 1, .5), P('+', 'left', 0, .5), P('-', 'right', 1, .5)],
      body: () =>
        `<line x1="0" y1="24" x2="34" y2="24"/>` +
        `<path d="M34 12 L34 36 L56 24 Z" fill="none"/>` +
        `<line x1="56" y1="12" x2="56" y2="36"/>` +
        `<line x1="56" y1="24" x2="96" y2="24"/>` +
        `<line x1="64" y1="2" x2="52" y2="14"/><polyline points="52,8 52,14 58,14" fill="none"/>` +
        `<line x1="72" y1="6" x2="60" y2="18"/><polyline points="60,12 60,18 66,18" fill="none"/>`,
    },
    // 光敏电阻（LDR）：电阻折线 + 两个射入箭头
    photocell: {
      w: 120, h: 48, ref: 'RG',
      ports: [P('1', 'left', 0, .5), P('2', 'right', 1, .5)],
      body: () =>
        `<polyline points="0,24 36,24 40,15 48,33 56,15 64,33 72,15 80,33 84,24 120,24"/>` +
        `<line x1="86" y1="4" x2="76" y2="14"/><polyline points="76,9 76,14 81,14" fill="none"/>` +
        `<line x1="96" y1="8" x2="86" y2="18"/><polyline points="86,13 86,18 91,18" fill="none"/>`,
    },
    // 热敏电阻：电阻折线 + IEC 斜杠（下端带短横脚）
    thermistor: {
      w: 120, h: 48, ref: 'TH',
      ports: [P('1', 'left', 0, .5), P('2', 'right', 1, .5)],
      body: () =>
        `<polyline points="0,24 36,24 40,15 48,33 56,15 64,33 72,15 80,33 84,24 120,24"/>` +
        `<line x1="34" y1="42" x2="88" y2="10"/>` +
        `<line x1="34" y1="42" x2="44" y2="42"/>`,
    },
    // P 沟道 MOS：几何与 nmos 相同，源极箭头朝外（=P沟道），与 nmos 互成镜像
    pmos: {
      w: 64, h: 96, ref: 'Q',
      ports: [P('G', 'left', 0, .5), P('D', 'top', .5, 0), P('S', 'bottom', .5, 1)],
      body: () =>
        `<line x1="0" y1="48" x2="22" y2="48"/>` +
        `<line x1="28" y1="28" x2="28" y2="68"/>` +
        `<line x1="40" y1="28" x2="40" y2="44"/>` +
        `<line x1="40" y1="52" x2="40" y2="68"/>` +
        `<line x1="40" y1="28" x2="40" y2="0"/>` +
        `<line x1="40" y1="68" x2="40" y2="96"/>` +
        `<line x1="40" y1="68" x2="54" y2="61"/>` +
        `<polyline points="47,63 54,61 51,67" fill="none"/>`,
    },
    // 交流电源：圆圈 + 正弦波（横放，+ 左 - 右）
    ac_source: {
      w: 96, h: 48, ref: 'VAC',
      ports: [P('+', 'left', 0, .5), P('-', 'right', 1, .5)],
      body: () =>
        `<line x1="0" y1="24" x2="28" y2="24"/>` +
        `<circle cx="48" cy="24" r="20" fill="none"/>` +
        `<path d="M34 24 q3.5 -10 7 0 q3.5 10 7 0 q3.5 -10 7 0 q3.5 10 7 0" fill="none"/>` +
        `<line x1="68" y1="24" x2="96" y2="24"/>`,
    },
    // 直流电机：圆圈 + M
    dc_motor: {
      w: 96, h: 48, ref: 'M',
      ports: [P('+', 'left', 0, .5), P('-', 'right', 1, .5)],
      body: () =>
        `<line x1="0" y1="24" x2="28" y2="24"/>` +
        `<circle cx="48" cy="24" r="20" fill="none"/>` +
        `<text x="48" y="29" font-size="14" text-anchor="middle" fill="currentColor" stroke="none">M</text>` +
        `<line x1="68" y1="24" x2="96" y2="24"/>`,
    },
    // 继电器（常开触点）：左侧线圈框 + 虚线耦合 + 右侧开关（com 上、no 下）
    relay: {
      w: 128, h: 72, ref: 'K',
      ports: [P('coil+', 'left', 0, .25), P('coil-', 'left', 0, .75),
              P('com', 'right', 1, .25), P('no', 'right', 1, .75)],
      body: () =>
        `<line x1="0" y1="18" x2="24" y2="18"/>` +
        `<line x1="0" y1="54" x2="24" y2="54"/>` +
        `<rect x="24" y="8" width="20" height="56" fill="none"/>` +
        `<line x1="54" y1="8" x2="54" y2="64" stroke-dasharray="4 3"/>` +
        `<line x1="104" y1="18" x2="128" y2="18"/>` +
        `<circle cx="104" cy="18" r="3" fill="currentColor" stroke="none"/>` +
        `<line x1="104" y1="18" x2="78" y2="46"/>` +
        `<circle cx="78" cy="54" r="3" fill="currentColor" stroke="none"/>` +
        `<line x1="78" y1="54" x2="128" y2="54"/>`,
    },
    // 整流桥：菱形框 + ~/~ 交流侧（左右）、+/- 直流侧（上下）
    bridge_rectifier: {
      w: 96, h: 96, ref: 'BR',
      ports: [P('+', 'top', .5, 0), P('-', 'bottom', .5, 1),
              P('AC1', 'left', 0, .5), P('AC2', 'right', 1, .5)],
      body: () =>
        `<path d="M48 4 L92 48 L48 92 L4 48 Z" fill="none"/>` +
        `<text x="48" y="24" font-size="13" text-anchor="middle" fill="currentColor" stroke="none">+</text>` +
        `<text x="48" y="82" font-size="13" text-anchor="middle" fill="currentColor" stroke="none">−</text>` +
        `<text x="13" y="53" font-size="13" text-anchor="middle" fill="currentColor" stroke="none">~</text>` +
        `<text x="77" y="53" font-size="13" text-anchor="middle" fill="currentColor" stroke="none">~</text>`,
    },
    // 接线端子：两个带斜杠的接线孔
    screw_terminal: {
      w: 96, h: 36, ref: 'J',
      ports: [P('1', 'left', 0, .5), P('2', 'right', 1, .5)],
      body: () =>
        `<line x1="0" y1="18" x2="23" y2="18"/>` +
        `<circle cx="30" cy="18" r="7" fill="none"/>` +
        `<line x1="25.5" y1="13.5" x2="34.5" y2="22.5"/>` +
        `<line x1="37" y1="18" x2="59" y2="18"/>` +
        `<circle cx="66" cy="18" r="7" fill="none"/>` +
        `<line x1="61.5" y1="13.5" x2="70.5" y2="22.5"/>` +
        `<line x1="73" y1="18" x2="96" y2="18"/>`,
    },
  };

  // ---------- IC 盒符号生成器（按四边引脚表自动生成，
  // 端口比例坐标与引脚图形同源，保证引线永远接在引脚上） ----------
  // 教学增强（可选）：opts.showPinNumbers + opts.pinNumbers{引脚名:脚号}
  // 在框外引脚根部画灰色小号 DIP 脚号角标（9px/#999），方便学生对照
  // 实物封装接线；【只加视觉元素，绝不改动 ports —— 引脚名/坐标仍是单一数据源】。
  function icBox(chipLabel, sides, opts) {
    opts = opts || {};
    const pitch = opts.pitch || 48, pad = opts.pad || 28, lead = opts.lead || 16;
    const L = sides.left || [], R = sides.right || [], T = sides.top || [], B = sides.bottom || [];
    const h = Math.max(L.length, R.length, 1) * pitch + pad * 2;
    const w = Math.max(Math.max(T.length, B.length, 1) * pitch + pad * 2, opts.minW || 140);

    const ports = [];
    const body = [];
    // 盒体
    body.push(`<rect x="${lead}" y="${lead}" width="${w - 2 * lead}" height="${h - 2 * lead}" fill="none"/>`);
    // 脚号角标文本（stroke:none 覆盖外层 g.symbol-body 的描边，避免小字糊掉）
    const numText = (x, y, anchor, n) =>
      `<text x="${x}" y="${y}" text-anchor="${anchor}" font-size="9" fill="#999" stroke="none" class="pin-num">${n}</text>`;
    const pinNo = name => (opts.showPinNumbers && opts.pinNumbers) ? opts.pinNumbers[name] : null;
    // 左/右引脚：引线 + 盒内引脚名 +（可选）框外脚号角标
    L.forEach((name, i) => {
      const y = pad + i * pitch + pitch / 2;
      ports.push(P(name, 'left', 0, y / h));
      body.push(`<line x1="0" y1="${y}" x2="${lead}" y2="${y}"/>`);
      body.push(`<text x="${lead + 7}" y="${y + 4}" font-size="11" fill="currentColor" stroke="none">${name}</text>`);
      const n = pinNo(name); if (n != null) body.push(numText(lead - 2, y + 3, 'end', n));
    });
    R.forEach((name, i) => {
      const y = pad + i * pitch + pitch / 2;
      ports.push(P(name, 'right', 1, y / h));
      body.push(`<line x1="${w - lead}" y1="${y}" x2="${w}" y2="${y}"/>`);
      body.push(`<text x="${w - lead - 7}" y="${y + 4}" font-size="11" text-anchor="end" fill="currentColor" stroke="none">${name}</text>`);
      const n = pinNo(name); if (n != null) body.push(numText(w - lead + 2, y + 3, 'start', n));
    });
    T.forEach((name, i) => {
      const x = pad + i * pitch + pitch / 2;
      ports.push(P(name, 'top', x / w, 0));
      body.push(`<line x1="${x}" y1="0" x2="${x}" y2="${lead}"/>`);
      body.push(`<text x="${x}" y="${lead + 14}" font-size="11" text-anchor="middle" fill="currentColor" stroke="none">${name}</text>`);
      const n = pinNo(name); if (n != null) body.push(numText(x, lead - 2, 'middle', n));
    });
    B.forEach((name, i) => {
      const x = pad + i * pitch + pitch / 2;
      ports.push(P(name, 'bottom', x / w, 1));
      body.push(`<line x1="${x}" y1="${h - lead}" x2="${x}" y2="${h}"/>`);
      body.push(`<text x="${x}" y="${h - lead - 6}" font-size="11" text-anchor="middle" fill="currentColor" stroke="none">${name}</text>`);
      const n = pinNo(name); if (n != null) body.push(numText(x, h - lead + 11, 'middle', n));
    });
    // 芯片名居中
    body.push(`<text x="${w / 2}" y="${h / 2 + 5}" font-size="14" font-weight="bold" text-anchor="middle" fill="currentColor" stroke="none">${chipLabel}</text>`);
    return { w, h, ref: 'U', ports, body: () => body.join('') };
  }

  // NE555：DIP-8 教科书摆法（左 TRIG/THR/DISCH，右 CTRL/OUT，上 RESET/VCC，下 GND）
  // pinNumbers 对照实物 DIP-8 脚号：1=GND 2=TRIG 3=OUT 4=RESET 5=CTRL 6=THR 7=DISCH 8=VCC
  LIB.ne555 = icBox('555', {
    left: ['TRIG', 'THR', 'DISCH'], right: ['CTRL', 'OUT'], top: ['RESET', 'VCC'], bottom: ['GND'],
  }, {
    showPinNumbers: true,
    pinNumbers: { GND: 1, TRIG: 2, OUT: 3, RESET: 4, CTRL: 5, THR: 6, DISCH: 7, VCC: 8 },
  });
  // 7805 三端稳压：左 IN、右 OUT、下 GND
  LIB.regulator_7805 = icBox('7805', { left: ['IN'], right: ['OUT'], bottom: ['GND'] }, { minW: 120 });

  // 2026-09-09 扩展批：舵机（左 SIG、上 VCC、下 GND，位号前缀改 M）
  // 与光耦（左 LED 侧 A/K、右三极管侧 C/E）——多引脚件统一走 icBox 生成器，
  // 端口坐标与引脚名自动同源，无需手算。
  LIB.servo = Object.assign(
    icBox('舵机', { left: ['SIG'], top: ['VCC'], bottom: ['GND'] }, { minW: 100 }),
    { ref: 'M' });
  LIB.optocoupler = icBox('光耦', { left: ['A', 'K'], right: ['C', 'E'] }, { minW: 120 });

  // 四种运放电路变体：几何与 opamp 完全一致（引脚名同为 +/−/out，pinWorld 通用），
  // 渲染时的名称标签由 renderSymbol(label) 传入，电路含义交给 ERC/模板层。
  ['opamp_inverting', 'opamp_noninverting', 'opamp_comparator', 'opamp_follower'].forEach(k => {
    LIB[k] = { w: LIB.opamp.w, h: LIB.opamp.h, ref: 'U', ports: LIB.opamp.ports, body: LIB.opamp.body };
  });

  function bjt(npn) {
    return {
      w: 96, h: 96, ref: 'Q',
      ports: [P('B', 'left', 0, .5), P('C', 'top', 64 / 96, 0), P('E', 'bottom', 64 / 96, 1)],
      body: () =>
        `<line x1="0" y1="48" x2="36" y2="48"/>` +       // base lead
        `<line x1="36" y1="30" x2="36" y2="66"/>` +      // base plate
        `<line x1="36" y1="42" x2="64" y2="20"/>` +      // collector diagonal
        `<line x1="64" y1="20" x2="64" y2="0"/>` +       // collector lead
        `<line x1="36" y1="54" x2="64" y2="76"/>` +      // emitter diagonal
        `<line x1="64" y1="76" x2="64" y2="96"/>` +      // emitter lead
        // 发射极箭头：NPN 箭头朝外(离开基极)，PNP 朝内
        (npn
          ? `<path d="M55.6 69.4 L46 67 L51 60.7 Z" fill="currentColor" stroke="none"/>`
          : `<path d="M43 59.5 L47.6 68.3 L52.6 62 Z" fill="currentColor" stroke="none"/>`),
    };
  }

  // 2026-09-11 工业级扩容：10 种教学高频件
  LIB.electrolytic = {
    w: 80, h: 48, ref: 'C',
    ports: [P('+', 'left', 0, .5), P('-', 'right', 1, .5)],
    body: () =>
      `<line x1="0" y1="24" x2="28" y2="24"/>` +
      `<line x1="28" y1="8" x2="28" y2="40"/>` +
      `<line x1="36" y1="12" x2="36" y2="36"/>` +
      `<line x1="36" y1="24" x2="80" y2="24"/>` +
      `<text x="20" y="12" font-size="12" fill="currentColor" stroke="none">+</text>`,
  };
  LIB.tvs = {
    w: 80, h: 36, ref: 'D',
    ports: [P('A', 'left', 0, .5), P('K', 'right', 1, .5), P('+', 'left', 0, .5), P('-', 'right', 1, .5)],
    body: () =>
      `<line x1="0" y1="18" x2="28" y2="18"/><line x1="56" y1="18" x2="80" y2="18"/>` +
      `<path d="M28,6 L28,30 L56,18 Z" fill="none"/>` +
      `<line x1="56" y1="6" x2="56" y2="30"/>` +
      `<line x1="56" y1="6" x2="64" y2="10"/><line x1="56" y1="30" x2="64" y2="26"/>`,
  };
  LIB.phototransistor = {
    w: 96, h: 96, ref: 'Q',
    ports: [P('C', 'top', 64 / 96, 0), P('E', 'bottom', 64 / 96, 1)],
    body: () =>
      `<line x1="36" y1="30" x2="36" y2="66"/>` +
      `<line x1="36" y1="42" x2="64" y2="20"/><line x1="64" y1="20" x2="64" y2="0"/>` +
      `<line x1="36" y1="54" x2="64" y2="76"/><line x1="64" y1="76" x2="64" y2="96"/>` +
      `<path d="M55.6 69.4 L46 67 L51 60.7 Z" fill="currentColor" stroke="none"/>` +
      `<line x1="8" y1="10" x2="28" y2="28"/><line x1="8" y1="22" x2="28" y2="40"/>` +
      `<path d="M24 22 L28 28 L20 30 Z" fill="currentColor" stroke="none"/>`,
  };
  LIB.ferrite_bead = {
    w: 100, h: 40, ref: 'FB',
    ports: [P('1', 'left', 0, .5), P('2', 'right', 1, .5)],
    body: () =>
      `<line x1="0" y1="20" x2="24" y2="20"/><line x1="76" y1="20" x2="100" y2="20"/>` +
      `<rect x="24" y="10" width="52" height="20" rx="4" fill="currentColor" fill-opacity=".15"/>` +
      `<text x="50" y="24" text-anchor="middle" font-size="11" fill="currentColor" stroke="none">FB</text>`,
  };
  LIB.varistor = {
    w: 100, h: 40, ref: 'RV',
    ports: [P('1', 'left', 0, .5), P('2', 'right', 1, .5)],
    body: () =>
      `<polyline points="0,20 20,20 24,11 32,29 40,11 48,29 56,11 64,29 68,20 100,20"/>` +
      `<line x1="18" y1="32" x2="82" y2="8"/>` +
      `<line x1="18" y1="32" x2="28" y2="32"/>`,
  };
  LIB.scr = {
    w: 88, h: 72, ref: 'VS',
    ports: [P('A', 'left', 0, .35), P('G', 'left', 0, .75), P('K', 'right', 1, .5)],
    body: () =>
      `<line x1="0" y1="25" x2="24" y2="25"/><line x1="64" y1="36" x2="88" y2="36"/>` +
      `<line x1="0" y1="54" x2="28" y2="48"/>` +
      `<path d="M24,18 L24,54 L64,36 Z" fill="none"/>` +
      `<line x1="64" y1="18" x2="64" y2="54"/>`,
  };
  LIB.buzzer_passive = {
    w: LIB.buzzer.w, h: LIB.buzzer.h, ref: 'BZ', ports: LIB.buzzer.ports,
    body: () =>
      LIB.buzzer.body() +
      `<text x="28" y="46" font-size="10" fill="currentColor" stroke="none">~</text>`,
  };
  LIB.ammeter = {
    w: 80, h: 80, ref: 'PA',
    ports: [P('+', 'left', 0, .5), P('-', 'right', 1, .5)],
    body: () =>
      `<line x1="0" y1="40" x2="16" y2="40"/><line x1="64" y1="40" x2="80" y2="40"/>` +
      `<circle cx="40" cy="40" r="24" fill="none"/>` +
      `<text x="40" y="46" text-anchor="middle" font-size="16" font-weight="700" fill="currentColor" stroke="none">A</text>`,
  };
  LIB.voltmeter = {
    w: 80, h: 80, ref: 'PV',
    ports: [P('+', 'left', 0, .5), P('-', 'right', 1, .5)],
    body: () =>
      `<line x1="0" y1="40" x2="16" y2="40"/><line x1="64" y1="40" x2="80" y2="40"/>` +
      `<circle cx="40" cy="40" r="24" fill="none"/>` +
      `<text x="40" y="46" text-anchor="middle" font-size="16" font-weight="700" fill="currentColor" stroke="none">V</text>`,
  };

  // 2026-09-11 精修：70+ 新件专用 SVG（教学标准画法）
  // 电容：平行极板；有极性带 + 号；薄膜用盒式；瓷片用弧形
  LIB.capacitor_ceramic = {
    w: 90, h: 48, ref: 'C',
    ports: [P('+', 'left', 0, .5), P('-', 'right', 1, .5)],
    body: () =>
      `<line x1="0" y1="24" x2="30" y2="24"/>` +
      `<line x1="30" y1="8" x2="30" y2="40"/>` +
      `<path d="M40,8 Q50,24 40,40" fill="none"/>` +
      `<line x1="60" y1="24" x2="90" y2="24"/>` +
      `<text x="45" y="46" text-anchor="middle" font-size="9" fill="currentColor" stroke="none">瓷片</text>`,
  };
  LIB.capacitor_film = {
    w: 100, h: 48, ref: 'C',
    ports: [P('+', 'left', 0, .5), P('-', 'right', 1, .5)],
    body: () =>
      `<line x1="0" y1="24" x2="22" y2="24"/>` +
      `<rect x="22" y="10" width="56" height="28" fill="none"/>` +
      `<line x1="40" y1="10" x2="40" y2="38"/><line x1="60" y1="10" x2="60" y2="38"/>` +
      `<line x1="78" y1="24" x2="100" y2="24"/>`,
  };
  LIB.capacitor_tantalum = {
    w: 90, h: 48, ref: 'C',
    ports: [P('+', 'left', 0, .5), P('-', 'right', 1, .5)],
    body: () =>
      `<line x1="0" y1="24" x2="28" y2="24"/>` +
      `<line x1="28" y1="8" x2="28" y2="40"/>` +
      `<rect x="36" y="14" width="22" height="20" fill="none"/>` +
      `<line x1="58" y1="24" x2="90" y2="24"/>` +
      `<text x="20" y="12" font-size="12" fill="currentColor" stroke="none">+</text>`,
  };
  LIB.resistor_network = {
    w: 140, h: 80, ref: 'RN',
    ports: [P('1', 'left', 0, .25), P('2', 'left', 0, .5), P('3', 'left', 0, .75), P('com', 'right', 1, .5)],
    body: () =>
      `<rect x="20" y="12" width="90" height="56" fill="none"/>` +
      `<polyline points="30,20 50,20 55,14 62,26 69,14 76,26 81,20 100,20"/>` +
      `<polyline points="30,40 50,40 55,34 62,46 69,34 76,46 81,40 100,40"/>` +
      `<polyline points="30,60 50,60 55,54 62,66 69,54 76,66 81,60 100,60"/>` +
      `<line x1="100" y1="20" x2="100" y2="60"/><line x1="100" y1="40" x2="120" y2="40"/>` +
      `<line x1="0" y1="20" x2="20" y2="20"/><line x1="0" y1="40" x2="20" y2="40"/><line x1="0" y1="60" x2="20" y2="60"/>`,
  };
  LIB.inductor_toroid = {
    w: 100, h: 60, ref: 'L',
    ports: [P('1', 'left', 0, .5), P('2', 'right', 1, .5)],
    body: () =>
      `<line x1="0" y1="30" x2="22" y2="30"/><line x1="78" y1="30" x2="100" y2="30"/>` +
      `<circle cx="50" cy="30" r="22" fill="none"/>` +
      `<circle cx="50" cy="30" r="12" fill="none"/>` +
      `<path d="M40,12 Q50,20 60,12" fill="none"/><path d="M70,22 Q78,30 70,40" fill="none"/>` +
      `<path d="M40,48 Q50,40 60,48" fill="none"/><path d="M30,40 Q22,30 30,22" fill="none"/>`,
  };
  LIB.transformer = {
    w: 140, h: 90, ref: 'T',
    ports: [P('p1', 'left', 0, .35), P('p2', 'left', 0, .65), P('s1', 'right', 1, .35), P('s2', 'right', 1, .65)],
    body: () =>
      `<line x1="0" y1="30" x2="20" y2="30"/><line x1="0" y1="60" x2="20" y2="60"/>` +
      `<path d="M20,20 Q30,30 20,40 Q30,50 20,60 Q30,70 20,80" fill="none"/>` +
      `<line x1="48" y1="15" x2="48" y2="75"/><line x1="54" y1="15" x2="54" y2="75"/>` +
      `<path d="M82,20 Q92,30 82,40 Q92,50 82,60 Q92,70 82,80" fill="none"/>` +
      `<line x1="120" y1="30" x2="140" y2="30"/><line x1="120" y1="60" x2="140" y2="60"/>`,
  };
  LIB.led_rgb = {
    w: 110, h: 80, ref: 'LED',
    ports: [P('R', 'left', 0, .3), P('G', 'left', 0, .5), P('B', 'left', 0, .7), P('com', 'right', 1, .5)],
    body: () =>
      `<line x1="0" y1="24" x2="20" y2="24"/><line x1="0" y1="40" x2="24" y2="40"/><line x1="0" y1="56" x2="20" y2="56"/>` +
      `<path d="M30,18 L30,62 L64,40 Z" fill="none"/>` +
      `<line x1="64" y1="18" x2="64" y2="62"/>` +
      `<line x1="64" y1="40" x2="110" y2="40"/>` +
      `<path d="M40,10 L52,2"/><path d="M48,14 L60,6"/>` +
      `<text x="55" y="76" text-anchor="middle" font-size="9" fill="currentColor" stroke="none">RGB</text>`,
  };
  LIB.diode_rectifier = {
    w: 80, h: 36, ref: 'D',
    ports: [P('A', 'left', 0, .5), P('K', 'right', 1, .5), P('+', 'left', 0, .5), P('-', 'right', 1, .5)],
    body: () =>
      `<line x1="0" y1="18" x2="26" y2="18"/><line x1="54" y1="18" x2="80" y2="18"/>` +
      `<path d="M26,6 L26,30 L54,18 Z" fill="none"/>` +
      `<line x1="54" y1="6" x2="54" y2="30"/>` +
      `<text x="40" y="34" text-anchor="middle" font-size="8" fill="currentColor" stroke="none">1N4007</text>`,
  };
  LIB.triac = {
    w: 90, h: 70, ref: 'VS',
    ports: [P('A1', 'left', 0, .4), P('G', 'left', 0, .8), P('A2', 'right', 1, .5)],
    body: () =>
      `<line x1="0" y1="28" x2="18" y2="28"/><line x1="72" y1="35" x2="90" y2="35"/>` +
      `<path d="M18,16 L18,40 L52,28 Z" fill="none"/><line x1="52" y1="16" x2="52" y2="40"/>` +
      `<path d="M52,48 L52,64 L18,52 Z" fill="none"/><line x1="18" y1="44" x2="18" y2="60"/>` +
      `<line x1="0" y1="56" x2="18" y2="52"/>`,
  };
  LIB.diac = {
    w: 80, h: 40, ref: 'DB',
    ports: [P('1', 'left', 0, .5), P('2', 'right', 1, .5)],
    body: () =>
      `<line x1="0" y1="20" x2="18" y2="20"/><line x1="62" y1="20" x2="80" y2="20"/>` +
      `<path d="M18,8 L18,32 L48,20 Z" fill="none"/><line x1="48" y1="8" x2="48" y2="32"/>` +
      `<path d="M48,28 L48,12 L18,24 Z" fill="none" opacity=".9"/>`,
  };
  LIB.fuse_resettable = {
    w: 100, h: 40, ref: 'F',
    ports: [P('1', 'left', 0, .5), P('2', 'right', 1, .5)],
    body: () =>
      `<line x1="0" y1="20" x2="20" y2="20"/><line x1="80" y1="20" x2="100" y2="20"/>` +
      `<rect x="20" y="10" width="60" height="20" rx="3" fill="none"/>` +
      `<text x="50" y="24" text-anchor="middle" font-size="9" fill="currentColor" stroke="none">PPTC</text>`,
  };
  LIB.igbt = {
    w: 100, h: 100, ref: 'Q',
    ports: [P('G', 'left', 0, .5), P('C', 'top', .6, 0), P('E', 'bottom', .6, 1)],
    body: () =>
      `<line x1="0" y1="50" x2="28" y2="50"/>` +
      `<line x1="36" y1="25" x2="36" y2="75"/>` +
      `<line x1="28" y1="40" x2="28" y2="60"/>` +
      `<line x1="36" y1="35" x2="64" y2="20"/><line x1="64" y1="20" x2="64" y2="0"/>` +
      `<line x1="36" y1="65" x2="64" y2="80"/><line x1="64" y1="80" x2="64" y2="100"/>` +
      `<path d="M55 73 L46 70 L51 64 Z" fill="currentColor" stroke="none"/>` +
      `<text x="50" y="96" font-size="8" fill="currentColor" stroke="none">IGBT</text>`,
  };
  LIB.ujt = {
    w: 90, h: 90, ref: 'Q',
    ports: [P('E', 'left', 0, .5), P('B1', 'bottom', .45, 1), P('B2', 'top', .45, 0)],
    body: () =>
      `<line x1="0" y1="45" x2="30" y2="45"/>` +
      `<line x1="36" y1="20" x2="36" y2="70"/>` +
      `<line x1="30" y1="35" x2="50" y2="20"/><line x1="50" y1="20" x2="50" y2="0"/>` +
      `<line x1="30" y1="55" x2="50" y2="70"/><line x1="50" y1="70" x2="50" y2="90"/>` +
      `<path d="M42 28 L34 26 L38 20 Z" fill="currentColor" stroke="none"/>`,
  };
  LIB.phototriac = {
    w: 110, h: 80, ref: 'U',
    ports: [P('A', 'left', 0, .35), P('K', 'left', 0, .7), P('A1', 'right', 1, .4), P('A2', 'right', 1, .75)],
    body: () =>
      `<line x1="0" y1="28" x2="16" y2="28"/><line x1="0" y1="56" x2="16" y2="56"/>` +
      `<path d="M16,18 L16,38 L42,28 Z" fill="none"/>` +
      `<line x1="42" y1="18" x2="42" y2="38"/>` +
      `<line x1="20" y1="8" x2="40" y2="24"/><line x1="28" y1="6" x2="48" y2="22"/>` +
      `<rect x="55" y="20" width="30" height="40" fill="none"/>` +
      `<line x1="95" y1="32" x2="110" y2="32"/><line x1="95" y1="60" x2="110" y2="60"/>` +
      `<path d="M70,30 L70,50 L90,40 Z" fill="none"/><line x1="90" y1="30" x2="90" y2="50"/>`,
  };
  // 逻辑门（ANSI 风格）
  function logicGate(w, h, shape, ref) {
    return {
      w, h, ref: ref || 'U',
      ports: [P('A', 'left', 0, .3), P('B', 'left', 0, .7), P('Y', 'right', 1, .5)],
      body: () => shape + `<line x1="0" y1="${h*0.3}" x2="${w*0.25}" y2="${h*0.3}"/>` +
        `<line x1="0" y1="${h*0.7}" x2="${w*0.25}" y2="${h*0.7}"/>` +
        `<line x1="${w*0.85}" y1="${h*0.5}" x2="${w}" y2="${h*0.5}"/>`,
    };
  }
  const AND_S = `<path d="M40,16 L40,64 L70,64 Q90,64 90,40 Q90,16 70,16 Z" fill="none"/>`;
  const OR_S = `<path d="M35,16 Q55,16 75,30 Q95,40 75,50 Q55,64 35,64 Q48,40 35,16 Z" fill="none"/>`;
  const INV_S = `<path d="M40,18 L40,62 L78,40 Z" fill="none"/><circle cx="84" cy="40" r="6" fill="none"/>`;
  LIB.ic_and = logicGate(110, 80, AND_S);
  LIB.ic_or = logicGate(110, 80, OR_S);
  LIB.ic_nand = logicGate(110, 80, AND_S + `<circle cx="96" cy="40" r="6" fill="none"/>`);
  LIB.ic_nor = logicGate(110, 80, OR_S + `<circle cx="90" cy="40" r="6" fill="none"/>`);
  LIB.ic_xor = logicGate(110, 80,
    `<path d="M28,16 Q48,16 68,30 Q88,40 68,50 Q48,64 28,64 Q41,40 28,16 Z" fill="none"/>` +
    `<path d="M40,16 Q60,16 80,30 Q100,40 80,50 Q60,64 40,64" fill="none"/>` +
    `<line x1="0" y1="24" x2="28" y2="24"/><line x1="0" y1="56" x2="28" y2="56"/>` +
    `<line x1="88" y1="40" x2="110" y2="40"/>`);
  LIB.ic_not = {
    w: 100, h: 70, ref: 'U',
    ports: [P('A', 'left', 0, .5), P('Y', 'right', 1, .5)],
    body: () => INV_S + `<line x1="0" y1="35" x2="40" y2="35"/><line x1="90" y1="35" x2="100" y2="35"/>`,
  };
  LIB.ic_buffer = {
    w: 100, h: 70, ref: 'U',
    ports: [P('A', 'left', 0, .5), P('Y', 'right', 1, .5)],
    body: () =>
      `<path d="M40,18 L40,62 L78,40 Z" fill="none"/>` +
      `<line x1="0" y1="35" x2="40" y2="35"/><line x1="78" y1="35" x2="100" y2="35"/>`,
  };
  // 数码管
  LIB.sevenseg = {
    w: 80, h: 100, ref: 'DS',
    ports: [P('com', 'right', 1, .5), P('a', 'top', .5, 0), P('b', 'right', 1, .2), P('c', 'right', 1, .7),
            P('d', 'bottom', .5, 1), P('e', 'left', 0, .7), P('f', 'left', 0, .2), P('g', 'left', 0, .5)],
    body: () =>
      `<rect x="16" y="12" width="48" height="76" rx="4" fill="none"/>` +
      `<line x1="24" y1="20" x2="56" y2="20" stroke-width="3"/>` + // a
      `<line x1="58" y1="24" x2="58" y2="46" stroke-width="3"/>` + // b
      `<line x1="58" y1="54" x2="58" y2="76" stroke-width="3"/>` + // c
      `<line x1="24" y1="80" x2="56" y2="80" stroke-width="3"/>` + // d
      `<line x1="22" y1="54" x2="22" y2="76" stroke-width="3"/>` + // e
      `<line x1="22" y1="24" x2="22" y2="46" stroke-width="3"/>` + // f
      `<line x1="24" y1="50" x2="56" y2="50" stroke-width="3"/>`, // g
  };
  LIB.lcd1602 = {
    w: 180, h: 90, ref: 'LCD',
    ports: [P('VSS', 'left', 0, .15), P('VDD', 'left', 0, .3), P('VO', 'left', 0, .45),
            P('RS', 'left', 0, .6), P('RW', 'left', 0, .75), P('E', 'left', 0, .9),
            P('D4', 'bottom', .3, 1), P('D5', 'bottom', .45, 1), P('D6', 'bottom', .6, 1), P('D7', 'bottom', .75, 1),
            P('A', 'top', .2, 0), P('K', 'top', .8, 0)],
    body: () =>
      `<rect x="24" y="16" width="140" height="58" fill="none"/>` +
      `<rect x="32" y="24" width="124" height="42" fill="currentColor" fill-opacity=".06"/>` +
      `<text x="94" y="48" text-anchor="middle" font-size="11" fill="currentColor" stroke="none">LCD1602</text>`,
  };
  LIB.oled_ssd1306 = {
    w: 140, h: 90, ref: 'OLED',
    ports: [P('VCC', 'left', 0, .3), P('GND', 'left', 0, .7), P('SCL', 'top', .35, 0), P('SDA', 'top', .65, 0)],
    body: () =>
      `<rect x="24" y="20" width="100" height="55" rx="3" fill="none"/>` +
      `<rect x="32" y="28" width="84" height="39" fill="currentColor" fill-opacity=".12"/>` +
      `<text x="74" y="52" text-anchor="middle" font-size="10" fill="currentColor" stroke="none">OLED</text>`,
  };
  LIB.stepper = {
    w: 120, h: 90, ref: 'M',
    ports: [P('A+', 'left', 0, .25), P('A-', 'left', 0, .45), P('B+', 'right', 1, .25), P('B-', 'right', 1, .45)],
    body: () =>
      `<circle cx="60" cy="45" r="28" fill="none"/>` +
      `<text x="60" y="50" text-anchor="middle" font-size="11" fill="currentColor" stroke="none">M</text>` +
      `<line x1="0" y1="22" x2="32" y2="22"/><line x1="0" y1="40" x2="32" y2="40"/>` +
      `<line x1="88" y1="22" x2="120" y2="22"/><line x1="88" y1="40" x2="120" y2="40"/>` +
      `<path d="M60,10 L66,20 L54,20 Z" fill="currentColor" stroke="none"/>`,
  };
  LIB.solenoid = {
    w: 110, h: 60, ref: 'Y',
    ports: [P('+', 'left', 0, .5), P('-', 'right', 1, .5)],
    body: () =>
      `<line x1="0" y1="30" x2="16" y2="30"/>` +
      `<path d="M16,18 Q28,30 16,42 Q28,54 16,42" fill="none"/>` +
      `<rect x="40" y="18" width="30" height="24" fill="none"/>` +
      `<line x1="70" y1="30" x2="90" y2="30"/><path d="M90,22 L100,30 L90,38" fill="none"/>` +
      `<line x1="95" y1="30" x2="110" y2="30"/>`,
  };
  LIB.heater = {
    w: 100, h: 50, ref: 'EH',
    ports: [P('+', 'left', 0, .5), P('-', 'right', 1, .5)],
    body: () =>
      `<line x1="0" y1="25" x2="20" y2="25"/><line x1="80" y1="25" x2="100" y2="25"/>` +
      `<polyline points="20,25 28,12 40,38 52,12 64,38 72,25 80,25" fill="none"/>` +
      `<path d="M35,8 Q38,2 41,8" fill="none"/><path d="M50,8 Q53,2 56,8" fill="none"/><path d="M65,8 Q68,2 71,8" fill="none"/>`,
  };
  LIB.lm317 = icBox('LM317', { left: ['IN'], right: ['OUT'], bottom: ['ADJ'] }, { minW: 120 });
  LIB.lm317.ref = 'U';
  LIB.buck_module = icBox('DC-DC↓', { left: ['IN+', 'IN-'], right: ['OUT+', 'OUT-'] }, { minW: 140 });
  LIB.boost_module = icBox('DC-DC↑', { left: ['IN+', 'IN-'], right: ['OUT+', 'OUT-'] }, { minW: 140 });
  LIB.lipo_battery = {
    w: 90, h: 70, ref: 'BT',
    ports: [P('+', 'top', .5, 0), P('-', 'bottom', .5, 1)],
    body: () =>
      `<rect x="25" y="20" width="40" height="40" rx="3" fill="none"/>` +
      `<rect x="38" y="12" width="14" height="10" fill="none"/>` +
      `<text x="45" y="45" text-anchor="middle" font-size="10" fill="currentColor" stroke="none">Li+</text>` +
      `<line x1="45" y1="0" x2="45" y2="20"/><line x1="45" y1="60" x2="45" y2="70"/>`,
  };
  LIB.solar_cell = {
    w: 110, h: 70, ref: 'BT',
    ports: [P('+', 'top', .35, 0), P('-', 'bottom', .35, 1)],
    body: () =>
      `<rect x="20" y="18" width="60" height="40" fill="none"/>` +
      `<line x1="20" y1="31" x2="80" y2="31"/><line x1="20" y1="45" x2="80" y2="45"/>` +
      `<line x1="40" y1="18" x2="40" y2="58"/><line x1="60" y1="18" x2="60" y2="58"/>` +
      `<circle cx="95" cy="20" r="8" fill="none"/><line x1="95" y1="8" x2="95" y2="4"/><line x1="95" y1="36" x2="95" y2="32"/>` +
      `<line x1="83" y1="20" x2="79" y2="20"/><line x1="111" y1="20" x2="107" y2="20"/>`,
  };
  LIB.usb_port = {
    w: 100, h: 70, ref: 'J',
    ports: [P('VBUS', 'left', 0, .2), P('D-', 'left', 0, .45), P('D+', 'left', 0, .7), P('GND', 'right', 1, .5), P('ID', 'top', .5, 0)],
    body: () =>
      `<path d="M30,15 L70,15 L78,35 L78,55 L22,55 L22,35 Z" fill="none"/>` +
      `<line x1="40" y1="28" x2="60" y2="28"/><line x1="40" y1="40" x2="60" y2="40"/>` +
      `<text x="50" y="68" text-anchor="middle" font-size="9" fill="currentColor" stroke="none">USB</text>`,
  };
  LIB.barrel_jack = {
    w: 100, h: 60, ref: 'J',
    ports: [P('tip', 'left', 0, .35), P('sleeve', 'left', 0, .75), P('switch', 'right', 1, .5)],
    body: () =>
      `<circle cx="50" cy="30" r="18" fill="none"/>` +
      `<circle cx="50" cy="30" r="8" fill="none"/>` +
      `<line x1="0" y1="21" x2="32" y2="21"/><line x1="0" y1="45" x2="32" y2="45"/>` +
      `<line x1="68" y1="30" x2="100" y2="30"/>`,
  };
  LIB.conn_header = {
    w: 140, h: 50, ref: 'J',
    ports: [P('1', 'bottom', .15, 1), P('2', 'bottom', .4, 1), P('3', 'bottom', .65, 1), P('4', 'bottom', .9, 1)],
    body: () =>
      `<rect x="15" y="12" width="110" height="22" fill="none"/>` +
      [0,1,2,3].map(i => `<rect x="${22+i*28}" y="16" width="16" height="14" fill="none"/>`).join('') +
      [0,1,2,3].map(i => `<line x1="${30+i*28}" y1="34" x2="${30+i*28}" y2="50"/>`).join(''),
  };
  LIB.switch_toggle = {
    w: 100, h: 50, ref: 'SW',
    ports: [P('1', 'left', 0, .5), P('2', 'top', .7, 0), P('3', 'right', 1, .5)],
    body: () =>
      `<line x1="0" y1="25" x2="30" y2="25"/><line x1="70" y1="25" x2="100" y2="25"/>` +
      `<circle cx="30" cy="25" r="3" fill="currentColor" stroke="none"/>` +
      `<circle cx="70" cy="25" r="3" fill="currentColor" stroke="none"/>` +
      `<line x1="30" y1="25" x2="62" y2="8"/>` +
      `<line x1="70" y1="0" x2="70" y2="12"/>`,
  };
  LIB.switch_dip = {
    w: 140, h: 55, ref: 'SW',
    ports: [P('1', 'bottom', .12, 1), P('2', 'bottom', .28, 1), P('3', 'bottom', .44, 1),
            P('4', 'bottom', .60, 1), P('5', 'bottom', .76, 1), P('6', 'bottom', .92, 1)],
    body: () =>
      `<rect x="12" y="10" width="116" height="28" fill="none"/>` +
      [0,1,2,3,4,5].map(i => `<rect x="${18+i*20}" y="14" width="12" height="12" fill="currentColor" fill-opacity=".2"/>`).join('') +
      [0,1,2,3,4,5].map(i => `<line x1="${24+i*20}" y1="38" x2="${24+i*20}" y2="55"/>`).join(''),
  };
  LIB.switch_reed = {
    w: 100, h: 40, ref: 'SW',
    ports: [P('1', 'left', 0, .5), P('2', 'right', 1, .5)],
    body: () =>
      `<line x1="0" y1="20" x2="30" y2="20"/><line x1="70" y1="20" x2="100" y2="20"/>` +
      `<line x1="30" y1="14" x2="30" y2="26"/><line x1="70" y1="14" x2="70" y2="26"/>` +
      `<line x1="32" y1="18" x2="62" y2="18"/>` +
      `<rect x="38" y="8" width="28" height="24" rx="10" fill="none" stroke-dasharray="3 2"/>`,
  };
  LIB.connector_screw2 = {
    w: 90, h: 50, ref: 'J',
    ports: [P('1', 'left', 0, .35), P('2', 'left', 0, .75)],
    body: () =>
      `<rect x="25" y="10" width="55" height="35" fill="none"/>` +
      `<circle cx="42" cy="22" r="6" fill="none"/><circle cx="65" cy="22" r="6" fill="none"/>` +
      `<line x1="0" y1="18" x2="36" y2="18"/><line x1="0" y1="38" x2="59" y2="38"/>`,
  };
  LIB.potentiometer_trim = {
    w: 100, h: 55, ref: 'RV',
    ports: [P('1', 'left', 0, .5), P('2', 'right', 1, .5), P('3', 'top', .5, 0)],
    body: () =>
      `<polyline points="0,28 20,28 26,18 34,38 42,18 50,38 58,18 66,38 72,28 100,28" fill="none"/>` +
      `<line x1="50" y1="0" x2="50" y2="14"/><path d="M44,14 L56,14 L50,22 Z" fill="currentColor" stroke="none"/>` +
      `<circle cx="50" cy="28" r="3" fill="currentColor" stroke="none"/>`,
  };
  LIB.varactor = {
    w: 90, h: 40, ref: 'D',
    ports: [P('A', 'left', 0, .5), P('K', 'right', 1, .5), P('+', 'left', 0, .5), P('-', 'right', 1, .5)],
    body: () =>
      `<line x1="0" y1="20" x2="28" y2="20"/><line x1="58" y1="20" x2="90" y2="20"/>` +
      `<path d="M28,8 L28,32 L54,20 Z" fill="none"/>` +
      `<line x1="54" y1="8" x2="54" y2="32"/>` +
      `<line x1="48" y1="6" x2="48" y2="34" stroke-dasharray="2 2"/>`,
  };
  LIB.optointerrupter = {
    w: 120, h: 70, ref: 'U',
    ports: [P('VCC', 'left', 0, .3), P('GND', 'left', 0, .7), P('OUT', 'right', 1, .5)],
    body: () =>
      `<rect x="10" y="15" width="30" height="40" fill="none"/>` +
      `<path d="M25,25 L25,45 L38,35 Z" fill="none"/>` +
      `<rect x="80" y="15" width="30" height="40" fill="none"/>` +
      `<line x1="70" y1="20" x2="80" y2="28"/><line x1="70" y1="35" x2="80" y2="35"/>` +
      `<line x1="70" y1="50" x2="80" y2="42"/>` +
      `<line x1="110" y1="35" x2="120" y2="35"/>` +
      `<text x="60" y="68" text-anchor="middle" font-size="9" fill="currentColor" stroke="none">光电门</text>`,
  };
  LIB.buzzer_magnetic = Object.assign(
    { w: LIB.buzzer.w, h: LIB.buzzer.h, ref: 'BZ', ports: LIB.buzzer.ports },
    { body: () => LIB.buzzer.body() + `<text x="28" y="46" font-size="9" fill="currentColor" stroke="none">EM</text>` }
  );
  LIB.speaker_8ohm = {
    w: 80, h: 70, ref: 'LS',
    ports: [P('+', 'left', 0, .5), P('-', 'left', 0, .5)],
    body: () =>
      `<line x1="0" y1="35" x2="18" y2="35"/>` +
      `<path d="M18,25 L18,45 L35,55 L35,15 Z" fill="none"/>` +
      `<path d="M35,20 Q50,35 35,50" fill="none"/>` +
      `<path d="M45,15 Q65,35 45,55" fill="none"/>` +
      `<text x="50" y="68" text-anchor="middle" font-size="9" fill="currentColor" stroke="none">8Ω</text>`,
  };
  LIB.antenna = {
    w: 80, h: 70, ref: 'AE',
    ports: [P('feed', 'bottom', .5, 1)],
    body: () =>
      `<line x1="40" y1="70" x2="40" y2="30"/>` +
      `<line x1="40" y1="30" x2="20" y2="8"/><line x1="40" y1="30" x2="60" y2="8"/>` +
      `<circle cx="40" cy="30" r="3" fill="currentColor" stroke="none"/>`,
  };
  LIB.jumper_wire = {
    w: 110, h: 40, ref: 'W',
    ports: [P('1', 'left', 0, .5), P('2', 'right', 1, .5)],
    body: () =>
      `<rect x="0" y="14" width="18" height="12" fill="none"/>` +
      `<line x1="18" y1="20" x2="92" y2="20" stroke-dasharray="4 2"/>` +
      `<rect x="92" y="14" width="18" height="12" fill="none"/>`,
  };
  // MCU / 传感器 / 驱动等：IC 盒 + 引脚名（工业可读，几何统一）
  const ic = (label, left, right, opts) => {
    const o = icBox(label, { left, right }, opts || { minW: 150 });
    return o;
  };
  LIB.atmega328 = ic('ATmega328P',
    ['VCC', 'GND', 'PB0', 'PB1', 'PC0', 'PC4', 'RESET', 'XTAL1', 'XTAL2', 'PB2', 'PB3', 'PB4', 'PB5', 'GND2'],
    ['PC1', 'PC5', 'PD0', 'PD1', 'AREF', 'AVCC', 'PD2', 'PD3', 'PD4', 'PD5', 'PD6', 'PD7', 'PC2', 'PC3']);
  LIB.esp32 = ic('ESP32', ['3V3', 'GND', 'EN', 'GPIO0', 'GPIO21'], ['GPIO2', 'GPIO22', 'TX', 'RX']);
  LIB.dht11 = ic('DHT11', ['VCC', 'DATA'], ['NC', 'GND']);
  LIB.ds18b20 = ic('DS18B20', ['VDD', 'DQ'], ['GND']);
  LIB.i2c_rh_sensor = ic('SHT4x', ['SDA', 'SCL'], ['VDD', 'VSS']);
  LIB.bmp280 = ic('BMP280', ['VCC', 'GND', 'SCL'], ['SDA', 'SDO', 'CSB']);
  LIB.mpu6050 = ic('MPU6050', ['VCC', 'GND', 'SCL'], ['SDA', 'AD0', 'INT']);
  LIB.hc_sr04 = ic('HC-SR04', ['VCC', 'TRIG'], ['ECHO', 'GND']);
  LIB.pir = ic('PIR', ['VCC', 'OUT'], ['GND']);
  LIB.mq2 = ic('MQ-2', ['VCC', 'GND'], ['DO', 'AO']);
  LIB.sound_sensor = ic('声音', ['VCC', 'GND'], ['DO', 'AO']);
  LIB.ir_receiver = ic('VS1838', ['VCC', 'OUT'], ['GND']);
  LIB.ir_led = {
    w: 90, h: 40, ref: 'D',
    ports: [P('A', 'left', 0, .5), P('K', 'right', 1, .5), P('+', 'left', 0, .5), P('-', 'right', 1, .5)],
    body: () =>
      `<line x1="0" y1="20" x2="26" y2="20"/><line x1="54" y1="20" x2="90" y2="20"/>` +
      `<path d="M26,8 L26,32 L54,20 Z" fill="none"/><line x1="54" y1="8" x2="54" y2="32"/>` +
      `<line x1="34" y1="6" x2="44" y2="0"/><line x1="42" y1="8" x2="52" y2="2"/>`,
  };
  LIB.hall_sensor = ic('Hall', ['VCC', 'OUT'], ['GND']);
  LIB.load_cell = ic('LoadCell', ['E+', 'E-'], ['A+', 'A-']);
  LIB.microphone = {
    w: 70, h: 70, ref: 'MK',
    ports: [P('+', 'bottom', .35, 1), P('-', 'bottom', .65, 1)],
    body: () =>
      `<circle cx="35" cy="30" r="18" fill="none"/>` +
      `<line x1="35" y1="48" x2="35" y2="70"/>` +
      `<line x1="25" y1="70" x2="45" y2="70"/>` +
      `<line x1="30" y1="22" x2="40" y2="38"/><line x1="40" y1="22" x2="30" y2="38"/>`,
  };
  LIB.max7219 = ic('MAX7219', ['DIN', 'CS', 'CLK'], ['DOUT', 'VCC', 'GND']);
  LIB.tm1637 = ic('TM1637', ['CLK', 'DIO'], ['VCC', 'GND']);
  LIB.lm393 = ic('LM393', ['1IN+', '1IN-', '1OUT'], ['2IN+', '2IN-', '2OUT', 'VCC', 'GND']);
  LIB.lm324 = ic('LM324', ['1IN+', '1IN-', '1OUT'], ['2IN+', '2IN-', '2OUT', 'VCC', 'GND']);
  LIB.ne556 = ic('NE556', ['GND', 'TRIG1', 'OUT1', 'RESET'], ['CTRL1', 'THR1', 'DIS1', 'VCC']);
  LIB.cd4017 = ic('CD4017', ['CLK', 'RST', 'EN', 'Q0'], ['Q1', 'Q2', 'VDD', 'VSS']);
  LIB.counter_4bit = ic('计数器', ['CLK', 'RST'], ['Q0', 'Q1', 'Q2', 'Q3']);
  LIB.shiftreg_8bit = ic('74HC595', ['DS', 'SHCP', 'STCP'], ['Q7S', 'OE', 'MR']);
  LIB.flipflop_d = ic('D-FF', ['D', 'CLK'], ['Q', 'Qn', 'RST']);
  // 需要角标 ref
  ['lm317','buck_module','boost_module','atmega328','esp32','dht11','ds18b20','bmp280','mpu6050',
   'hc_sr04','pir','mq2','sound_sensor','ir_receiver','hall_sensor','load_cell','max7219','tm1637',
   'i2c_rh_sensor',
   'lm393','lm324','ne556','cd4017','counter_4bit','shiftreg_8bit','flipflop_d'].forEach(k => {
    if (LIB[k]) LIB[k].ref = 'U';
  });

  // 2026-09-11 官方库导入批：中文草稿落库符号
  LIB.capacitor_trim = {
    w: 100, h: 80, ref: 'C',
    ports: [P('+', 'left', 0, .5), P('-', 'right', 1, .5)],
    body: () =>
      `<line x1="0" y1="30" x2="28" y2="30"/>` +
      `<line x1="28" y1="10" x2="28" y2="50"/>` +
      `<line x1="44" y1="10" x2="44" y2="50"/>` +
      `<line x1="44" y1="30" x2="100" y2="30"/>` +
      `<path d="M36,0 L36,18" fill="none"/>` +
      `<path d="M30,18 L42,18 L36,28 Z" fill="currentColor" stroke="none"/>`,
  };
  LIB.capacitor_variable = {
    w: 100, h: 80, ref: 'C',
    ports: [P('+', 'left', 0, .5), P('-', 'right', 1, .5)],
    body: () =>
      `<line x1="0" y1="30" x2="28" y2="30"/>` +
      `<line x1="28" y1="10" x2="28" y2="50"/>` +
      `<line x1="44" y1="10" x2="44" y2="50"/>` +
      `<line x1="44" y1="30" x2="100" y2="30"/>` +
      `<path d="M36,0 L36,18" fill="none"/>` +
      `<path d="M30,18 L42,18 L36,28 Z" fill="currentColor" stroke="none"/>`,
  };
  LIB.capacitor_feedthrough = {
    w: 100, h: 70, ref: 'C',
    ports: [P('1', 'left', 0, 0.5), P('2', 'right', 1, 0.5), P('3', 'top', 0.5, 0)],
    body: () =>
      `<rect x="25" y="15" width="50" height="40" fill="none"/>` +
      `<text x="50" y="40" text-anchor="middle" font-size="10" fill="currentColor" stroke="none">穿心电容</text>`,
  };
  LIB.crystal_gnd = icBox('接地晶振', { left: ['1', '2'], right: ['3', '4'] }, { minW: 140 });
  LIB.crystal_gnd.ref = 'Y';
  LIB.circuit_breaker_1p = {
    w: 100, h: 80, ref: 'QF',
    ports: [P('1', 'left', 0, .5), P('2', 'right', 1, .5)],
    body: () =>
      `<line x1="0" y1="40" x2="30" y2="40"/>` +
      `<line x1="70" y1="40" x2="100" y2="40"/>` +
      `<rect x="30" y="20" width="40" height="40" rx="4" fill="none"/>` +
      `<path d="M35,55 L65,25" fill="none"/>` +
      `<text x="50" y="78" text-anchor="middle" font-size="10" fill="currentColor" stroke="none">单极断路</text>`,
  };
  LIB.circuit_breaker_2p = icBox('双极断路器', { left: ['1', '2'], right: ['3', '4'] }, { minW: 140 });
  LIB.circuit_breaker_2p.ref = 'QF';
  LIB.circuit_breaker_3p = icBox('三极断路器', { left: ['1', '2', '3'], right: ['4', '5', '6'] }, { minW: 140 });
  LIB.circuit_breaker_3p.ref = 'QF';
  LIB.capacitor_network = icBox('电容排', { left: ['1', '2', '3'], right: ['4', 'com'] }, { minW: 140 });
  LIB.capacitor_network.ref = 'CN';
  LIB.antenna_dipole = {
    w: 100, h: 80, ref: 'AE',
    ports: [P('1', 'left', 0, .5), P('2', 'right', 1, .5)],
    body: () =>
      `<line x1="0" y1="40" x2="30" y2="40"/>` +
      `<line x1="70" y1="40" x2="100" y2="40"/>` +
      `<path d="M30,25 L30,55" fill="none"/>` +
      `<path d="M70,25 L70,55" fill="none"/>` +
      `<text x="50" y="70" text-anchor="middle" font-size="10" fill="currentColor" stroke="none">偶极天线</text>`,
  };
  LIB.battery_cell_1v5 = {
    w: 100, h: 80, ref: 'BT',
    ports: [P('+', 'left', 0, .5), P('-', 'right', 1, .5)],
    body: () =>
      `<line x1="0" y1="40" x2="35" y2="40"/>` +
      `<line x1="35" y1="20" x2="35" y2="60"/>` +
      `<line x1="50" y1="28" x2="50" y2="52"/>` +
      `<line x1="50" y1="40" x2="100" y2="40"/>` +
      `<text x="50" y="78" text-anchor="middle" font-size="10" fill="currentColor" stroke="none">1.5V</text>`,
  };

  // ---------- 2026 电源管理 + 通信接口 + 保护器件 + 模拟前端扩容（24 种） ----------
  LIB.ldo_3v3 = icBox('LDO3.3', { left: ['IN'], right: ['OUT'], bottom: ['GND'] }, { minW: 120 });
  LIB.ldo_1v8 = icBox('LDO1.8', { left: ['IN'], right: ['OUT'], bottom: ['GND'] }, { minW: 120 });
  LIB.charge_pump = icBox('电荷泵', { left: ['VIN'], right: ['VOUT'], top: ['C+', 'C-'], bottom: ['GND'] }, { minW: 130 });
  LIB.voltage_monitor = icBox('电压监控', { left: ['VCC', 'MR'], right: ['RESET'], bottom: ['GND'] }, { minW: 130 });
  LIB.power_sequencer = icBox('电源时序', { left: ['VIN', 'EN'], right: ['PG', 'VOUT'], bottom: ['GND'] }, { minW: 130 });
  LIB.uart_header = icBox('UART', { left: ['TX', 'RX'], right: ['VCC'], bottom: ['GND'] }, { minW: 100 });
  LIB.uart_header.ref = 'J';
  LIB.spi_header = icBox('SPI', { left: ['SCK', 'MOSI', 'MISO', 'CS'], right: ['VCC'], bottom: ['GND'] }, { minW: 110 });
  LIB.spi_header.ref = 'J';
  LIB.i2c_header = icBox('I2C', { left: ['SCL', 'SDA'], right: ['VCC'], bottom: ['GND'] }, { minW: 100 });
  LIB.i2c_header.ref = 'J';
  LIB.can_transceiver = icBox('CAN', { left: ['TXD', 'RXD', 'VCC'], right: ['CANH', 'CANL'], bottom: ['GND'] }, { minW: 110 });
  LIB.rs485_transceiver = icBox('RS485', { left: ['RO', 'DI', 'DE', 'RE'], right: ['A', 'B', 'VCC'], bottom: ['GND'] }, { minW: 110 });
  LIB.usb_c = icBox('Type-C', { left: ['VBUS', 'D+', 'D-'], right: ['GND'], top: ['CC1', 'CC2'] }, { minW: 120 });
  LIB.usb_c.ref = 'J';
  LIB.tvs_array = icBox('TVS阵列', { left: ['IO1', 'IO2'], right: ['VCC'], bottom: ['GND'] }, { minW: 120 });
  LIB.esd_diode = {
    w: 96, h: 36, ref: 'D',
    ports: [P('A', 'left', 0, .5), P('K', 'right', 1, .5), P('+', 'left', 0, .5), P('-', 'right', 1, .5)],
    body: () =>
      `<line x1="0" y1="18" x2="36" y2="18"/>` +
      `<path d="M36 8 L36 28 L58 18 Z" fill="none"/>` +
      `<line x1="58" y1="8" x2="58" y2="28"/>` +
      `<line x1="58" y1="18" x2="96" y2="18"/>` +
      `<text x="48" y="34" text-anchor="middle" font-size="9" fill="currentColor" stroke="none">ESD</text>`,
  };
  LIB.common_mode_choke = icBox('共模', { left: ['1', '2'], right: ['3', '4'] }, { minW: 120 });
  LIB.common_mode_choke.ref = 'L';
  LIB.gas_discharge_tube = {
    w: 100, h: 50, ref: 'GDT',
    ports: [P('1', 'left', 0, .5), P('2', 'right', 1, .5)],
    body: () =>
      `<line x1="0" y1="25" x2="30" y2="25"/><line x1="70" y1="25" x2="100" y2="25"/>` +
      `<rect x="30" y="12" width="40" height="26" rx="8" fill="none"/>` +
      `<path d="M40 30 L50 18 L60 30" fill="none"/>`,
  };
  LIB.resettable_fuse_1206 = Object.assign(
    { w: LIB.fuse.w, h: LIB.fuse.h, ref: 'F', ports: LIB.fuse.ports },
    { body: () => LIB.fuse.body() + `<text x="48" y="34" text-anchor="middle" font-size="9" fill="currentColor" stroke="none">PPTC</text>` }
  );
  LIB.instrumentation_amp = icBox('INA', { left: ['IN+', 'IN-'], right: ['OUT'], top: ['V+'], bottom: ['V-', 'REF'] }, { minW: 110 });
  LIB.current_sense_amp = icBox('电流检测', { left: ['IN+', 'IN-'], right: ['OUT'], top: ['VCC'], bottom: ['GND'] }, { minW: 120 });
  LIB.digital_pot = icBox('数字电位器', { left: ['A', 'W', 'B'], right: ['CS', 'SCK', 'SI'], top: ['VCC'], bottom: ['GND'] }, { minW: 130 });
  LIB.dac_8bit = icBox('DAC8', { left: ['CS', 'SCK', 'DIN'], right: ['OUT', 'VREF'], top: ['VCC'], bottom: ['GND'] }, { minW: 110 });
  LIB.adc_8bit = icBox('ADC8', { left: ['AIN', 'CLK', 'CS'], right: ['DOUT', 'VREF'], top: ['VCC'], bottom: ['GND'] }, { minW: 110 });
  LIB.buzzer_magnetic_5v = Object.assign(
    { w: LIB.buzzer.w, h: LIB.buzzer.h, ref: 'BZ', ports: LIB.buzzer.ports },
    { body: () => LIB.buzzer.body() + `<text x="28" y="46" font-size="9" fill="currentColor" stroke="none">5V</text>` }
  );
  LIB.relay_solid_state = icBox('SSR', { left: ['IN+', 'IN-'], right: ['OUT1', 'OUT2'] }, { minW: 120 });
  LIB.relay_solid_state.ref = 'K';
  LIB.optocoupler_ac = Object.assign(
    icBox('AC光耦', { left: ['A', 'K'], right: ['C', 'E'] }, { minW: 120 }),
    { ref: 'U' }
  );

  // ---------- 坐标变换：本地端口点 -> 绕符号中心旋转 ----------
  function localPin(def, pinId) {
    const p = def.ports.find(q => q.id === pinId);
    if (!p) return null;
    return { x: p.cx * def.w, y: p.cy * def.h };
  }
  function rotateAround(pt, cx, cy, deg) {
    const r = ((deg % 360) + 360) % 360 * Math.PI / 180;
    const dx = pt.x - cx, dy = pt.y - cy;
    return { x: cx + dx * Math.cos(r) - dy * Math.sin(r), y: cy + dx * Math.sin(r) + dy * Math.cos(r) };
  }

  /**
   * 计算某元件某引脚的世界坐标（走线模块用它取线端点，保证和符号严丝合缝）
   * @param type 元件类型  @param pinId 引脚id
   * @param x,y 实例左上角  @param deg 旋转角(0/90/180/270)
   */
  function pinWorld(type, pinId, x, y, deg = 0) {
    const def = LIB[type];
    if (!def) throw new Error('未知元件类型: ' + type);
    const local = localPin(def, pinId);
    if (!local) throw new Error(`${type} 没有引脚 ${pinId}`);
    const r = rotateAround(local, def.w / 2, def.h / 2, deg);
    return { x: x + r.x, y: y + r.y };
  }

  /** 属性值转义（data-* 绑定钩子用，防止引号/尖括号破坏标签） */
  function escAttr(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  /**
   * 渲染一个元件实例为 SVG <g> 字符串。
   * @param type 元件类型  @param x,y 实例左上角  @param deg 旋转角(0/90/180/270)
   * @param label 位号（如 R1）  @param value 参数值标签
   * @param dataAttrs 可选 {part, type}：交互层（interact.js）的绑定钩子，
   *                 生成 data-part/data-type 属性，供拖动/点选时定位元件
   *
   * 安全说明：label/value 来自 LLM 输出（或用户手工输入），最终页面以
   * innerHTML 注入渲染，这里必须转义——否则形如
   * "R1<img src=x onerror=…>" 的位号就是一次存储型 XSS；
   * x/y/deg 坐标同样做数字强制转换，防止属性注入（如 left:"0 onload=…"）。
   */
  function renderSymbol(type, x, y, deg = 0, label, value, dataAttrs, labelPos) {
    const def = LIB[type];
    if (!def) return '';
    const nx = toFinite(x), ny = toFinite(y), nd = toFinite(deg);
    const rot = nd ? ` rotate(${nd} ${def.w / 2} ${def.h / 2})` : '';
    // 标签排布两级策略：
    //   1) 调用方（render.js）传入 labelPos={ref:{x,y,anchor},val:{...}} 时
    //      直接采用 —— 那是基于实际走线几何做过避让决策的位置（首选）；
    //   2) 未传入时的独立使用默认：位号上方居中、值下方居中；符号有
    //      “顶部/底部正中央”引脚（gnd/vcc/BJT C·E/电位器调节脚等）时
    //      改放右侧，避免中央逃逸引线穿过标签。
    const centerTB = def.ports.some(p =>
      (p.side === 'top' || p.side === 'bottom') && Math.abs(p.cx - 0.5) < 0.02);
    const refRight = def.refAt ? def.refAt === 'right' : centerTB;
    const valRight = centerTB;
    const lp = labelPos || {};
    const lab = label ? (lp.ref
      ? `<text x="${lp.ref.x}" y="${lp.ref.y}"${lp.ref.anchor ? ` text-anchor="${lp.ref.anchor}"` : ''} font-size="13" class="ref-label" stroke="none">${escAttr(label)}</text>`
      : (refRight
        ? `<text x="${def.w + 6}" y="${def.h / 2 + 4}" font-size="13" class="ref-label" stroke="none">${escAttr(label)}</text>`
        : `<text x="${def.w / 2}" y="-6" text-anchor="middle" font-size="13" class="ref-label" stroke="none">${escAttr(label)}</text>`)) : '';
    const val = value ? (lp.val
      ? `<text x="${lp.val.x}" y="${lp.val.y}"${lp.val.anchor ? ` text-anchor="${lp.val.anchor}"` : ''} font-size="12" class="val-label" stroke="none">${escAttr(value)}</text>`
      : (valRight
        ? `<text x="${def.w + 6}" y="${def.h / 2 + 20}" font-size="12" class="val-label" stroke="none">${escAttr(value)}</text>`
        : `<text x="${def.w / 2}" y="${def.h + 14}" text-anchor="middle" font-size="12" class="val-label" stroke="none">${escAttr(value)}</text>`)) : '';
    const data = dataAttrs ? ` data-part="${escAttr(dataAttrs.part)}" data-type="${escAttr(dataAttrs.type)}"` : '';
    return `<g class="sym sym-${escAttr(type)}"${data} transform="translate(${nx},${ny})${rot}">` +
      `<g class="symbol-body" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" stroke-linecap="round">${bodyOf(type)}</g>` +
      lab + val + `</g>`;
  }

  /** 任意输入 -> 有限数字，非法值（字符串注入/NaN/Infinity）归 0 */
  function toFinite(v) {
    const n = Number(v);
    return isFinite(n) ? n : 0;
  }

  // ---------- 符号 body 片段缓存 ----------
  // body() 是纯函数（不依赖实例状态），同一类型的 SVG 片段在整图渲染、
  // 以及 interact.js 拖动触发的重绘里被反复拼接；按类型缓存一份，
  // 每次渲染的拼接次数从 O(元件数) 降为 O(类型数)，输出完全一致。
  const bodyCache = new Map();
  function bodyOf(type) {
    let b = bodyCache.get(type);
    if (b === undefined) {
      b = LIB[type].body();
      bodyCache.set(type, b);
    }
    return b;
  }

  function sizeOf(type) { const d = LIB[type]; return d ? { w: d.w, h: d.h } : null; }
  function pinsOf(type) { const d = LIB[type]; return d ? d.ports.map(p => p.id) : []; }
  function types() { return Object.keys(LIB); }

  return { LIB, pinWorld, renderSymbol, sizeOf, pinsOf, types };
});
