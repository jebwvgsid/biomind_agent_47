// Credential bytes arrive on stdin only; never start an agent or contact a model.
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const fail = code => { throw Object.assign(new Error(code), { safeCode: code }); };
try {
  const [modulePath, authPath] = process.argv.slice(2);
  if (!path.isAbsolute(modulePath ?? '') || !path.isAbsolute(authPath ?? '')) fail('ABSOLUTE_PATH_REQUIRED');
  const parent = path.dirname(authPath);
  if (fs.realpathSync(parent) !== parent) fail('AUTH_PARENT_SYMLINK_REFUSED');
  const stat = fs.lstatSync(authPath);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.uid !== process.getuid() || (stat.mode & 0o077)) fail('PRIVATE_AUTH_FILE_REQUIRED');
  const { AuthStorage } = await import(pathToFileURL(modulePath).href);
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > 16384) fail('CREDENTIAL_INPUT_TOO_LARGE');
    chunks.push(chunk);
  }
  const input = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  const key = input.key;
  if (typeof key !== 'string' || key.length < 8 || key.length > 8192 || /\s|[\x00-\x1f\x7f-\x9f]/.test(key) || key.startsWith('!')) fail('INVALID_LITERAL_CREDENTIAL');
  const store = AuthStorage.create(authPath);
  await store.modify('deepseek', current => {
    // The native lock covers the read/merge/write, including this no-overwrite check.
    if (current !== undefined) fail('CANDIDATE_PROVIDER_CREDENTIAL_ALREADY_EXISTS');
    return { type: 'api_key', key };
  });
  if (!(await store.list()).some(row => row.providerId === 'deepseek' && row.type === 'api_key')) fail('CREDENTIAL_METADATA_READBACK_FAILED');
  process.stdout.write(JSON.stringify({ result: 'PASS', provider: 'deepseek', store: 'native AuthStorage', credentialPrinted: false }) + '\n');
} catch (error) {
  process.stdout.write(JSON.stringify({ result: 'BLOCKED', code: error.safeCode ?? 'NATIVE_CREDENTIAL_SAVE_FAILED', credentialPrinted: false }) + '\n');
  process.exitCode = 2;
}
