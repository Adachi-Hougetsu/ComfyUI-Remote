/* 应用外壳：hash 路由 + fetch 封装 + 模板状态 + boot。
 * 页面模块在 window.Pages.{generate,gallery,settings} 上注册 {mount(root), unmount()}。
 * 脚本加载顺序：ui.js -> ws.js -> app.js -> pages/*.js（boot 在 DOMContentLoaded 时执行）
 */
(function () {
  'use strict';

  window.App = (function () {
    const ROUTES = {};
    const TITLES = {
      '/generate': '生成 · ComfyUI 遥控',
      '/gallery': '图库 · ComfyUI 遥控',
      '/settings': '设置 · ComfyUI 遥控'
    };
    let appEl = null;
    let currentRoute = null;
    let currentPage = null;
    let _tpl = null;

    /* ---------- 访问令牌（可选鉴权） ---------- */
    const TOKEN_KEY = 'comfyui-remote.token';
    const THEME_KEY = 'comfyui-remote.theme';
    function getToken() {
      try { return localStorage.getItem(TOKEN_KEY) || ''; } catch (e) { return ''; }
    }
    function setToken(v) {
      try { localStorage.setItem(TOKEN_KEY, (v || '').trim()); } catch (e) { /* ignore */ }
      return getToken();
    }

    /* ---------- 外观（跟随系统 / 浅色 / 深色） ---------- */
    function applyTheme(theme) {
      const el = document.documentElement;
      if (theme === 'light' || theme === 'dark') el.dataset.theme = theme;
      else delete el.dataset.theme;
      try { localStorage.setItem(THEME_KEY, theme || ''); } catch (e) { /* ignore */ }
    }
    function currentTheme() {
      try { return localStorage.getItem(THEME_KEY) || ''; } catch (e) { return ''; }
    }

    /* ---------- fetch 封装 ---------- */
    async function api(path, opts) {
      opts = opts || {};
      const init = { method: opts.method || 'GET', headers: {} };
      if (opts.body != null) {
        init.body = opts.body;
        if (!(opts.body instanceof FormData)) init.headers['Content-Type'] = 'application/json';
      }
      if (opts.headers) Object.assign(init.headers, opts.headers);
      const token = getToken();
      if (token) init.headers['X-Access-Token'] = token;
      const res = await fetch(path, init);
      if (res.status === 401 && token) {
        // 令牌被拒：可能后端换了令牌，清掉让用户重填
        setToken('');
      }
      if (!res.ok) {
        let detail = '';
        try { const j = await res.json(); detail = j.detail || j.error || ''; } catch (e) {}
        let msg = '请求失败 (' + res.status + ')' + (detail ? '：' + detail : '');
        if (res.status === 401) msg = '需要访问令牌，请在设置页填写';
        if (!opts.silent) UI.toast(msg, 'error');
        throw new Error(msg);
      }
      if (opts.raw) return res;
      const ct = (res.headers.get('content-type') || '').toLowerCase();
      if (ct.includes('application/json')) return res.json();
      return res.text();
    }

    /* ---------- 模板列表刷新（导入/删除后同步顶部下拉） ---------- */
    async function refreshTemplates() {
      try {
        const templates = await api('/api/templates');
        App.templates = templates;
        const tplSel = document.getElementById('tpl-select');
        const cur = App.currentTemplate;
        tplSel.innerHTML = '';
        templates.forEach((t) => tplSel.append(UI.createEl('option', { value: t.id, text: t.title })));
        if (cur && templates.some((t) => t.id === cur)) {
          tplSel.value = cur;
        } else if (templates.length) {
          App.currentTemplate = templates[0].id;
          tplSel.value = templates[0].id;
        } else {
          App.currentTemplate = null;
        }
        UI.enhanceSelect(tplSel);
        if (cur !== App.currentTemplate && currentRoute === '/generate') navigate();
      } catch (err) { /* 静默：下拉保持原样 */ }
    }

    /* ---------- 路由 ---------- */
    function navigate() {
      const hash = (location.hash || '').replace(/^#/, '');
      const path = hash.split('?')[0] || '/generate';
      const route = ROUTES[path];
      if (!route) {
        if (path !== '/generate') location.replace('#/generate');
        return;
      }
      if (currentPage && currentPage.unmount) {
        try { currentPage.unmount(); } catch (err) { console.error('[app] unmount', err); }
      }
      document.querySelectorAll('.tab').forEach((t) => {
        t.classList.toggle('active', t.getAttribute('href') === '#' + path);
      });
      document.getElementById('bottom-bar').style.display = path === '/generate' ? '' : 'none';
      document.title = TITLES[path] || 'ComfyUI 遥控';
      appEl.innerHTML = '';
      currentRoute = path;
      currentPage = route;
      Promise.resolve(route.mount(appEl)).catch((err) => {
        console.error('[app] mount', err);
        appEl.innerHTML = '';
        appEl.append(UI.createEl('div', { class: 'empty', text: '页面加载失败，请检查后端服务' }));
      });
    }

    /* ---------- boot ---------- */
    async function boot() {
      applyTheme(currentTheme());
      const tplSel = document.getElementById('tpl-select');
      tplSel.innerHTML = '';
      try {
        const templates = await api('/api/templates');
        App.templates = templates;
        templates.forEach((t) => tplSel.append(UI.createEl('option', { value: t.id, text: t.title })));
        const saved = localStorage.getItem('crtpl');
        const cur = templates.find((t) => t.id === saved) || templates[0];
        if (cur) { App.currentTemplate = cur.id; tplSel.value = cur.id; }
        else { App.currentTemplate = null; }
      } catch (err) {
        App.currentTemplate = null;
        tplSel.append(UI.createEl('option', { text: '模板加载失败' }));
      }
      UI.enhanceSelect(tplSel);
      tplSel.addEventListener('change', () => {
        App.currentTemplate = tplSel.value || null;
        localStorage.setItem('crtpl', App.currentTemplate || '');
        if (currentRoute === '/generate') navigate(); // 生成页依赖模板，需重载
      });
      window.addEventListener('hashchange', navigate);
      navigate();

      if ('serviceWorker' in navigator) {
        navigator.serviceWorker.register('sw.js').catch((err) => console.warn('[app] sw register', err));
      }
    }

    window.addEventListener('DOMContentLoaded', () => {
      appEl = document.getElementById('app');
      if (!appEl) return;
      ROUTES['/generate'] = window.Pages.generate;
      ROUTES['/gallery'] = window.Pages.gallery;
      ROUTES['/settings'] = window.Pages.settings;
      boot();
    });

    return {
      api: api,
      navigate: navigate,
      refreshTemplates: refreshTemplates,
      getToken: getToken,
      setToken: setToken,
      applyTheme: applyTheme,
      currentTheme: currentTheme,
      templates: [],
      get currentTemplate() { return _tpl; },
      set currentTemplate(v) { _tpl = v; }
    };
  })();
})();
