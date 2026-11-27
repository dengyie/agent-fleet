/** awesome-ui ToolCallBadge, adapted to the Fleet public tool DTO. */
import uiIcon from './UiIcon.js';

const STATUS = {
  running: ['loader', '运行中'], success: ['check-circle', '已完成'],
  error: ['circle-x', '失败'], unknown: ['monitor', '结果未知'],
};

function readable(value) {
  if (!value) return '';
  try {
    const parsed = JSON.parse(value);
    if (parsed && typeof parsed === 'object') return JSON.stringify(parsed, null, 2);
  } catch (_) { /* Commands and plain output remain verbatim. */ }
  return value;
}

class ToolCallBadge extends HTMLElement {
  static get observedAttributes() { return ['name', 'label', 'status', 'args', 'output', 'error']; }

  connectedCallback() {
    if (this.isOpen === undefined) this.isOpen = this.hasAttribute('default-open');
    this.render();
  }

  attributeChangedCallback() { if (this.isConnected) this.render(); }

  render() {
    if (!this._parts) {
      // Only trusted static markup and local icons enter innerHTML. All public
      // text is assigned below, and updates keep the existing disclosure DOM.
      this.innerHTML = `<div class="tool-card">
        <button type="button" id="toggle-btn">
          <div>${uiIcon('tool', {size:16})}<span class="tool-call-label"></span></div>
          <div><span class="tool-call-state"></span>${uiIcon('chevron-right', {size:14, className:'tool-call-chevron'})}</div>
        </button>
        <div id="drawer">
          <p class="tool-detail-name"></p>
          <section class="tool-detail-input"><h4>调用参数</h4><pre></pre><p class="tool-detail-empty"></p></section>
          <section class="tool-detail-output"><h4>执行结果</h4><pre></pre><p class="tool-detail-empty"></p></section>
          <section class="tool-detail-error"><h4>错误详情</h4><pre></pre></section>
        </div>
      </div>`;
      const section = name => this.querySelector('.tool-detail-' + name);
      this._parts = {button:this.querySelector('#toggle-btn'), drawer:this.querySelector('#drawer'),
        label:this.querySelector('.tool-call-label'), name:this.querySelector('.tool-detail-name'), state:this.querySelector('.tool-call-state'),
        chevron:this.querySelector('.tool-call-chevron'), input:section('input'), output:section('output'), error:section('error')};
      this._parts.button.addEventListener('click', () => { this.isOpen = !this.isOpen; this.updateDisclosure(); });
    }
    const parts = this._parts, name = this.getAttribute('name') || '工具';
    const label = this.getAttribute('label') || name;
    if (parts.label.textContent !== label) parts.label.textContent = label;
    if (parts.name.textContent !== name) parts.name.textContent = name;
    parts.button.title = label === name ? name : label + '\n' + name;
    const status = this.getAttribute('status') || 'running';
    if (parts.state.dataset.status !== status) {
      const [icon, text] = Object.hasOwn(STATUS, status) ? STATUS[status] : STATUS.unknown;
      parts.state.innerHTML = uiIcon(icon, {size:14, className:status === 'running' ? 'animate-spin' : ''});
      parts.state.append(document.createTextNode(text)); parts.state.dataset.status = status;
    }
    const input = this.getAttribute('args') || '', output = this.getAttribute('output') || '', error = this.getAttribute('error') || '';
    for (const [section, value, empty] of [
      [parts.input, input, '未提供调用参数'],
      [parts.output, output, status === 'running' ? '正在执行，等待返回结果…' : status === 'unknown' ? '尚未收到明确结果' : '未返回输出内容'],
      [parts.error, error, ''],
    ]) {
      const pre = section.querySelector('pre'), text = readable(value);
      if (pre.textContent !== text) pre.textContent = text;
      pre.hidden = !value;
      const hint = section.querySelector('p');
      if (hint) { hint.textContent = empty; hint.hidden = Boolean(value); }
    }
    parts.error.hidden = !error;
    this.updateDisclosure();
  }

  updateDisclosure() {
    if (!this._parts) return;
    this._parts.button.setAttribute('aria-expanded', String(Boolean(this.isOpen)));
    this._parts.drawer.hidden = !this.isOpen;
    this._parts.chevron.classList.toggle('rotate-90', Boolean(this.isOpen));
  }
}

if (!customElements.get('tool-call-badge')) customElements.define('tool-call-badge', ToolCallBadge);
export default ToolCallBadge;
