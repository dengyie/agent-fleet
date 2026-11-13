/** Adapted from awesome-ui/vanilla/ChatPromptInput.js; see THIRD_PARTY.md.
 * Enhances supplied form controls, keeping request/value ownership in the app.
 */
class ChatPromptInput extends HTMLElement {
  connectedCallback() {
    const textarea = this.querySelector('textarea');
    if (!textarea || this.bound) return;
    this.bound = true;
    textarea.addEventListener('input', () => {
      textarea.style.height = 'auto';
      textarea.style.height = `${Math.max(Math.min(textarea.scrollHeight, 240), 48)}px`;
    });
    textarea.addEventListener('keydown', event => {
      if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
      event.preventDefault();
      if (!textarea.readOnly && !textarea.disabled) this.closest('form').requestSubmit();
    });
  }
}
if (!customElements.get('chat-prompt-input')) customElements.define('chat-prompt-input', ChatPromptInput);
export default ChatPromptInput;
