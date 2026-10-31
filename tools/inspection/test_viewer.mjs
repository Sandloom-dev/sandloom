import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
const source=readFileSync(new URL('../../src/sandloom/core/cli/inspection/viewer.js',import.meta.url),'utf8');
const layoutSource=readFileSync(new URL('../../src/sandloom/core/cli/inspection/viewer.layout.js',import.meta.url),'utf8');
class Element {
  constructor(tag,attrs={},text=''){this.tag=tag;this.attrs={...attrs};this.textContent=text;this.children=[];this.style={};this.classList={add:()=>{}};}
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this.children=items;}
  setAttribute(key,value){this.attrs[key]=String(value);}
  contains(){return false;}
}
function render(items,links){
  const diagram=new Element('svg'), empty={};
  const context=vm.createContext({visibleNodes:items,visibleLinks:links,selectedId:'',viewbox:[],document:{activeElement:null},kinds:{module:'Business module'},cards:new Map(),name:id=>id,human:kind=>kind, svgEl:(...args)=>new Element(...args),$:(id)=>id==='diagram'?diagram:empty});
  vm.runInContext(layoutSource,context);
  vm.runInContext(source.slice(source.indexOf('function marker('),source.indexOf('function zoom(')),context);
  vm.runInContext('drawDiagram(true)',context);
  const boxes=diagram.children.filter(e=>e.tag==='g').map(g=>({id:g.attrs['data-item'],...g.children.find(e=>e.tag==='rect').attrs}));
  const paths=diagram.children.filter(e=>e.tag==='path'&&e.attrs.class.startsWith('edge ' )||e.tag==='path'&&e.attrs.class==='edge').map(e=>e.attrs.d);
  return {context,diagram,boxes,paths};
}
function points(d){
  const nums=d.match(/-?\d+(?:\.\d+)?/g).map(Number);
  if(d.includes('C')){const [x,y,a,b,c,f,tx,ty]=nums;return Array.from({length:201},(_,i)=>{const t=i/200,u=1-t;return [u*u*u*x+3*u*u*t*a+3*u*t*t*c+t*t*t*tx,u*u*u*y+3*u*u*t*b+3*u*t*t*f+t*t*t*ty];});}
  return nums.reduce((rows,n,i)=>{if(i%2===0)rows.push([n,nums[i+1]]);return rows;},[]);
}
function clearNodes(result,links){
  assert.equal(result.paths.length,links.length,'Every relationship needs a visible path');
  for(let i=0;i<links.length;i++){
    const route=points(result.paths[i]);
    for(const [id,p] of [[links[i].source,route[0]],[links[i].target,route.at(-1)]]){
      const box=result.boxes.find(b=>b.id===id);
      assert.ok((Math.abs(p[0]-box.x)<.00001||Math.abs(p[0]-box.x-box.width)<.00001)&&p[1]>=box.y&&p[1]<=box.y+box.height,'Arrow must attach to its actual endpoint');
    }
  }
  for(let i=0;i<links.length;i++)for(const box of result.boxes){
    if([links[i].source,links[i].target].includes(box.id))continue;
    const p=points(result.paths[i]);
    for(let j=0;j<p.length-1;j++){
      const [a,b]=[p[j],p[j+1]],steps=Math.max(1,Math.ceil(Math.hypot(a[0]-b[0],a[1]-b[1])/2));
      for(let k=0;k<=steps;k++) {const x=a[0]+(b[0]-a[0])*k/steps,y=a[1]+(b[1]-a[1])*k/steps;assert.ok(!(x>box.x&&x<box.x+box.width&&y>box.y&&y<box.y+box.height),`${links[i].source} → ${links[i].target} crosses unrelated ${box.id}`);}
    }
  }
}
const node=id=>({id,name:id,kind:'module'});
const edge=(source,target,i)=>({id:`edge:${i}`,source,target,kind:'required_contract'});
test('Helpdesk arrows never pass through identity or an intermediate module',()=>{
  const nodes=['tickets','identity','billing','tenancy'].map(node);
  const links=[['tickets','billing'],['tickets','tenancy'],['billing','tenancy']].map(([a,b],i)=>edge(a,b,i));
  const result=render(nodes,links);clearNodes(result,links);
  for(let i=0;i<result.paths.length;i++)for(let j=i+1;j<result.paths.length;j++){
    const a=points(result.paths[i]),b=points(result.paths[j]);
    for(let x=1;x<a.length;x++)for(let y=1;y<b.length;y++){
      const turn=(p,q,r)=>(q[0]-p[0])*(r[1]-p[1])-(q[1]-p[1])*(r[0]-p[0]);
      const cross=turn(a[x-1],a[x],b[y-1])*turn(a[x-1],a[x],b[y])<0&&turn(b[y-1],b[y],a[x-1])*turn(b[y-1],b[y],a[x])<0;
      assert.equal(cross,false,'The three Helpdesk arrows should not cross');
    }
  }
});
test('Every real project raw-interface arrow avoids foreign blocks and keeps readable scale',()=>{
  for(const name of ['root','academy','helpdesk','linkly','projecthub','tutorial']){
    const root=name==='root'?'../../':'../../examples/'+name+'/';
    const doc=JSON.parse(readFileSync(new URL(root+'.sandloom/inspection.json',import.meta.url),'utf8'));
    const graph=render(doc.nodes,doc.edges);
    clearNodes(graph,doc.edges);
    assert.ok(vm.runInContext('diagramScale',graph.context)>=.9,name+' must not shrink text to fit all entities');
  }
});
test('Cycles, disconnected components, parallel edges and self loops have obstacle-free routes',()=>{
  for(let seed=1;seed<=10;seed++){
    let state=seed;const random=n=>{state=(Math.imul(state,1664525)+1013904223)>>>0;return state%n;};
    const nodes=Array.from({length:12},(_,i)=>node('module:'+i));
    const links=Array.from({length:24},(_,i)=>edge(nodes[random(11)].id,nodes[random(11)].id,i));
    clearNodes(render(nodes,links),links);
  }
});
function reportFunctions(extra={}){
  const context=vm.createContext({nodes:new Map(),name:id=>id,human:kind=>kind,witnessDescription:()=>'',...extra});
  vm.runInContext(source.slice(source.indexOf('function declarationChanges('),source.indexOf('function inspection(')),context);
  return context;
}
test('Diff explains the added topup method, even when backward compatible',()=>{
  const context=reportFunctions();context.change={before:{interface:{methods:{charge:{kind:'command'}}}},after:{interface:{methods:{charge:{kind:'command'},topup:{kind:'command'}}}}};
  assert.deepEqual(Array.from(vm.runInContext('declarationChanges(change)',context)),['Added method: topup']);
});
test('Impact names the exact interface witness',()=>{
  const context=reportFunctions({nodes:new Map([['contract:payments.charges.v1',{kind:'contract'}]])});context.item={seed_witnesses:[{kind:'requires_contract',source:'module:enrollment',target:'contract:payments.charges.v1'}],witness_path:[]};
  assert.deepEqual(Array.from(vm.runInContext('impactEvidence(item)',context)),['requires_contract: contract:payments.charges.v1']);
});
test('Contract layer module filter includes that owner only, not every neighbor interface',()=>{
  const doc=JSON.parse(readFileSync(new URL('../../examples/helpdesk/.sandloom/inspection.json',import.meta.url),'utf8'));
  const elements={focus:{value:'module:tickets'},hops:{value:'1'},direction:{value:'both'},layer:{value:'interfaces'},relation:{value:''},query:{value:''}};
  const context=vm.createContext({$:(id)=>elements[id],allNodes:doc.nodes,allEdges:doc.edges,modules:doc.modules,nodes:new Map(doc.nodes.map(n=>[n.id,n])),cards:new Map(),relations:[{source:'module:tickets',target:'module:billing'},{source:'module:tickets',target:'module:tenancy'}]});
  vm.runInContext(source.slice(source.indexOf('function moduleScope('),source.indexOf('function searchHints(')),context);
  const model=vm.runInContext('layerModel()',context);
  assert.ok(model.nodes.length<15);
  assert.ok(model.links.every(e=>e.owner==='module:tickets'));
});

test('Route lifecycle wraps full labels at narrow widths instead of truncating them',()=>{
  const {context}=render([],[]),svg=new Element('svg');svg.parentElement={clientWidth:375};context.svg=svg;context.process={name:'POST /organizations/{organization_id}/tickets'};context.messages=[{source:'framework',target:'authorization',label:'Session policy: live, re-check before commit',condition:'refusal stops before the handler and the transaction never begins'}];
  vm.runInContext(source.slice(source.indexOf('function drawRoute('),source.indexOf('function drawStates(')),context);
  vm.runInContext('drawRoute(svg,messages,process)',context);
  assert.ok(svg.attrs.viewBox.startsWith('0 0 375 '));
  assert.equal(svg.style.width,'100%');
  const group=svg.children.find(e=>e.tag==='g');const rect=group.children[0];
  assert.ok(rect.attrs.x+rect.attrs.width<=375);
  const text=group.children.filter(e=>e.tag==='text').map(e=>e.textContent).join(' ');
  assert.ok(text.includes(context.messages[0].label));assert.ok(!text.includes('…'));
});
