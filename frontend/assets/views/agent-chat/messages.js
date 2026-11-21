import {el} from './dom.js';
import {nativeMessages} from './state.js';

// Stable keyed nodes retain tool disclosures, text selection and scroll position.
export function createNativeMessages(host) {
  const nodes = new Map();
  return events => {
    const rows = nativeMessages(events), keys = new Set(rows.map(row => row.key));
    for (const [key, entry] of nodes) if (!keys.has(key)) { entry.node.remove(); nodes.delete(key); }
    let previous = null;
    for (const row of rows) {
      let entry = nodes.get(row.key);
      const signature = JSON.stringify(row);
      if (!entry) {
        const node = row.kind === 'tool' ? el('tool-call-badge') : el('article', row.kind === 'notice' ? 'native-notice' : 'assistant-message ' + (row.kind === 'user_message' ? 'user' : 'assistant'));
        entry = {node}; nodes.set(row.key, entry);
      }
      if (entry.signature !== signature) {
        const node = entry.node;
        if (row.kind === 'tool') {
          for (const [key, value] of Object.entries({name: row.name, label: row.label, status: row.status, args: row.args, output: row.output})) {
            if (node.getAttribute(key) !== (value || '')) node.setAttribute(key, value || '');
          }
        } else if (row.kind === 'notice') { node.textContent = row.text; node.setAttribute('role', 'status'); }
        else {
          const markdown = node.querySelector('stream-markdown') || el('stream-markdown');
          if (!markdown.isConnected) node.append(el('div', 'assistant-message-role', row.kind === 'user_message' ? '你' : 'Agent'), markdown);
          markdown.setAttribute('content', row.text);
          markdown.toggleAttribute('is-streaming', !row.complete);
          if (row.text.length > 32768) {
            let details = node.querySelector('.native-full-text');
            if (!details) {
              details = el('details', 'native-full-text'); details.append(el('summary', null, '查看完整原文'), el('pre')); node.append(details);
            }
            details.querySelector('pre').textContent = row.text;
          }
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
