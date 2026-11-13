/**
 * AutoScrollAnchor - Vanilla Web Component
 * Usage: <auto-scroll-anchor is-streaming></auto-scroll-anchor>
 */
import uiIcon from './UiIcon.js';
class AutoScrollAnchor extends HTMLElement {
  static get observedAttributes() {
    return ["is-streaming"];
  }

  connectedCallback() {
    this.scrollRoot = this.closest("[data-chat-scroll]") || window;
    this.isAtBottom = true;
    this.render();
    this.bindEvents();
  }

  disconnectedCallback() {
    if (this._scrollHandler) {
      this.scrollRoot.removeEventListener("scroll", this._scrollHandler);
      window.removeEventListener("resize", this._scrollHandler);
      cancelAnimationFrame(this._frame);
    }
    this._resizeObserver?.disconnect();
  }

  attributeChangedCallback(name, oldValue, newValue) {
    if (name === "is-streaming" && newValue !== null && this.isAtBottom) {
      cancelAnimationFrame(this._frame);
      this._frame = requestAnimationFrame(() => this.scrollToBottom());
    }
  }

  render() {
    this.innerHTML = `
      <div id="anchor" class="h-px w-full pointer-events-none"></div>
      <button type="button" id="back-to-bottom" class="hidden fixed bottom-24 right-8 z-30 p-2.5 rounded-full bg-white dark:bg-zinc-800 text-zinc-600 dark:text-zinc-200 border border-zinc-200 dark:border-zinc-700 shadow-lg hover:shadow-xl hover:scale-105 active:scale-95 transition-all flex items-center gap-1.5 text-xs font-medium" aria-label="滚动到最新消息" title="滚动到最新消息">
        ${uiIcon('arrow-down', {size: 18})}
      </button>
    `;
  }

  bindEvents() {
    const btn = this.querySelector("#back-to-bottom");
    this._scrollHandler = () => {
      // Follow the conversation viewport; retain document fallback for other hosts.
      const anchor = this.querySelector("#anchor").getBoundingClientRect();
      if (this.scrollRoot === window) {
        this.isAtBottom = anchor.top >= 0 && anchor.bottom <= window.innerHeight;
      } else {
        this.isAtBottom = this.scrollRoot.scrollHeight - this.scrollRoot.scrollTop - this.scrollRoot.clientHeight < 72;
      }
      btn.classList.toggle("hidden", this.isAtBottom);
    };

    this.scrollRoot.addEventListener("scroll", this._scrollHandler, { passive: true });
    window.addEventListener("resize", this._scrollHandler, { passive: true });
    if (this.scrollRoot !== window) {
      this._resizeObserver = new ResizeObserver(this._scrollHandler);
      this._resizeObserver.observe(this.scrollRoot);
    }
    this._scrollHandler();
    btn.addEventListener("click", () => this.scrollToBottom());
  }

  scrollToBottom() {
    const behavior = window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth';
    if (this.scrollRoot && this.scrollRoot !== window) {
      this.scrollRoot.scrollTo({ top: this.scrollRoot.scrollHeight, behavior });
    } else {
      this.querySelector("#anchor")?.scrollIntoView({ behavior, block: "end" });
    }
  }
}

if (!customElements.get("auto-scroll-anchor")) {
  customElements.define("auto-scroll-anchor", AutoScrollAnchor);
}
export default AutoScrollAnchor;
