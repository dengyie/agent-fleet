import { startBridge } from './bridge.mjs';

let bridge;
export async function activate(ctx) {
  bridge = await startBridge(ctx);
  ctx.log.info('Agent Fleet desktop session bridge ready (loopback only).');
}
export async function deactivate() {
  await bridge?.close();
  bridge = undefined;
}
