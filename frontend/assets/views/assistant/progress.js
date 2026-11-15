// Assemble existing Markdown and tool components from the public Run projection.
export function renderProgress(host, run) {
  const progress = run.progress || {};
  const segments = Array.isArray(progress.segments) ? [...progress.segments] : [];
  if (progress.truncated) segments.unshift({id:'progress-retention-notice',kind:'notice',
    text:'部分过程记录已省略，当前显示保留的执行过程。'});
  const active = !['succeeded','failed','unknown','cancelled'].includes(run.state);
  if (!host._segments) host._segments = new Map();
  const keep = new Set();
  let index = 0;
  segments.forEach(segment => {
    if (segment.kind === 'text' && (!segment.text || (segment.final && run.result_text && run.state === 'succeeded'))) return;
    keep.add(segment.id);
    let node = host._segments.get(segment.id);
    if (!node) {
      node = document.createElement(segment.kind === 'notice' ? 'p' : segment.kind === 'tool' ? 'tool-call-badge' : 'stream-markdown');
      node.className = 'assistant-progress-segment';
      host._segments.set(segment.id,node);
    }
    if (segment.kind === 'notice') {
      node.classList.add('meta');
      if (node.textContent !== segment.text) node.textContent = segment.text;
    } else if (segment.kind === 'tool') {
      const name = segment.tool || '工具执行';
      const status = segment.state === 'succeeded' ? 'success' : segment.state === 'running' ? (active ? 'running' : 'unknown') : segment.state === 'unknown' ? 'unknown' : 'error';
      if (node.getAttribute('name') !== name) node.setAttribute('name',name);
      if (node.getAttribute('status') !== status) node.setAttribute('status',status);
      for (const [attribute, value] of [['label', segment.title], ['args', segment.input], ['output', segment.output]]) {
        if (value && node.getAttribute(attribute) !== value) node.setAttribute(attribute,value);
        if (!value && node.hasAttribute(attribute)) node.removeAttribute(attribute);
      }
      const error = segment.error_code || '';
      if (error && node.getAttribute('error') !== error) node.setAttribute('error',error);
      if (!error && node.hasAttribute('error')) node.removeAttribute('error');
    } else {
      if (node.getAttribute('content') !== segment.text) node.setAttribute('content',segment.text);
      node.toggleAttribute('is-streaming',active && !segment.complete);
    }
    if (host.children[index] !== node) host.insertBefore(node,host.children[index] || null);
    index += 1;
  });
  host._segments.forEach((node,key) => { if (!keep.has(key)) {node.remove();host._segments.delete(key);} });
  const retry = progress.retry;
  return retry && active ? '上游暂时不可用' + (retry.provider_status ? '（HTTP ' + retry.provider_status + '）' : '') + '，正在自动重试 ' + retry.retry_index + '/' + retry.max_retries + '…' : '';
}
