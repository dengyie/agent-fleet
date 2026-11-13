/** awesome-ui StreamMarkdown, with local parser + sanitizer and safe DOM output.
 * No model HTML, embedded media, inline styles or network-loaded dependencies.
 */
import { marked } from '../vendor/marked.js';
import DOMPurify from '../vendor/purify.js';
class StreamMarkdown extends HTMLElement {
  static get observedAttributes() { return ['content', 'is-streaming']; }
  connectedCallback() { this.render(); }
  attributeChangedCallback() { if (this.isConnected) this.render(); }
  render() {
    const codepoints = Array.from(this.getAttribute('content') || '');
    const truncated = codepoints.length > 32768;
    const content = codepoints.slice(0, 32768).join('');
    const fragment = DOMPurify.sanitize(marked.parse(content, {breaks: true, gfm: true}), {
      RETURN_DOM_FRAGMENT: true,
      ALLOWED_TAGS: ['p', 'br', 'strong', 'em', 'del', 'blockquote', 'ul', 'ol', 'li', 'pre', 'code', 'h1', 'h2', 'h3', 'h4', 'hr', 'a', 'table', 'thead', 'tbody', 'tr', 'th', 'td'],
      ALLOWED_ATTR: ['href', 'title'],
    });
    this.replaceChildren(fragment);
    if (truncated) {
      const notice = document.createElement('p');
      notice.textContent = '内容已截断（最多 32768 个字符）';
      notice.setAttribute('role', 'status');
      this.appendChild(notice);
    }
    this.querySelectorAll('a').forEach(link => {
      const href = link.getAttribute('href') || '';
      if (!/^https?:\/\//i.test(href)) link.removeAttribute('href');
      else { link.setAttribute('target', '_blank'); link.setAttribute('rel', 'noopener noreferrer'); }
    });
    this.setAttribute('aria-busy', String(this.hasAttribute('is-streaming')));
  }
}
if (!customElements.get('stream-markdown')) customElements.define('stream-markdown', StreamMarkdown);
export default StreamMarkdown;
