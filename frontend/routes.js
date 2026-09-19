/* frontend/routes.js — 前端路径助手（Task 12 + Task 13）
 *
 * apiPath / pagePath 是浏览器路径拼接的唯一入口：每个路径段都经过
 * percent-encoding（encodeURIComponent），任何仍含裸 '/' 的段直接抛错，
 * 绝不把原始斜杠拼接成意外路径层级。额外拒绝 "." / ".." 段，防止把相对
 * 路径穿越混入路径层级。视图层不得自行拼接 /api 路径。
 */
export function apiPath(...segments) {
  return buildPath(window.FleetConfig.apiBaseUrl, segments);
}

export function pagePath(...segments) {
  return buildPath("", segments);
}

/**
 * 将若干路径段编码后接在 base 之后。base 为 API 基址或页面基址。
 * 段中的任何 '/' 或 "." / ".." 都视为非法（防止路径穿越/层级混入）。
 */
function buildPath(base, segments) {
  var encoded = segments.map(function (segment) {
    var s = String(segment);
    if (s === "." || s === "..") {
      throw new Error('path segment must not be "." or ".."');
    }
    if (s.indexOf("/") !== -1) {
      throw new Error('path segment must not contain "/"');
    }
    return encodeURIComponent(s);
  });
  if (encoded.length === 0) {
    return base;
  }
  return base.replace(/\/$/, "") + "/" + encoded.join("/");
}

/* -- Awesome UI Kit: 内嵌纯矢量 SVG 图标集 (Tabler-style) -------------------- */
var ICONS = {
  'server': '<rect x="2" y="2" width="20" height="8" rx="2" ry="2"/><rect x="2" y="14" width="20" height="8" rx="2" ry="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/>',
  'cpu': '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><line x1="9" y1="1" x2="9" y2="4"/><line x1="15" y1="1" x2="15" y2="4"/><line x1="9" y1="20" x2="9" y2="23"/><line x1="15" y1="20" x2="15" y2="23"/><line x1="20" y1="9" x2="23" y2="9"/><line x1="20" y1="14" x2="23" y2="14"/><line x1="1" y1="9" x2="4" y2="9"/><line x1="1" y1="14" x2="4" y2="14"/>',
  'terminal': '<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>',
  'activity': '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
  'sparkles': '<path d="m12 3-1.2 4.8L6 9l4.8 1.2L12 15l1.2-4.8L18 9l-4.8-1.2L12 3Z"/>',
  'check': '<path d="m5 12 4 4L19 6"/>',
  'check-circle': '<circle cx="12" cy="12" r="9"/><path d="m8 12 2.5 2.5L16 9"/>',
  'alert-triangle': '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
  'x': '<path d="m6 6 12 12M18 6 6 18"/>',
  'circle-x': '<circle cx="12" cy="12" r="9"/><path d="m9 9 6 6m0-6-6 6"/>',
  'refresh': '<path d="M20 11a8 8 0 0 0-14.5-4L3 10m0-5v5h5M4 13a8 8 0 0 0 14.5 4L21 14m0 5v-5h-5"/>',
  'play': '<polygon points="5 3 19 12 5 21 5 3"/>',
  'pause': '<rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/>',
  'stop': '<rect x="4" y="4" width="16" height="16" rx="2"/>',
  'moon': '<path d="M20 15.5A8 8 0 0 1 8.5 4 8 8 0 0 0 20 15.5Z"/>',
  'sun': '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2"/>',
  'monitor': '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4"/>',
  'copy': '<rect x="8" y="8" width="11" height="11" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
  'external-link': '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>',
  'git-branch': '<path d="M6 3v12a3 3 0 0 0 3 3h6a3 3 0 0 0 3-3V9"/><circle cx="6" cy="3" r="2"/><circle cx="18" cy="7" r="2"/><circle cx="12" cy="18" r="2"/>',
  'code': '<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>',
  'layers': '<polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/>',
  'shield': '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
  'lock': '<rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
  'unlock': '<rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/>',
  'key': '<path d="m21 2-2 2m-1.5 1.5L14 9l-3 3-2-2-4 4 3 3 4-4-2-2 3.5-3.5a5 5 0 1 0-7-7 5 5 0 0 0 7 7Z"/>',
  'chevron-right': '<polyline points="9 18 15 12 9 6"/>',
  'chevron-down': '<polyline points="6 9 12 15 18 9"/>',
  'arrow-right': '<line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/>',
  'plus': '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
  'clock': '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
  'box': '<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/>',
  'menu': '<line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="18" x2="21" y2="18"/>',
  'globe': '<circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>'
};

export function uiIcon(name, options) {
  var opt = options || {};
  var size = opt.size || 16;
  var strokeWidth = opt.strokeWidth || 2;
  var inner = ICONS[name] || ICONS['box'];
  var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', String(strokeWidth));
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  if (opt.className) {
    svg.setAttribute('class', opt.className);
  }
  if (opt.label) {
    svg.setAttribute('aria-label', opt.label);
  } else {
    svg.setAttribute('aria-hidden', 'true');
  }
  // 使用 XMLSerializer / DOMParser 安全解析 SVG 片段
  var parser = new DOMParser();
  var doc = parser.parseFromString('<svg xmlns="http://www.w3.org/2000/svg">' + inner + '</svg>', 'image/svg+xml');
  var child = doc.documentElement.firstChild;
  while (child) {
    svg.appendChild(child.cloneNode(true));
    child = child.nextSibling;
  }
  return svg;
}
