import { copyFileSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const preview = new URL('../preview/', import.meta.url);
mkdirSync(preview, { recursive: true });
const target = new URL('BioMind-Workbench.html', preview);
copyFileSync(new URL('../dist/index.html', import.meta.url), target);
console.log(`Exported ${fileURLToPath(target)}. This offline demo can be copied to Windows and opened directly.`);
