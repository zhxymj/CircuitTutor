/* ============================================================
 * interact.js —— 智绘电路交互层（原生 JS，零依赖，IIFE 风格）
 * ------------------------------------------------------------
 * 给 render.js 渲染出的整图 SVG 加上“对话改图”所需的交互能力：
 *  1) 拖动元件：pointerdown 命中 g[data-part] → 拖动中改写该 g 的
 *     transform 做视觉跟随 → 松手时吸附 10px 网格写回
 *     circuit.parts[i].left/top，再调 onRerender，宿主页用
 *     layout:'keep' 重渲染，走线自动重排；
 *  2) 点选编辑：位移 < 4px 视为单击 → 高亮选中 + 显示属性面板
 *     （容器由宿主页提供）：位号/类型只读、value 可编辑、可删除
 *     元件（连带删除涉及它的 connections，删除前提示用户）；
 *  3) 缩放平移：滚轮以指针为中心缩放 viewBox（0.4~3 倍），
 *     空白处按住拖动平移；
 *  4) 撤销/重做：内置历史栈（深拷贝快照，上限 50 步）。任何变更
 *     （拖动松手写回坐标、参数修改、删除、插入）都走统一入口
 *     commit()，提交前把【变更前】状态压入 undoStack 并清空
 *     redoStack；undo/redo 用快照整体替换 circuit 内容（就地清空
 *     重填属性，保持宿主页持有的引用不变），再触发 onChange +
 *     onRerender。历史栈挂在 circuit 对象上（WeakMap），跨“重
 *     渲染-重新 attach”共享，换新电路自动作废；
 *  5) 插入元件：insertPart(type, {left, top, id, value}) 自动分配
 *     不冲突的位号（类型 ref 前缀 + 递增数字）、默认参数值取自
 *     内置元件信息表（与 frontend_manifest.json 同源），缺省位置
 *     放在当前视图中心并吸附 10px 网格，插入后自动选中。新元件
 *     不带接线（初始悬空，ERC 会标记），需要用户手动接线。
 *  6) 引脚连线：单击元件旁的引脚端点开始连线（十字光标 +
 *     红色虚线预览跟随指针），再单击另一引脚落线成导线（走统一
 *     commit 入口，可撤销）；点回起点引脚 / ESC / 点击引脚以外区域
 *     取消。引脚世界坐标用 CircuitSymbols.pinWorld 计算（与
 *     render.js 走线端点同源几何，符号旋转自动跟随），命中半径按
 *     当前缩放换算保持“屏幕上约 9px”的手感。同对引脚重复连线被
 *     静默忽略，同一引脚自环不允许。
 * 事件统一用 Pointer Events（鼠标/触摸一致），配合
 * setPointerCapture + preventDefault 避免页面滚动干扰。
 *
 * 用法（浏览器）：
 *   const ctl = CircuitInteract.attach(svgEl, circuit, {
 *     panel: document.getElementById('props'), // 属性面板容器（可选）
 *     selectedId: 'R1', viewBox: {x,y,w,h},    // 重渲染后恢复选中/视图（可选）
 *     onChange(circuit) {}, onRerender() {},
 *   });
 *   ctl.zoomIn(); ctl.select('R1');
 *   ctl.undo(); ctl.redo(); ctl.canUndo(); ctl.canRedo();
 *   ctl.insertPart('resistor');            // 视图中心插入一个电阻
 * 纯函数（Node 冒烟测试直接调）：snap / partOfRef / connsRelatedTo /
 * nextId（位号分配）/ createHistory（撤销重做）/ partInfo（元件信息表）。
 * ============================================================ */
(function (root, factory) {
  if (typeof module !== 'undefined' && module.exports) module.exports = factory();
  else root.CircuitInteract = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const GRID = 10;        // 位置吸附网格（与 layout.js 的 GRID 一致）
  const CLICK_PX = 4;     // 位移小于该屏幕像素数视为“单击”
  const PIN_HIT_R_PX = 9; // 引脚命中半径（屏幕像素；按缩放换算成用户坐标）
  const ZOOM_MIN = 0.4, ZOOM_MAX = 3, ZOOM_STEP = 1.2; // 缩放倍率范围/步长
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const HISTORY_LIMIT = 50; // 撤销栈深度上限（超出丢最旧）

  // ---------- 纯函数（Node 冒烟测试直接调用） ----------
  const snap = v => Math.round(v / GRID) * GRID;            // 吸附到 10px 网格
  const partOfRef = ref => String(ref).split(':', 1)[0];    // "R1:1" -> "R1"
  // connections 形如 ["R1:1","LED1:+","red",[]]：找出涉及某元件的全部连接
  const connsRelatedTo = (conns, id) => (conns || []).filter(c =>
    Array.isArray(c) && c.slice(0, 2).some(r => typeof r === 'string' && partOfRef(r) === id));

  // 解析 viewBox 属性 -> {x,y,w,h}
  function parseVB(attr) {
    if (!attr) return null;
    const n = String(attr).trim().split(/[\s,]+/).map(Number);
    return n.length === 4 && n.every(isFinite) ? { x: n[0], y: n[1], w: n[2], h: n[3] } : null;
  }
  const validVB = v => v && isFinite(v.x) && isFinite(v.y) && v.w > 0 && v.h > 0;
  // 解析 <g transform="translate(x,y) rotate(...)"> 的平移分量
  function readTranslate(g) {
    const m = /translate\(\s*(-?[\d.]+)[ ,]+(-?[\d.]+)/.exec(g.getAttribute('transform') || '');
    return m ? { x: parseFloat(m[1]), y: parseFloat(m[2]) } : null;
  }
  const attrSel = v => String(v).replace(/["\\\]]/g, ''); // id 兜底转义（属性选择器用）

  // ---------- 元件信息表（插入元件用） ----------
  // 与根目录 frontend_manifest.json 全量同源（server.py 只分发 5 个 JS，
  // 不分发该 JSON，故在此内置同款数据）：ref 位号前缀 / name 中文名 /
  // value 默认参数值。共 38 种（manifest 37 种 + 符号库基础 opamp），
  // 测试会对 manifest 与 symbols.js 做全量一致性断言。
  const PART_INFO = {
    resistor:          { ref: 'R',   name: '电阻',           value: '10k' },
    capacitor:         { ref: 'C',   name: '电容',           value: '100nF' },
    inductor:          { ref: 'L',   name: '电感',           value: '10mH' },
    potentiometer:     { ref: 'RV',  name: '电位器',         value: '10k' },
    diode:             { ref: 'D',   name: '二极管',         value: '1N4148' },
    zener:             { ref: 'D',   name: '稳压二极管',     value: '5.1V' },
    led:               { ref: 'LED', name: '发光二极管',     value: '红' },
    npn:               { ref: 'Q',   name: 'NPN三极管',      value: '2N3904' },
    pnp:               { ref: 'Q',   name: 'PNP三极管',      value: '2N3906' },
    nmos:              { ref: 'Q',   name: 'N沟道MOS管',     value: 'IRF540' },
    fuse:              { ref: 'F',   name: '保险丝',         value: '1A' },
    crystal:           { ref: 'Y',   name: '晶振',           value: '12MHz' },
    battery:           { ref: 'BAT', name: '电池/直流源',     value: '5V' },
    vcc:               { ref: 'VCC', name: '电源正',         value: '5V' },
    gnd:               { ref: 'GND', name: '接地',           value: '' },
    opamp:             { ref: 'U',   name: '通用运放',       value: 'LM358' },
    opamp_inverting:   { ref: 'U',   name: '反相比例放大器', value: '' },
    opamp_noninverting:{ ref: 'U',   name: '同相比例放大器', value: '' },
    opamp_comparator:  { ref: 'U',   name: '电压比较器',     value: '' },
    opamp_follower:    { ref: 'U',   name: '电压跟随器',     value: '' },
    buzzer:            { ref: 'BZ',  name: '蜂鸣器',         value: '有源' },
    speaker:           { ref: 'LS',  name: '扬声器',         value: '8Ω' },
    regulator_7805:    { ref: 'U',   name: '7805稳压',       value: 'LM7805' },
    ne555:             { ref: 'U',   name: '555定时器',      value: 'NE555' },
    switch:            { ref: 'SW',  name: '开关',           value: 'SPST' },
    pushbutton:        { ref: 'SW',  name: '按钮',           value: 'NO' },
    // 2026-09-09 常用元件扩展批（与 component_catalog.CATALOG 同源）
    pmos:              { ref: 'Q',   name: 'P沟道MOS管',     value: 'IRF9540' },
    schottky:          { ref: 'D',   name: '肖特基二极管',   value: '1N5819' },
    photodiode:        { ref: 'D',   name: '光电二极管',     value: 'BPW34' },
    photocell:         { ref: 'RG',  name: '光敏电阻',       value: 'GL5516' },
    thermistor:        { ref: 'TH',  name: '热敏电阻',       value: '10k' },
    ac_source:         { ref: 'VAC', name: '交流电源',       value: '220V' },
    dc_motor:          { ref: 'M',   name: '直流电机',       value: '3V' },
    servo:             { ref: 'M',   name: '舵机',           value: 'SG90' },
    relay:             { ref: 'K',   name: '继电器',         value: '5V' },
    optocoupler:       { ref: 'U',   name: '光耦',           value: 'PC817' },
    bridge_rectifier:  { ref: 'BR',  name: '整流桥',         value: 'KBP210' },
    screw_terminal:    { ref: 'J',   name: '接线端子',       value: '2P' },
    // 2026-09-11 工业级扩容
    electrolytic:      { ref: 'C',   name: '电解电容',       value: '10µF' },
    tvs:               { ref: 'D',   name: 'TVS二极管',      value: 'SMBJ5.0A' },
    phototransistor:   { ref: 'Q',   name: '光敏三极管',     value: '3DU5' },
    ferrite_bead:      { ref: 'FB',  name: '磁珠',           value: '600Ω' },
    varistor:          { ref: 'RV',  name: '压敏电阻',       value: '10D471K' },
    scr:               { ref: 'VS',  name: '晶闸管',         value: 'BT151' },
    buzzer_passive:    { ref: 'BZ',  name: '无源蜂鸣器',     value: '无源' },
    ammeter:           { ref: 'PA',  name: '电流表',         value: 'mA' },
    voltmeter:         { ref: 'PV',  name: '电压表',         value: 'V' },
    // 2026-09-11 官方库导入批
    capacitor_trim:        { ref: 'C',   name: '微调电容',           value: '5-30pF' },
    capacitor_variable:    { ref: 'C',   name: '可变电容',           value: '365pF' },
    capacitor_feedthrough: { ref: 'C',   name: '穿心电容',           value: '100pF' },
    crystal_gnd:           { ref: 'Y',   name: '接地晶振',           value: '12MHz' },
    circuit_breaker_1p:    { ref: 'QF',   name: '单极断路器',           value: 'C16' },
    circuit_breaker_2p:    { ref: 'QF',   name: '双极断路器',           value: 'C16/2P' },
    circuit_breaker_3p:    { ref: 'QF',   name: '三极断路器',           value: 'C16/3P' },
    capacitor_network:     { ref: 'CN',   name: '电容排',           value: '100nF×4' },
    antenna_dipole:        { ref: 'AE',   name: '偶极天线',           value: 'λ/2' },
    battery_cell_1v5:      { ref: 'BT',   name: '1.5V电池',           value: '1.5V' },
    capacitor_ceramic:     { ref: 'C',   name: '瓷片电容',           value: '100nF' },
    capacitor_film:        { ref: 'C',   name: '薄膜电容',           value: '1µF' },
    capacitor_tantalum:    { ref: 'C',   name: '钽电容',           value: '10µF' },
    resistor_network:      { ref: 'RN',   name: '排阻',           value: '10k×8' },
    inductor_toroid:       { ref: 'L',   name: '环形电感',           value: '100µH' },
    transformer:           { ref: 'T',   name: '变压器',           value: '220V-12V' },
    led_rgb:               { ref: 'LED',   name: 'RGB灯珠',           value: '共阴' },
    diode_rectifier:       { ref: 'D',   name: '整流二极管',           value: '1N4007' },
    triac:                 { ref: 'VS',   name: '双向可控硅',           value: 'BTA16' },
    diac:                  { ref: 'DB',   name: '双向触发二极管',           value: 'DB3' },
    fuse_resettable:       { ref: 'F',   name: '自恢复保险丝',           value: '500mA' },
    igbt:                  { ref: 'Q',   name: 'IGBT',           value: 'IRG4PC40U' },
    ujt:                   { ref: 'Q',   name: '单结晶体管',           value: '2N2646' },
    phototriac:            { ref: 'U',   name: '光耦可控硅',           value: 'MOC3021' },
    ic_and:                { ref: 'U',   name: '与门',           value: '74HC08' },
    ic_or:                 { ref: 'U',   name: '或门',           value: '74HC32' },
    ic_not:                { ref: 'U',   name: '非门',           value: '74HC04' },
    ic_xor:                { ref: 'U',   name: '异或门',           value: '74HC86' },
    ic_nand:               { ref: 'U',   name: '与非门',           value: '74HC00' },
    ic_nor:                { ref: 'U',   name: '或非门',           value: '74HC02' },
    ic_buffer:             { ref: 'U',   name: '缓冲器',           value: '74HC244' },
    flipflop_d:            { ref: 'U',   name: 'D触发器',           value: '74HC74' },
    counter_4bit:          { ref: 'U',   name: '4位计数器',           value: '74HC93' },
    shiftreg_8bit:         { ref: 'U',   name: '8位移位寄存器',           value: '74HC595' },
    lm393:                 { ref: 'U',   name: '双电压比较器',           value: 'LM393' },
    lm324:                 { ref: 'U',   name: '四运放',           value: 'LM324' },
    ne556:                 { ref: 'U',   name: '双555定时器',           value: 'NE556' },
    cd4017:                { ref: 'U',   name: '十进制计数器',           value: 'CD4017' },
    atmega328:             { ref: 'U',   name: 'ATmega328',           value: 'ATmega328P' },
    esp32:                 { ref: 'U',   name: 'ESP32模块',           value: 'ESP32-WROOM' },
    dht11:                 { ref: 'U',   name: '温湿度传感器',           value: 'DHT11' },
    i2c_rh_sensor:         { ref: 'U',   name: 'I2C温湿度传感器',       value: 'SHT41-AD1B' },
    ds18b20:               { ref: 'U',   name: '数字温度传感器',           value: 'DS18B20' },
    bmp280:                { ref: 'U',   name: '气压传感器',           value: 'BMP280' },
    mpu6050:               { ref: 'U',   name: '陀螺仪加速度计',           value: 'MPU6050' },
    hc_sr04:               { ref: 'U',   name: '超声波测距',           value: 'HC-SR04' },
    pir:                   { ref: 'U',   name: '人体红外',           value: 'HC-SR501' },
    mq2:                   { ref: 'U',   name: '气体传感器',           value: 'MQ-2' },
    sound_sensor:          { ref: 'U',   name: '声音传感器',           value: 'KY-037' },
    ir_receiver:           { ref: 'U',   name: '红外接收头',           value: 'VS1838B' },
    ir_led:                { ref: 'D',   name: '红外发射管',           value: 'TSAL6200' },
    hall_sensor:           { ref: 'U',   name: '霍尔传感器',           value: '49E' },
    load_cell:             { ref: 'U',   name: '称重传感器',           value: '5kg' },
    microphone:            { ref: 'MK',   name: '驻极体话筒',           value: '驻极体' },
    sevenseg:              { ref: 'DS',   name: '数码管',           value: '共阴0.56' },
    lcd1602:               { ref: 'LCD',   name: 'LCD1602',           value: 'LCD1602' },
    oled_ssd1306:          { ref: 'OLED',   name: 'OLED屏',           value: 'SSD1306' },
    max7219:               { ref: 'U',   name: 'LED驱动芯片',           value: 'MAX7219' },
    tm1637:                { ref: 'U',   name: '四位数码管模块',           value: 'TM1637' },
    stepper:               { ref: 'M',   name: '步进电机',           value: '28BYJ-48' },
    solenoid:              { ref: 'Y',   name: '电磁铁',           value: '5V' },
    heater:                { ref: 'EH',   name: '加热片',           value: '5V 5W' },
    lm317:                 { ref: 'U',   name: '可调稳压',           value: 'LM317' },
    buck_module:           { ref: 'U',   name: 'DC-DC降压模块',           value: 'LM2596' },
    boost_module:          { ref: 'U',   name: 'DC-DC升压模块',           value: 'MT3608' },
    lipo_battery:          { ref: 'BT',   name: '锂电池',           value: '3.7V 1000mAh' },
    solar_cell:            { ref: 'BT',   name: '太阳能板',           value: '5V 1W' },
    usb_port:              { ref: 'J',   name: 'USB接口',           value: 'Micro-USB' },
    barrel_jack:           { ref: 'J',   name: 'DC电源座',           value: '5.5×2.1' },
    conn_header:           { ref: 'J',   name: '排针',           value: '1×4' },
    switch_toggle:         { ref: 'SW',   name: '拨动开关',           value: 'SPDT' },
    switch_dip:            { ref: 'SW',   name: '拨码开关',           value: '6位' },
    switch_reed:           { ref: 'SW',   name: '干簧管',           value: 'Φ3×14' },
    connector_screw2:      { ref: 'J',   name: '两位接线端子',           value: '5.08mm' },
    potentiometer_trim:    { ref: 'RV',   name: '微调电位器',           value: '10k' },
    varactor:              { ref: 'D',   name: '变容二极管',           value: 'BB910' },
    optointerrupter:       { ref: 'U',   name: '光电开关',           value: 'ITR9606' },
    buzzer_magnetic:       { ref: 'BZ',   name: '电磁蜂鸣器',           value: '5V' },
    speaker_8ohm:          { ref: 'LS',   name: '8欧扬声器',           value: '8Ω 0.5W' },
    antenna:               { ref: 'AE',   name: '天线',           value: '2.4G' },
    jumper_wire:           { ref: 'W',   name: '跳线',           value: 'M-M 20cm' },
    // 2026 电源管理 + 通信接口 + 保护器件 + 模拟前端扩容（24 种）
    ldo_3v3:               { ref: 'U',   name: 'LDO3.3V稳压',       value: 'AMS1117-3.3' },
    ldo_1v8:               { ref: 'U',   name: 'LDO1.8V稳压',       value: 'AMS1117-1.8' },
    charge_pump:           { ref: 'U',   name: '电荷泵',           value: 'ICL7660' },
    voltage_monitor:       { ref: 'U',   name: '电压监控器',       value: 'MAX809' },
    power_sequencer:       { ref: 'U',   name: '电源时序器',       value: 'TPS3808' },
    uart_header:           { ref: 'J',   name: 'UART排针',         value: '1×4' },
    spi_header:            { ref: 'J',   name: 'SPI排针',          value: '1×6' },
    i2c_header:            { ref: 'J',   name: 'I2C排针',          value: '1×4' },
    can_transceiver:       { ref: 'U',   name: 'CAN收发器',        value: 'TJA1050' },
    rs485_transceiver:     { ref: 'U',   name: 'RS485收发器',      value: 'MAX485' },
    usb_c:                 { ref: 'J',   name: 'USB Type-C',       value: 'USB-C 16P' },
    tvs_array:             { ref: 'D',   name: 'TVS阵列',          value: 'PESD5V0L4' },
    esd_diode:             { ref: 'D',   name: 'ESD保护二极管',    value: 'PESD5V0S1BA' },
    common_mode_choke:     { ref: 'L',   name: '共模扼流圈',       value: '10mH 1A' },
    gas_discharge_tube:    { ref: 'GDT', name: '气体放电管',       value: '2R75-230' },
    resettable_fuse_1206:  { ref: 'F',   name: '1206自恢复保险丝', value: '1206 500mA' },
    instrumentation_amp:   { ref: 'U',   name: '高精度仪表放大器', value: 'INA128' },
    current_sense_amp:     { ref: 'U',   name: '电流检测放大器',   value: 'INA219' },
    digital_pot:           { ref: 'U',   name: '数字电位器',       value: 'MCP41010' },
    dac_8bit:              { ref: 'U',   name: '8位DAC',           value: 'DAC0808' },
    adc_8bit:              { ref: 'U',   name: '8位ADC',           value: 'ADC0804' },
    buzzer_magnetic_5v:    { ref: 'BZ',  name: '5V电磁蜂鸣器',     value: '5V' },
    relay_solid_state:     { ref: 'K',   name: '固态继电器',       value: 'SSR-25DA' },
    optocoupler_ac:        { ref: 'U',   name: '交流输入光耦',     value: 'H11AA1' },
  };

  /**
   * 查元件信息：优先内置表；浏览器里符号库（CircuitSymbols.LIB）有该类型
   * 而内置表没跟上时，退化为用其 ref 前缀 + 空默认值。查不到返回 null。
   */
  function partInfo(type) {
    if (PART_INFO[type]) return PART_INFO[type];
    try {
      const lib = typeof CircuitSymbols !== 'undefined' ? CircuitSymbols : null;
      const def = lib && lib.LIB && lib.LIB[type];
      if (def) return { ref: def.ref || 'U', name: type, value: '' };
    } catch (e) { /* Node 无全局符号库，忽略 */ }
    return null;
  }

  /**
   * 自动分配不冲突的位号：按类型 ref 前缀 + 递增数字，跳过已占用 id。
   * parts 里已有 R1、R2 时插 resistor 得 R3；有 LED1 时插 led 得 LED2；
   * 跨类型互不冲突（R 与 LED 计数各自独立）。
   */
  function nextId(parts, type) {
    const info = partInfo(type);
    const prefix = (info && info.ref) || 'U';
    const used = new Set();
    (parts || []).forEach(p => { if (p && p.id != null) used.add(p.id); });
    for (let i = 1; ; i++) {
      const id = prefix + i;
      if (!used.has(id)) return id;
    }
  }

  // 元件符号尺寸：浏览器取自 CircuitSymbols.sizeOf；Node（无 DOM 无全局
  // 符号库）返回 null，由调用方退化为 0 尺寸（中心点即 left/top）
  function sizeOfSym(type) {
    try {
      if (typeof CircuitSymbols !== 'undefined' && CircuitSymbols && CircuitSymbols.sizeOf)
        return CircuitSymbols.sizeOf(type);
    } catch (e) { /* 忽略 */ }
    return null;
  }

  // ---------- 深拷贝 / 整体替换（撤销-重做与变更提交共用） ----------
  // 电路规模小，快照直接 JSON 序列化深拷贝即可
  function cloneState(c) { return JSON.parse(JSON.stringify(c)); }
  // 用快照整体替换 dst 的全部自有属性（就地清空重填，宿主页持有的引用不变）
  function replaceState(dst, src) {
    Object.keys(dst).forEach(k => { delete dst[k]; });
    Object.keys(src).forEach(k => { dst[k] = src[k]; });
    return dst;
  }

  /**
   * 撤销/重做历史栈（纯函数，Node 冒烟测试直接调用）。
   * 语义：undoStack 存【变更前】的快照（栈顶是最近一次变更前的状态），
   * redoStack 存被 undo 撤掉的状态；current 是“当前状态”快照（init 建
   * 立、commit 后更新）。commit 会把 current 压入 undoStack 并清空
   * redoStack；栈深超过 limit 丢最旧。
   * @param {number} limit 撤销步数上限（默认 50）
   */
  function createHistory(limit) {
    const lim = limit && limit > 0 ? Math.floor(limit) : HISTORY_LIMIT;
    let undoStack = [], redoStack = [], current = null;
    const pushCapped = stack => {            // 压栈 + 超限丢最旧
      stack.push(current);
      if (stack.length > lim) stack.shift();
    };
    return {
      init(snap) { current = snap; undoStack = []; redoStack = []; },
      commit(next) {                          // 提交一次变更（next=变更后状态）
        if (current != null) pushCapped(undoStack);
        redoStack = [];                       // 新提交作废所有“重做”
        current = next;
      },
      undo() {                                // 返回要恢复的状态；无可撤销返回 null
        if (!undoStack.length) return null;
        redoStack.push(current);
        current = undoStack.pop();
        return current;
      },
      redo() {                                // 返回要恢复的状态；无可重做返回 null
        if (!redoStack.length) return null;
        pushCapped(undoStack);
        current = redoStack.pop();
        return current;
      },
      canUndo: () => undoStack.length > 0,
      canRedo: () => redoStack.length > 0,
      depth: () => undoStack.length,
      /**
       * 外部整体替换（如 AI 增量改图）后，把「变更前」快照注入撤销栈。
       * 调用时机：current 已是变更后状态（init 或 live），本方法只压入
       * prior，使 ctl.undo() 能回到改图前；同时清空 redo。
       */
      seedUndo(priorSnap) {
        if (priorSnap == null) return false;
        undoStack.push(priorSnap);
        if (undoStack.length > lim) undoStack.shift();
        redoStack = [];
        return true;
      },
    };
  }

  // 同一 circuit 对象跨“重渲染-重新 attach”共享同一份历史（宿主页每次
  // 重渲染都会重建控制器，历史不能跟着丢）；WeakMap 按引用键控，换新
  // 电路对象（SSE 新结果）自动作废旧历史，且不阻止垃圾回收。
  const histories = new WeakMap();

  // 交互层样式（cursor），整页只注入一次
  let styleDone = false;
  function injectStyle() {
    if (styleDone || typeof document === 'undefined') return;
    styleDone = true;
    const css = document.createElement('style');
    css.textContent =
      'svg.circuit-svg{cursor:grab;user-select:none;-webkit-user-select:none}' +
      'svg.circuit-svg g[data-part]{cursor:move}' +
      'svg.circuit-svg.panning{cursor:grabbing}' +
      'svg.circuit-svg.wiring{cursor:crosshair}' +
      // 连线模式：引脚命中区可视化（interact 动态插入的 .pin-mark）
      'svg.circuit-svg.wiring .pin-mark{display:block}' +
      '.pin-mark{display:none;pointer-events:none}' +
      '.pin-mark circle{fill:none;stroke:#8a0d0d;stroke-width:1.5;opacity:.85}';
    document.head.appendChild(css);
  }

  /**
   * 把交互层挂到一个已渲染好的 SVG 上。
   * @param {SVGElement} svg renderCircuit 的产物（含 viewBox）
   * @param {Object} circuit CircuitJSON（交互层整体替换 parts/connections，引用不变）
   * @param {Object} opts { panel, selectedId, viewBox, onChange, onRerender }
   * @returns 控制器 { select, clearSelection, zoomIn, zoomOut, resetView,
   *                   getViewBox, getSelected, undo, redo, canUndo, canRedo,
   *                   insertPart, detach }（宿主页用它接缩放/历史按钮等）
   */
  function attach(svg, circuit, opts) {
    opts = opts || {};
    if (!svg || !circuit || typeof document === 'undefined') return null;
    const onChange = opts.onChange || function () {};
    const onRerender = opts.onRerender || function () {};
    const onWireChange = opts.onWireChange || function () {};
    const panel = opts.panel || null;
    const parts = () => circuit.parts || [];
    const findPart = id => parts().find(p => p && p.id === id) || null;
    const gOf = id => svg.querySelector('g[data-part="' + attrSel(id) + '"]');
    // 本电路的历史栈（首次 attach 在坐标写回后 init；之后每次重新
    // attach 都复用同一份，保证撤销/重做跨重渲染连续）
    let history = histories.get(circuit) || null;

    // ---------- 视图（viewBox）状态 ----------
    const base = parseVB(svg.getAttribute('viewBox')) || {
      x: 0, y: 0, w: parseFloat(svg.getAttribute('width')) || 800,
      h: parseFloat(svg.getAttribute('height')) || 600,
    };
    // 恢复宿主页传来的视图时，把宽高比规范成当前 svg 的宽高比：
    // 避免 preserveAspectRatio 留白导致指针坐标换算出现偏差
    let vb = validVB(opts.viewBox)
      ? { x: opts.viewBox.x, y: opts.viewBox.y, w: opts.viewBox.w, h: opts.viewBox.w * base.h / base.w }
      : { ...base };
    const applyVB = () => svg.setAttribute('viewBox', vb.x + ' ' + vb.y + ' ' + vb.w + ' ' + vb.h);
    applyVB();

    const scale = () => base.w / vb.w;                       // 当前缩放倍率
    const viewCenter = () => ({ x: vb.x + vb.w / 2, y: vb.y + vb.h / 2 });
    // 使用真实 SVG 变换矩阵，包含 preserveAspectRatio 在工程画布中的留白。
    // viewport 和 viewBox 的宽高比不同，不能按外接矩形各自线性缩放。
    function svgPoint(ev) {
      const inverse = svg.getScreenCTM().inverse();
      return { x: inverse.a * ev.clientX + inverse.c * ev.clientY + inverse.e,
               y: inverse.b * ev.clientX + inverse.d * ev.clientY + inverse.f };
    }
    function svgDelta(dx, dy) {
      const inverse = svg.getScreenCTM().inverse();
      return { x: inverse.a * dx + inverse.c * dy,
               y: inverse.b * dx + inverse.d * dy };
    }
    // 围绕 (px,py) 缩放 k 倍（k>1 放大），倍率夹在 [ZOOM_MIN, ZOOM_MAX]
    function zoomAt(px, py, k) {
      const s = scale();
      k = Math.min(ZOOM_MAX / s, Math.max(ZOOM_MIN / s, k));
      if (!isFinite(k) || k === 1) return;
      vb.x = px - (px - vb.x) / k;   // 保持指针下的内容不动
      vb.y = py - (py - vb.y) / k;
      vb.w /= k; vb.h /= k;
      applyVB();
    }
    const zoomIn = () => { const c = viewCenter(); zoomAt(c.x, c.y, ZOOM_STEP); };
    const zoomOut = () => { const c = viewCenter(); zoomAt(c.x, c.y, 1 / ZOOM_STEP); };
    const resetView = () => { vb = { ...base }; applyVB(); };

    // ---------- 变更提交 / 撤销 / 重做（历史一致性单一入口） ----------
    /**
     * 所有变更（拖动写回、参数修改、删除、插入）的统一提交入口。
     * @param {Object} next 变更后的完整电路状态（调用方用 cloneState 构建）
     * @param {string} label 变更说明（调试用，如 'move R1' / 'insert C2'）
     */
    function commit(next, label) {
      history.commit(cloneState(next));      // 变更前状态入 undoStack，redoStack 清空
      replaceState(circuit, next);           // 整体替换，宿主页引用不变
      if (sel && !findPart(sel)) sel = null; // 选中元件随变更消失时清掉
      onChange(circuit); onRerender();
    }
    function undo() {
      const st = history.undo();
      if (!st) return false;
      // 用快照的副本替换，避免与历史栈内部引用共享（attach 时的坐标
      // 写回等就地操作不会污染历史）
      replaceState(circuit, cloneState(st));
      if (sel && !findPart(sel)) sel = null;
      onChange(circuit); onRerender();       // 宿主感知状态变化（如显示“已修改”）
      return true;
    }
    function redo() {
      const st = history.redo();
      if (!st) return false;
      replaceState(circuit, cloneState(st));
      if (sel && !findPart(sel)) sel = null;
      onChange(circuit); onRerender();
      return true;
    }

    /**
     * 插入一个新元件（不接线，初始悬空，ERC 会标记，需手动接线）。
     * @param {string} type 元件类型（symbols.js LIB 的键名，如 'resistor'）
     * @param {Object} o 可选 {left, top, id, value}；位置缺省时放当前视图中心
     * @returns {Object|null} 新元件对象；类型未知或指定位号冲突时返回 null
     */
    function insertPart(type, o) {
      const info = partInfo(type);
      if (!info) return null;                 // 前端符号库没有该类型，插不了
      o = o || {};
      const next = cloneState(circuit);
      next.parts = next.parts || [];
      next.connections = next.connections || [];
      const id = o.id != null ? String(o.id) : nextId(next.parts, type);
      if (next.parts.some(p => p && p.id === id)) return null; // 位号冲突
      // 位置：显式给出则直接用（仍吸附网格）；否则放到当前视图中心
      let left = o.left, top = o.top;
      if (left == null || top == null) {
        const c = viewCenter();
        const sz = sizeOfSym(type) || { w: 0, h: 0 };
        left = c.x - sz.w / 2;                // 让符号框中心对准视图中心
        top = c.y - sz.h / 2;
      }
      const value = 'value' in o ? o.value : info.value;
      const part = {
        type: type, id: id, rotate: 0,
        left: snap(left), top: snap(top),
        attrs: value != null && String(value) !== '' ? { value: String(value) } : {},
      };
      next.parts.push(part);
      sel = id; // 提交后自动选中（宿主重渲染时经 getSelected 恢复面板）
      commit(next, 'insert ' + id);
      return part;
    }

    // ---------- 选中高亮 + 属性面板 ----------
    let sel = null; // 当前选中的元件 id
    function clearHighlight() {
      svg.querySelectorAll('.interact-sel').forEach(el => el.parentNode.removeChild(el));
    }
    function select(id) {
      clearHighlight();
      sel = id != null && findPart(id) ? id : null;
      const g = sel && gOf(sel);
      if (g) {
        try { // 虚线选框：getBBox 是符号本地坐标，直接挂进该 g 即可
          const b = g.getBBox();
          const r = document.createElementNS(SVG_NS, 'rect');
          r.setAttribute('class', 'interact-sel');
          r.setAttribute('x', b.x - 6); r.setAttribute('y', b.y - 6);
          r.setAttribute('width', b.width + 12); r.setAttribute('height', b.height + 12);
          r.setAttribute('fill', 'none'); r.setAttribute('stroke', '#8a0d0d');
          r.setAttribute('stroke-width', '1.5'); r.setAttribute('stroke-dasharray', '6 4');
          r.setAttribute('pointer-events', 'none');
          g.appendChild(r);
        } catch (e) { /* 元素未渲染时 getBBox 会抛错，忽略 */ }
      }
      renderPanel();
    }
    const clearSelection = () => { sel = null; clearHighlight(); renderPanel(); };
    const hidePanel = () => { if (panel) panel.style.display = 'none'; };

    function mkRow(labelText, value, readonly) { // 面板一行“标签 + 输入框”
      const row = document.createElement('div'); row.className = 'row';
      const lab = document.createElement('label'); lab.textContent = labelText;
      const input = document.createElement('input');
      input.type = 'text'; input.value = value;
      if (readonly) { input.readOnly = true; input.tabIndex = -1; }
      row.appendChild(lab); row.appendChild(input);
      return row;
    }
    function renderPanel() {
      if (!panel) return;
      const keepClose = panel.querySelector('#propsClose');
      panel.innerHTML = '';
      if (keepClose) panel.appendChild(keepClose);
      const p = sel ? findPart(sel) : null;
      if (!p) { hidePanel(); return; }
      panel.style.display = 'block';
      const h = document.createElement('h3');
      h.textContent = '元件属性';
      panel.appendChild(h);
      panel.appendChild(mkRow('位号', String(p.id), true));    // 只读
      panel.appendChild(mkRow('类型', String(p.type), true));  // 只读
      const valRow = mkRow('参数值', p.attrs && p.attrs.value != null ? String(p.attrs.value) : '', false);
      valRow.querySelector('input').addEventListener('change', function () {
        const v = this.value;
        const next = cloneState(circuit);      // 走统一提交入口（可撤销）
        const np = (next.parts || []).find(q => q && q.id === p.id);
        if (!np) return;
        np.attrs = np.attrs || {};
        np.attrs.value = v;
        commit(next, 'value ' + p.id);         // 宿主重渲染后用 selectedId 恢复面板
      });
      panel.appendChild(valRow);
      // 旋转按钮：把元件绕自身顺时针转 90°（0→90→180→270→0 循环）。
      // 走线几何与引脚命中都尊重 rotate，此前只是缺一个 UI 入口。
      const rotRow = document.createElement('div');
      rotRow.className = 'row rot-row';
      const rotLab = document.createElement('label');
      rotLab.textContent = '旋转';
      const rotBtn = document.createElement('button');
      rotBtn.type = 'button'; rotBtn.className = 'rot';
      const rotNow = ((Number(p.rotate) || 0) % 360 + 360) % 360;
      rotBtn.textContent = '顺时针 90°（当前 ' + rotNow + '°）';
      rotBtn.title = '把该元件绕自身旋转 90°（快捷键 R）；旋转可撤销';
      rotBtn.addEventListener('click', () => rotatePart(p.id, 90));
      rotRow.appendChild(rotLab); rotRow.appendChild(rotBtn);
      panel.appendChild(rotRow);
      const del = document.createElement('button');
      del.className = 'danger'; del.type = 'button';
      del.textContent = '删除元件（连带导线）';
      del.addEventListener('click', () => deletePart(p.id));
      panel.appendChild(del);
      const tip = document.createElement('p');
      tip.className = 'tip';
      // 悬空提示：新插入的元件（或被删了导线的元件）当前没有接线，
      // 顺带给出连线操作指引
      const dangling = connsRelatedTo(circuit.connections, p.id).length === 0;
      const src = p.attrs && p.attrs.value_source;
      tip.textContent = (dangling ? '该元件当前悬空未接线（ERC 会标记）· '
        + '单击其引脚端点开始连线，再单击另一引脚完成 · ' : '') +
        (src ? String(src) + ' · ' : '') +
        '修改参数值后按回车 / 点击别处生效';
      panel.appendChild(tip);
    }
    /**
     * 旋转选中元件 90°（顺时针）。
     *
     * 为什么需要：rotate 字段一直被走线几何与引脚命中逻辑尊重
     * （symbols.pinWorld / render 都传 p.rotate），但此前没有任何 UI 入口
     * ——insertPart 硬编码 rotate:0，用户无法把三极管/LED 转成想要的方向。
     *
     * 实现要点：
     *  1) 只改 rotate，不动 left/top —— 元件围绕自身左上角旋转，视觉锚点稳定；
     *  2) 走 commit() 统一提交入口，故天然可撤销（与拖动/改值同栈）；
     *  3) 旋转 90° 后引脚世界坐标随之改变，已连的导线端点会由渲染层的
     *     走线几何自动吸附到新引脚位置（连线关系按引脚名保存，不受旋转影响）。
     *
     * @param {string} id   元件 id
     * @param {number} deg  旋转增量（默认 +90；负值即逆时针）
     * @returns {Object|null} 旋转后的元件（未找到或无变化时 null）
     */
    function rotatePart(id, deg) {
      const p = findPart(id);
      if (!p) return null;
      // 归一化到 [0,360) 且保持 90° 的整数倍：模型里 rotate 一直是
      // 0/90/180/270 四态，避免浮点累加出 90.00000000000001 这类脏值
      const step = (deg == null ? 90 : deg);
      const cur = Number(p.rotate) || 0;
      let next = Math.round((cur + step) / 90) * 90;
      next = ((next % 360) + 360) % 360;
      if (next === cur) return p;
      const state = cloneState(circuit);         // 走统一提交入口（可撤销）
      const np = (state.parts || []).find(q => q && q.id === id);
      if (!np) return null;
      np.rotate = next;
      sel = id;                                  // 旋转后保持选中（面板/选框跟随）
      commit(state, 'rotate ' + id);
      return np;
    }

    function deletePart(id) {      if (!findPart(id)) return;
      const related = connsRelatedTo(circuit.connections, id);
      const msg = '删除 ' + id + ' 将同时删除 ' + related.length + ' 条相关导线，确定吗？';
      if (typeof confirm === 'function' && !confirm(msg)) return;
      const next = cloneState(circuit);       // 走统一提交入口（可撤销）
      next.parts = (next.parts || []).filter(q => !q || q.id !== id);
      // 在克隆里重新取关联（对象身份与原 circuit 不同，不能跨着比）
      const rel2 = connsRelatedTo(next.connections, id);
      next.connections = (next.connections || []).filter(c => rel2.indexOf(c) < 0);
      sel = null; hidePanel();
      commit(next, 'delete ' + id);
    }

    // ---------- 引脚连线（单击引脚拾取 → 预览 → 再击落线） ----------
    // 引脚世界坐标与 render.js 走线端点同源（CircuitSymbols.pinWorld，
    // 传入元件 left/top/rotate），符号旋转后命中与视觉始终一致。
    // Node 冒烟测试无符号库全局：pinsWorldAll 返回空、nearestPin 恒 null，
    // 连线分支自然短路，不影响纯函数断言。
    let wireFrom = null, preview = null, pinMarks = null;

    function pinsWorldAll() {
      const sym = (typeof CircuitSymbols !== 'undefined') ? CircuitSymbols : null;
      if (!sym || !sym.pinWorld || !sym.pinsOf) return [];
      const out = [];
      (circuit.parts || []).forEach(p => {
        if (!p || !p.type || p.id == null) return;
        sym.pinsOf(p.type).forEach(pin => {
          let w = null;
          try {
            w = sym.pinWorld(p.type, pin, p.left || 0, p.top || 0, p.rotate || 0);
          } catch (e) { w = null; }   // 类型/引脚不在符号库：跳过该引脚
          if (w && isFinite(w.x) && isFinite(w.y))
            out.push({ id: p.id, pin: pin, x: w.x, y: w.y });
        });
      });
      return out;
    }

    // (pt, radius) 内最近的引脚；radius 为用户坐标单位
    function nearestPin(pt, radius) {
      let best = null, bestD = radius * radius;
      pinsWorldAll().forEach(w => {
        const dx = w.x - pt.x, dy = w.y - pt.y, d = dx * dx + dy * dy;
        if (d <= bestD) { bestD = d; best = w; }
      });
      return best;
    }

    // 屏幕像素半径 → 用户坐标半径（随缩放不变手感）
    function pinHitRadius() {
      const inverse = svg.getScreenCTM().inverse();
      return PIN_HIT_R_PX * Math.max(Math.hypot(inverse.a, inverse.b), Math.hypot(inverse.c, inverse.d));
    }

    function removePreview() {
      if (preview && preview.parentNode) preview.parentNode.removeChild(preview);
      preview = null;
    }

    function startWire(hit) {
      wireFrom = hit;
      svg.classList.add('wiring');            // 十字光标
      // 引脚命中可视化：全部可连端点画空心圆，降低「找不到脚」成本
      removePinMarks();
      pinMarks = document.createElementNS(SVG_NS, 'g');
      pinMarks.setAttribute('class', 'pin-mark');
      pinMarks.setAttribute('pointer-events', 'none');
      const all = pinsWorldAll();
      all.forEach(function (pt) {
        const c = document.createElementNS(SVG_NS, 'circle');
        c.setAttribute('cx', pt.x);
        c.setAttribute('cy', pt.y);
        c.setAttribute('r', pinHitRadius() * 0.85);
        pinMarks.appendChild(c);
      });
      svg.appendChild(pinMarks);
      preview = document.createElementNS(SVG_NS, 'line');
      preview.setAttribute('x1', hit.x); preview.setAttribute('y1', hit.y);
      preview.setAttribute('x2', hit.x); preview.setAttribute('y2', hit.y);
      preview.setAttribute('stroke', 'red');
      preview.setAttribute('stroke-width', '2');
      preview.setAttribute('stroke-dasharray', '6 4');
      preview.setAttribute('pointer-events', 'none');  // 不挡下方引脚命中
      svg.appendChild(preview);               // 挂顶层，虚线跟随指针
      onWireChange(true, hit.id + ':' + hit.pin);
    }

    function removePinMarks() {
      if (pinMarks && pinMarks.parentNode) pinMarks.parentNode.removeChild(pinMarks);
      pinMarks = null;
    }

    function cancelWire() {
      const was = !!wireFrom;
      wireFrom = null;
      svg.classList.remove('wiring');
      removePreview();
      removePinMarks();
      if (was) onWireChange(false, null);
    }

    /**
     * 落一条导线：走统一 commit 入口（可撤销）。同对引脚已有导线时
     * 静默忽略（不去重会堆出视觉重叠的重复线），同一引脚自环不允许。
     * @returns {boolean} 是否真的新增了一条导线
     */
    function addWire(a, b) {
      const fromRef = a.id + ':' + a.pin, toRef = b.id + ':' + b.pin;
      if (fromRef === toRef) return false;
      const next = cloneState(circuit);
      next.connections = next.connections || [];
      const pair = [fromRef, toRef].slice().sort();
      const dup = (next.connections).some(c =>
        Array.isArray(c) && c.slice(0, 2).slice().sort().join('|') === pair.join('|'));
      if (dup) return false;
      // 颜色固定 red（新导线醒目）；路径给 [] 交前端自动布线
      next.connections.push([fromRef, toRef, 'red', []]);
      commit(next, 'wire ' + fromRef + '-' + toRef);
      return true;
    }

    // ---------- 拖动元件 / 空白平移 / 单击选中（Pointer Events） ----------
    let drag = null, pan = null;

    function hitPart(el) { // 从事件目标向上找 g[data-part]
      for (let n = el; n && n !== svg; n = n.parentNode) {
        if (n.getAttribute && n.getAttribute('data-part')) return n;
      }
      return null;
    }
    function onDown(ev) {
      if (drag || pan) return;                    // 已有指针在操作（不做双指手势）
      if (ev.button !== undefined && ev.button !== 0) return; // 只响应主键/触摸/笔
      ev.preventDefault();                        // 防止选中文本、拖动滚动页面
      try { svg.setPointerCapture(ev.pointerId); } catch (e) { /* 旧浏览器忽略 */ }
      // 引脚命中优先于拖动/平移：点在引脚端点附近（屏幕约 9px）
      // 视为连线操作，其余照常拖动/平移
      const hit = nearestPin(svgPoint(ev), pinHitRadius());
      if (hit) {
        if (!wireFrom) {
          startWire(hit);                          // 第一击：拾取起点，进入连线模式
        } else if (wireFrom.id === hit.id && wireFrom.pin === hit.pin) {
          cancelWire();                            // 点回起点引脚：取消本次连线
        } else {
          addWire(wireFrom, hit);                  // 第二击：落线（可撤销）
          cancelWire();
        }
        return;                                    // 连线分支不进入拖动/平移
      }
      if (wireFrom) cancelWire();                  // 点在引脚外：退出连线模式，照常操作
      const g = hitPart(ev.target);
      if (g) {
        const tr = g.getAttribute('transform') || '';
        const t = readTranslate(g) || { x: 0, y: 0 };
        drag = {
          g, id: g.getAttribute('data-part'),
          x0: ev.clientX, y0: ev.clientY, tx: t.x, ty: t.y,
          rest: tr.replace(/^\s*translate\([^)]*\)\s*/, ''), // 保留 rotate 等后续变换
          orig: tr, moved: false,
        };
      } else {
        pan = { x0: ev.clientX, y0: ev.clientY, vx: vb.x, vy: vb.y, moved: false };
        svg.classList.add('panning');
      }
    }
    function onMove(ev) {
      if (wireFrom && preview) {
        // 连线模式：红色虚线预览跟随指针（用户坐标，随缩放/平移正确）
        const pt = svgPoint(ev);
        preview.setAttribute('x2', pt.x); preview.setAttribute('y2', pt.y);
        return;                                   // 连线中不做拖动/平移
      }
      if (drag) {
        const delta = svgDelta(ev.clientX - drag.x0, ev.clientY - drag.y0);
        if (!drag.moved && Math.hypot(ev.clientX - drag.x0, ev.clientY - drag.y0) > CLICK_PX) drag.moved = true;
        if (drag.moved) // 拖动视觉跟随：平移该元件的 g（松手后由重渲染接管）
          drag.g.setAttribute('transform', 'translate(' + (drag.tx + delta.x) + ',' + (drag.ty + delta.y) + ')' + drag.rest);
      } else if (pan) {
        const delta = svgDelta(ev.clientX - pan.x0, ev.clientY - pan.y0);
        if (!pan.moved && Math.hypot(ev.clientX - pan.x0, ev.clientY - pan.y0) > CLICK_PX) pan.moved = true;
        if (pan.moved) {
          vb.x = pan.vx - delta.x;
          vb.y = pan.vy - delta.y;
          applyVB();
        }
      }
    }
    function onUp(ev) {
      const d = drag, pn = pan;
      drag = null; pan = null; svg.classList.remove('panning');
      if (d) {
        if (!d.moved) { d.g.setAttribute('transform', d.orig); select(d.id); return; } // 单击＝选中
        // 拖动结束：吸附网格写回 circuit，交给宿主重渲染（走线自动重排）
        const delta = svgDelta(ev.clientX - d.x0, ev.clientY - d.y0);
        d.g.setAttribute('transform', d.orig); // 先复位，视觉由重渲染接管
        const p = findPart(d.id);
        if (p) {
          const nl = snap(d.tx + delta.x), nt = snap(d.ty + delta.y);
          // 位置真的变了才提交（避免吸附回原位产生空的历史记录）
          if (nl !== p.left || nt !== p.top) {
            const next = cloneState(circuit);  // 走统一提交入口（可撤销）
            const np = (next.parts || []).find(q => q && q.id === d.id);
            if (np) { np.left = nl; np.top = nt; commit(next, 'move ' + d.id); }
          }
        }
      } else if (pn && !pn.moved) {
        clearSelection(); // 单击空白处：取消选中
      }
    }
    function onCancel() { // 拖动被系统打断（如来电）：还原视觉状态
      if (drag && drag.orig != null) drag.g.setAttribute('transform', drag.orig);
      drag = null; pan = null; svg.classList.remove('panning');
    }
    function onWheel(ev) {
      ev.preventDefault(); // 阻止页面跟随滚动
      const pt = svgPoint(ev);
      zoomAt(pt.x, pt.y, ev.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP);
    }

    // ---------- 绑定 / 初始化 ----------
    function onKey(ev) {          // ESC 取消进行中的连线
      if (ev.key === 'Escape' && wireFrom) { cancelWire(); return; }
      // R：旋转选中元件 90°（与属性面板旋转按钮同一入口，同样可撤销）
      // 仅在无修饰键且选中存在时生效；输入框内打字不拦截——
      // 否则用户在「参数值」里输入含 r 的文本（如 4.7kR）会被误旋转
      if ((ev.key === 'r' || ev.key === 'R') && !ev.ctrlKey && !ev.metaKey
          && !ev.altKey && sel
          && !(ev.target && /^(INPUT|TEXTAREA|SELECT)$/.test(ev.target.tagName))) {
        ev.preventDefault();
        rotatePart(sel, 90);
      }
    }
    svg.addEventListener('pointerdown', onDown);
    svg.addEventListener('pointermove', onMove);
    svg.addEventListener('pointerup', onUp);
    svg.addEventListener('pointercancel', onCancel);
    svg.addEventListener('wheel', onWheel, { passive: false });
    document.addEventListener('keydown', onKey);
    svg.style.touchAction = 'none'; // 触屏上拖动不滚屏
    injectStyle();

    // 把渲染坐标写回 circuit.parts：首次渲染可能是 auto 布局，坐标只在
    // 渲染产物里；写回后宿主用 layout:'keep' 重渲染才不会发生位置跳变
    svg.querySelectorAll('g[data-part]').forEach(g => {
      const p = findPart(g.getAttribute('data-part'));
      const t = readTranslate(g);
      if (p && t) { p.left = t.x; p.top = t.y; }
    });

    // 历史栈：同一 circuit 对象跨重渲染共享，只在首次 attach 时记
    // 初始快照（此时坐标写回已完成，快照里已含最终布局坐标）
    if (!history) {
      history = createHistory(HISTORY_LIMIT);
      histories.set(circuit, history);
      history.init(cloneState(circuit));
    }
    if (opts.selectedId) select(opts.selectedId);

    return {
      select, clearSelection, zoomIn, zoomOut, resetView,
      getViewBox: () => ({ x: vb.x, y: vb.y, w: vb.w, h: vb.h }),
      getSelected: () => sel, // 宿主重渲染前读取，用于恢复选中状态
      undo, redo,                                     // 撤销/重做（整体替换 circuit 后重渲染）
      canUndo: () => history.canUndo(),              // 供宿主切换按钮 disabled 态
      canRedo: () => history.canRedo(),
      // 外部候选图在用户接受后作为一次普通编辑提交，保留之前的撤销历史。
      applyCircuit(nextSnap) {
        if (!history || !nextSnap) return false;
        commit(cloneState(nextSnap), 'accept AI edit');
        return true;
      },
      /**
       * 宿主在外部整体替换 circuit（AI 改图）后调用：
       * 把改图前快照注入撤销栈，使「撤回更改 / Ctrl+Z」可回到改前。
       * @param {Object} priorSnap 变更前 CircuitJSON（调用方深拷贝）
       */
      seedUndo(priorSnap) {
        if (!history || priorSnap == null) return false;
        return history.seedUndo(cloneState(priorSnap));
      },
      insertPart,                                    // 插入新元件（自动位号/选中）
      rotatePart,                                    // 旋转元件 90°（属性面板按钮 / R 快捷键）
      getRotate: () => {                             // 当前选中元件的旋转角（0/90/180/270）
        const p = sel ? findPart(sel) : null;
        return p ? ((Number(p.rotate) || 0) % 360 + 360) % 360 : null;
      },
      cancelWire,                                    // 取消进行中的连线（宿主切换电路/清空时调）
      isWiring: () => wireFrom != null,              // 连线模式中？（宿主提示用）
      detach() {
        svg.removeEventListener('pointerdown', onDown);
        svg.removeEventListener('pointermove', onMove);
        svg.removeEventListener('pointerup', onUp);
        svg.removeEventListener('pointercancel', onCancel);
        svg.removeEventListener('wheel', onWheel);
        document.removeEventListener('keydown', onKey);
        cancelWire();
        hidePanel();
      },
    };
  }

  // 浏览器：window.CircuitInteract；Node（冒烟测试）：module.exports
  return {
    attach, snap, partOfRef, connsRelatedTo,
    nextId,          // 位号分配（纯函数）
    createHistory,    // 撤销/重做历史栈（纯函数）
    partInfo,         // 元件信息（ref 前缀/中文名/默认值）
    PART_INFO,        // 内置元件信息表（与 frontend_manifest.json 同源）
  };
});
