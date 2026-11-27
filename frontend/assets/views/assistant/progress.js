// Assemble existing Markdown and tool components from the public Run projection.
export function renderProgress(host, run) {
  const progress = run.progress || {};
  const segments = Array.isArray(progress.segments) ? [...progress.segments] : [];
  if (progress.truncated) segments.unshift({id:'progress-retention-notice',kind:'notice',
    text:'部分过程记录已省略，当前显示保留的执行过程。'});
  const active = !['succeeded','failed','unknown','cancelled'].includes(run.state);
  let thinking = host.querySelector(':scope > thinking-block');
  const phase = progress.activity?.phase;
  const phaseText = {waiting:'等待模型开始回复。', thinking:'模型正在思考，当前仅提供状态，尚未返回可展示的思考正文。',
    responding:'模型正在组织回复，正文会持续显示在下方。', tool_call:'模型正在准备工具调用，参数和执行结果会显示在工具卡片中。'};
  if (phaseText[phase] || progress.retry || segments.length) {
    if (!thinking) { thinking = document.createElement('thinking-block'); host.prepend(thinking); }
    const title = !active ? '执行过程' : progress.retry ? '模型正在重试' : phase === 'thinking' ? '正在思考' : '执行进度';
    const ended = {succeeded:'本轮执行已完成。',failed:'本轮执行失败。',cancelled:'本轮已取消。',unknown:'本轮执行结果待确认。'};
    const content = !active ? (ended[run.state] || '本轮已结束。') + '已返回的说明、工具参数和结果保留在下方。' :
      progress.retry ? '上游暂时不可用，正在按配置重试；已收到的内容保留。' : phaseText[phase] || '正在执行；工具参数和结果显示在下方。';
    if (thinking.getAttribute('title') !== title) thinking.setAttribute('title', title);
    if (thinking.getAttribute('content') !== content) thinking.setAttribute('content', content);
    thinking.toggleAttribute('is-thinking', active && phase === 'thinking');
  } else thinking?.remove();
  if (!host._segments) host._segments = new Map();
  const keep = new Set();
  let index = thinking?.parentNode === host ? 1 : 0;
  segments.forEach(segment => {
    if (segment.kind === 'text' && (!segment.text || (segment.final && run.result_text && run.state === 'succeeded'))) return;
    keep.add(segment.id);
    let node = host._segments.get(segment.id);
    if (!node) {
      node = document.createElement(segment.kind === 'notice' ? 'p' : segment.kind === 'tool' ? 'tool-call-badge' : 'stream-markdown');
      node.className = 'assistant-progress-segment';
      if (segment.kind === 'tool') node.setAttribute('default-open','');
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
  if (!active) return '';
  if (retry) {
    const reason = retry.provider_error === 'timeout' ? '模型响应超时' :
      retry.provider_error === 'network_error' ? '模型连接中断' : '上游暂时不可用';
    const status = retry.provider_status && retry.provider_status >= 400 ? '（HTTP ' + retry.provider_status + '）' : '';
    return reason + status + '，正在自动重试 ' + retry.retry_index + '/' + retry.max_retries + '…';
  }
  const phases = {waiting:'正在等待模型响应…', thinking:'模型正在思考…',
    responding:'正在生成回复…', tool_call:'正在准备工具调用…'};
  return phases[progress.activity?.phase] || '';
}
