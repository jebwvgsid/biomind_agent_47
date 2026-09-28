/** Explicit native-test observer. Only numeric counters; no production renderer/transport replacement. */
import fs from 'node:fs';
import type { ExtensionAPI } from '@earendil-works/pi-coding-agent';
import { getDisplayPressure, TOOL_DISPLAY_LIMITS } from '../tool-display.js';
import { DOMAIN_TRANSPORT_LIMITS } from '../domain-client.js';

export function installNativePressureProbe(pi: ExtensionAPI) {
  const trace = process.env.BIOMIND_PI_PRESSURE_TRACE!;
  const root = process.env.BIOMIND_PI_TEST_ROOT;
  if (!root || !trace.startsWith(root + '/')) throw new Error('Pressure trace must be inside explicit synthetic root');
  const record = (event: object) => fs.appendFileSync(trace, JSON.stringify({ ...event, pid: process.pid }) + '\n');
  record({ type: 'limits', display: TOOL_DISPLAY_LIMITS, transport: DOMAIN_TRANSPORT_LIMITS });
  let previous = -1;
  const sample = () => {
    const metrics = getDisplayPressure();
    if (metrics.renderCount !== previous) { previous = metrics.renderCount; record({ type: 'render', ...metrics }); }
    // Pi reloads extension modules independently. Its production registry already
    // shares live connections; inspect only their public payload-free counters.
    const connections = (globalThis as any)[Symbol.for('biomind.pi.domain-connections.v1')];
    for (const client of connections?.values() ?? []) if (typeof client.getPressure === 'function') {
      record({ type: 'transport', ...client.getPressure() });
    }
  };
  const timer = setInterval(sample, 100); timer.unref();
  pi.on('agent_end', () => { sample(); record({ type: 'agent_end', heapUsed: process.memoryUsage().heapUsed }); });
  pi.on('session_shutdown', () => { sample(); clearInterval(timer); });
}
