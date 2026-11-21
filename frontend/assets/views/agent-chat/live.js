/* One visible reader, coalesced wakeups, read-only retries. A suspended request
 * cannot publish or schedule work after resume even if its transport ignores abort. */
export function createNativeReader({read, onError, interval = 4000}) {
  let active = false, disposed = false, running = false, dirty = false;
  let timer, controller, generation = 0, failures = 0;
  function schedule(delay) {
    clearTimeout(timer);
    if (active && !disposed) timer = setTimeout(tick, delay);
  }
  async function tick() {
    if (!active || disposed) return;
    if (running) { dirty = true; return; }
    const epoch = generation;
    running = true; dirty = false; controller = new AbortController();
    try {
      await read(controller.signal, () => active && !disposed && epoch === generation);
      if (epoch === generation) failures = 0;
    } catch (error) {
      if (epoch === generation && active && !disposed) { failures++; onError(error); }
    } finally {
      running = false;
      if (active && !disposed) schedule(dirty || epoch !== generation ? 0 : failures ? Math.min(15000, 1000 * 2 ** Math.min(failures, 4)) : interval);
    }
  }
  return {
    wake() { if (!active || disposed) return; if (running) dirty = true; else schedule(0); },
    start() { if (disposed || active) return; active = true; generation++; if (running) dirty = true; else schedule(0); },
    stop() { active = false; generation++; dirty = false; clearTimeout(timer); controller?.abort(); },
    dispose() { this.stop(); disposed = true; },
  };
}
