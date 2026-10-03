import React, {createContext, useContext, useState, useEffect} from 'react';
import {createRoot} from 'react-dom/client';
const Context = createContext(null);
export const Easing = {easeOutCubic:p=>1-(1-p)**3,easeInOutCubic:p=>p<.5?4*p**3:1-(-2*p+2)**3/2,easeInOutQuad:p=>p<.5?2*p*p:1-(-2*p+2)**2/2};
export const useComposition = ()=>useContext(Context);
export function CompositionStage({width,height,bg,children}) {
  const spec=window.BLUEPRINT, upstream=spec.mode==='upstream';
  const CUES=upstream?{Before:0,Actions:1.4,Details:8.4,'Open work':16.8,Timeline:23.8,Panel:30.8,After:37.8}:{};
  const authoredTotal=upstream?43.4:spec.steps.length*7+2;
  const [T,setT]=useState(0),[playing,setPlaying]=useState(false);
  useEffect(()=>{window.blueprint={seek:t=>setT(Math.max(0,Math.min(authoredTotal,Number(t)))),play:()=>setPlaying(true),pause:()=>setPlaying(false),duration:authoredTotal};},[]);
  useEffect(()=>{if(!playing)return;let id,last=performance.now();const tick=now=>{const delta=(now-last)/1000;last=now;setT(t=>{if(t+delta>=authoredTotal){setPlaying(false);return authoredTotal;}return t+delta;});id=requestAnimationFrame(tick);};id=requestAnimationFrame(tick);return()=>cancelAnimationFrame(id);},[playing]);
  useEffect(()=>{document.documentElement.dataset.time=T.toFixed(3);document.documentElement.dataset.ready='true';},[T]);
  return <><div id="capture" style={{width,height,position:'relative',background:bg||'#f2f2f2',overflow:'hidden'}}><Context.Provider value={{T,CUES,authoredTotal}}>{children}</Context.Provider></div><nav><button onClick={()=>setPlaying(!playing)}>{playing?'Pause':'Play'}</button><button onClick={()=>{setT(0);setPlaying(true);}}>Replay</button><input aria-label="Seek" type="range" min="0" max={authoredTotal} step=".01" value={T} onChange={e=>{setPlaying(false);setT(+e.target.value);}}/><output>{T.toFixed(2)} / {authoredTotal.toFixed(2)} s</output></nav></>;
}
window.React=React;window.Easing=Easing;window.CompositionStage=CompositionStage;window.useComposition=useComposition;
const clamp=p=>Math.max(0,Math.min(1,p));
const tween=(t,a,b)=>Easing.easeInOutCubic(clamp((t-a)/(b-a)));
function InputPiece(){
 const {T}=useComposition(),s=window.BLUEPRINT,w=s.width,h=s.height;
 const index=Math.min(s.steps.length-1,Math.max(0,Math.floor((T-1)/7))),step=s.steps[index],t=(T-1-index*7)/1.4;
 const wipe=tween(t,.95,1.75),reveal=tween(t,3.25,4.05),construct=tween(t,1.9,3.2),focus=tween(t,0,.4)*(1-tween(t,4.4,5));
 const top=reveal*h,bottom=wipe*h,blue=bottom>top&&T>=1&&T<1+s.steps.length*7;
 const r=step.rect,a=s.after?step.afterRect||r:r,rect=r.map((v,i)=>v+(a[i]-v)*construct);
 const changes=s.after&&t>=1.8;
 // Each step reveals only its supplied target region. Prior regions remain changed.
 const completed=s.steps.slice(0,index+(changes?1:0));
 return <svg width={w+160} height={h+270} viewBox={`0 0 ${w+160} ${h+270}`}>
 <defs><clipPath id="band"><rect x="0" y={top} width={w} height={Math.max(0,bottom-top)}/></clipPath>{s.steps.map((st,i)=><clipPath id={'target'+i} key={i}><rect x={st.rect[0]} y={st.rect[1]} width={st.rect[2]} height={st.rect[3]}/>{st.afterRect&&<rect x={st.afterRect[0]} y={st.afterRect[1]} width={st.afterRect[2]} height={st.afterRect[3]}/>}</clipPath>)}</defs>
 <g transform="translate(80 60)"><image href={s.screenData} width={w} height={h}/>{s.after&&completed.map((st,i)=><image key={i} href={s.afterData} width={w} height={h} clipPath={`url(#target${i})`}/>)}{s.after&&T>=1+s.steps.length*7&&<image href={s.afterData} width={w} height={h}/>}
 {focus>0&&<path d={`M0 0H${w}V${h}H0Z M${r[0]} ${r[1]}V${r[1]+r[3]}H${r[0]+r[2]}V${r[1]}Z`} fill="white" fillRule="evenodd" opacity={focus*.78}/>}
 {blue&&<g clipPath="url(#band)"><rect width={w} height={h} fill="white"/><g stroke="#0B8FC2" fill="#2ACCFF14" opacity={.7-.4*construct}>{s.wires.map((b,i)=><g key={i}><rect x={b[0]} y={b[1]} width={b[2]} height={b[3]} rx={b[4]||0}/>{b[5]&&<text x={b[0]+8} y={b[1]+18} stroke="none" fill="#0B8FC2" fontSize="12">{b[5]}</text>}</g>)}</g><rect x={rect[0]} y={rect[1]} width={rect[2]} height={rect[3]} fill="none" stroke="#0B8FC2" strokeWidth="2"/>{[0,1].flatMap(x=>[0,1].map(y=><rect key={`${x}${y}`} x={rect[0]+x*rect[2]-2.5} y={rect[1]+y*rect[3]-2.5} width="5" height="5" fill="#0B8FC2" opacity={construct}/>))}<path d={`M${rect[0]} 0V${h} M0 ${rect[1]}H${w}`} stroke="#0B8FC2" strokeDasharray="2 4" opacity={construct}/></g>}
 {blue&&<path d={`M0 ${reveal>0?top:bottom}H${w}`} stroke="#0B8FC2" strokeWidth="2"/>}</g>
 {T>=1&&T<1+s.steps.length*7&&<g transform={`translate(80 ${h+155})`} fontFamily="Arial"><circle cx="16" cy="0" r="16" fill="#222"/><text x="16" y="5" fill="white" textAnchor="middle" fontSize="13">{index+1}</text><text x="44" y="5" fontSize="18">{step.name}</text><foreignObject x="0" y="25" width={w/2-20} height="100"><div xmlns="http://www.w3.org/1999/xhtml" style={{fontSize:18}}>{step.what||step.problem}</div></foreignObject><foreignObject x={w/2} y="25" width={w/2} height="100"><div xmlns="http://www.w3.org/1999/xhtml" style={{fontSize:18,color:'#0B8FC2',opacity:tween(t,3.65,4.15)}}>{step.why||step.fix}</div></foreignObject></g>}
 </svg>;
}
export function mountInput(){const s=window.BLUEPRINT;createRoot(document.getElementById('root')).render(<CompositionStage width={s.width+160} height={s.height+270}><InputPiece/></CompositionStage>);}
export function mountUpstream(){createRoot(document.getElementById('root')).render(<window.NorthwindBlueprintApp/>);}
