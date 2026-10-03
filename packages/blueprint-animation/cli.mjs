import fs from 'node:fs/promises';
import path from 'node:path';
import http from 'node:http';
import {fileURLToPath} from 'node:url';
import {build} from 'esbuild';
const here=path.dirname(fileURLToPath(import.meta.url)),root=path.resolve(here,'../..');
const [command='build',...args]=process.argv.slice(2);
const option=(name,fallback)=>{const i=args.indexOf('--'+name);return i<0?fallback:args[i+1];};
const out=path.resolve(option('out',path.join(root,'artifacts/blueprint')));
const input=option('input','sample'),upstream=input==='sample';
async function make(){
 let spec={mode:'upstream'};
 if(!upstream){const file=path.resolve(input);spec=JSON.parse(await fs.readFile(file,'utf8'));if(!Number.isFinite(spec.width)||!Number.isFinite(spec.height)||spec.width<=0||spec.height<=0||!Array.isArray(spec.steps)||spec.steps.length<3||spec.steps.length>6)throw Error('Input requires positive width/height and 3–6 steps');for(const st of spec.steps){if(!st.name||!(st.what||st.problem)||!(st.why||st.fix)||!Array.isArray(st.rect)||st.rect.length!==4||st.rect.some(n=>!Number.isFinite(n)))throw Error('Each step requires name, what/why or problem/fix and numeric rect');}
 for(const key of ['screen','after'])if(spec[key]){const bytes=await fs.readFile(path.resolve(path.dirname(file),spec[key]));if(path.extname(spec[key]).toLowerCase()!=='.svg')throw Error('Screen inputs must be self-contained SVG exports');spec[key+'Data']='data:image/svg+xml;base64,'+bytes.toString('base64');}
 if(!spec.screenData)throw Error('screen SVG required');spec.mode=spec.after?'before-after':'explain';
 if(!spec.wires){const svg=await fs.readFile(path.resolve(path.dirname(file),spec.screen),'utf8');spec.wires=[...svg.matchAll(/<rect\b([^>]+)>/g)].map(m=>{const attrs=Object.fromEntries([...m[1].matchAll(/([\w-]+)="([^"]*)"/g)].map(a=>[a[1],a[2]]));return ['x','y','width','height','rx'].map(k=>Number(attrs[k]||0));}).filter(r=>r.every(Number.isFinite)&&r[2]>0&&r[3]>0);if(!spec.wires.length)throw Error('Supply wires for SVG without absolute rect elements');}
 }
 await fs.mkdir(out,{recursive:true});
 const entry=upstream?`import {mountUpstream} from './runtime.jsx';import '../../noncommercial/blueprint-animation/example-scene.jsx';mountUpstream();`:`import {mountInput} from './runtime.jsx';mountInput();`;
 await build({stdin:{contents:entry,resolveDir:here,loader:'jsx'},bundle:true,outfile:path.join(out,'bundle.js'),minify:false,define:{'process.env.NODE_ENV':'"production"'},jsx:'transform'});
 const css=`*{box-sizing:border-box}body{margin:0;background:#ddd;font-family:Arial}#capture{margin:auto}nav{position:sticky;bottom:0;display:flex;gap:12px;background:white;padding:12px;align-items:center}input{flex:1}button{padding:8px 16px}:root{--amp-gray-2:#f2f2f2;--amp-gray-3:#e0e0e0;--amp-gray-4:#c5c5c5;--amp-gray-5:#888;--amp-gray-6:#666;--amp-gray-7:#222;--amp-font-ui:Arial,sans-serif;--amp-font-display:Arial,sans-serif;--amp-error-bg:#ffeded;--amp-error:#c52c2c;--amp-pending:#bf5f1b;--amp-success-bg:#e9f5ec;--amp-success:#34834b}`;
 await fs.writeFile(path.join(out,'index.html'),`<!doctype html><html><meta charset="utf-8"><title>Noncommercial Blueprint preview</title><style>${css}</style><div id="root"></div><script>window.BLUEPRINT=${JSON.stringify(spec).replaceAll('<','\\u003c')};window.OM_SCENES=['Before','Actions','Details','Open work','Timeline','Panel','After'];window.OM_PLAYBACK={};</script><script src="bundle.js"></script></html>`);
 await fs.copyFile(path.join(root,'noncommercial/blueprint-animation/LICENSE'),path.join(out,'LICENSE'));
 await fs.writeFile(path.join(out,'ATTRIBUTION.txt'),'Noncommercial adaptation of Blueprint Animation by Oğuz Bülbül (@moguzbulbul). Source https://github.com/moguzbulbul/blueprint-animation/tree/29aa30b83db4632daf586420c52a251e7dac2d92 . CC BY-NC 4.0. Changes: original local React timeline adapter, controls, SVG-input renderer; no Claude Design runtime. Sample font tokens use Arial because original fonts are not supplied. Not an operator Figma design.\n');
 console.log(JSON.stringify({output:out,mode:spec.mode}));
}
function server(port){return new Promise(resolve=>{const srv=http.createServer(async(req,res)=>{try{const name=new URL(req.url,'http://localhost').pathname;const p=path.resolve(out,'.'+(name==='/'?'/index.html':name));if(!p.startsWith(out+path.sep))throw Error('Invalid path');const data=await fs.readFile(p);res.setHeader('Content-Type',p.endsWith('.js')?'text/javascript':p.endsWith('.html')?'text/html':'text/plain');res.end(data);}catch{res.writeHead(404);res.end();}});srv.listen(port,'127.0.0.1',()=>resolve(srv));});}
if(command==='build')await make();
else if(command==='preview'){await make();const port=Number(option('port','3582'));await server(port);console.log(`Ready http://127.0.0.1:${port}/`);}
else if(command==='capture'){
 await make();const {chromium}=await import('playwright');const srv=await server(0);let browser;
 try{browser=await chromium.launch({headless:true});const page=await browser.newPage({viewport:{width:1700,height:1350}});const errors=[];page.on('pageerror',e=>errors.push(e.message));await page.goto(`http://127.0.0.1:${srv.address().port}/`);await page.waitForFunction(()=>window.blueprint&&document.documentElement.dataset.ready==='true');const duration=await page.evaluate(()=>window.blueprint.duration);const times=option('times','0,2,3,4,5,6,8,'+(duration-2)).split(',').map(Number);const frames=[];
 for(const t of times){await page.evaluate(t=>window.blueprint.seek(t),t);await page.waitForFunction(t=>Math.abs(+document.documentElement.dataset.time-t)<.002,t);const name=`frame-${t.toFixed(2)}.png`;await page.locator('#capture').screenshot({path:path.join(out,name)});frames.push({time:t,file:name});}
 if(errors.length)throw Error(errors.join('\n'));await fs.writeFile(path.join(out,'frames.json'),JSON.stringify({duration,frames},null,2));console.log(`Captured ${frames.length} PNG frames`);
 }finally{if(browser)await browser.close();await new Promise(r=>srv.close(r));}
}else throw Error('Command must be build, preview or capture');
