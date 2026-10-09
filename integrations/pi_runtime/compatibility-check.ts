import { createHash } from 'node:crypto';

type PatchFile={path:string;originalSha256:string;patchedSha256:string;replacements:{before:string;after:string}[]};
const sha=(value:string|Buffer)=>createHash('sha256').update(value).digest('hex');
/** Accept a combined pinned patch only when its exact inverse proves the base. */
export function verifyObserverBytes(bytes:Buffer,expected:string,guard?:PatchFile):boolean {
  if(sha(bytes)===expected)return true;
  if(!guard||guard.originalSha256!==expected||sha(bytes)!==guard.patchedSha256)return false;
  let text=bytes.toString('utf8');
  for(const edit of [...guard.replacements].reverse()){
    if(!edit.after||text.split(edit.after).length!==2)return false;
    text=text.replace(edit.after,edit.before);
  }
  return sha(text)===expected;
}
