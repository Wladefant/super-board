import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync, readFileSync, writeFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {chromium} from 'playwright';
const here=path.dirname(fileURLToPath(import.meta.url));
function run(args){const result=spawnSync(process.execPath,[path.join(here,'cli.mjs'),...args],{encoding:'utf8',timeout:30000});assert.equal(result.error,undefined);return result;}
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
test('transparent After replaces Before pixels during step and at final state',async()=>{
 const dir=mkdtempSync(path.join(tmpdir(),'blueprint-alpha-'));let browser;
 try{
  writeFileSync(path.join(dir,'before.svg'),'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect x="10" y="10" width="20" height="20" fill="#ff0000"/></svg>');
  writeFileSync(path.join(dir,'after.svg'),'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect x="60" y="10" width="20" height="20" fill="#0000ff"/></svg>');
  const step={name:'Move supplied square',problem:'Old location',fix:'New location',rect:[10,10,20,20],afterRect:[60,10,20,20]};
  writeFileSync(path.join(dir,'input.json'),JSON.stringify({width:100,height:100,screen:'before.svg',after:'after.svg',steps:[step,step,step]}));
  assert.equal(run(['build','--input',path.join(dir,'input.json'),'--out',path.join(dir,'output')]).status,0);
  browser=await chromium.launch({headless:true});const page=await browser.newPage();
  await page.goto(new URL('file:///'+path.join(dir,'output/index.html').replaceAll('\\','/')).href);
  await page.waitForFunction(()=>window.blueprint&&document.documentElement.dataset.ready==='true');
  async function pixel(t,x,y){
   await page.evaluate(t=>window.blueprint.seek(t),t);await page.waitForFunction(t=>Math.abs(+document.documentElement.dataset.time-t)<.002,t);
   const png=await page.locator('#capture').screenshot();
   return page.evaluate(async({data,x,y})=>{const img=new Image();img.src='data:image/png;base64,'+data;await img.decode();const canvas=document.createElement('canvas');canvas.width=img.width;canvas.height=img.height;const context=canvas.getContext('2d');context.drawImage(img,0,0);return [...context.getImageData(x,y,1,1).data];},{data:png.toString('base64'),x,y});
  }
  assert.deepEqual(await pixel(0,100,80),[255,0,0,255]);
  for(const t of [7.99,22]){assert.deepEqual(await pixel(t,100,80),[242,242,242,255]);assert.deepEqual(await pixel(t,150,80),[0,0,255,255]);}
 }finally{if(browser)await browser.close();rmSync(dir,{recursive:true,force:true});}
});
