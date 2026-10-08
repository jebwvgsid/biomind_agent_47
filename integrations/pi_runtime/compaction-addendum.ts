/** A single editable resource; Pi still owns every summarization operation. */
import { fileURLToPath } from 'node:url';
import { readEditableFile } from './instructions.js';

// Both the SDK module and the native extension bundle live in runtime/dist/.
// Resolve beside the installed runtime, never against the selected project.
export const compactionAddendumPath = fileURLToPath(new URL('../prompts/compaction-bioinformatics-v1.txt', import.meta.url));

export async function loadCompactionInstructions(customInstructions?: string): Promise<string> {
  const resource = await readEditableFile(compactionAddendumPath);
  if (!resource.exists) throw new Error(`BioMind 压缩增补文件不存在：${compactionAddendumPath}`);
  const text = resource.text.trim();
  // Read afresh for this operation. Never write into the event, a previous
  // summary or session state, so retries and later compactions cannot accumulate it.
  const addendum = text ? `<biomind_compaction_addendum_v1>\n${text}\n</biomind_compaction_addendum_v1>` : '';
  return [addendum, customInstructions].filter(part => part !== undefined && part !== '').join('\n\n');
}
