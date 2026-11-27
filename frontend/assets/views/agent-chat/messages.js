import {el} from './dom.js';
import {nativeTranscript} from './state.js';

// Stable keyed nodes retain tool disclosures, text selection and scroll position.
export function createNativeMessages(host) {
  const nodes = new Map();
  return events => {
    const rows = nativeTranscript(events), keys = new Set(rows.map(row => row.key));
    for (const [key, entry] of nodes) if (!keys.has(key)) { entry.node.remove(); nodes.delete(key); }
    let previous = null;
    for (const row of rows) {
      let entry = nodes.get(row.key);
      const signature = JSON.stringify(row);
      if (!entry) {
        const node = el('article', row.kind === 'tool' ? 'native-tool-event' :
          row.kind === 'notice' ? 'native-notice' : row.kind === 'thinking' ? 'native-thinking' :
            'assistant-message ' + (row.kind === 'user_message' ? 'user' : 'assistant'));
        if (row.kind === 'tool') {
          entry = {node, tool: el('tool-call-badge')};
          entry.tool.setAttribute('default-open', '');
          node.append(entry.tool);
        } else if (row.kind === 'thinking') {
          entry = {node, thinking:el('thinking-block')};
          node.append(entry.thinking);
        } else entry = {node};
        nodes.set(row.key, entry);
      }
      if (entry.signature !== signature) {
        const {node, tool} = entry;
        if (row.kind === 'tool') {
          for (const [key, value] of Object.entries({name: row.name, label: row.label, status: row.status, args: row.args, output: row.output})) {
            if (tool.getAttribute(key) !== (value || '')) tool.setAttribute(key, value || '');
          }
          renderTime(node, row.emitted_at);
        } else if (row.kind === 'thinking') {
          for (const [key, value] of Object.entries({title:row.title, content:row.content})) {
            if (entry.thinking.getAttribute(key) !== value) entry.thinking.setAttribute(key, value);
          }
          entry.thinking.toggleAttribute('is-thinking', row.active);
        } else if (row.kind === 'notice') {
          node.replaceChildren(el('span', null, row.text));
          node.setAttribute('role', 'status');
          renderTime(node, row.emitted_at);
        }
        else {
          const markdown = node.querySelector('stream-markdown') || el('stream-markdown');
          if (!markdown.isConnected) node.append(el('div', 'assistant-message-role', row.kind === 'user_message' ? '你' : 'Agent'), markdown);
          markdown.setAttribute('content', row.text);
          markdown.toggleAttribute('is-streaming', !row.complete);
          let interruption = node.querySelector('.native-message-interrupted');
          if (row.interrupted) {
            if (!interruption) {
              interruption = el('p', 'native-message-interrupted meta', '输出已中断，已收到的内容保留。');
              interruption.setAttribute('role', 'status'); node.append(interruption);
            }
          } else interruption?.remove();
          let time = node.querySelector('.native-message-time');
          if (row.updated_at) {
            if (!time) { time = el('time', 'native-message-time'); node.append(time); }
            setTime(time, row.updated_at);
          }
          if (row.text.length > 32768) {
            let details = node.querySelector('.native-full-text');
            if (!details) {
              details = el('details', 'native-full-text'); details.append(el('summary', null, '查看完整原文'), el('pre')); node.append(details);
            }
            details.querySelector('pre').textContent = row.text;
          } else node.querySelector('.native-full-text')?.remove();
        }
        entry.signature = signature;
      }
      const expected = previous ? previous.nextSibling : host.firstChild;
      if (expected !== entry.node) host.insertBefore(entry.node, expected);
      previous = entry.node;
    }
    return rows.length;
  };
}

function renderTime(node, value) {
  let time = node.querySelector('.native-message-time');
  if (!value) { time?.remove(); return; }
  if (!time) { time = el('time', 'native-message-time'); node.append(time); }
  setTime(time, value);
}

function setTime(node, value) {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return;
  node.dateTime = value;
  node.textContent = new Date(timestamp).toLocaleString();
}
