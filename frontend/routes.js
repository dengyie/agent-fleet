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
