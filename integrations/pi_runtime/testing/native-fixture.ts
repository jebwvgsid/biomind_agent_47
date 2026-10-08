import type { ExtensionAPI } from '@earendil-works/pi-coding-agent';
import fs from 'node:fs';
import { scriptedProvider } from './scripted-provider.js';
import { installNativePressureProbe } from './native-pressure-probe.js';
export default function fixture(pi: ExtensionAPI): void {
  const filename = process.env.BIOMIND_PI_PROVIDER_FIXTURE;
  if (!filename) throw new Error('Explicit native test fixture required');
  pi.registerProvider('biomind-fixture', scriptedProvider(filename));
  const trace = process.env.BIOMIND_PI_UI_TRACE;
  if (trace) {
    pi.on('ui_prompt_start', event => { fs.appendFileSync(trace, JSON.stringify(event)+'\n'); });
    pi.events.on('biomind:ui_request', request => fs.appendFileSync(trace, JSON.stringify({ type:'ui_request', request })+'\n'));
    pi.on('session_start', (event, ctx) => { fs.appendFileSync(trace, JSON.stringify({ type:'session_start', reason:event.reason, sessionId:ctx.sessionManager.getSessionId(), tools:pi.getActiveTools() })+'\n'); });
  }
  if (process.env.BIOMIND_PI_PRESSURE_TRACE) installNativePressureProbe(pi);
}
