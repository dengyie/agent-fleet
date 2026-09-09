/* timeline.js — 24h uptime timeline renderer */

const FleetApp = {
  timeline: function(container, history) {
    if (!container || !history || !Array.isArray(history)) return;

    const now = Date.now() / 1000;
    const dayAgo = now - 86400;
    const bars = [];

    for (let i = 0; i < history.length; i++) {
      const h = history[i];
      const ts = h.ts || 0;
      const online = h.reachable === true;

      if (ts < dayAgo) continue;

      const heightPct = Math.min(100, Math.max(5, (ts - dayAgo) / 864)); // 0-100% based on time in last 24h
      const barClass = online ? '' : 'down';
      bars.push(`<i class="${barClass}" style="height:${heightPct}%"></i>`);
    }

    if (bars.length === 0) {
      container.innerHTML = '<div class="meta">无 24h 历史数据</div>';
      return;
    }

    const startLabel = new Date((dayAgo) * 1000).toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit'});
    const endLabel = new Date(now * 1000).toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit'});

    container.innerHTML = `
      <div class="timeline">${bars.join('')}</div>
      <div class="timeline-legend">
        <span>${startLabel}</span>
        <span>${endLabel}</span>
      </div>
    `;
  }
};
