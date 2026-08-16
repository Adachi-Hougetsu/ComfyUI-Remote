/* WebSocket 封装：暴露 window.WS。
 * 单个 WebSocket 连接 ws(s)://<host>/ws，自动重连；按事件分发给订阅者。
 * API: { connect(batchId), disconnect(batchId), on(event, handler), off(event, handler), isConnected() }
 * 服务端事件：hello / batch_start / progress / run_done / run_error / batch_done / batch_error
 * 本地状态事件：status（{connected: bool}），供 UI 显示连接状态
 */
(function () {
  'use strict';

  window.WS = (function () {
    const listeners = Object.create(null); // event -> Set(handler)
    const subscribed = new Set(); // batchIds
    let ws = null;
    let connected = false;
    let reconnectDelay = 1000;
    let reconnectTimer = null;

    function emit(event, data) {
      const set = listeners[event];
      if (!set) return;
      set.forEach((h) => {
        try { h(data); } catch (err) { console.error('[WS] handler error', event, err); }
      });
    }

    function send(obj) {
      if (ws && ws.readyState === WebSocket.OPEN) {
        try { ws.send(JSON.stringify(obj)); } catch (err) { console.error('[WS] send error', err); }
      }
    }

    function scheduleReconnect() {
      if (reconnectTimer) return;
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        open();
      }, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 10000);
    }

    function open() {
      if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;
      const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
      const q = new URLSearchParams();
      let token = '';
      try { token = localStorage.getItem('comfyui-remote.token') || ''; } catch (e) {}
      if (token) q.set('token', token);
      const qs = q.toString();
      ws = new WebSocket(proto + location.host + '/ws' + (qs ? '?' + qs : ''));
      ws.onopen = () => {
        connected = true;
        reconnectDelay = 1000;
        emit('status', { connected: true });
        // 重连后重新订阅所有批次
        subscribed.forEach((id) => send({ type: 'subscribe', batch_id: id }));
      };
      ws.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch (err) { return; }
        if (msg && typeof msg === 'object' && msg.type) {
          emit(msg.type, msg);
        } else if (msg && typeof msg === 'object') {
          emit('hello', msg);
        }
      };
      ws.onerror = () => { try { ws.close(); } catch (err) {} };
      ws.onclose = () => {
        connected = false;
        emit('status', { connected: false });
        scheduleReconnect();
      };
    }

    function connect(batchId) {
      if (batchId) subscribed.add(batchId);
      open();
    }

    function disconnect(batchId) {
      subscribed.delete(batchId);
      send({ type: 'unsubscribe', batch_id: batchId });
    }

    function on(event, handler) {
      (listeners[event] = listeners[event] || new Set()).add(handler);
      return () => off(event, handler);
    }

    function off(event, handler) {
      if (listeners[event]) listeners[event].delete(handler);
    }

    return {
      connect: connect,
      disconnect: disconnect,
      on: on,
      off: off,
      send: send,
      isConnected: () => connected
    };
  })();
})();
