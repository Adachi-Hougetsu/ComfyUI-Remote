/* 设置页：GET /api/health 显示连接状态、ComfyUI 地址（只读）、SW 缓存清理。 */
(function () {
  'use strict';

  window.Pages = window.Pages || {};

  const el = (tag, props, children) => UI.createEl(tag, props, children);
  let rootEl = null;

  function statusDot(ok) {
    return el('span', { class: 'status-dot ' + (ok ? 'ok' : 'bad') });
  }

  async function render() {
    const root = rootEl;
    if (!root) return;
    root.innerHTML = '';
    root.append(UI.spinner());

    // 访问令牌已停用：局域网访问由 Tailscale-only 防火墙保护（详见 README 安全章节）。
    // 如未来需要，可启用环境变量 ACCESS_TOKEN（config.py）。此处不再展示令牌 UI。

    // 外观卡片：跟随系统 / 浅色 / 深色（立即生效并持久化；不依赖后端，先创建）
    const themeCard = el('div', { class: 'card' });
    themeCard.append(el('div', { class: 'section-title', text: '外观' }));
    const curTheme = App.currentTheme();
    const themeOpts = [
      { key: '', label: '跟随系统' },
      { key: 'light', label: '浅色' },
      { key: 'dark', label: '深色' }
    ];
    const seg = el('div', { class: 'theme-seg' });
    themeOpts.forEach((o) => {
      const b = el('button', {
        class: 'theme-seg-btn' + (curTheme === o.key ? ' active' : ''),
        text: o.label,
        onclick: () => {
          App.applyTheme(o.key);
          seg.querySelectorAll('.theme-seg-btn').forEach((x) => x.classList.remove('active'));
          b.classList.add('active');
          UI.toast('外观已切换', 'success');
        }
      });
      seg.append(b);
    });
    themeCard.append(seg);
    themeCard.append(el('div', { class: 'status-note', text: '浅色 / 深色立即生效；「跟随系统」随设备外观自动切换。' }));

    let health = null;
    try {
      health = await App.api('/api/health');
    } catch (err) {
      root.innerHTML = '';
      root.append(tokenCard);
      root.append(themeCard);
      root.append(el('div', {
        class: 'empty',
        text: '无法连接控制层（令牌错误或后端未启动），请检查后重试'
      }));
      return;
    }
    root.innerHTML = '';
    root.append(tokenCard);
    root.append(themeCard);

    // 连接状态
    const card1 = el('div', { class: 'card' });
    card1.append(el('div', { class: 'section-title', text: '连接状态' }));
    const ok = !!health.ok;
    const comfyOk = !!(health.comfy && health.comfy.ok);
    const row1 = el('div', { class: 'status-row' });
    row1.append(statusDot(ok), el('span', { text: ok ? '控制层正常' : '控制层异常' }));
    const row2 = el('div', { class: 'status-row' });
    row2.append(statusDot(comfyOk), el('span', { text: comfyOk ? 'ComfyUI 已连接' : 'ComfyUI 不可达' }));
    card1.append(row1, row2);
    if (health.comfy && health.comfy.queue_remaining != null) {
      card1.append(el('div', { class: 'status-note', text: 'ComfyUI 队列剩余任务：' + health.comfy.queue_remaining }));
    }
    root.append(card1);

    // ComfyUI 地址（可编辑，保存后热重连，免改代码重启）
    const card2 = el('div', { class: 'card' });
    card2.append(el('div', { class: 'section-title', text: 'ComfyUI 地址' }));
    const addr = (health.comfy && health.comfy.url) || health.comfy_url || '';
    const field = el('div', { class: 'settings-field' });
    field.append(el('div', { class: 'field-label', text: '后端连接地址' }));
    const addrInput = el('input', { class: 'input', value: addr, inputmode: 'url', placeholder: 'http://127.0.0.1:8188' });
    field.append(addrInput);
    const addrSave = el('button', { class: 'btn btn-primary', text: '保存并重连' });
    addrSave.addEventListener('click', async () => {
      const v = addrInput.value.trim();
      if (!v) { UI.toast('请输入地址', 'error'); return; }
      try {
        const r = await App.api('/api/settings', {
          method: 'PUT', body: JSON.stringify({ comfy_url: v })
        });
        UI.toast('已保存并重连（' + r.comfy_url + '）', 'success');
        render();
      } catch (err) { /* App.api 已提示 */ }
    });
    card2.append(field);
    card2.append(el('div', { class: 'settings-actions' }, [addrSave]));
    card2.append(el('div', {
      class: 'status-note',
      text: '保存后立即重连（免重启）；ComfyUI 地址通常为 http://127.0.0.1:8188。'
    }));
    root.append(card2);

    // 缺失输入文件
    const missing = health.missing_images || [];
    if (missing.length) {
      const card3 = el('div', { class: 'card' });
      card3.append(el('div', { class: 'section-title', text: '缺失输入文件' }));
      missing.forEach((m) => card3.append(el('div', { class: 'status-note warn', text: '· ' + m })));
      card3.append(el('div', { class: 'status-note', text: '请在生成页上传对应参考图后重试。' }));
      root.append(card3);
    }

    // 应用缓存
    const card4 = el('div', { class: 'card' });
    card4.append(el('div', { class: 'section-title', text: '应用缓存' }));
    card4.append(el('div', {
      class: 'status-note',
      text: '离线使用依赖 Service Worker 缓存。更新版本后若界面异常，可清理缓存强制刷新。'
    }));
    // 当前 SW 缓存版本（用于确认是否已加载最新前端）
    const verNote = el('div', { class: 'status-note', text: '缓存版本：…' });
    card4.append(verNote);
    (async () => {
      try {
        const keys = await caches.keys();
        const v = keys.filter((k) => k.indexOf('comfyui-remote-') === 0).sort().pop();
        verNote.textContent = '缓存版本：' + (v ? v.replace('comfyui-remote-', '') : '无');
      } catch (err) {
        verNote.textContent = '缓存版本：不可读';
      }
    })();
    const swBtn = el('button', { class: 'btn btn-danger', text: '清理缓存并重载' });
    swBtn.addEventListener('click', async () => {
      if (!(await UI.confirmDialog('确定清理 PWA 缓存并刷新页面？'))) return;
      try {
        if (navigator.serviceWorker) {
          const regs = await navigator.serviceWorker.getRegistrations();
          await Promise.all(regs.map((r) => r.unregister()));
        }
        if (window.caches) {
          const keys = await caches.keys();
          await Promise.all(keys.map((k) => caches.delete(k)));
        }
        UI.toast('缓存已清理', 'success');
        setTimeout(() => location.reload(), 800);
      } catch (err) {
        UI.toast('清理失败', 'error');
      }
    });
    card4.append(swBtn);
    root.append(card4);

    // 刷新
    const refresh = el('button', { class: 'btn btn-ghost', text: '刷新状态' });
    refresh.addEventListener('click', render);
    root.append(el('div', { class: 'settings-actions' }, [refresh]));

    await renderImport(root);
  }

  /* ---------- 工作流导入 ---------- */

  function wfBadge(wf) {
    if (wf.registered) return el('span', { class: 'wf-badge imported', text: '已导入' });
    if (wf.has_schema) return el('span', { class: 'wf-badge', text: '有 schema' });
    return null;
  }

  async function renderImport(root) {
    const card = el('div', { class: 'card' });
    card.append(el('div', { class: 'section-title', text: '工作流（自动同步）' }));
    card.append(el('div', {
      class: 'status-note',
      text: '电脑 ComfyUI 里的工作流在服务启动时自动同步到手机端，无需手动导入。' +
        '「重新导入」在工作流改动后刷新面板；「删除」从手机端移除。'
    }));

    let list = el('div', { class: 'empty', text: '加载中…' });
    card.append(list);

    async function load() {
      let data;
      try {
        data = await App.api('/api/workflows');
      } catch (err) {
        list.replaceWith(el('div', { class: 'status-note warn', text: '扫描工作流目录失败（控制层未启动？）' }));
        return;
      }
      const box = el('div', {});
      (data.workflows || []).forEach((wf) => box.append(buildRow(wf)));
      if (!(data.workflows || []).length) {
        box.append(el('div', { class: 'status-note', text: '来源目录没有 workflow（' + (data.directory || '') + '）' }));
      }
      list.replaceWith(box);
    }

    function buildRow(wf) {
      const row = el('div', { class: 'wf-row' });
      const main = el('div', { class: 'wf-main' });
      const title = el('div', { class: 'wf-title', text: wf.title || wf.filename });
      title.append(wfBadge(wf));
      main.append(title);
      const meta = el('div', {
        class: 'wf-meta',
        text: (wf.samplers ? wf.samplers + ' 采样器 · ' : '无采样器 · ') +
          (wf.description || '') + ' · ' + wf.filename
      });
      main.append(meta);
      row.append(main);

      const actions = el('div', { class: 'wf-actions' });
      if (wf.registered) {
        actions.append(btn('重新导入', 'btn-sm', async () => {
          const ok = await UI.confirmDialog(
            '重新导入「' + wf.title + '」会重新生成面板定义，可能覆盖手工微调。继续？');
          if (!ok) return;
          await doImport(wf, true);
        }));
        actions.append(btn('删除', 'btn-sm btn-danger', async () => {
          const ok = await UI.confirmDialog('删除模板「' + wf.title + '」？将同时移除面板与工作流副本。');
          if (!ok) return;
          try {
            await App.api('/api/templates/' + encodeURIComponent(wf.id), { method: 'DELETE' });
            UI.toast('已删除', 'success');
            App.refreshTemplates();
            await load();
          } catch (err) { /* App.api 已提示 */ }
        }));
      } else {
        actions.append(btn('导入', 'btn-sm btn-primary', async () => {
          await doImport(wf, false);
        }));
      }
      row.append(actions);
      return row;
    }

    function btn(text, cls, onClick) {
      const b = el('button', { class: 'btn ' + cls, text: text });
      b.addEventListener('click', onClick);
      return b;
    }

    async function doImport(wf, overwrite) {
      try {
        const r = await App.api('/api/templates/import', {
          method: 'POST',
          body: JSON.stringify({ filename: wf.filename, overwrite: overwrite })
        });
        UI.toast('已导入「' + (r.title || wf.title) + '」', 'success');
        showImportResult(r);
        App.refreshTemplates();
        await load();
      } catch (err) { /* App.api 已 toast 错误 */ }
    }

    // 导入结果对话框：识别出的组件开关 + 跳过的群组框及原因（后端 group_notes）
    function showImportResult(r) {
      const body = el('div', { class: 'import-result' });
      const groups = r.groups || [];
      const notes = r.group_notes || [];
      body.append(el('div', { class: 'modal-msg', text: groups.length
        ? ('识别出 ' + groups.length + ' 个组件开关：')
        : '未识别出任何组件开关——在 ComfyUI 里把组件框成 group 后重新导入。' }));
      groups.forEach((g) => {
        const modeTag = g.mode === 'bypass' ? '旁路' : '补线';
        body.append(el('div', { class: 'import-group-row', children: [
          el('span', { class: 'import-group-name', text: g.label }),
          el('span', { class: 'import-group-mode', text: modeTag }),
        ] }));
      });
      const skipped = notes.filter((n) => n.indexOf('跳过') >= 0);
      if (skipped.length) {
        body.append(el('div', { class: 'modal-msg import-skip-title', text: '跳过的群组框：' }));
        skipped.forEach((n) => body.append(el('div', { class: 'import-skip', text: n })));
      }
      UI.modal({ title: '导入结果', body: body, actions: [{ text: '知道了', class: 'btn-primary' }] });
    }

    card.append(el('div', { class: 'settings-actions' }, [
      el('button', { class: 'btn btn-ghost', text: '刷新列表', onClick: load })
    ]));

    root.append(card);
    await load();
  }

  async function mount(root) {
    rootEl = root;
    await render();
  }

  function unmount() {
    rootEl = null;
  }

  window.Pages.settings = { mount: mount, unmount: unmount };
})();
