/** One scoped instruction action; no session, model runtime, domain or writer. */
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { realpath } from 'node:fs/promises';
import { parseArgs } from 'node:util';
import { readConfig } from './host.js';
import { inspectWelcomePreference } from './welcome-status.js';
import { controlInstructions, InstructionControlError } from './studio-instructions.js';

export async function queryInstructions(argv: string[], input: string) {
  const {values} = parseArgs({args:argv,options:{config:{type:'string'},project:{type:'string'}}});
  if (!values.config || !values.project || !path.isAbsolute(values.config) || !path.isAbsolute(values.project) || Buffer.byteLength(input)>1153434) throw new InstructionControlError('INSTRUCTIONS_INVALID_REQUEST');
  const config=readConfig(values.config),cwd=await realpath(values.project);
  const runtime=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
  const installRoot=path.resolve(config.installRoot ?? path.join(runtime,'../..'));
  // Use the owner's directory resolution without opening auth or settings.
  const {getAgentDir,loadProjectContextFiles}=await import('@earendil-works/pi-coding-agent');
  const agentDir=path.resolve(config.agentDir ?? (config.providerFixture ? path.join(cwd,'.biomind/pi-agent') : getAgentDir()));
  const preferencesPath=config.preferencesPath ?? process.env.BIOMIND_PI_PREFERENCES_PATH ?? process.env.BIOMIND_USER_PREFERENCES;
  const env={BIOMIND_PI_INSTALL_ROOT:installRoot,
    ...(preferencesPath ? {BIOMIND_PI_PREFERENCES_PATH:preferencesPath,BIOMIND_PI_PREFERENCES_SOURCE:config.preferencesPath!==undefined?'config':'environment'} : {}),
    ...(config.memoryRoot ? {BIOMIND_PI_MEMORY_ROOT:config.memoryRoot} : {})};
  const preference=await inspectWelcomePreference(cwd,env);
  const contextFiles=loadProjectContextFiles({cwd,agentDir});
  let request:unknown;try{request=JSON.parse(input);}catch{throw new InstructionControlError('INSTRUCTIONS_INVALID_REQUEST');}
  if (!request || typeof request!=='object' || Array.isArray(request)) throw new InstructionControlError('INSTRUCTIONS_INVALID_REQUEST');
  return controlInstructions({cwd,agentDir,preference,contextFiles,ownerLoaded:false},request as Record<string,any>);
}
if (process.argv[1] && path.resolve(process.argv[1])===fileURLToPath(import.meta.url)) {
  try {
    const chunks:Buffer[]=[];let bytes=0;
    for await(const chunk of process.stdin){const value=Buffer.from(chunk);bytes+=value.length;if(bytes>1153434)throw new InstructionControlError('INSTRUCTIONS_INVALID_REQUEST');chunks.push(value);}
    const data=await queryInstructions(process.argv.slice(2),Buffer.concat(chunks).toString('utf8'));
    process.stdout.write(JSON.stringify({status:'ok',data})+'\n');
  } catch(error) {
    process.stdout.write(JSON.stringify({status:'error',code:error instanceof InstructionControlError ? error.code : 'INSTRUCTIONS_READ_FAILED',
      ...(error instanceof InstructionControlError && error.currentVersion ? {currentVersion:error.currentVersion} : {})})+'\n');process.exitCode=1;
  }
}
