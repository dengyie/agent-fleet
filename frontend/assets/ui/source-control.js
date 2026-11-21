/* Shared DOM presentation for source controls; callers supply admitted actions. */
import { uiIcon } from '../routes.js';

function h(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.setAttribute('class', className);
  if (text !== undefined) el.textContent = text;
  return el;
}
function fmtValue(value) { return value === null || value === undefined || value === '' ? '?' : String(value); }
function headingWithIcon(text, iconName) {
  const title = h('h3');
  title.appendChild(uiIcon(iconName, {size:15}));
  title.appendChild(document.createTextNode(text));
  return title;
}

// 仅五个既有动作令牌：从固定映射枚举，绝不从任意命令文本构造动作名。
var SOURCE_ACTIONS_LABELS = {
  pause_session: '暂停会话',
  resume_session: '恢复会话',
  terminate_session: '终止会话',
  quarantine_session: '隔离会话',
  cancel_attempt: '取消任务',
};

// 有界状态白名单（pending|adopted|revoked|terminal）；白名单之外一律落入
// 安全回退类，绝不把原始状态字符串拼进 class 属性（状态文本仍由 h() 的
// textContent 呈现）。
var SOURCE_STATUS_CLASSES = {
  pending: 'st-queued',
  adopted: 'st-ok',
  managed: 'st-ok',
  revoked: 'st-err',
  terminal: 'st-unknown',
};
var SOURCE_STATUS_FALLBACK_CLASS = 'st-unknown';

function sourceStatusClass(statusText) {
  var key = String(statusText);
  if (Object.prototype.hasOwnProperty.call(SOURCE_STATUS_CLASSES, key)) {
    return SOURCE_STATUS_CLASSES[key];
  }
  return SOURCE_STATUS_FALLBACK_CLASS;
}

// Adopted sources and native managed sessions use their declared action subset.
// 非 admitted 状态展示的固定标签（有界常量串，绝不拼接任意状态文本）。
var SOURCE_PENDING_LABEL = '纳管处理中…';
var SOURCE_REVOKED_LABEL = '纳管已撤销';
var SOURCE_OTHER_LABEL = '无可用控制动作';

/**
 * 渲染源控制面板（有界、DOM-safe）。
 *
 * source 只带视图层已有的有界字段（session_id + 固定 status），actions 是可
 * 执行动作令牌子集。控制状态必须是 adopted 或原生 managed：
 * pending -> 状态章 + 固定“纳管处理中…”标签（无按钮）；revoked -> 状态章 +
 * 固定“纳管已撤销”标签（无按钮）；其它状态只显示章 + 固定 empty 标签。
 * 点击仅把固定令牌 + session_id 交给 onAction —— 本模块不 fetch、不拼
 * /api 路径、不读取任何输入文本 / pid / signal（client.js 拥有全部 HTTP）。
 * 返回面板容器节点，由调用方负责追加。
 */
export function renderSourceControl(source, actions, onAction) {
  var panel = h('div', 'panel');
  panel.appendChild(headingWithIcon('源控制', 'shield'));
  if (!source || typeof source !== 'object' ||
      typeof source.session_id !== 'string' || !source.session_id) {
    panel.appendChild(h('div', 'meta', '暂无纳管会话'));
    return panel;
  }
  var statusText = fmtValue(source.status);
  panel.appendChild(h('div', 'source-status ' + sourceStatusClass(statusText),
                      statusText === 'managed' ? '受管会话' : statusText));
  if (statusText === 'pending') {
    panel.appendChild(h('div', 'meta', SOURCE_PENDING_LABEL));
    return panel;
  }
  if (statusText === 'revoked') {
    panel.appendChild(h('div', 'meta', SOURCE_REVOKED_LABEL));
    return panel;
  }
  if (statusText !== 'adopted' && statusText !== 'managed') {
    // terminal / 未知状态：只显示，不存在可交互按钮。
    panel.appendChild(h('div', 'meta', SOURCE_OTHER_LABEL));
    return panel;
  }
  var allowed = {};
  if (Array.isArray(actions)) {
    for (var i = 0; i < actions.length; i += 1) {
      if (Object.prototype.hasOwnProperty.call(
            SOURCE_ACTIONS_LABELS, actions[i])) {
        allowed[actions[i]] = true;
      }
    }
  }
  var keys = Object.keys(SOURCE_ACTIONS_LABELS).filter(function (tk) {
    return allowed[tk] === true;
  });
  if (keys.length === 0) {
    panel.appendChild(h('div', 'meta', SOURCE_OTHER_LABEL));
    return panel;
  }
  keys.forEach(function (tk) {
    var btn = document.createElement('button');
    btn.setAttribute('type', 'button');
    btn.textContent = SOURCE_ACTIONS_LABELS[tk];
    if (typeof onAction === 'function') {
      btn.addEventListener('click', function () {
        // 点击只交固定动作令牌 + 有界 session_id，绝无自由文本接口。
        onAction(tk, source.session_id);
      });
    }
    panel.appendChild(btn);
  });
  return panel;
}
