/* frontend/config.js — 前端运行时配置（仅非敏感值，Task 12 + Task 13）
 *
 * 独立静态 release 的一部分，由纯静态服务器直接提供，无 Flask 模板渲染。
 * 配置只在静态部署时生成；绝不含 ingest token、runner credential、
 * Access header、内部文件路径或数据库信息。默认 apiBaseUrl 为同源 /api；
 * pageBaseUrl 为页面路径基址（当前为同源根路径）。
 */
window.FleetConfig = Object.freeze({
  apiBaseUrl: "/api",
  pageBaseUrl: ""
});
