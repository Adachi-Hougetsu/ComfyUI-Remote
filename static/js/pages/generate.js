/* 生成页 —— 完全由 schema 数据驱动渲染。
 * 数据来源：GET /api/templates/{tpl} -> {schema, values}（面板定义 + 当前参数值）
 *           GET /api/templates/{tpl}/model-lists -> {ckpt_name, lora_name, sampler_name, ...}
 * 提交：POST /api/generate {tpl, params(扁平 dict), runs, enabled_groups(对象), image_slots({key:文件名})}
 * 实时：window.WS.connect(batch_id)，事件 progress/run_done/run_error/batch_done/batch_error
 * 兜底：周期性 GET /api/generate/{batch_id}（按 prompt_id 聚合 status/progress/images/error）
 *
 * 布局（节点气泡）：每个 ComfyUI 节点 = 一个气泡（底模 / 正负提示词 / 每个 LoRA /
 * K采样器 / 尺寸 / 功能开关组 / 其它节点 / 高级参数），点击弹居中小窗口调参。
 * 扁平参数按 widget 名约定归组（steps/cfg/seed... → K采样器，width/height → 尺寸，
 * strength/start/end → ControlNet）；参考图槽位归属到各自开关组气泡
 * （无开关组的槽位回退成单个"参考图"气泡）。
 */
(function () {
  'use strict';

  window.Pages = window.Pages || {};

  const state = {
    schema: null, modelLists: null, values: null,
    loras: [], enabledGroups: {}, extraParams: {},
    controls: {}, seedMode: 'random',
    imageSlots: {}, // key -> {filename[, url]}
    runs: 1, running: false, batchId: null,
    addedFiles: new Set(), pollTimer: null, saveTimer: null,
    defs: [], defByKey: {},
    runParams: {},   // prompt_id -> 该次实际注入的参数（snapshot 提供，结果区参数按钮用）
    lastParams: null // 本次批次提交时的参数（runParams 缺失时兜底）
  };
  const els = {};

  // 跨页面批次追踪：生成中途离开页面，回来恢复进度（sessionStorage，仅本标签页）
  const BATCH_TRACK_KEY = 'comfyui-remote.active-batch';
  function saveBatchTrack() {
    try { sessionStorage.setItem(BATCH_TRACK_KEY, JSON.stringify({ batchId: state.batchId, tpl: App.currentTemplate })); } catch (e) { /* 隐私模式忽略 */ }
  }
  function clearBatchTrack() {
    try { sessionStorage.removeItem(BATCH_TRACK_KEY); } catch (e) { /* ignore */ }
  }
  function loadBatchTrack() {
    try {
      const raw = sessionStorage.getItem(BATCH_TRACK_KEY);
      if (!raw) return null;
      const o = JSON.parse(raw);
      return (o && o.batchId && o.tpl) ? o : null;
    } catch (e) { return null; }
  }

  const el = (tag, props, children) => UI.createEl(tag, props, children);
  const clampInt = (v, min, max) => { const n = parseInt(v, 10); if (isNaN(n)) return min; return Math.max(min, Math.min(max, n)); };

  // 填充下拉：当前值不在选项里时保留为一个"（已失效）"选项，不静默替换成别的
  function fillCombo(sel, opts, value) {
    const stale = value && !opts.includes(value);
    opts.forEach((o) => sel.append(el('option', { value: o, text: o })));
    if (stale) sel.append(el('option', { value: value, text: String(value) + '（已失效）' }));
    if (value && (opts.includes(value) || stale)) sel.value = value;
    else if (opts.length) sel.value = opts[0];
    else if (value) sel.value = value;
    else sel.value = '';
  }

  // 图片加载失败兜底：加占位类并停掉 src，避免显示裂图图标
  function imgBroken(ev) {
    const img = ev && ev.target;
    if (!img || img.dataset.fb) return;
    img.dataset.fb = '1';
    img.classList.add('img-broken');
    img.removeAttribute('src');
  }

  function mid(p) {
    if (p.min != null && p.max != null) return (p.min + p.max) / 2;
    return 0;
  }
  function groupLabel(key) {
    const g = (state.schema.node_groups || []).find((x) => x.key === key);
    return g ? g.label : key;
  }
  function slotImgUrl(filename) {
    const q = new URLSearchParams({ type: 'input', preview: 'webp;80' });
    const tk = App.getToken();
    if (tk) q.set('token', tk);
    return '/api/images/' + encodeURIComponent(filename) + '?' + q.toString();
  }

  /* ---------- 数据加载 ---------- */
  async function load() {
    const tpl = App.currentTemplate;
    if (!tpl) throw new Error('未选择模板');
    const data = await App.api('/api/templates/' + encodeURIComponent(tpl));
    state.schema = data.schema || {};
    state.values = data.values || {};
    const modelLists = await App.api('/api/templates/' + encodeURIComponent(tpl) + '/model-lists', { silent: true }).catch(() => ({}));
    state.modelLists = modelLists || {};
    const v = state.values;

    // 底模
    state.modelValue = (v.model != null) ? v.model : null;

    // LoRA 列表
    state.loras = (Array.isArray(v.loras) && v.loras.length)
      ? v.loras.map((l) => ({ name: l.name, strength_model: l.strength_model != null ? l.strength_model : 0.8, strength_clip: l.strength_clip != null ? l.strength_clip : 1.0 }))
      : ((state.schema.lora && state.schema.lora.default) || []).map((l) => ({ name: l.name, strength_model: l.strength_model != null ? l.strength_model : 0.8, strength_clip: l.strength_clip != null ? l.strength_clip : 1.0 }));

    // 提示词
    state.positive = (v.prompts && v.prompts.positive != null) ? v.prompts.positive : '';
    state.negative = (v.prompts && v.prompts.negative != null) ? v.prompts.negative : '';

    // 节点群开关（values.groups 是对象）
    state.enabledGroups = {};
    (state.schema.node_groups || []).forEach((g) => {
      state.enabledGroups[g.key] = (v.groups && typeof v.groups === 'object' && g.key in v.groups)
        ? !!v.groups[g.key] : !!g.default_enabled;
    });

    // 参数值（values.params 是扁平 dict）
    state.controls = {};
    state.paramByKey = {};
    (state.schema.params || []).forEach((p) => {
      state.paramByKey[p.key] = p;
      if (p.type === 'combo') { state.controls[p.key] = (v.params && v.params[p.key] != null) ? v.params[p.key] : null; return; }
      if (p.type === 'string') { state.controls[p.key] = (v.params && v.params[p.key] != null) ? v.params[p.key] : (p.default != null ? p.default : ''); return; }
      state.controls[p.key] = (v.params && v.params[p.key] != null) ? v.params[p.key] : (p.default != null ? p.default : mid(p));
    });
    state.seedMode = (v.params && v.params.seed_mode === 'fixed') ? 'fixed' : 'random';
    // 种子未保存且无显式默认值时给随机初值（seed_mode 默认 random，后端会接管）
    const seedParam = (state.schema.params || []).find((p) => p.key === 'seed');
    if (seedParam && seedParam.seed_mode && !(v.params && v.params.seed != null) && seedParam.default == null) {
      state.controls.seed = Math.floor(Math.random() * 1e9);
    }

    // 节点群 extra_params
    state.extraParams = {};
    (state.schema.node_groups || []).forEach((g) => {
      (g.extra_params || []).forEach((ep) => {
        state.extraParams[ep.key] = (v.params && v.params[ep.key] != null) ? v.params[ep.key] : (ep.default != null ? ep.default : (ep.min != null ? ep.min : 0));
      });
    });

    // 参考图槽位（values.image_slots: {key: 文件名}）
    state.imageSlots = {};
    (state.schema.image_slots || []).forEach((sl) => {
      const val = v.image_slots && v.image_slots[sl.key];
      if (typeof val === 'string' && val) state.imageSlots[sl.key] = { filename: val };
      else if (val && typeof val === 'object' && val.filename) state.imageSlots[sl.key] = { filename: val.filename, url: val.url };
    });

    state.runs = clampInt(v.runs || 1, 1, 20);
  }

  /* ---------- 面板渲染：节点气泡网格 ---------- */
  function buildShell(root) {
    state.defs = [];
    state.defByKey = {};
    const wrap = el('div', { class: 'generate' });

    // 提示条
    const bar = el('div', { class: 'bubble-bar' });
    bar.append(el('span', { class: 'bubble-hint', text: '点击气泡调参' }));
    wrap.append(bar);

    // 气泡网格（统一方块，铺满整行）
    els.flow = el('div', { class: 'bubble-flow' });
    els.flow.addEventListener('click', onFlowClick);
    wrap.append(els.flow);

    // 居中小窗口（挂在 body 上）
    buildSheet();

    // 结果
    const resultSec = el('section', { class: 'section' }, [el('h2', { class: 'section-title', text: '结果' })]);
    els.resultGrid = el('div', { class: 'result-grid' });
    els.resultEmpty = el('div', { class: 'empty', text: '还没有结果，点击下方「生成」开始' });
    resultSec.append(els.resultEmpty, els.resultGrid);
    wrap.append(resultSec);

    root.append(wrap);

    buildDefs();
    renderBubbleFlow();
  }

  // 小窗口骨架：遮罩 + 居中卡片（顶栏标题 + 完成按钮），各气泡的调参内容按需预渲染成 pane
  function buildSheet() {
    els.sheetTitle = el('div', { class: 'sheet-title' });
    els.sheetBody = el('div', { class: 'sheet-body' });
    els.sheet = el('div', { class: 'sheet' }, [
      el('div', { class: 'sheet-head' }, [
        els.sheetTitle,
        el('button', { class: 'btn btn-ghost btn-sm sheet-done', text: '完成', onclick: closeSheet })
      ]),
      els.sheetBody
    ]);
    els.sheetBackdrop = el('div', { class: 'sheet-backdrop' });
    els.sheetBackdrop.append(els.sheet);
    els.sheetBackdrop.addEventListener('click', (e) => {
      if (e.target === els.sheetBackdrop) closeSheet();
    });
    document.body.append(els.sheetBackdrop);
  }

  function openSheet(key) {
    const def = state.defByKey[key];
    if (!def || !def.paneEl) return;
    els.sheetTitle.textContent = def.title;
    Array.prototype.forEach.call(els.sheetBody.children, (p) => p.classList.remove('active'));
    def.paneEl.classList.add('active');
    els.sheetBody.scrollTop = 0;
    els.sheetBackdrop.classList.add('show');
    els.sheet.classList.add('show');
    els.sheetCurrentKey = key;
    document.body.classList.add('sheet-open');
  }

  function closeSheet() {
    if (!els.sheetBackdrop) return; // 加载失败等未建窗口场景
    els.sheetBackdrop.classList.remove('show');
    if (els.sheet) els.sheet.classList.remove('show');
    els.sheetCurrentKey = null;
    document.body.classList.remove('sheet-open');
  }

  // 点击气泡 → 打开小窗口；再点同一个气泡 → 收回（raw 类型"＋ LoRA"直接执行）
  function onFlowClick(e) {
    const b = e.target.closest('.bubble');
    if (!b) return;
    const def = state.defByKey[b.dataset.key];
    if (!def) return;
    if (def.raw) { if (def.onTap) def.onTap(); return; }
    if (els.sheet && els.sheet.classList.contains('show') && els.sheetCurrentKey === def.key) {
      closeSheet();
    } else {
      openSheet(def.key);
    }
  }

  /* ---------- 气泡定义：key -> 标题 / 摘要 / 小窗口内容 ---------- */
  // 扁平参数按 widget 名约定归组到节点气泡：KSampler / 尺寸 / ControlNet，其余进"高级参数"
  function bucketParams() {
    const miscKeys = new Set((state.schema.misc_nodes || []).flatMap((m) => m.keys || []));
    const groupKeys = new Set((state.schema.node_groups || []).flatMap((g) => (g.extra_params || []).map((ep) => ep.key)));
    const K = ['seed', 'steps', 'cfg', 'sampler_name', 'scheduler', 'denoise'];
    const L = ['width', 'height', 'batch_size'];
    const C = ['strength', 'start_percent', 'end_percent'];
    const buckets = { ksampler: [], latent: [], cn: [], other: [] };
    (state.schema.params || []).forEach((p) => {
      if (miscKeys.has(p.key) || groupKeys.has(p.key)) return; // 归「其它节点」/「开关组」
      if (K.includes(p.widget)) buckets.ksampler.push(p);
      else if (L.includes(p.widget)) buckets.latent.push(p);
      else if (C.includes(p.widget)) buckets.cn.push(p);
      else buckets.other.push(p);
    });
    return buckets;
  }
  function keyOfWidget(bucket, w) {
    const p = bucket.find((x) => x.widget === w);
    return p ? p.key : null;
  }

  function buildDefs() {
    const buckets = bucketParams();
    const ungrouped = (state.schema.image_slots || []).filter((s) => !s.group);

    function makePane() {
      const pane = el('div', { class: 'sheet-pane' });
      els.sheetBody.append(pane);
      return pane;
    }
    // pane 构建 + rerender 用于刷新模型列表后原地重画
    function withPane(def) {
      def.pane = () => { def.paneEl = makePane(); def.content(); return def.paneEl; };
      def.rerender = () => { if (!def.paneEl) return; def.paneEl.innerHTML = ''; def.content(); };
      return def;
    }
    function add(def) {
      state.defs.push(def);
      state.defByKey[def.key] = def;
    }

    // 底模：摘要显示当前底模名，小窗口含选择 + 刷新
    if (state.schema.model) {
      const def = {
        key: 'model', title: '底模',
        summary: () => {
          const v = (els.modelSelect && els.modelSelect.value) || state.modelValue;
          return [el('div', { class: 'bubble-value' + (v ? '' : ' muted'), text: v || '（未选择）' })];
        }
      };
      def.content = () => {
        const head = el('div', { class: 'sheet-pane-head' });
        const refreshBtn = el('button', {
          class: 'btn btn-ghost btn-sm', text: '刷新模型',
          title: '重读 ComfyUI 里最新的底模 / LoRA / 下拉选项（不用等自动刷新）'
        });
        refreshBtn.addEventListener('click', refreshModels);
        els.modelRefreshBtn = refreshBtn;
        head.append(refreshBtn);
        els.modelBox = el('div', { class: 'field' });
        renderModel();
        def.paneEl.append(head, els.modelBox);
      };
      add(withPane(def));
    }

    // 提示词：正 / 负 两个独立气泡，各显示首行预览
    const posDef = {
      key: 'positive', title: '正向提示词',
      summary: () => promptSummary(state.positive)
    };
    posDef.content = () => { els.positiveTa = renderPromptField('positive', '正向提示词', posDef.paneEl); };
    add(withPane(posDef));
    const negDef = {
      key: 'negative', title: '负向提示词',
      summary: () => promptSummary(state.negative)
    };
    negDef.content = () => { els.negativeTa = renderPromptField('negative', '负向提示词', negDef.paneEl); };
    add(withPane(negDef));

    // K采样器：steps/cfg/sampler_name/scheduler/denoise/seed 合成一个气泡
    if (buckets.ksampler.length) {
      const def = {
        key: 'ksampler', title: 'K采样器',
        summary: () => {
          const c = state.controls;
          const sKey = keyOfWidget(buckets.ksampler, 'steps');
          const cKey = keyOfWidget(buckets.ksampler, 'cfg');
          const smKey = keyOfWidget(buckets.ksampler, 'sampler_name');
          const parts = [];
          if (sKey && c[sKey] != null) parts.push('步数 ' + Math.round(Number(c[sKey])));
          if (cKey && c[cKey] != null) parts.push('CFG ' + Number(c[cKey]).toFixed(1));
          if (smKey && c[smKey]) parts.push(String(c[smKey]));
          if (!parts.length) return [el('div', { class: 'bubble-value muted', text: '（默认）' })];
          return [el('div', { class: 'bubble-value', text: parts.join(' · ') })];
        }
      };
      def.content = () => { buckets.ksampler.forEach((p) => renderParam(p, def.paneEl)); };
      add(withPane(def));
    }

    // 尺寸：width/height/batch_size
    if (buckets.latent.length) {
      const def = {
        key: 'latent', title: '尺寸',
        summary: () => {
          const c = state.controls;
          const wKey = keyOfWidget(buckets.latent, 'width');
          const hKey = keyOfWidget(buckets.latent, 'height');
          if (wKey && hKey && c[wKey] != null && c[hKey] != null) {
            return [el('div', { class: 'bubble-value', text: Math.round(Number(c[wKey])) + ' × ' + Math.round(Number(c[hKey])) })];
          }
          return [el('div', { class: 'bubble-value muted', text: '（默认）' })];
        }
      };
      def.content = () => { buckets.latent.forEach((p) => renderParam(p, def.paneEl)); };
      add(withPane(def));
    }

    // 每个 LoRA 独立气泡（多个 LoRA = 多个节点）
    if (state.schema.lora || state.loras.length) {
      state.loras.forEach((lora, i) => {
        const def = {
          key: 'lora:' + i, title: 'LoRA ' + (i + 1),
          summary: () => [el('div', { class: 'bubble-value', text: String(lora.name || '（未选择）') })]
        };
        def.content = () => { renderLoraSingle(i, def.paneEl); };
        add(withPane(def));
      });
      // 添加 LoRA：raw 气泡，点击直接追加
      const addDef = {
        key: 'lora-add', title: '＋ LoRA', raw: true,
        onTap: () => {
          const opts = state.modelLists.lora_name || [];
          if (!opts.length) { UI.toast('当前没有可选的 LoRA', 'error'); return; }
          state.loras.push({ name: opts[0], strength_model: 0.8, strength_clip: 1.0 });
          rebuildDefs();
          scheduleSave();
        }
      };
      add(addDef);
    }

    // 参考图：仅"无开关组归属"的槽位回退成一个气泡（有开关组的槽位见下方开关组）
    if (ungrouped.length) {
      const def = {
        key: 'slots', title: '参考图',
        summary: () => {
          const uploaded = ungrouped.filter((sl) => state.imageSlots[sl.key]);
          if (!uploaded.length) return [el('div', { class: 'bubble-value muted', text: '（未上传）' })];
          const row = el('div', { class: 'bubble-slots' });
          uploaded.forEach((slot) => {
            const cur = state.imageSlots[slot.key];
            if (cur) row.append(el('img', { class: 'slot-mini', src: cur.url || slotImgUrl(cur.filename), draggable: 'false', onerror: imgBroken }));
          });
          return [row];
        }
      };
      def.content = () => {
        const box = el('div', { class: 'slot-grid slot-box', 'data-slot-filter': '*' });
        renderSlotCards(ungrouped, box);
        def.paneEl.append(box);
      };
      add(withPane(def));
    }

    // 每个功能开关组一个气泡：开关 + 额外参数 + 该组关联的参考图槽位
    (state.schema.node_groups || []).forEach((g) => {
      const def = {
        key: 'group:' + g.key, title: g.label,
        summary: () => {
          const on = !!state.enabledGroups[g.key];
          return [el('div', { class: 'bubble-status' }, [
            el('span', { class: 'bubble-dot' + (on ? ' on' : '') }),
            el('span', { text: on ? '开' : '关' })
          ])];
        }
      };
      def.content = () => {
        const card = el('div', { class: 'card group-card' });
        renderGroupCard(g, card);
        def.paneEl.append(card);
      };
      add(withPane(def));
    });

    // 每个 misc 节点一个气泡，小窗口内为该节点的参数卡片
    (state.schema.misc_nodes || []).forEach((m, i) => {
      const def = {
        key: 'misc:' + i, title: m.title || ('节点 ' + m.node),
        summary: () => [el('div', { class: 'bubble-value', text: String((m.keys || []).length) + ' 项参数' })]
      };
      def.content = () => { renderMiscNode(m, def.paneEl); };
      add(withPane(def));
    });

    // ControlNet 强度参数（strength/start_percent/end_percent）
    if (buckets.cn.length) {
      const def = {
        key: 'cn', title: 'ControlNet',
        summary: () => {
          const c = state.controls;
          const sKey = keyOfWidget(buckets.cn, 'strength');
          if (sKey && c[sKey] != null) {
            return [el('div', { class: 'bubble-value', text: '强度 ' + Number(c[sKey]).toFixed(2) })];
          }
          return [el('div', { class: 'bubble-value muted', text: String(buckets.cn.length) + ' 项参数' })];
        }
      };
      def.content = () => { buckets.cn.forEach((p) => renderParam(p, def.paneEl)); };
      add(withPane(def));
    }

    // 高级参数：归不进以上节点的扁平参数合成一个气泡，避免每个参数一个气泡
    if (buckets.other.length) {
      const def = {
        key: 'other', title: '高级参数',
        summary: () => [el('div', { class: 'bubble-value muted', text: String(buckets.other.length) + ' 项参数' })]
      };
      def.content = () => { buckets.other.forEach((p) => renderParam(p, def.paneEl)); };
      add(withPane(def));
    }

    // 小窗口内容全部预渲染（collectFlatParams 依赖这些控件常驻 DOM，即使窗口未打开过）
    state.defs.forEach((d) => { if (d.pane) d.pane(); });
  }

  // LoRA 数量变化（增/删）时重建全部气泡（def 的 key 带序号，必须整体重建）
  function rebuildDefs() {
    state.defs = [];
    state.defByKey = {};
    if (els.sheetBody) els.sheetBody.innerHTML = '';
    buildDefs();
    renderBubbleFlow();
    renderAllSlotBoxes();
  }

  function promptSummary(text) {
    const t = (text || '').trim();
    if (!t) return [el('div', { class: 'bubble-value muted', text: '（空）' })];
    return [el('div', { class: 'bubble-value', text: t.split('\n')[0] })];
  }

  /* ---------- 气泡网格渲染（固定顺序，无拖拽） ---------- */
  function makeBubbleEl(key) {
    const def = state.defByKey[key];
    if (!def) return null;
    if (def.raw) {
      const b = el('div', { class: 'bubble bubble-add', role: 'button', tabindex: '0', 'aria-label': def.title });
      b.dataset.key = key;
      b.append(el('span', { text: def.title }));
      b.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); if (def.onTap) def.onTap(); } });
      return b;
    }
    const b = el('div', { class: 'bubble', role: 'button', tabindex: '0', 'aria-label': def.title + '，点击展开调节' });
    b.dataset.key = key;
    b.append(el('div', { class: 'bubble-title', text: def.title }), el('div', { class: 'bubble-summary' }));
    b.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openSheet(key); }
    });
    fillSummary(b, def);
    return b;
  }

  function fillSummary(b, def) {
    const sum = b.querySelector('.bubble-summary');
    if (!sum) return;
    sum.innerHTML = '';
    def.summary().forEach((n) => sum.append(n));
  }

  // 控件值变化后同步气泡摘要（只改文本/缩略图，不重建 DOM，安全）
  function refreshBubbles() {
    if (!els.flow) return;
    els.flow.querySelectorAll('.bubble').forEach((b) => {
      const def = state.defByKey[b.dataset.key];
      if (def && !def.raw) fillSummary(b, def);
    });
  }

  function renderBubbleFlow() {
    const flow = els.flow;
    if (!flow) return;
    flow.innerHTML = '';
    const max = (state.schema.lora && state.schema.lora.max != null) ? state.schema.lora.max : 8;
    state.defs.forEach((def) => {
      if (def.key === 'lora-add' && (!state.schema.lora || state.loras.length >= max)) return;
      const b = makeBubbleEl(def.key);
      if (b) flow.append(b);
    });
  }

  /* ---------- 各渲染器（小窗口内容复用） ---------- */
  // 槽位卡片：按归属（开关组 / 未分组）渲染到指定容器
  function renderSlotCards(slots, box) {
    if (!box) return;
    box.innerHTML = '';
    slots.forEach((slot) => {
      const card = el('div', { class: 'card slot-card' });
      const head = el('div', { class: 'slot-header' });
      const title = el('div', { class: 'slot-title' });
      title.append(el('span', { text: slot.label }));
      if (slot.group) title.append(el('span', { class: 'slot-group', text: groupLabel(slot.group) }));
      head.append(title);

      const cur = state.imageSlots[slot.key];
      if (cur) {
        head.append(el('button', {
          class: 'btn btn-ghost btn-sm', text: '还原',
          title: '恢复为工作流里的默认参考图',
          onclick: () => { delete state.imageSlots[slot.key]; renderAllSlotBoxes(); scheduleSave(); }
        }));
      } else {
        const fi = el('input', { type: 'file', accept: 'image/*', style: { display: 'none' } });
        fi.addEventListener('change', () => {
          const f = fi.files && fi.files[0];
          if (f) uploadSlot(slot, f);
          fi.value = '';
        });
        head.append(el('button', { class: 'btn btn-primary btn-sm', text: '上传', onclick: () => fi.click() }));
      }

      const body = el('div', { class: 'slot-body' });
      if (cur) {
        body.append(el('img', { class: 'slot-thumb', src: cur.url || slotImgUrl(cur.filename), loading: 'lazy', onerror: imgBroken }));
      } else {
        body.append(el('div', { class: 'slot-placeholder', text: '未上传' }));
      }
      // 启用的组关联槽位为空时提示上传
      if (slot.group && !cur) {
        body.append(el('div', { class: 'slot-hint warn', text: '已启用「' + groupLabel(slot.group) + '」，请上传' + slot.label }));
      }
      card.append(head, body);
      box.append(card);
    });
  }

  // 重画页面上所有槽位容器（.slot-box，data-slot-filter 标记归属）
  function renderAllSlotBoxes() {
    document.querySelectorAll('.slot-box').forEach((box) => {
      const f = box.dataset.slotFilter;
      const all = state.schema.image_slots || [];
      const slots = f === '*' ? all.filter((s) => !s.group) : all.filter((s) => s.group === f);
      renderSlotCards(slots, box);
    });
  }

  async function uploadSlot(slot, file) {
    const fd = new FormData();
    fd.append('file', file);
    fd.append('key', slot.key);
    try {
      const res = await App.api('/api/upload', { method: 'POST', body: fd });
      state.imageSlots[slot.key] = { filename: res.filename };
      UI.toast('上传成功', 'success');
    } catch (err) {
      UI.toast('上传失败', 'error');
    }
    renderAllSlotBoxes();
    scheduleSave();
  }

  function renderModel() {
    const box = els.modelBox;
    if (!box) return;
    box.innerHTML = '';
    box.append(el('label', { class: 'field-label', text: state.schema.model.label || '底模' }));
    const opts = state.modelLists.ckpt_name || [];
    const sel = el('select', { class: 'select' });
    if (!opts.length) sel.append(el('option', { value: '', text: '（无可用底模）' }));
    opts.forEach((o) => sel.append(el('option', { value: o, text: o })));
    if (opts.includes(state.modelValue)) sel.value = state.modelValue;
    else if (opts.length) sel.value = opts[0];
    sel.addEventListener('change', () => scheduleSave());
    box.append(sel);
    UI.enhanceSelect(sel);
    els.modelSelect = sel;
  }

  // 手动刷新模型下拉：强制后端重读 object_info（?refresh=1），原地重渲染相关区域。
  // 值都存在 state 里（controls/loras/modelValue），重渲染不会丢，只有 ComfyUI 里新放的模型会补进来。
  async function refreshModels() {
    const btn = els.modelRefreshBtn;
    if (!btn) return;
    btn.disabled = true;
    const orig = btn.textContent;
    btn.textContent = '刷新中…';
    try {
      const tpl = App.currentTemplate;
      if (!tpl) throw new Error('未选择模板');
      const fresh = await App.api('/api/templates/' + encodeURIComponent(tpl) + '/model-lists?refresh=1', { silent: true });
      if (fresh && typeof fresh === 'object') state.modelLists = fresh;
      state.defs.forEach((d) => { if (typeof d.rerender === 'function') d.rerender(); });
      refreshBubbles();
      renderAllSlotBoxes();
      UI.toast('模型列表已刷新', 'success');
    } catch (err) {
      UI.toast('刷新失败：' + (err && err.message ? err.message : '网络错误'), 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = orig;
    }
  }

  // 单个 LoRA 编辑：下拉选模型 + 两个强度滑杆 + 删除
  function renderLoraSingle(idx, container) {
    const lora = state.loras[idx];
    if (!lora) return;
    const opts = state.modelLists.lora_name || [];
    const field = el('div', { class: 'field' });
    field.append(el('label', { class: 'field-label', text: '模型' }));
    const sel = el('select', { class: 'select' });
    if (!opts.length) sel.append(el('option', { value: '', text: '（无可用 LoRA）' }));
    fillCombo(sel, opts, lora.name);
    sel.addEventListener('change', () => { lora.name = sel.value; scheduleSave(); });
    field.append(sel);
    UI.enhanceSelect(sel);
    container.append(field);
    container.append(UI.slider({
      label: 'model 强度', min: 0, max: 2, step: 0.05, value: lora.strength_model,
      format: (v) => Number(v).toFixed(2),
      onInput: (v) => { lora.strength_model = Number(v); scheduleSave(); }
    }));
    container.append(UI.slider({
      label: 'clip 强度', min: 0, max: 2, step: 0.05, value: lora.strength_clip,
      format: (v) => Number(v).toFixed(2),
      onInput: (v) => { lora.strength_clip = Number(v); scheduleSave(); }
    }));
    container.append(el('button', {
      class: 'btn btn-ghost btn-sm del', text: '删除此 LoRA',
      onclick: () => { state.loras.splice(idx, 1); rebuildDefs(); scheduleSave(); }
    }));
  }

  // 提示词输入框；返回 textarea 供参数收集（控件常驻窗口 DOM）
  function renderPromptField(key, label, container) {
    const field = el('div', { class: 'field' });
    field.append(el('label', { class: 'field-label', text: label }));
    const ta = el('textarea', { class: 'textarea', rows: key === 'positive' ? 4 : 3, placeholder: label });
    const declared = state.schema.prompts && state.schema.prompts[key];
    ta.value = key === 'positive' ? state.positive : state.negative;
    // 工作流里没有该侧提示词节点：禁用输入，避免编辑被静默丢弃（识别引擎没找到）
    ta.disabled = !declared;
    if (!declared) ta.placeholder = '（工作流中未识别到该提示词）';
    ta.addEventListener('input', () => {
      if (key === 'positive') state.positive = ta.value;
      else state.negative = ta.value;
      scheduleSave();
    });
    field.append(ta);
    container.append(field);
    return ta;
  }

  // 通用参数渲染：combo（下拉）/ string（文本框）/ seed（数字+开关）/ 数值（滑杆）
  function renderParam(p, container) {
    if (p.type === 'combo') {
      const field = el('div', { class: 'field' });
      field.append(el('label', { class: 'field-label', text: p.label || p.key }));
      // 下拉选项按 widget 名查 model-lists（后端按 widget 建键），回退 key / schema 内 options
      const opts = state.modelLists[p.widget] || state.modelLists[p.key] || p.options || [];
      const sel = el('select', { class: 'select' });
      if (!opts.length) sel.append(el('option', { value: '', text: '（无选项）' }));
      fillCombo(sel, opts, state.controls[p.key]);
      sel.addEventListener('change', () => { state.controls[p.key] = sel.value; scheduleSave(); });
      field.append(sel);
      UI.enhanceSelect(sel);
      if (state.controls[p.key] == null) state.controls[p.key] = sel.value;
      container.append(field);
    } else if (p.type === 'string') {
      const field = el('div', { class: 'field' });
      field.append(el('label', { class: 'field-label', text: p.label || p.key }));
      const ta = el('textarea', { class: 'textarea misc-text', rows: 2 });
      ta.value = (state.controls[p.key] != null) ? state.controls[p.key] : (p.default != null ? p.default : '');
      ta.addEventListener('input', () => { state.controls[p.key] = ta.value; scheduleSave(); });
      field.append(ta);
      container.append(field);
    } else if (p.key === 'seed' && p.seed_mode) {
      const field = el('div', { class: 'field' });
      field.append(el('label', { class: 'field-label', text: p.label || '种子' }));
      const row = el('div', { class: 'seed-row' });
      const num = el('input', { type: 'number', class: 'input seed-input', inputmode: 'numeric' });
      num.value = state.controls.seed;
      num.addEventListener('input', () => { state.controls.seed = num.value; scheduleSave(); });
      const sw = UI.switch({
        label: '随机种子',
        checked: state.seedMode === 'random',
        onChange: (v) => { state.seedMode = v ? 'random' : 'fixed'; scheduleSave(); }
      });
      row.append(num, sw);
      field.append(row);
      container.append(field);
    } else {
      const min = p.min != null ? p.min : 0;
      const max = p.max != null ? p.max : 100;
      const step = p.step != null ? p.step : (p.type === 'float' ? 0.1 : 1);
      const cur = (state.controls[p.key] != null) ? state.controls[p.key] : (p.default != null ? p.default : mid(p));
      const fmt = p.type === 'int' ? (v) => String(Math.round(v)) : (v) => Number(v).toFixed(2);
      container.append(UI.slider({
        label: p.label || p.key, min: min, max: max, step: step, value: Number(cur), format: fmt,
        onInput: (v) => { state.controls[p.key] = Number(v); scheduleSave(); }
      }));
    }
  }

  // 单个功能开关组卡片（开关 + 启用时的额外参数 + 该组关联的参考图槽位）
  function renderGroupCard(g, card) {
    const sw = UI.switch({
      label: g.label,
      checked: state.enabledGroups[g.key],
      onChange: (v) => {
        state.enabledGroups[g.key] = v;
        renderGroupExtras(g, card);
        renderGroupSlots(g, card);
        scheduleSave();
      }
    });
    card.append(sw);
    renderGroupExtras(g, card);
    renderGroupSlots(g, card);
  }

  function renderGroupExtras(g, card) {
    card.querySelectorAll('.group-extra').forEach((n) => n.remove());
    if (!state.enabledGroups[g.key]) return;
    (g.extra_params || []).forEach((ep) => {
      if (ep.type === 'combo') {
        const field = el('div', { class: 'field group-extra' });
        field.append(el('label', { class: 'field-label', text: ep.label || ep.key }));
        const opts = state.modelLists[ep.widget] || ep.options || [];
        const sel = el('select', { class: 'select' });
        if (!opts.length) sel.append(el('option', { value: '', text: '（无选项）' }));
        opts.forEach((o) => sel.append(el('option', { value: o, text: o })));
        const cur = state.extraParams[ep.key];
        if (opts.includes(cur)) sel.value = cur;
        else if (opts.length) sel.value = opts[0];
        sel.addEventListener('change', () => { state.extraParams[ep.key] = sel.value; scheduleSave(); });
        if (state.extraParams[ep.key] == null) state.extraParams[ep.key] = sel.value;
        field.append(sel);
        UI.enhanceSelect(sel);
        card.append(field);
        return;
      }
      const cur = state.extraParams[ep.key] != null ? state.extraParams[ep.key] : (ep.default != null ? ep.default : (ep.min != null ? ep.min : 0));
      const sl = UI.slider({
        label: ep.label || ep.key,
        min: ep.min != null ? ep.min : 0,
        max: ep.max != null ? ep.max : 1,
        step: ep.step != null ? ep.step : 0.05,
        value: Number(cur),
        format: (v) => Number(v).toFixed(2),
        onInput: (v) => { state.extraParams[ep.key] = Number(v); scheduleSave(); }
      });
      sl.classList.add('group-extra');
      card.append(sl);
    });
  }

  // 组内参考图槽位：仅在总开关开启时显示（关闭即整组回收；已传图保留在 state，重开自动回来）
  function renderGroupSlots(g, card) {
    card.querySelectorAll('.group-slots').forEach((n) => n.remove());
    if (!state.enabledGroups[g.key]) return;
    const slots = (state.schema.image_slots || []).filter((s) => s.group === g.key);
    if (!slots.length) return;
    const wrap = el('div', { class: 'group-slots' });
    wrap.append(el('div', { class: 'slot-group-title', text: '参考图' }));
    const box = el('div', { class: 'slot-grid slot-box', 'data-slot-filter': g.key });
    renderSlotCards(slots, box);
    wrap.append(box);
    card.append(wrap);
  }

  // 其它节点：每个节点一个折叠卡片，内部复用 renderParam 渲染其参数
  function renderMiscNode(m, container) {
    const details = el('details', { class: 'misc-node' });
    details.append(el('summary', { class: 'misc-node-title', text: m.title || ('节点 ' + m.node) }));
    const body = el('div', { class: 'misc-node-body' });
    (m.keys || []).forEach((k) => {
      const p = state.paramByKey[k];
      if (p) renderParam(p, body);
    });
    details.append(body);
    container.append(details);
  }

  /* ---------- 参数收集 ---------- */
  // 扁平 params dict（POST /api/generate 使用，含 model/loras/提示词）
  function collectFlatParams() {
    const p = {};
    if (els.modelSelect) p.model = els.modelSelect.value;
    p.loras = state.loras.map((l) => ({ name: l.name, strength_model: Number(l.strength_model), strength_clip: Number(l.strength_clip) }));
    p.positive = els.positiveTa ? els.positiveTa.value : state.positive || '';
    p.negative = els.negativeTa ? els.negativeTa.value : state.negative || '';
    (state.schema.params || []).forEach((prm) => {
      if (prm.key === 'seed' && prm.seed_mode) {
        p.seed = Number(state.controls.seed) || 0;
        p.seed_mode = state.seedMode;
      } else {
        p[prm.key] = state.controls[prm.key];
      }
    });
    for (const [k, v] of Object.entries(state.extraParams)) p[k] = v;
    return p;
  }
  // 已上传参考图: {key: 文件名}
  // 收集全部已上传槽位（含隐藏），关节点群不清掉引用——重新开启时图还在
  function collectImageSlots() {
    const imgs = {};
    (state.schema.image_slots || []).forEach((sl) => {
      if (state.imageSlots[sl.key]) imgs[sl.key] = state.imageSlots[sl.key].filename;
    });
    return imgs;
  }
  // PUT /params 的结构化 body：{model, loras, prompts, params, groups, image_slots}
  function collectPanelState() {
    const flat = collectFlatParams();
    return {
      model: flat.model != null ? flat.model : null,
      loras: flat.loras,
      runs: state.runs,
      prompts: { positive: flat.positive, negative: flat.negative },
      params: (() => {
        const prm = {};
        (state.schema.params || []).forEach((p) => {
          if (p.key === 'seed' && p.seed_mode) { prm.seed = flat.seed; prm.seed_mode = flat.seed_mode; }
          else prm[p.key] = flat[p.key];
        });
        for (const [k, v] of Object.entries(state.extraParams)) prm[k] = v;
        return prm;
      })(),
      groups: Object.assign({}, state.enabledGroups),
      image_slots: collectImageSlots()
    };
  }

  function scheduleSave() {
    refreshBubbles(); // 气泡摘要即时跟随控件变化
    clearTimeout(state.saveTimer);
    state.saveTimer = setTimeout(() => {
      App.api('/api/templates/' + encodeURIComponent(App.currentTemplate) + '/params', {
        method: 'PUT', body: JSON.stringify(collectPanelState()), silent: true
      }).catch(() => UI.toast('参数保存失败，改动未持久化', 'error'));
    }, 600);
  }

  /* ---------- 底部操作条 ---------- */
  function setupBottomBar() {
    els.runsMinus = document.getElementById('runs-minus');
    els.runsPlus = document.getElementById('runs-plus');
    els.runsInput = document.getElementById('runs-input');
    els.genBtn = document.getElementById('gen-btn');
    els.cancelBtn = document.getElementById('cancel-btn');
    els.progressWrap = document.getElementById('progress-wrap');
    els.progressBar = document.getElementById('progress-bar');
    els.progressLabel = document.getElementById('progress-label');
    els.wsStatus = document.getElementById('ws-status');
    els.runsInput.value = state.runs;
    els.runsMinus.onclick = () => { state.runs = clampInt(state.runs - 1, 1, 20); els.runsInput.value = state.runs; scheduleSave(); };
    els.runsPlus.onclick = () => { state.runs = clampInt(state.runs + 1, 1, 20); els.runsInput.value = state.runs; scheduleSave(); };
    els.runsInput.oninput = () => { state.runs = clampInt(els.runsInput.value, 1, 20); els.runsInput.value = state.runs; scheduleSave(); };
    els.genBtn.onclick = startGenerate;
    els.cancelBtn.onclick = cancelGenerate;
  }

  function setRunning(running) {
    state.running = running;
    els.genBtn.disabled = running;
    els.genBtn.textContent = running ? '生成中…' : '生成';
    els.cancelBtn.style.display = running ? '' : 'none';
    els.progressWrap.style.display = running ? '' : 'none';
    if (running) setProgress(0, null);
  }
  function setProgress(pct, queueRemaining) {
    if (!els.progressBar || !els.progressLabel) return;
    if (pct != null) {
      const p = Math.max(0, Math.min(100, Math.round(pct || 0)));
      els.progressBar.style.width = p + '%';
      els.progressLabel.textContent = '进度 ' + p + '%' + (queueRemaining != null ? ' · 队列剩余 ' + queueRemaining : '');
    } else if (queueRemaining != null) {
      // 只更新队列剩余（queue 事件），不移动进度条
      const cur = (els.progressBar.style.width || '0%');
      els.progressLabel.textContent = '进度 ' + cur + (queueRemaining != null ? ' · 队列剩余 ' + queueRemaining : '');
    }
  }

  // WS 连接状态点：connected 绿 / 断连黄（轮询兜底中）/ 未知灰
  function setWSIndicator(connected) {
    if (!els.wsStatus) return;
    els.wsStatus.className = 'ws-status ' + (connected === true ? 'ok' : connected === false ? 'warn' : '');
  }

  async function startGenerate() {
    if (state.running) return;
    setRunning(true);
    clearResults();
    state.runParams = {};
    state.lastParams = collectFlatParams();
    const body = {
      tpl: App.currentTemplate,
      params: collectFlatParams(),
      runs: state.runs,
      enabled_groups: Object.assign({}, state.enabledGroups),
      image_slots: collectImageSlots()
    };
    try {
      const res = await App.api('/api/generate', { method: 'POST', body: JSON.stringify(body) });
      state.batchId = res.batch_id;
      state.addedFiles = new Set();
      WS.connect(state.batchId);
      saveBatchTrack();
      pollStatus();
      startPollTimer();
    } catch (err) {
      setRunning(false);
    }
  }

  async function cancelGenerate() {
    if (!state.batchId) return;
    try {
      await App.api('/api/generate/' + encodeURIComponent(state.batchId), { method: 'DELETE' });
      UI.toast('已取消');
    } catch (err) {
      UI.toast('取消失败', 'error');
    }
    stopPollTimer();
    WS.disconnect(state.batchId);
    clearBatchTrack();
    setRunning(false);
  }

  function clearResults() {
    state.addedFiles = new Set();
    els.resultGrid.innerHTML = '';
    els.resultEmpty.style.display = '';
  }

  /* ---------- 轮询兜底（GET /api/generate/{batch_id}，按 prompt_id 聚合） ---------- */
  async function pollStatus() {
    if (!state.batchId || !state.running) return;
    try {
      const st = await App.api('/api/generate/' + encodeURIComponent(state.batchId), { silent: true });
      // 进度聚合：sum(value)/sum(max)
      const pr = st.progress || {};
      let tv = 0, tm = 0;
      Object.values(pr).forEach((arr) => { if (Array.isArray(arr)) { tv += (arr[0] || 0); tm += (arr[1] || 0); } });
      if (tm > 0) setProgress(tv / tm * 100, st.queue_remaining);
      // 每 prompt 实际注入参数（含真实种子）
      if (st.run_params) state.runParams = st.run_params;
      // 图片
      const imgs = st.images || {};
      Object.entries(imgs).forEach(([pid, arr]) => (arr || []).forEach((it) => addResultImage(it, pid)));
      // 错误 / 完成
      const errs = st.error || {};
      if (Object.keys(errs).length) {
        stopPollTimer();
        setRunning(false);
        UI.toast('批次出错：' + Object.values(errs)[0], 'error');
        return;
      }
      if (st.done) {
        stopPollTimer();
        setRunning(false);
        setProgress(100, st.queue_remaining);
        UI.toast('生成完成', 'success');
      }
    } catch (err) { /* 轮询失败静默，等下次 */ }
  }
  function startPollTimer() {
    stopPollTimer();
    state.pollTimer = setInterval(pollStatus, 4000);
  }
  function stopPollTimer() {
    if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  }

  /* ---------- WS 事件 ----------
   * 后端推送统一为 { type, data }，WS.emit(msg.type, msg) 把整个 msg 交给 handler。
   * 因此 handler 一律从 msg.data.* 读取载荷。 */
  function isMine(d) { return !state.batchId || !d.batch_id || d.batch_id === state.batchId; }

  // progress 载荷 { batch_id, prompt_id, value, max, queue_remaining }：value/max 聚合为百分比
  function onProgress(msg) {
    const d = msg.data || msg;
    if (!isMine(d)) return;
    let pct = d.value != null && d.max ? Math.round((d.value / d.max) * 100) : d.value;
    setProgress(pct, d.queue_remaining != null ? d.queue_remaining : null);
  }
  // 载荷 { batch_id, prompt_id, images:[url], progress:pct(0-100), queue_remaining }
  function onRunDone(msg) {
    const d = msg.data || msg;
    if (!isMine(d)) return;
    addResultImage(d, d.prompt_id);
    if (typeof d.progress === 'number') {
      const p = d.progress <= 1 ? d.progress * 100 : d.progress;
      setProgress(p, d.queue_remaining);
    }
  }
  // 载荷 { batch_id, prompt_id, message, error }
  function onRunError(msg) {
    const d = msg.data || msg;
    if (!isMine(d)) return;
    const idx = d.prompt_id ? 'prompt ' + d.prompt_id : (d.index != null ? '第 ' + (d.index + 1) + ' 个运行' : '运行');
    UI.toast(idx + '出错：' + (d.error || d.message || '未知错误'), 'error');
    setProgress(null, d.queue_remaining);
  }
  // 载荷 { batch_id, n, status, error }
  function onBatchDone(msg) {
    const d = msg.data || msg;
    if (!isMine(d)) return;
    stopPollTimer();
    WS.disconnect(state.batchId);
    clearBatchTrack();
    state.batchId = null;
    setRunning(false);
    setProgress(100, null);
    // 取消触发的 batch_done 带 error，提示"已取消"而非"生成完成"
    const err = d.error || {};
    const cancelled = Object.values(err).some((e) => typeof e === 'string' && e.indexOf('取消') !== -1);
    UI.toast(cancelled ? '已取消' : '生成完成', cancelled ? 'info' : 'success');
  }
  // 载荷 { batch_id, message, error }
  function onBatchError(msg) {
    const d = msg.data || msg;
    if (!isMine(d)) return;
    stopPollTimer();
    WS.disconnect(state.batchId);
    clearBatchTrack();
    state.batchId = null;
    setRunning(false);
    UI.toast('批次出错：' + (d.error || d.message || '未知错误'), 'error');
  }
  // 载荷 { batch_id, queue_remaining }：只更新队列剩余，不移动进度条
  function onQueue(msg) {
    const d = msg.data || msg;
    if (!isMine(d)) return;
    setProgress(null, d.queue_remaining != null ? d.queue_remaining : null);
  }
  // WS 本地状态事件 { connected }：驱动底部状态点
  function onWSStatus(msg) {
    const st = msg && typeof msg === 'object' ? msg : { connected: !!msg };
    setWSIndicator(st.connected);
  }

  function filenameFromUrl(url) {
    try { return decodeURIComponent(new URL(url, location.origin).pathname.split('/').pop()); }
    catch (err) { return String(url); }
  }
  function buildImgUrl(it) {
    const q = new URLSearchParams();
    if (it.subfolder) q.set('subfolder', it.subfolder);
    if (it.type) q.set('type', it.type);
    q.set('preview', 'webp;80');
    const tk = App.getToken();
    if (tk) q.set('token', tk);
    return '/api/images/' + encodeURIComponent(it.filename) + '?' + q.toString();
  }
  // 触发原图下载（?download=1 后端返回原文件附件）
  function downloadFile(url) {
    const a = document.createElement('a');
    a.href = url + (url.indexOf('?') >= 0 ? '&' : '?') + 'download=1';
    a.download = '';
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  // 结果图参数弹窗：显示该次生成实际注入的参数（含真实种子）
  function showRunParams(pid) {
    const params = (pid && state.runParams[pid]) || state.lastParams || {};
    const strip = { positive: 1, negative: 1 };
    const labels = {
      model: '底模', seed: '种子', seed_mode: '种子模式', steps: '步数', cfg: 'CFG',
      sampler_name: '采样器', scheduler: '调度器', denoise: '降噪',
      width: '宽度', height: '高度', batch_size: '批量', loras: 'LoRA'
    };
    const body = el('div', { class: 'gallery-params' });
    const rows = Object.keys(params).filter((k) => !strip[k]).map((k) => {
      let v = params[k];
      let txt;
      if (k === 'loras') {
        txt = (Array.isArray(v) && v.length)
          ? v.map((l) => String((l && l.name) || '?')
              + ((l && l.strength_model != null) ? ' (M:' + Number(l.strength_model).toFixed(2) + ')' : '')
              + ((l && l.strength_clip != null) ? ' C:' + Number(l.strength_clip).toFixed(2) + ')' : ''))
            .join('\n')
          : '无';
      } else if (k === 'seed_mode') {
        txt = v === 'fixed' ? '固定' : '随机';
      } else if (typeof v === 'number') {
        txt = Number.isInteger(v) ? String(v) : Number(v).toFixed(4);
      } else {
        txt = String(v == null ? '' : v);
      }
      const row = el('div', { class: 'gallery-param-row' });
      row.append(el('span', { class: 'gallery-param-key', text: labels[k] || k }));
      row.append(el('span', { class: 'gallery-param-val', text: txt }));
      return row;
    });
    if (!rows.length) body.append(el('div', { class: 'status-note', text: '（无参数记录）' }));
    rows.forEach((r) => body.append(r));
    UI.modal({ title: '本次生成参数', body: body, actions: [{ text: '知道了', class: 'btn-primary' }] });
  }

  // 兼容两种来源：WS run_done -> {images:[url,...]}；轮询 -> {filename, subfolder, type}
  function addResultImage(data, pid) {
    const srcs = [];
    if (Array.isArray(data.images) && data.images.length) srcs.push(...data.images);
    else if (data.filename) srcs.push(buildImgUrl(data));
    srcs.forEach((src) => {
      const fn = filenameFromUrl(src);
      if (!fn || state.addedFiles.has(fn)) return;
      state.addedFiles.add(fn);
      const item = el('figure', { class: 'result-item' });
      item.append(el('img', { class: 'result-img', src: src, loading: 'lazy', onclick: () => UI.lightbox(src), onerror: imgBroken }));
      item.append(el('figcaption', { class: 'result-index', text: String(state.addedFiles.size) }));
      item.append(el('button', {
        class: 'btn btn-ghost btn-sm result-info', text: '参数', title: '查看本次生成参数',
        onclick: (ev) => { ev.stopPropagation(); showRunParams(pid); }
      }));
      item.append(el('button', {
        class: 'btn btn-ghost btn-sm result-dl', text: '保存', title: '保存原图',
        onclick: (ev) => { ev.stopPropagation(); downloadFile(src); }
      }));
      els.resultEmpty.style.display = 'none';
      els.resultGrid.prepend(item);
    });
  }

  /* ---------- 生命周期 ---------- */
  function registerWS() {
    WS.on('progress', onProgress);
    WS.on('run_done', onRunDone);
    WS.on('run_error', onRunError);
    WS.on('batch_done', onBatchDone);
    WS.on('batch_error', onBatchError);
    WS.on('queue', onQueue);
    WS.on('status', onWSStatus);
  }
  function unregisterWS() {
    WS.off('progress', onProgress);
    WS.off('run_done', onRunDone);
    WS.off('run_error', onRunError);
    WS.off('batch_done', onBatchDone);
    WS.off('batch_error', onBatchError);
    WS.off('queue', onQueue);
    WS.off('status', onWSStatus);
    if (state.batchId) WS.disconnect(state.batchId);
    // 不置空 batchId：切走再回来时 restoreBatch 优先用它恢复（sessionStorage 可能不可用）
    stopPollTimer();
  }

  // 恢复上次生成中断的批次（页面切走又回来的场景）
  // 注意：state.running 已为 true 也必须恢复——unmount 时 UI 被重置（按钮/进度条），
  // 回来必须重新挂上进度 UI 并继续轮询；因此不能把 running 当作短路条件。
  async function restoreBatch() {
    const track = loadBatchTrack();
    const bid = state.batchId || (track && track.batchId);
    if (!bid || (track && track.tpl !== App.currentTemplate)) return;
    try {
      const st = await App.api('/api/generate/' + encodeURIComponent(bid), { silent: true });
      if (st.done) {
        clearBatchTrack();  // 后台已完成，直接清追踪并复位 UI
        state.batchId = null;
        state.running = false;
        setRunning(false);
        return;
      }
      state.batchId = bid;
      state.addedFiles = new Set();
      state.running = true;
      setRunning(true);
      if (st.run_params) state.runParams = st.run_params;
      // 已出的图补回来
      Object.entries(st.images || {}).forEach(([pid, arr]) => (arr || []).forEach((it) => addResultImage(it, pid)));
      const errs = st.error || {};
      if (Object.keys(errs).length) UI.toast('批次有出错：' + Object.values(errs)[0], 'error');
      WS.connect(state.batchId);
      pollStatus();
      startPollTimer();
      UI.toast('已恢复生成进度', 'info');
    } catch (err) {
      clearBatchTrack();  // 批次不存在（可能服务重启清掉了）
      state.running = false;
      setRunning(false);
    }
  }

  async function mount(root) {
    root.innerHTML = '';
    root.append(UI.spinner());
    try {
      await load();
    } catch (err) {
      console.error('[generate] load', err);
      root.innerHTML = '';
      root.append(el('div', { class: 'empty', text: '模板加载失败，请检查后端服务与模板配置' }));
      return;
    }
    root.innerHTML = '';
    buildShell(root);
    setupBottomBar();
    registerWS();
    restoreBatch();
  }

  function unmount() {
    if (els.flow) els.flow.removeEventListener('click', onFlowClick);
    closeSheet(); // 清 body.sheet-open 锁滚动（小窗口打开时切页）
    if (els.sheetBackdrop) { els.sheetBackdrop.remove(); els.sheetBackdrop = null; els.sheet = null; }
    unregisterWS();
    if (els.genBtn) { els.genBtn.disabled = false; els.genBtn.textContent = '生成'; }
    if (els.cancelBtn) els.cancelBtn.style.display = 'none';
    if (els.progressWrap) els.progressWrap.style.display = 'none';
    if (els.runsInput) els.runsInput.value = state.runs;
  }

  window.Pages.generate = { mount: mount, unmount: unmount };
})();
