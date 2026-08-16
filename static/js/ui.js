/* UI 工具库：纯 DOM，无框架。暴露 window.UI。
 * createEl / toast / modal / confirmDialog / spinner / slider / switch / lightbox
 */
(function () {
  'use strict';

  window.UI = (function () {

    function appendChild(node, child) {
      if (child == null) return;
      if (Array.isArray(child)) {
        child.forEach((c) => appendChild(node, c));
      } else if (typeof child === 'string' || typeof child === 'number') {
        node.appendChild(document.createTextNode(String(child)));
      } else {
        node.appendChild(child);
      }
    }

    function createEl(tag, props, children) {
      const node = document.createElement(tag);
      if (props) {
        for (const [k, v] of Object.entries(props)) {
          if (v == null) continue;
          if (k === 'class' || k === 'className') node.className = v;
          else if (k === 'text') node.textContent = v;
          else if (k === 'html') node.innerHTML = v;
          else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
          else if (k.startsWith('on') && typeof v === 'function') {
            node.addEventListener(k.slice(2).toLowerCase(), v);
          } else if (k === 'dataset') Object.assign(node.dataset, v);
          else if (k === 'value') node.value = v;
          else if (k === 'checked') node.checked = !!v;
          else if (k === 'disabled') node.disabled = !!v;
          else if (k === 'src') node.src = v;
          else if (k === 'href') node.href = v;
          else node.setAttribute(k, v);
        }
      }
      appendChild(node, children);
      return node;
    }

    function toast(msg, type, duration) {
      const root = document.getElementById('toast-root');
      if (!root) return;
      const cls = 'toast' + (type ? ' toast-' + type : '');
      const t = createEl('div', { class: cls, text: msg });
      root.appendChild(t);
      requestAnimationFrame(() => t.classList.add('show'));
      setTimeout(() => {
        t.classList.remove('show');
        setTimeout(() => t.remove(), 300);
      }, duration || 2600);
    }

    function modal(opts) {
      opts = opts || {};
      const root = document.getElementById('modal-root');
      if (!root) return null;
      const overlay = createEl('div', { class: 'modal-overlay' });
      const box = createEl('div', { class: 'modal' });
      if (opts.title) box.append(createEl('div', { class: 'modal-title', text: opts.title }));
      if (opts.body) box.append(opts.body);
      if (opts.actions && opts.actions.length) {
        const footer = createEl('div', { class: 'modal-actions' });
        opts.actions.forEach((a) => {
          const b = createEl('button', {
            class: 'btn ' + (a.class || 'btn-primary'),
            text: a.text,
            onclick: () => {
              close();
              if (typeof a.onClick === 'function') a.onClick();
            }
          });
          footer.append(b);
        });
        box.append(footer);
      }
      overlay.append(box);
      if (opts.dismissable !== false) {
        overlay.addEventListener('click', (e) => {
          if (e.target === overlay) close();
        });
      }
      root.appendChild(overlay);
      function close() { overlay.remove(); }
      return { close: close, el: overlay };
    }

    function confirmDialog(msg) {
      return new Promise((resolve) => {
        let done = false;
        const finish = (v) => { if (!done) { done = true; resolve(v); } };
        const m = modal({
          title: '确认',
          body: createEl('div', { class: 'modal-msg', text: msg }),
          dismissable: true,
          actions: [
            { text: '取消', class: 'btn-ghost', onClick: () => finish(false) },
            { text: '确定', class: 'btn-danger', onClick: () => finish(true) }
          ]
        });
        if (!m) return finish(false);
        m.el.addEventListener('click', (e) => {
          if (e.target === m.el) finish(false);
        });
      });
    }

    function spinner() {
      return createEl('div', { class: 'spinner-wrap' }, [
        createEl('div', { class: 'spinner' }),
        createEl('div', { class: 'spinner-text', text: '加载中…' })
      ]);
    }

    function slider(opts) {
      opts = opts || {};
      const min = opts.min != null ? opts.min : 0;
      const max = opts.max != null ? opts.max : 100;
      const step = opts.step != null ? opts.step : 1;
      const value = opts.value != null ? opts.value : (min + max) / 2;
      const wrap = createEl('div', { class: 'slider' });
      const head = createEl('div', { class: 'slider-head' });
      if (opts.label) head.append(createEl('span', { class: 'slider-label', text: opts.label }));
      const valEl = createEl('span', { class: 'slider-value' });
      head.append(valEl);
      const range = createEl('input', {
        type: 'range', class: 'slider-input',
        min: min, max: max, step: step, value: value
      });
      const fmt = (v) => {
        const n = Number(v);
        if (typeof opts.format === 'function') return opts.format(n);
        return Number.isInteger(n) ? String(n) : n.toFixed(2);
      };
      valEl.textContent = fmt(range.value);
      range.addEventListener('input', () => {
        valEl.textContent = fmt(range.value);
        if (typeof opts.onInput === 'function') opts.onInput(Number(range.value));
      });
      wrap.append(head, range);
      return wrap;
    }

    function switchEl(opts) {
      opts = opts || {};
      const wrap = createEl('label', { class: 'switch-row' });
      const input = createEl('input', {
        type: 'checkbox', class: 'switch-input', checked: !!opts.checked
      });
      const track = createEl('span', { class: 'switch' });
      const txt = createEl('span', { class: 'switch-label' });
      if (opts.label) txt.append(opts.label);
      if (opts.hint) txt.append(createEl('span', { class: 'switch-hint', text: opts.hint }));
      input.addEventListener('change', () => {
        if (typeof opts.onChange === 'function') opts.onChange(input.checked);
      });
      wrap.append(input, track, txt);
      return wrap;
    }

    function lightbox(src, alt) {
      const root = document.getElementById('modal-root');
      if (!root) return null;
      const overlay = createEl('div', { class: 'lightbox' });
      const img = createEl('img', { src: src, alt: alt || '', draggable: 'false' });
      const closeBtn = createEl('button', {
        class: 'lightbox-close', text: '✕', 'aria-label': '关闭',
        onclick: () => overlay.remove()
      });
      overlay.append(img, closeBtn);
      overlay.addEventListener('click', (e) => {
        if (e.target === overlay || e.target === img) overlay.remove();
      });
      root.appendChild(overlay);
      return { close: () => overlay.remove() };
    }

    /* ---------- 平板/宽屏 select → 弹窗选择器（手机保持原生弹窗） ---------- */
    const PICKER_MIN_WIDTH = 700;

    function openPicker(sel, trigger) {
      const root = document.getElementById('modal-root');
      if (!root) return;
      const overlay = createEl('div', { class: 'picker-backdrop' });
      const box = createEl('div', { class: 'picker' });
      const head = createEl('div', { class: 'picker-head' });
      head.append(createEl('div', {
        class: 'picker-title',
        text: sel.getAttribute('aria-label') || '选择'
      }));
      head.append(createEl('button', {
        class: 'btn btn-ghost btn-sm', text: '完成',
        onclick: () => close()
      }));
      box.append(head);
      const list = createEl('div', { class: 'picker-list' });
      Array.prototype.forEach.call(sel.options, (o) => {
        const row = createEl('button', {
          class: 'picker-option' + (o.selected ? ' active' : ''),
          type: 'button', text: o.text,
          onclick: () => {
            sel.value = o.value;
            sel.dispatchEvent(new Event('change', { bubbles: true }));
            close();
          }
        });
        list.append(row);
      });
      box.append(list);
      overlay.append(box);
      overlay.addEventListener('click', (e) => {
        if (e.target === overlay) close();
      });
      root.appendChild(overlay);
      requestAnimationFrame(() => overlay.classList.add('show'));
      function close() {
        overlay.classList.remove('show');
        setTimeout(() => overlay.remove(), 260);
      }
    }

    function enhanceSelect(sel) {
      // 仅宽屏（平板/桌面）启用：原生 select 隐藏，点击弹出 Picker。
      // 手机/窄屏保持原生控件（iOS/Android 自带选择弹窗，体验更好）。
      if (!sel || sel.dataset.enhanced) return sel;
      if (!sel.isConnected) return sel; // 未挂载到文档：跳过（挂载后需重新调用）
      if (!window.matchMedia('(min-width: ' + PICKER_MIN_WIDTH + 'px)').matches) return sel;
      sel.dataset.enhanced = '1';
      sel.classList.add('select-hidden');
      const trigger = createEl('button', {
        class: 'select select-trigger', type: 'button',
        'aria-label': sel.getAttribute('aria-label') || '选择'
      });
      const syncText = () => {
        const o = sel.options[sel.selectedIndex];
        trigger.textContent = o ? o.text : '';
      };
      syncText();
      // options 被重建（如刷新模型列表）时同步触发器文本
      try {
        new MutationObserver(syncText).observe(sel, { childList: true, subtree: true });
      } catch (e) { /* 老浏览器忽略 */ }
      sel.addEventListener('change', syncText);
      trigger.addEventListener('click', () => openPicker(sel, trigger));
      sel.insertAdjacentElement('afterend', trigger);
      return sel;
    }

    return {
      createEl: createEl, toast: toast, modal: modal, confirmDialog: confirmDialog,
      spinner: spinner, slider: slider, switch: switchEl, lightbox: lightbox,
      enhanceSelect: enhanceSelect
    };
  })();
})();
