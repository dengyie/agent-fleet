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
    this.isAtBottom = true;
    this.render();
    this.bindEvents();
  }

  disconnectedCallback() {
    if (this._scrollHandler) {
      window.removeEventListener("scroll", this._scrollHandler);
    }
  }

  attributeChangedCallback(name, oldValue, newValue) {
    if (name === "is-streaming" && newValue !== null && this.isAtBottom) {
      this.scrollToBottom();
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
      // The inspector can extend below the conversation on narrow screens.
      // Follow the actual message anchor, not the bottom of the document.
      const anchor = this.querySelector("#anchor").getBoundingClientRect();
      this.isAtBottom = anchor.top >= 0 && anchor.bottom <= window.innerHeight;
      btn.classList.toggle("hidden", this.isAtBottom);
    };

    window.addEventListener("scroll", this._scrollHandler, { passive: true });
    this._scrollHandler();
    btn.addEventListener("click", () => this.scrollToBottom());
  }

  scrollToBottom() {
    this.querySelector("#anchor")?.scrollIntoView({ behavior: "smooth" });
  }
}

if (!customElements.get("auto-scroll-anchor")) {
  customElements.define("auto-scroll-anchor", AutoScrollAnchor);
}
export default AutoScrollAnchor;
