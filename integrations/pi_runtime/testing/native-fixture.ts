import type { ExtensionAPI } from '@earendil-works/pi-coding-agent';
import { scriptedProvider } from './scripted-provider.js';
import { installNativePressureProbe } from './native-pressure-probe.js';
export default function fixture(pi: ExtensionAPI): void {
  const filename = process.env.BIOMIND_PI_PROVIDER_FIXTURE;
  if (!filename) throw new Error('Explicit native test fixture required');
  pi.registerProvider('biomind-fixture', scriptedProvider(filename));
  if (process.env.BIOMIND_PI_PRESSURE_TRACE) installNativePressureProbe(pi);
}
