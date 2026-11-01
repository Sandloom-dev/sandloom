import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import test from 'node:test';
import vm from 'node:vm';
const source=readFileSync(new URL('../../src/sandloom/core/cli/inspection/viewer.js',import.meta.url),'utf8');
const layoutSource=readFileSync(new URL('../../src/sandloom/core/cli/inspection/viewer.layout.js',import.meta.url),'utf8');
const {JSDOM}=createRequire(new URL('../../admin/package.json',import.meta.url))('jsdom');

test('hostile model text stays text and documentation links cannot execute JavaScript',()=>{
  const cli=fileURLToPath(new URL('../../.venv/bin/sandloom',import.meta.url));
  const app=fileURLToPath(new URL('../../examples/helpdesk',import.meta.url));
  const exported=spawnSync(cli,['graph','--root',app,'--format','html'],{encoding:'utf8'});
  assert.equal(exported.status,0,exported.stderr);
  const payload='<img src=x onerror="globalThis.untrustedExecuted=true">';
  let html=exported.stdout.replace(/(<script id="graph-data"[^>]*>)([\s\S]*?)(<\/script>)/,(_match,open,json,close)=>{
    const model=JSON.parse(json), data=model.workspace||model.data;
    const node=data.nodes.find(n=>n.id==='module:tickets');
    Object.assign(node,{name:payload,notes:[payload],docs:'javascript:globalThis.untrustedExecuted=true'});
    const card=data.modules.find(m=>m.id===node.id);card.summary=payload;
    return open+JSON.stringify(model).replaceAll('<','\\u003c')+close;
  });
  const dom=new JSDOM(html,{url:'https://viewer.example/#selected=module%3Atickets&selection-kind=entity',runScripts:'dangerously'});
  try {
    const document=dom.window.document;
    assert.ok(document.querySelector('#inspector-content').textContent.includes(payload));
    assert.equal(document.querySelectorAll('img, [onerror]').length,0);
    assert.notEqual(dom.window.untrustedExecuted,true);
    const docs=[...document.querySelectorAll('#inspector-content a')];
    assert.ok(docs.every(a=>!a.href||['https:','http:'].includes(new URL(a.href).protocol)));
  } finally {dom.window.close();}
});

test('documentation navigation accepts web URLs and rejects executable schemes',()=>{
  const dom=new JSDOM('',{url:'https://viewer.example/'});
  try {
    const context=vm.createContext({document:dom.window.document,URL:dom.window.URL});
    vm.runInContext(source.slice(source.indexOf('function el('),source.indexOf('function svgEl(')),context);
    for(const value of ['javascript:alert(1)','java\tscript:alert(1)','data:text/html,unsafe','file:///tmp/private',{}]) {
      context.value=value;
      const link=vm.runInContext('documentationLink("Docs",value)',context);
      assert.equal(link.getAttribute('href'),null);
      assert.equal(link.getAttribute('aria-disabled'),'true');
    }
    context.value='/docs/modules/between/events/';
    assert.equal(vm.runInContext('documentationLink("Docs",value).href',context),'https://sandloom.dev/docs/modules/between/events/');
    context.value='https://example.test/module-docs/';
    assert.equal(vm.runInContext('documentationLink("Docs",value).href',context),context.value);
  } finally {dom.window.close();}
});
// The real page in a DOM: covers what the vm slices below cannot see.
function livePage(root,hash=''){
  const cli=fileURLToPath(new URL('../../.venv/bin/sandloom',import.meta.url));
  const cwd=fileURLToPath(new URL('../../',import.meta.url));
  const exported=spawnSync(cli,['graph',...(root?['--root',root]:[]),'--format','html'],{cwd,encoding:'utf8',maxBuffer:1<<26});
  assert.equal(exported.status,0,exported.stderr);
  const dom=new JSDOM(exported.stdout,{url:'https://viewer.example/'+hash,runScripts:'dangerously',beforeParse(window){
    window.CSS={escape:value=>value};window.Element.prototype.scrollIntoView=()=>{};
  }});
  const document=dom.window.document, byId=id=>document.getElementById(id);
  const envelope=JSON.parse(byId('graph-data').textContent);
  return {dom,document,byId,data:envelope.workspace||envelope.data,search(text){byId('query').value=text;byId('query').dispatchEvent(new dom.window.Event('input'));}};
}
test('Search opens a consumer through its owner module, drawn and selected',()=>{
  const page=livePage('examples/projecthub');
  try {
    const consumer=page.data.nodes.find(n=>n.kind==='consumer');
    const owner=page.data.edges.find(e=>e.target===consumer.id&&e.kind==='owns_consumer').source;
    page.search(consumer.id);
    assert.match(page.byId('search-hints').querySelector('p').textContent,/^1 match across/);
    page.byId('search-hints').querySelector('button').click();
    assert.equal(page.byId('selection-state').hidden,true);
    assert.equal(page.byId('inspector-content').querySelector('h2').textContent,page.data.nodes.find(n=>n.id===owner).name);
    assert.ok(page.document.querySelector(`#diagram [data-item="${owner}"]`).getAttribute('class').includes('selected'));
  } finally {page.dom.window.close();}
});
test('The chooser replaces the diagram until a module is chosen',()=>{
  const page=livePage('','#layer=interfaces');
  try {
    const diagram=page.byId('diagram'), chooser=page.byId('canvas-choose');
    assert.equal(chooser.hidden,false);
    assert.equal(diagram.hasAttribute('hidden'),true);
    assert.equal(diagram.childElementCount,0);
    assert.equal(page.byId('save-svg').disabled,true);
    page.byId('nodes').querySelector('button').click();
    assert.equal(diagram.childElementCount,0);
    chooser.querySelector('.choose-grid button').click();
    assert.equal(chooser.hidden,true);
    assert.equal(diagram.hasAttribute('hidden'),false);
    assert.ok(diagram.childElementCount>0);
    assert.equal(page.byId('save-svg').disabled,false);
  } finally {page.dom.window.close();}
});
test('Keyboard selection of a block highlights its links and dims the rest',()=>{
  const page=livePage('examples/projecthub');
  try {
    const block=page.document.querySelector('#diagram [data-item="module:tasks"]');
    block.dispatchEvent(new page.dom.window.KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));
    assert.equal(page.byId('inspector-content').querySelector('h2').textContent,'tasks');
    assert.ok(page.document.querySelectorAll('#diagram .dim').length>0);
    assert.ok(page.document.querySelectorAll('#diagram .active').length>0);
  } finally {page.dom.window.close();}
});
class Element {
  constructor(tag,attrs={},text=''){this.tag=tag;this.attrs={...attrs};this.textContent=text;this.children=[];this.style={};this.classList={add:()=>{}};}
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this.children=items;}
  setAttribute(key,value){this.attrs[key]=String(value);}
  contains(){return false;}
}
function render(items,links){
  const diagram=new Element('svg'), empty={};
  const context=vm.createContext({visibleNodes:items,visibleLinks:links,selectedId:'',viewbox:[],document:{activeElement:null},kinds:{module:'Business module'},cards:new Map(),name:id=>id,human:kind=>kind, svgEl:(...args)=>new Element(...args),byId:(id)=>id==='diagram'?diagram:empty});
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
function overview(project){
  const doc=JSON.parse(readFileSync(new URL('../../examples/'+project+'/.sandloom/inspection.json',import.meta.url),'utf8'));
  const elements={focus:{value:''},hops:{value:'1'},direction:{value:'both'},layer:{value:'interfaces'},relation:{value:''},query:{value:''}};
  const context=vm.createContext({byId:(id)=>elements[id],allNodes:doc.nodes,allEdges:doc.edges,modules:doc.nodes.filter(n=>n.kind==='module'),nodes:new Map(doc.nodes.map(n=>[n.id,n])),edges:new Map(doc.edges.map(e=>[e.id,e])),cards:new Map(),relations:[],interfaceNames:()=>[]});
  vm.runInContext(layoutSource,context);
  vm.runInContext(source.slice(source.indexOf('function moduleScope('),source.indexOf('function searchHints(')),context);
  vm.runInContext(source.slice(source.indexOf('function edgesOf('),source.indexOf('function homeModules(')),context);
  vm.runInContext('function name(id){return nodes.get(id)?.name||id;}',context);
  return {context,elements};
}
test('A focused module shows its interfaces and the modules on their far side',()=>{
  const {context,elements}=overview('projecthub');elements.focus.value='module:tasks';
  const model=vm.runInContext('layerModel()',context),ids=Array.from(model.nodes,n=>n.id);
  assert.ok(ids.includes('module:taskboard'),'the caller of tasks.read.v1 must be visible');
  assert.ok(ids.includes('module:notifications'),'a listener of tasks.task.created.v1 must be visible');
  assert.ok(!ids.some(id=>id.startsWith('consumer:')||id.startsWith('projection:')),'consumers collapse into their owner');
  assert.ok(model.nodes.length<15);
});
test('An event names who publishes it and who receives it, by role',()=>{
  const {context}=overview('projecthub');
  const groups=vm.runInContext("connectionGroups(nodes.get('event:tasks.task.created.v1'))",context);
  const of=title=>Array.from(groups.find(g=>g.title===title).rows,r=>r.id).sort();
  assert.deepEqual(of('Published by'),['module:tasks']);
  assert.deepEqual(of('Delivered to'),['module:notifications','module:taskboard']);
});
test('A module lists what it provides, calls, publishes and listens to',()=>{
  const {context}=overview('projecthub');
  const groups=vm.runInContext("connectionGroups(nodes.get('module:taskboard'))",context);
  const of=title=>Array.from(groups.find(g=>g.title===title)?.rows||[],r=>r.id).sort();
  assert.deepEqual(of('Calls'),['contract:projects.read.v1','contract:tasks.read.v1']);
  assert.ok(of('Listens to').includes('event:tasks.task.created.v1'));
});
const projecthub=[['tasks','taskboard','event_impact','tasks.task.created.v1'],['tasks','taskboard','event_impact','tasks.task.updated.v1'],['projects','taskboard','event_impact','projects.project.created.v1'],['projects','taskboard','event_impact','projects.project.updated.v1'],['tasks','notifications','event_impact','tasks.task.created.v1'],['taskboard','projects','required_contract','projects.read.v1'],['taskboard','tasks','required_contract','tasks.read.v1'],['taskboard','projects','ordering',''],['taskboard','tasks','ordering',''],['notifications','tasks','ordering','']].map(([source,target,kind,via],i)=>({id:'r'+i,source,target,kind,via}));
function pairsOf(rows){const c=vm.createContext({});vm.runInContext(layoutSource,c);c.rows=rows;c.names=r=>r.via?[r.via]:[];return Array.from(vm.runInContext('pairLinks(rows,names)',c));}
test('Parallel declarations become one labelled line per direction; explained ordering stays in the list',()=>{
  const pairs=pairsOf(projecthub),label=(a,b)=>pairs.find(p=>p.source===a&&p.target===b)?.label;
  assert.equal(pairs.length,5);
  assert.equal(label('tasks','taskboard'),'2 events');
  assert.equal(label('taskboard','tasks'),'tasks.read.v1');
  assert.equal(label('notifications','tasks'),undefined);
  const [only]=pairsOf([{id:'o',source:'a',target:'b',kind:'ordering'}]);
  assert.equal(only.label,'load order');
});
test('Mutual links spread across columns, keep clear routes and readable labels',()=>{
  const items=['notifications','projects','taskboard','tasks'].map(node),links=pairsOf(projecthub).map(p=>({...p,members:undefined}));
  const result=render(items,links);clearNodes(result,links);
  assert.ok(new Set(result.boxes.map(b=>b.x)).size>=2,'four linked modules must not collapse into one column');
  const labels=result.diagram.children.filter(e=>e.tag==='text');
  assert.equal(labels.length,links.length,'Every connection carries a label here');
  const at=vm.runInContext('positions(visibleNodes,visibleLinks,cards)',result.context);
  const placed=vm.runInContext('placeLabels(routedLinks(positions(visibleNodes,visibleLinks,cards),visibleLinks),positions(visibleNodes,visibleLinks,cards))',result.context);
  const hit=(a,b)=>a.x<b.x+b.width&&a.x+a.width>b.x&&a.y<b.y+b.height&&a.y+a.height>b.y;
  for(const [i,{box}] of Array.from(placed).entries()){
    for(const block of at.values())assert.ok(!hit(box,block),'A label must not cover a block');
    for(const other of Array.from(placed).slice(i+1))assert.ok(!hit(box,other.box),'Labels must not cover each other');
  }
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
