import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync, readFileSync, writeFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const here=path.dirname(fileURLToPath(import.meta.url));
function run(args){const result=spawnSync(process.execPath,[path.join(here,'cli.mjs'),...args],{encoding:'utf8',timeout:5000});assert.equal(result.error,undefined);return result;}
test('capture rejects impossible or nonnumeric seek times before browser launch',()=>{
 for(const value of ['-5','999','abc','']){const result=run(['capture','--times',value]);assert.notEqual(result.status,0);assert.match(result.stderr,/Capture times must be finite/);assert.doesNotMatch(result.stderr,/TimeoutError/);}
});
test('invalid supplied geometry fails before preview generation',()=>{
 const dir=mkdtempSync(path.join(tmpdir(),'blueprint-contract-'));
 try{for(const kind of ['wires','afterRect','text']){
  const spec=JSON.parse(readFileSync(path.join(here,'../../noncommercial/blueprint-animation/before-after.json'),'utf8'));
  if(kind==='wires')spec.wires='invalid';
  if(kind==='afterRect')spec.steps[0].afterRect=[1,2,3];
  if(kind==='text')spec.steps[0].problem={invalid:true};
  const file=path.join(dir,'input.json');writeFileSync(file,JSON.stringify(spec));
  const result=run(['build','--input',file,'--out',path.join(dir,'output')]);assert.notEqual(result.status,0);assert.match(result.stderr,/wires must be|Each step requires/);
 }}finally{rmSync(dir,{recursive:true,force:true});}
});
