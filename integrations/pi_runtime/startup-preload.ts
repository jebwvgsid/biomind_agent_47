/** Loaded by the existing Python launcher before the host dependency graph. */
import { startup } from './startup-diagnostics.js';
startup.mark('node_start', 'begin');
process.once('exit', code => startup.mark('exit', 'exit', { exitCode: code }));
