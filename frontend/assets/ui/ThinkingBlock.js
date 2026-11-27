/** awesome-ui ThinkingBlock pattern for public activity summaries only. */
import uiIcon from './UiIcon.js';

class ThinkingBlock extends HTMLElement {
  static get observedAttributes() { return ['content', 'is-thinking', 'title']; }
  connectedCallback() {
    if (this.isExpanded === undefined) this.isExpanded = true;
    this.render();
  }
  attributeChangedCallback() { if (this.isConnected) this.render(); }

  render() {
    if (!this._parts) {
      this.innerHTML = `<div class="thinking-block-shell">
        <button type="button" class="thinking-block-toggle">
          <span class="thinking-block-label">${uiIcon('sparkles', {size:15})}<span></span></span>
          ${uiIcon('chevron-right', {size:14, className:'thinking-block-chevron'})}
        </button>
        <div class="thinking-block-body"><pre></pre></div>
      </div>`;
      this._parts = {button:this.querySelector('button'), label:this.querySelector('.thinking-block-label span'),
        body:this.querySelector('.thinking-block-body'), pre:this.querySelector('pre'), chevron:this.querySelector('.thinking-block-chevron')};
      this._parts.button.addEventListener('click', () => { this.isExpanded = !this.isExpanded; this.updateDisclosure(); });
    }
    const parts = this._parts, active = this.hasAttribute('is-thinking');
    parts.label.textContent = this.getAttribute('title') || (active ? '正在思考' : '执行过程');
    parts.button.classList.toggle('is-active', active);
    const text = this.getAttribute('content') || '等待公开进度…';
    if (parts.pre.textContent !== text) parts.pre.textContent = text;
    this.updateDisclosure();
  }

  updateDisclosure() {
    this._parts.button.setAttribute('aria-expanded', String(Boolean(this.isExpanded)));
    this._parts.body.hidden = !this.isExpanded;
    this._parts.chevron.classList.toggle('is-open', Boolean(this.isExpanded));
  }
}

if (!customElements.get('thinking-block')) customElements.define('thinking-block', ThinkingBlock);
export default ThinkingBlock;
