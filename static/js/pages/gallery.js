/* 图库页：GET /api/gallery?limit=50 网格 + 大图 modal + 复制 URL + 加载更多。
 * 加载更多使用 offset 游标（?limit=50&offset=N）；后端不支持 offset 时靠去重兜底不重复渲染。
 * v2：顶部模板筛选（chips）+ 每张图的生成参数详情。
 */
(function () {
  'use strict';

  window.Pages = window.Pages || {};

  const PAGE = 50;
  let rootEl = null;
  let offset = 0;
  let seen = null; // Set<batch_id:filename>
  let done = false;
  let loading = false;
  let filterTpl = ''; // '' = 全部

  function imgSrc(it) {
    if (it.url) return it.url;
    const q = new URLSearchParams();
    if (it.subfolder) q.set('subfolder', it.subfolder);
    if (it.type) q.set('type', it.type);
    q.set('preview', 'webp;80');
    const tk = App.getToken();
    if (tk) q.set('token', tk);
    return '/api/images/' + encodeURIComponent(it.filename || '') + '?' + q.toString();
  }

  /* ---------- 参数格式化 ---------- */
  const PARAM_LABELS = {
    model: '底模', seed: '种子', seed_mode: '种子模式', steps: '步数', cfg: 'CFG',
    sampler_name: '采样器', scheduler: '调度器', denoise: '降噪',
    width: '宽度', height: '高度', batch_size: '批量', loras: 'LoRA'
  };

  function fmtParamValue(key, v) {
    if (key === 'seed' && v == null) return '随机';
    if (key === 'seed_mode') return v === 'fixed' ? '固定' : '随机';
    if (key === 'loras') {
      if (!Array.isArray(v) || !v.length) return '无';
      return v.map((l) => {
        const name = String((l && l.name) || '?');
        const m = l && l.strength_model != null ? Number(l.strength_model).toFixed(2) : null;
        const c = l && l.strength_clip != null ? Number(l.strength_clip).toFixed(2) : null;
        return name + (m != null ? ' (M:' + m + (c != null ? ' C:' + c : '') + ')' : '');
      }).join('\n');
    }
    if (typeof v === 'number') return Number.isInteger(v) ? String(v) : Number(v).toFixed(4);
    return String(v == null ? '' : v);
  }

  function fmtTime(ts) {
    try {
      const d = new Date(ts * 1000);
      return d.toLocaleString('zh-CN', { hour12: false });
    } catch (e) { return ''; }
  }

  function showParams(entry) {
    const body = UI.createEl('div', { class: 'gallery-params' });
    const params = (entry && entry.params) || {};
    const rows = Object.keys(params).map((k) => {
      const v = fmtParamValue(k, params[k]);
      const row = UI.createEl('div', { class: 'gallery-param-row' });
      row.append(UI.createEl('span', { class: 'gallery-param-key', text: PARAM_LABELS[k] || k }));
      row.append(UI.createEl('span', { class: 'gallery-param-val', text: v }));
      return row;
    });
    if (!rows.length) body.append(UI.createEl('div', { class: 'status-note', text: '（无参数记录）' }));
    rows.forEach((r) => body.append(r));
    if (entry && entry.time) {
      body.append(UI.createEl('div', { class: 'gallery-param-time', text: fmtTime(entry.time) }));
    }
    UI.modal({ title: '生成参数', body: body, actions: [{ text: '知道了', class: 'btn-primary' }] });
  }

  /* ---------- 模板筛选 ---------- */
  function buildFilterBar(root) {
    const bar = UI.createEl('div', { class: 'gallery-filter' });
    const opts = [{ id: '', label: '全部' }].concat(
      (App.templates || []).map((t) => ({ id: t.id, label: t.title }))
    );
    opts.forEach((o) => {
      const chip = UI.createEl('button', {
        class: 'gallery-chip' + (filterTpl === o.id ? ' active' : ''),
        text: o.label,
        onclick: () => {
          if (filterTpl === o.id) return;
          filterTpl = o.id;
          bar.querySelectorAll('.gallery-chip').forEach((c) => c.classList.remove('active'));
          chip.classList.add('active');
          reset();
          loadMore();
        }
      });
      bar.append(chip);
    });
    root.append(bar);
  }

  async function loadMore() {
    if (loading || done) return;
    loading = true;
    const btn = rootEl ? rootEl.querySelector('#load-more-btn') : null;
    if (btn) { btn.disabled = true; btn.textContent = '加载中…'; }
    try {
      const q = 'limit=' + PAGE + '&offset=' + offset + (filterTpl ? '&tpl=' + encodeURIComponent(filterTpl) : '');
      const list = await App.api('/api/gallery?' + q);
      const grid = rootEl.querySelector('#gallery-grid');
      let added = 0;
      (list || []).forEach((batch) => {
        (batch.images || []).forEach((it) => {
          const key = batch.batch_id + ':' + (it.filename || '');
          if (seen.has(key)) return;
          seen.add(key);
          added++;
          const src = imgSrc(it);
          const item = UI.createEl('div', { class: 'gallery-item' });
          item.append(UI.createEl('img', {
            class: 'gallery-img', src: src, loading: 'lazy',
            onerror: (ev) => {
              const img = ev && ev.target;
              if (!img || img.dataset.fb) return;
              img.dataset.fb = '1';
              img.classList.add('img-broken');
              img.removeAttribute('src');
            }
          }));
          item.append(UI.createEl('button', {
            class: 'btn btn-ghost btn-sm gallery-copy', text: '复制', title: '复制图片链接',
            onclick: async (e) => {
              e.stopPropagation();
              try { await navigator.clipboard.writeText(src); UI.toast('已复制链接', 'success'); }
              catch (err) { UI.toast('复制失败', 'error'); }
            }
          }));
          item.append(UI.createEl('button', {
            class: 'btn btn-ghost btn-sm gallery-dl', text: '保存', title: '保存原图',
            onclick: (e) => {
              e.stopPropagation();
              const a = UI.createEl('a');
              a.href = src + (src.indexOf('?') >= 0 ? '&' : '?') + 'download=1';
              a.download = '';
              document.body.appendChild(a);
              a.click();
              a.remove();
            }
          }));
          item.append(UI.createEl('button', {
            class: 'btn btn-ghost btn-sm gallery-info', text: '参数', title: '查看该次生成参数',
            onclick: (e) => {
              e.stopPropagation();
              showParams(batch);
            }
          }));
          item.addEventListener('click', () => UI.lightbox(src));
          grid.append(item);
        });
      });
      offset += (list || []).length;
      if (!(list || []).length || (list || []).length < PAGE || added === 0) done = true;
      if (done) { if (btn) btn.style.display = 'none'; }
      const empty = rootEl.querySelector('#gallery-empty');
      if (empty) empty.style.display = seen.size ? 'none' : '';
    } catch (err) { /* App.api 已 toast */ }
    loading = false;
    if (btn) { btn.disabled = false; btn.textContent = '加载更多'; }
  }

  function reset() {
    offset = 0;
    done = false;
    if (seen) seen.clear();
    const grid = rootEl ? rootEl.querySelector('#gallery-grid') : null;
    if (grid) grid.innerHTML = '';
    const empty = rootEl ? rootEl.querySelector('#gallery-empty') : null;
    if (empty) empty.style.display = '';
    const btn = rootEl ? rootEl.querySelector('#load-more-btn') : null;
    if (btn) btn.style.display = '';
  }

  async function mount(root) {
    rootEl = root;
    root.innerHTML = '';
    offset = 0;
    seen = new Set();
    done = false;
    buildFilterBar(root);
    const grid = UI.createEl('div', { id: 'gallery-grid', class: 'gallery-grid' });
    const empty = UI.createEl('div', { id: 'gallery-empty', class: 'empty', text: '暂无图库记录' });
    const btn = UI.createEl('button', { id: 'load-more-btn', class: 'btn btn-ghost load-more', text: '加载更多' });
    btn.addEventListener('click', loadMore);
    root.append(grid, empty, btn);
    await loadMore();
  }

  function unmount() {
    rootEl = null;
    seen = null;
  }

  window.Pages.gallery = { mount: mount, unmount: unmount };
})();
