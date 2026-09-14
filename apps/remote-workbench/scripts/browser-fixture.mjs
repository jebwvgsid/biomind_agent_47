import { readFileSync, writeFileSync } from 'node:fs';
const html = readFileSync(new URL('../dist/index.html', import.meta.url), 'utf8');
const smoke = readFileSync(new URL('../tests/browser-smoke.js', import.meta.url), 'utf8');
writeFileSync(new URL('../dist/browser-test.html', import.meta.url), html.replace('</body>', () => `<script>${smoke.replace(/<\/script/gi, '<\\/script')}</script></body>`));
console.log('Prepared dist/browser-test.html. Open it in a browser to RUN the DOM tests; preparing the file does not run or pass them.');
