'use strict';
const $ = id => document.getElementById(id);
const envelope = JSON.parse($('graph-data').textContent);
const data = envelope.workspace || envelope.data;
const allNodes = data.nodes || [], allEdges = data.edges || [], relations = data.module_relations || [];
const nodes = new Map(allNodes.map(n => [n.id, n])), edges = new Map(allEdges.map(e => [e.id, e]));
const modules = allNodes.filter(n => n.kind === 'module');
const cards = new Map((data.modules || []).map(m => [m.id, m]));
const processes = envelope.sequences || [];
const processMap = new Map(processes.map(p => [p.id, p]));
const labels = {required_contract:'Calls contract · required', optional_contract:'Calls contract · optional', event_impact:'May deliver event', ordering:'Declares load ordering', provides_contract:'Provides contract', requires_contract:'Requires contract', produces_event:'Publishes event', consumes_event:'Delivers to consumer', owns_consumer:'Owns consumer', projects_event:'Feeds projection', owns_projection:'Owns projection', requires_capability:'Requires infrastructure'};
const kinds = {module:'Business module', contract:'Public contract', event:'Versioned event', consumer:'Event consumer', projection:'Projection', capability:'Infrastructure requirement'};
const ns = 'http://www.w3.org/2000/svg';
let selectedId = '', selectedKind = 'entity', currentView = 'overview', visibleNodes = [], visibleLinks = [], viewbox = [0,0,900,460];
let feedbackTimer;
function el(tag, text, className) { const e = document.createElement(tag); if(text !== undefined) e.textContent = text; if(className) e.className = className; return e; }
function svgEl(tag, attrs = {}, text) { const e = document.createElementNS(ns,tag); for(const [k,v] of Object.entries(attrs)) e.setAttribute(k,String(v)); if(text !== undefined) e.textContent = text; return e; }
function name(id) { return nodes.get(id)?.name || id; }
function human(kind) { return labels[kind] || kind.replaceAll('_',' '); }
function feedback(text) { $('feedback').textContent=text; clearTimeout(feedbackTimer); feedbackTimer=setTimeout(()=>{$('feedback').textContent='';},4500); }
function json(value) { return JSON.stringify(value,null,2); }
function shell(value) { return /^[a-zA-Z0-9_./:@=-]+$/.test(value) ? value : "'"+value.replaceAll("'", "'\"'\"'")+"'"; }
async function copy(text) {
  try { await navigator.clipboard.writeText(text); feedback('Copied to clipboard.'); }
  catch { const field=el('textarea'); field.value=text; $('inspector-content').append(field); field.focus(); field.select(); feedback('Clipboard unavailable. Selected text is ready to copy.'); }
}
function button(text, action) { const b=el('button',text); b.type='button'; b.onclick=action; return b; }
function fact(list,key,value) { if(value === undefined || value === null || value === '') return; list.append(el('dt',key),el('dd',typeof value === 'object' ? json(value) : String(value))); }
function stateURL() {
  const params=new URLSearchParams();
  for(const id of ['query','layer','relation','focus','direction','hops','process','process-view']) if($(id).value) params.set(id,$(id).value);
  params.set('view',currentView); if(selectedId) {params.set('selected',selectedId);params.set('selection-kind',selectedKind);}
  try { history.replaceState(null,'','#'+params.toString()); } catch { /* file: exports can still be inspected */ }
}
function selectView(view, save=true) {
  currentView=view;
  for(const v of ['overview','sequence','changes']) {
    $('panel-'+v).hidden=v!==view; $('tab-'+v).setAttribute('aria-selected',String(v===view)); $('tab-'+v).tabIndex=v===view?0:-1;
  }
  if(view==='sequence') drawProcess();
  if(save) stateURL();
}
for(const v of ['overview','sequence','changes']) {
  $('tab-'+v).onclick=()=>selectView(v);
  $('tab-'+v).onkeydown=e=>{
    const order=['overview','sequence','changes']; let next=order.indexOf(v);
    if(e.key==='ArrowRight') next=(next+1)%3; else if(e.key==='ArrowLeft') next=(next+2)%3; else if(e.key==='Home') next=0; else if(e.key==='End') next=2; else return;
    e.preventDefault(); selectView(order[next]); $('tab-'+order[next]).focus();
  };
}
function owners(item) {
  if(item.witnesses) return [...new Set(item.witnesses.map(id=>edges.get(id)?.owner).filter(Boolean))].sort();
  if(item.owner) return [item.owner];
  if(item.kind==='module') return [item.id];
  return [...new Set(allEdges.filter(e=>[e.source,e.target].includes(item.id)).map(e=>e.owner))].sort();
}
function witnessDescription(link) {
  const witness=(link.witnesses || [link.id]).map(id=>edges.get(id)).filter(Boolean);
  const interfaceIds=[...new Set(witness.flatMap(e=>[e.source,e.target]).filter(id=>nodes.get(id)?.kind==='contract'||nodes.get(id)?.kind==='event'))];
  return interfaceIds.map(name).join(', ');
}
function syncSelection() {
  for(const b of document.querySelectorAll('[data-selection]')) {
    const selected=b.dataset.selection===selectedId;
    b.classList.toggle('selected',selected);
    b.setAttribute('aria-pressed',String(selected));
  }
}
function impactRecord(item) {
  return {...item,id:'impact:'+item.module_id,name:name(item.module_id),owner:item.module_id,notes:[envelope.data.meaning]};
}
function resolveSelection(id,hint) {
  const kind=hint||(currentView==='changes'&&envelope.kind==='diff'?'change':currentView==='changes'&&envelope.kind==='impact'?'impact':'entity');
  if(kind==='change') {
    const item=envelope.kind==='diff'?envelope.data.changes.find(c=>c.id===id):null;
    return item?{item,kind}:null;
  }
  if(kind==='impact') {
    const item=envelope.kind==='impact'?envelope.data.affected.find(c=>'impact:'+c.module_id===id):null;
    return item?{item:impactRecord(item),kind}:null;
  }
  const match=id.match(/^(.*)#(sequence|compensation|state)-step-(\d+)$/);
  if(match) {
    const p=processMap.get(match[1]);
    const source=match[2]==='state'?envelope.saga_states?.transitions:match[2]==='compensation'?p?.alternatives:p?.steps;
    const step=source?.[Number(match[3])];
    return step&&p?{item:{...step,id,owner:p.owner,parent_id:p.id},kind:'step'}:null;
  }
  const item=nodes.get(id)||edges.get(id)||relations.find(r=>r.id===id)||processMap.get(id)?.declaration;
  return item?{item,kind}:null;
}
function inspection(item, kind='entity') {
  selectedId=item.id || ''; selectedKind=kind; const target=$('inspector-content'); target.replaceChildren();
  const title=item.name || item.label || (item.source && item.target ? `${name(item.source)} → ${name(item.target)}` : item.label || item.id || 'Selection');
  target.append(el('span',kind==='step'?'PROCESS STEP':kind==='change'?'ARCHITECTURE CHANGE':kinds[item.kind] || human(item.kind || kind),'eyebrow'),el('h2',title));
  const list=el('dl');
  fact(list,'Stable ID',item.id);
  if(item.source && item.target) { fact(list,'From',name(item.source)); fact(list,'To',name(item.target)); fact(list,'Meaning',human(item.kind || kind)); }
  const witness=witnessDescription(item); if(witness) fact(list,'Public interface',witness);
  const witnesses=(item.witnesses||[]).map(id=>edges.get(id)).filter(Boolean);
  for(const w of witnesses) { if(w.required!==undefined) fact(list,'Required',w.required?'Yes':'Optional'); if(w.methods?.length) fact(list,'Declared methods',w.methods.join(', ')); if(w.reason) fact(list,'Reason',w.reason); }
  if(item.required !== undefined) fact(list,'Required',item.required ? 'Yes' : 'Optional; caller must handle absence');
  if(item.owner) fact(list,'Owner',name(item.owner));
  for(const [key,label] of [['condition','Condition'],['call','Call'],['transaction','Unit boundary'],['durable','Durable consumer'],['group','Consumer group'],['reason','Declaration reason']]) fact(list,label,item[key]);
  const module=cards.get(item.id); if(module) { fact(list,'Package',module.import_path); fact(list,'Database chain',module.chain); fact(list,'Models',module.models?.length??module.model_count); fact(list,'Routes',module.routes?.length??module.route_count); }
  const ownerIds=owners(item); for(const owner of ownerIds) {
    const evidence=data.evidence?.[owner]; fact(list,'Source · '+name(owner),evidence?.source_root || 'Installed distribution; local implementation unavailable');
    if(evidence?.distributions && Object.keys(evidence.distributions).length) fact(list,'Installed distributions',evidence.distributions);
  }
  if(kind==='step') fact(list,'Step kind',item.kind);
  if(item.seed_witnesses){fact(list,'Impact scope',item.scope);fact(list,'Seed module',name(item.seed_module_id));fact(list,'Direct witnesses',item.seed_witnesses.map(e=>human(e.kind)+': '+name(e.source)+' → '+name(e.target)).join('; '));fact(list,'Transitive path',(item.witness_path||[]).map(r=>name(r.source)+' → '+name(r.target)+' · '+human(r.kind)).join('; ')||'Direct interface / owner');}
  fact(list,'Evidence',item.evidence_kind || (kind==='step'?'Declared sequence / conditional framework phase':'Registered authoring declaration'));
  target.append(list);
  if(kind==='change') { const c=item.compatibility || {}; target.append(el('h3','Compatibility'),el('p',c.state || 'Not assessed')); for(const reason of c.reasons || []) target.append(el('p',reason)); }
  if(item.notes) for(const note of item.notes) target.append(el('p',note,'muted'));
  const interfaceId = witnesses.flatMap(w=>[w.source,w.target]).find(id=>['contract','event'].includes(nodes.get(id)?.kind));
  if(item.id && (nodes.has(item.id)||processMap.has(item.id)||item.parent_id||interfaceId)) {
    target.append(el('h3','Next actions'));
    for(const owner of ownerIds.filter(id=>data.evidence?.[id]?.source_root).slice(0,6)) {
      const command=['sandloom','context',name(owner),'--focus',interfaceId||item.parent_id||item.id].map(shell).join(' ');
      target.append(button('Copy context command · '+name(owner),()=>copy(command)));
    }
    target.append(button('Inspect potential impact',()=>showImpact(nodes.get(interfaceId)||processMap.get(item.parent_id)?.declaration||item)));
    target.append(button('Copy stable ID',()=>copy(item.id)));
  }
  if(item.test_argv) target.append(button('Copy suggested module test',()=>copy(item.test_argv.map(shell).join(' '))));
  if(item.source_location) target.append(button('Copy source location',()=>copy(item.source_location.path+':'+item.source_location.line)));
  const details=el('details'); details.append(el('summary','Declaration evidence'),el('pre',json(witnesses.length?{...item,witnesses}:item))); target.append(details);
  const doc=el('a','Read the documentation'); doc.href=item.docs||'https://sandloom.dev/docs/concepts/'+(item.kind==='event'?'events/':item.kind==='contract'?'contracts/':item.id?.startsWith('saga:')?'sagas/':item.id?.startsWith('route:')?'lifecycle/':'modules/'); target.append(doc);
  syncSelection();
  $('inspect-selection').hidden=false; feedback('Selected '+title+'. Details are available in the inspector.');
  drawDiagram(); stateURL();
}
function showImpact(item) {
  selectedId=item.id;selectedKind='impact-preview';stateURL();
  const found=new Map(owners(item).map(id=>[id,[]])), pending=[...found.keys()];
  while(pending.length) {
    const id=pending.shift();
    for(const r of relations) {
      if(r.kind==='ordering') continue;
      const source=r.kind.endsWith('_contract')?r.target:r.source, target=r.kind.endsWith('_contract')?r.source:r.target;
      if(source===id&&!found.has(target)) { found.set(target,[...found.get(id),r]); pending.push(target); }
    }
  }
  const target=$('inspector-content'); target.replaceChildren(el('span','POTENTIAL IMPACT','eyebrow'),el('h2',item.name || item.id),el('p','Direct interface users plus transitive module reachability, including other interfaces. This overapproximates possible impact and is not complete test coverage.','muted'));
  for(const [id,path] of [...found].sort()) {
    const b=button(name(id)+' ',()=>inspection(nodes.get(id))); b.className='change-button'; b.append(el('span',path.length?path.map(r=>witnessDescription(r)||human(r.kind)).join(' → '):'Direct owner / user · '+allEdges.filter(e=>e.owner===id&&[e.source,e.target].includes(item.id)).map(e=>human(e.kind)).join(', '))); target.append(b);
  }
  target.append(button('Copy impact query',()=>copy(['sandloom','graph','impact',item.id].map(shell).join(' '))));
  target.append(el('p','Run normal quality gates; do not skip tests based on this view.','notice'));
  $('inspect-selection').hidden=false;syncSelection();drawDiagram();
  feedback('Potential impact is available in the inspector.');
}
function choose(item,kind) { inspection(item,kind); }
function moduleScope() {
  if(!$('focus').value) return new Set(modules.map(m=>m.id));
  const selected=new Set([$('focus').value]), direction=$('direction').value;
  for(let i=0;i<Number($('hops').value);i++) {
    const next=[];
    for(const r of relations) {
      if(direction!=='incoming'&&selected.has(r.source)) next.push(r.target);
      if(direction!=='outgoing'&&selected.has(r.target)) next.push(r.source);
    }
    next.forEach(id=>selected.add(id));
  }
  return selected;
}
function layerModel() {
  const selected=moduleScope(), layer=$('layer').value;
  let links, candidates;
  if(layer==='modules') { candidates=modules.filter(n=>selected.has(n.id)); links=relations.filter(r=>selected.has(r.source)&&selected.has(r.target)); }
  else {
    const allowed=layer==='interfaces'?new Set(['module','contract','event','consumer','projection']):new Set(['module','capability']);
    links=allEdges.filter(e=>selected.has(e.owner)&&allowed.has(nodes.get(e.source)?.kind)&&allowed.has(nodes.get(e.target)?.kind));
    const ids=new Set([...selected,...links.flatMap(e=>[e.source,e.target])]); candidates=allNodes.filter(n=>ids.has(n.id)&&allowed.has(n.kind));
  }
  const filter=$('relation').value; if(filter) { links=links.filter(e=>e.kind===filter); const ids=new Set(links.flatMap(e=>[e.source,e.target])); candidates=candidates.filter(n=>ids.has(n.id)); }
  const query=$('query').value.trim().toLocaleLowerCase(); let matches=candidates.length;
  if(query) {
    const matched=new Set(candidates.filter(n=>(n.name+' '+n.id).toLocaleLowerCase().includes(query)).map(n=>n.id)); matches=matched.size;
    links=links.filter(e=>matched.has(e.source)||matched.has(e.target));
    const ids=new Set([...matched,...links.flatMap(e=>[e.source,e.target])]); candidates=candidates.filter(n=>ids.has(n.id));
  }
  return {nodes:candidates,links,matches};
}
function searchHints(model) {
  const target=$('search-hints');target.replaceChildren();target.hidden=true;
  const query=$('query').value.trim().toLocaleLowerCase();if(!query||model.matches) return;
  const groups=new Map();
  for(const n of allNodes.filter(n=>(n.name+' '+n.id).toLocaleLowerCase().includes(query))) {
    const layer=n.kind==='module'?'modules':n.kind==='capability'?'infrastructure':'interfaces';
    groups.set(layer,(groups.get(layer)||0)+1);
  }
  if(!groups.size) return;
  target.hidden=false;target.append(el('p','Matching entities exist elsewhere in this export. Your current layer or filters hide them.'));
  for(const [layer,count] of groups) {
    const label=[...$('layer').options].find(o=>o.value===layer).textContent;
    target.append(button(`Show ${count} in ${label}`,()=>{
      $('layer').value=layer;$('focus').value='';relationOptions();$('relation').value='';renderOverview();
      $('query').focus({preventScroll:true});feedback('Searching the entire exported scope in '+label+'.');
    }));
  }
}
function relationOptions() {
  const previous=$('relation').value, layer=$('layer').value;
  const kinds=layer==='modules'?new Set(relations.map(r=>r.kind)):new Set(allEdges.filter(e=>layer==='infrastructure'?e.kind==='requires_capability':e.kind!=='requires_capability'&&e.kind!=='declares_ordering').map(e=>e.kind));
  $('relation').replaceChildren(el('option','All relation types')); $('relation').firstChild.value='';
  for(const kind of [...kinds].sort()) { const o=el('option',human(kind)); o.value=kind; $('relation').append(o); }
  if(kinds.has(previous)) $('relation').value=previous;
}
function renderOverview(fit=true) {
  const model=layerModel(); visibleNodes=model.nodes; visibleLinks=model.links;
  $('canvas-title').textContent=$('layer').selectedOptions[0].textContent;
  $('counts').textContent=`${visibleNodes.length} visible entities · ${visibleLinks.length} relationships · ${model.matches} search matches · ${allNodes.length-visibleNodes.length} entities outside this view`;
  $('entity-count').textContent=visibleNodes.length; $('relation-count').textContent=visibleLinks.length;
  $('nodes').replaceChildren();
  for(const n of visibleNodes) { const b=button(n.name,()=>choose(n)); b.dataset.selection=n.id; b.classList.toggle('selected',selectedId===n.id); b.append(el('span',kinds[n.kind]||n.kind)); $('nodes').append(b); }
  if(!visibleNodes.length) $('nodes').append(el('p','No entities match these filters.','empty'));
  $('rows').replaceChildren();
  for(const r of visibleLinks) { const b=button(`${name(r.source)} → ${name(r.target)}`,()=>choose(r,'relationship')); b.dataset.selection=r.id; b.classList.toggle('selected',selectedId===r.id); b.append(el('span',human(r.kind)+(witnessDescription(r)?' · '+witnessDescription(r):''))); $('rows').append(b); }
  if(!visibleLinks.length) $('rows').append(el('p','No relationships in this scope. An isolated module can still be inspected.','empty'));
  const outside=selectedId&&!visibleNodes.some(n=>n.id===selectedId)&&!visibleLinks.some(r=>r.id===selectedId);
  $('selection-state').hidden=!outside; $('selection-state').textContent='Selected item is outside these overview filters. Clear selection or reset filters to return.';
  searchHints(model);syncSelection();drawDiagram(fit); stateURL();
}
// Condense strongly connected components before assigning DAG ranks.
function positions(items,links) {
  const ids=items.map(n=>n.id).sort(), adjacent=new Map(ids.map(id=>[id,[]])), reverse=new Map(ids.map(id=>[id,[]]));
  for(const r of links) if(adjacent.has(r.source)&&adjacent.has(r.target)) { adjacent.get(r.source).push(r.target); reverse.get(r.target).push(r.source); }
  const visited=new Set(), finish=[];
  for(const id of ids) {
    if(visited.has(id)) continue; const stack=[[id,false]];
    while(stack.length) { const [at,exit]=stack.pop(); if(exit) { finish.push(at); continue; } if(visited.has(at)) continue; visited.add(at); stack.push([at,true]); for(const next of adjacent.get(at)) if(!visited.has(next)) stack.push([next,false]); }
  }
  const components=[], component=new Map();
  for(const id of finish.reverse()) {
    if(component.has(id)) continue; const group=[], queue=[id], index=components.length; component.set(id,index);
    while(queue.length) { const at=queue.pop(); group.push(at); for(const next of reverse.get(at)) if(!component.has(next)) { component.set(next,index); queue.push(next); } }
    components.push(group.sort());
  }
  const ranks=components.map(()=>0), outgoing=components.map(()=>new Set()), indegree=components.map(()=>0);
  for(const r of links) { const a=component.get(r.source), b=component.get(r.target); if(a===undefined||b===undefined||a===b||outgoing[a].has(b)) continue; outgoing[a].add(b); indegree[b]++; }
  const ready=indegree.map((n,i)=>n===0?i:-1).filter(i=>i>=0);
  while(ready.length) { const a=ready.shift(); for(const b of outgoing[a]) { ranks[b]=Math.max(ranks[b],ranks[a]+1); if(--indegree[b]===0) ready.push(b); } }
  const result=new Map(); let rankX=40;
  for(const rank of [...new Set(ranks)].sort((a,b)=>a-b)) {
    const groups=components.filter((_,i)=>ranks[i]===rank);
    const blocks=groups.map(group=>{const cols=Math.ceil(Math.sqrt(group.length));return {group,cols,width:cols*260,height:Math.ceil(group.length/cols)*104};});
    const shelfWidth=Math.max(520,...blocks.map(b=>b.width));let x=0,y=40,rowHeight=0;
    for(const block of blocks) {
      if(x+block.width>shelfWidth){x=0;y+=rowHeight+24;rowHeight=0;}
      block.group.forEach((id,i)=>result.set(id,{x:rankX+x+(i%block.cols)*260,y:y+Math.floor(i/block.cols)*104}));
      x+=block.width;rowHeight=Math.max(rowHeight,block.height);
    }
    rankX+=shelfWidth+80;
  }
  return result;
}
function marker(svg,id='arrow') { const defs=svgEl('defs'), mark=svgEl('marker',{id,viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:7,markerHeight:7,orient:'auto-start-reverse'}); mark.append(svgEl('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:'currentColor'})); defs.append(mark); svg.append(defs); }
function applyBox() { $('diagram').setAttribute('viewBox',viewbox.join(' ')); }
function drawDiagram(fit=false) {
  const svg=$('diagram'); const restoreFocus = svg.contains(document.activeElement) ? document.activeElement.getAttribute('data-item') : null; svg.replaceChildren(); marker(svg); const at=positions(visibleNodes,visibleLinks);
  $('canvas-empty').hidden=visibleNodes.length>0; svg.hidden=!visibleNodes.length;
  for(let i=0;i<visibleLinks.length;i++) {
    const r=visibleLinks[i], a=at.get(r.source), b=at.get(r.target); if(!a||!b) continue;
    const forward=b.x>a.x, ax=a.x+(forward?220:0), bx=b.x+(forward?0:220), ay=a.y+34, by=b.y+34;
    const bend=Math.max(60,Math.abs(bx-ax)*.45)+(i%3)*9;
    const d=a.x===b.x?`M ${a.x+220} ${ay} C ${a.x+280+(i%3)*14} ${ay}, ${b.x+280+(i%3)*14} ${by}, ${b.x+220} ${by}`:`M ${ax} ${ay} C ${ax+(forward?bend:-bend)} ${ay}, ${bx+(forward?-bend:bend)} ${by}, ${bx} ${by}`;
    const type=r.kind.includes('event')?' event':r.kind.includes('optional')||r.kind==='ordering'||r.required===false?' optional':'';
    const path=svgEl('path',{d,class:'edge'+type+(selectedId===r.id?' selected':''),'marker-end':'url(#arrow)'});
    path.append(svgEl('title',{},`${name(r.source)} → ${name(r.target)}: ${human(r.kind)}`)); svg.append(path);
    const hit=svgEl('path',{d,class:'edge-hit',tabindex:0,role:'button','data-item':r.id,'aria-label':`${name(r.source)} to ${name(r.target)}: ${human(r.kind)}`}); hit.onclick=()=>choose(r,'relationship'); hit.onkeydown=e=>{if(['Enter',' '].includes(e.key)){e.preventDefault();choose(r,'relationship');}}; svg.append(hit);
    if(selectedId===r.id) svg.append(svgEl('text',{x:(ax+bx)/2,y:(ay+by)/2-10,class:'edge-label','text-anchor':'middle'},human(r.kind)));
  }
  for(const n of visibleNodes) {
    const p=at.get(n.id), group=svgEl('g',{class:'node'+(selectedId===n.id?' selected':''),tabindex:0,role:'button','data-item':n.id,'aria-label':`${kinds[n.kind]||n.kind}: ${n.name}`});
    group.append(svgEl('title',{},n.name),svgEl('rect',{x:p.x,y:p.y,width:220,height:68,rx:8}));
    group.append(svgEl('text',{x:p.x+14,y:p.y+22,class:'kind'},kinds[n.kind]||n.kind));
    group.append(svgEl('text',{x:p.x+14,y:p.y+45},n.name.length>25?n.name.slice(0,24)+'…':n.name));
    group.onclick=()=>choose(n); group.onkeydown=e=>{if(['Enter',' '].includes(e.key)){e.preventDefault();choose(n);}}; svg.append(group);
  }
  if(fit) {
    const width=Math.max(500,...[...at.values()].map(p=>p.x+300)), height=Math.max(250,...[...at.values()].map(p=>p.y+110));
    viewbox=[0,0,width,height];
  }
  applyBox();
  if(restoreFocus) [...svg.querySelectorAll('[data-item]')].find(n=>n.getAttribute('data-item')===restoreFocus)?.focus({preventScroll:true});
}
function zoom(factor) { const [x,y,w,h]=viewbox; viewbox=[x+w*(1-factor)/2,y+h*(1-factor)/2,w*factor,h*factor]; if(viewbox[2]<120||viewbox[2]>20000) {viewbox=[x,y,w,h];return;} applyBox(); }
$('zoom-in').onclick=()=>zoom(.8); $('zoom-out').onclick=()=>zoom(1.25); $('fit').onclick=()=>drawDiagram(true);
for(const [id,dx,dy] of [['pan-left',-.2,0],['pan-right',.2,0],['pan-up',0,-.2],['pan-down',0,.2]]) $(id).onclick=()=>{viewbox[0]+=viewbox[2]*dx;viewbox[1]+=viewbox[3]*dy;applyBox();};
let drag;
$('diagram').onpointerdown=e=>{if(e.target.closest('[role=button]')) return; drag={x:e.clientX,y:e.clientY,box:[...viewbox]}; $('diagram').setPointerCapture(e.pointerId); $('diagram').classList.add('dragging');};
$('diagram').onpointermove=e=>{if(!drag)return;const rect=$('diagram').getBoundingClientRect();const scale=Math.max(drag.box[2]/rect.width,drag.box[3]/rect.height);viewbox=[drag.box[0]-(e.clientX-drag.x)*scale,drag.box[1]-(e.clientY-drag.y)*scale,drag.box[2],drag.box[3]];applyBox();};
for(const event of ['pointerup','pointercancel','lostpointercapture']) $('diagram').addEventListener(event,()=>{drag=null;$('diagram').classList.remove('dragging');});
function drawProcess() {
  const process=processMap.get($('process').value), svg=$('sequence-diagram'); svg.replaceChildren(); $('steps').replaceChildren(); $('process-notes').replaceChildren();
  if(!process) { $('process-summary').textContent=processes.length?'Choose a process. Each sequence has an equivalent numbered step list.':'No recorded processes. Regenerate the authoring index to collect saga and route declarations.'; $('process-notes').textContent='Module edges cannot supply business chronology. No observed attempt is recorded in this export.'; return; }
  const state=$('process-view').value==='state';
  if(state&&!process.id.startsWith('saga:')) { $('process-summary').textContent='State view applies to sagas. Choose a saga or switch to Sequence.'; return; }
  $('process-summary').textContent=process.evidence;
  const transitions=envelope.saga_states?.transitions || [];
  const compensation=$('process-view').value==='compensation';
  if(compensation&&!process.id.startsWith('saga:')) { $('process-summary').textContent='Compensation applies to declared sagas. Choose a saga or Forward sequence.'; return; }
  $('process-summary').textContent=(state?'Conditional saga states':compensation?'Alternative: compensation before pivot':'Forward path')+' · '+process.evidence;
  let messages=state?transitions.map(t=>({...t,condition:'Conditional framework transition'})):compensation?process.alternatives:process.steps;
  const participants=[...new Set(messages.flatMap(m=>[m.source,m.target]))], ids=new Map(participants.map((p,i)=>[p,i]));
  const width=Math.max(650,participants.length*190+60), height=100+messages.length*84;
  svg.setAttribute('viewBox',`0 0 ${width} ${height}`); svg.style.height=height+'px'; svg.style.width=width+'px'; marker(svg,'sequence-arrow');
  for(const [participant,i] of ids) { const x=40+i*190; svg.append(svgEl('rect',{x,y:18,width:160,height:44,rx:6}),svgEl('text',{x:x+80,y:45,'text-anchor':'middle'},name(participant).length>22?name(participant).slice(0,21)+'…':name(participant)),svgEl('line',{x1:x+80,x2:x+80,y1:62,y2:height-10,class:'lifeline'})); }
  let compensationHeading=false;
  messages.forEach((message,i)=>{
    const y=98+i*84, a=120+ids.get(message.source)*190, b=120+ids.get(message.target)*190, undo=message.kind==='compensation';
    const path=svgEl('path',{d:a===b?`M ${a} ${y} h 55 v 25 h -55`:`M ${a} ${y} H ${b}`,class:'message'+(undo?' undo':''),'marker-end':'url(#sequence-arrow)'});
    path.append(svgEl('title',{},message.label+' · '+(message.condition||''))); svg.append(path,svgEl('text',{x:Math.min(a,b)+8,y:y-12},`${i+1}. ${message.label.length>32?message.label.slice(0,31)+'…':message.label}${message.kind==='pivot'?' · PIVOT':''}`));
    if(undo&&!compensationHeading) { $('steps').append(el('h3','Alternative: compensation before pivot')); compensationHeading=true; }
    const item={...message,id:process.id+'#'+$('process-view').value+'-step-'+i,owner:process.owner,parent_id:process.id};
    const bttn=button(`${i+1}. ${message.label}`,()=>inspection(item,'step')); bttn.className='step-button'; bttn.dataset.selection=item.id; bttn.append(el('span',`${name(message.source)} → ${name(message.target)} · ${message.kind?message.kind+' · ':''}${message.condition||'declared step'}`)); $('steps').append(bttn);
  });
  if(state) drawStates(svg,transitions);
  if(!messages.length) $('steps').append(el('p','No declared compensation callbacks. This does not imply rollback across provider commits.','notice'));
  syncSelection();
  $('steps-title').textContent=state?'Conditional state transitions':compensation?'Compensation alternatives · reverse completed steps':'Forward steps';
  for(const note of process.notes) $('process-notes').append(el('p',note));
  const a=el('a','Lifecycle documentation'); a.href=process.docs; $('process-notes').append(a,button('Inspect process declaration',()=>inspection({...process.declaration,name:process.name})),button('Copy Mermaid command',()=>copy(['sandloom','graph',state?'state':'sequence',process.id,'--format','mermaid'].map(shell).join(' '))));
}
function drawStates(svg,transitions) {
  svg.replaceChildren(); marker(svg,'state-arrow'); svg.setAttribute('viewBox','0 0 720 640');svg.style.width='720px';svg.style.height='640px';
  const at={running:[60,60],completed:[440,60],compensating:[60,300],compensated:[440,300],parked:[250,540]};
  for(const t of transitions) {
    const a=at[t.source],b=at[t.target];let d;
    if(t.source===t.target) d=`M ${a[0]+155} ${a[1]+10} C ${a[0]+240} ${a[1]-65},${a[0]+235} ${a[1]+95},${a[0]+155} ${a[1]+40}`;
    else if(t.source==='running'&&t.target==='parked') d='M 140 114 V 155 H 680 V 567 H 410';
    else if(a[1]===b[1]) d=`M ${a[0]+160} ${a[1]+27} L ${b[0]} ${b[1]+27}`;
    else d=`M ${a[0]+80} ${a[1]+54} L ${b[0]+80} ${b[1]}`;
    const line=svgEl('path',{d,class:'message','marker-end':'url(#state-arrow)'});line.append(svgEl('title',{},t.label));svg.append(line);
    const short=t.short_label||t.label;
    svg.append(svgEl('text',{x:t.source==='running'&&t.target==='parked'?500:t.source===t.target?a[0]+175:(a[0]+b[0])/2+85,y:t.source==='running'&&t.target==='parked'?175:t.source===t.target?a[1]-8:(a[1]+b[1])/2+20},short));
  }
  for(const [state,[x,y]] of Object.entries(at)) svg.append(svgEl('rect',{x,y,width:160,height:54,rx:25}),svgEl('text',{x:x+80,y:y+32,'text-anchor':'middle'},state));
}
function drawChanges() {
  const report=envelope.kind==='diff'?envelope.data:null, impact=envelope.kind==='impact'?envelope.data:null;
  $('changes').replaceChildren();
  if(report) {
    $('changes-summary').textContent=`${report.counts.added} added · ${report.counts.removed} removed · ${report.counts.changed} changed. ${report.meaning}. Before: ${report.basis.before}`;
    for(const c of report.changes) { const b=button(`${c.change}: ${c.id}`,()=>inspection(c,'change')); b.className='change-button'; b.dataset.selection=c.id;b.append(el('span','Compatibility: '+c.compatibility.state)); $('changes').append(b); }
    if(!report.changes.length) $('changes').append(el('p','No declared semantic changes. Implementation changes are outside this comparison.','notice'));
  } else if(impact) {
    $('changes-title').textContent='Potential impact'; $('changes-summary').textContent=impact.meaning;
    for(const item of impact.affected) { const b=button(name(item.module_id),()=>inspection(impactRecord(item),'impact')); b.className='change-button'; b.dataset.selection='impact:'+item.module_id;b.append(el('span',item.witness_path.length+' relationship hops · '+item.test_argv.map(shell).join(' '))); $('changes').append(b); }
  } else {
    $('changes-summary').textContent='Compare two saved authoring indexes. Source bodies are excluded; missing interface evidence stays unknown.';
    $('changes').append(el('pre','cp .sandloom/inspection.json architecture-before.json\n# make changes; run sandloom generate\nsandloom graph diff architecture-before.json --format html > architecture-diff.html'),el('p','For a single interface, choose “Inspect potential impact” in its inspector. No Git access or application import is required.','notice'));
  }
}
function save(text,type,filename) { const url=URL.createObjectURL(new Blob([text],{type})), a=el('a'); a.href=url;a.download=filename;a.click();setTimeout(()=>URL.revokeObjectURL(url),0); }
$('download').onclick=()=>save(json(envelope),'application/json','sandloom-architecture.json');
$('save-svg').onclick=()=>{ const svg=$('diagram').cloneNode(true); svg.setAttribute('xmlns',ns); const css=getComputedStyle(document.documentElement); const style=svgEl('style',{},document.querySelector('style').textContent); svg.prepend(style); for(const key of ['--text','--raised','--border','--accent','--muted','--event','--panel']) svg.style.setProperty(key,css.getPropertyValue(key)); save(new XMLSerializer().serializeToString(svg),'image/svg+xml','sandloom-architecture.svg'); };
$('theme').onclick=()=>{document.documentElement.dataset.theme=document.documentElement.dataset.theme==='light'?'dark':'light';};
$('inspect-selection').onclick=()=>{$('inspector').scrollIntoView({block:'start'});const h=$('inspector-content').querySelector('h2');if(h){h.tabIndex=-1;h.focus({preventScroll:true});}};
$('clear-selection').onclick=()=>{$('inspect-selection').hidden=true;selectedId='';selectedKind='entity';$('inspector-content').replaceChildren(el('h2','Select something.'),el('p','Choose an entity, relationship or process step.','muted'));renderOverview(false);};
for(const module of modules) { const o=el('option',module.name);o.value=module.id;$('focus').append(o); }
for(const process of processes) { const o=el('option',(process.id.startsWith('saga:')?'Saga · ':'Route · ')+process.name+' · '+name(process.owner));o.value=process.id;$('process').append(o); }
$('status').textContent='Freshness: '+(envelope.freshness?.state||'unknown');
$('scope').textContent=(data.scope?.kind||'saved model')+' · '+modules.length+' modules · '+(data.hidden?.nodes||0)+' nodes outside export';
const limits=[...(data.cycles||[]).map(c=>'Declared '+human(c.kind)+' cycle: '+c.module_ids.map(name).join(' ↔ ')+'. Not proof of deadlock.'),...(data.unresolved||[]).map(u=>`${name(u.module_id)}: ${u.related_id} not in this composition (${u.required?'required':'optional'}).`)];
$('uncertainty-title').textContent=`Model limits · ${limits.length} cycle / unresolved declarations`;
$('uncertainty').append(el('p','This graph records an authoring composition. Deployment bindings, runtime availability, method call order and delivery outcomes are not inferred.'));
for(const text of limits) $('uncertainty').append(el('p',text));
for(const id of ['query','focus','direction','hops','relation']) $(id).addEventListener('input',()=>renderOverview());
$('layer').onchange=()=>{relationOptions();renderOverview();};
for(const id of ['process','process-view']) $(id).onchange=()=>{if(selectedId.includes('#')) $('clear-selection').onclick();drawProcess();stateURL();};
$('reset').onclick=()=>{for(const [id,value] of [['query',''],['layer','modules'],['focus',''],['direction','both'],['hops','1']])$(id).value=value;relationOptions();$('relation').value='';renderOverview();feedback('Filters reset.');};
const params=new URLSearchParams(location.hash.slice(1));
for(const id of ['layer','focus','direction','hops','process','process-view','query']) if(params.has(id)) { const value=params.get(id);if($(id).tagName!=='SELECT'||[...$(id).options].some(o=>o.value===value)) $(id).value=value; }
relationOptions();if([...$('relation').options].some(o=>o.value===params.get('relation'))) $('relation').value=params.get('relation');
const initial=params.get('view') || (['sequence','state'].includes(envelope.kind)?'sequence':['impact','diff'].includes(envelope.kind)?'changes':'overview');
if(envelope.selection?.process_id&&!params.has('process')) $('process').value=envelope.selection.process_id;
if(envelope.kind==='state'&&!params.has('process-view')) $('process-view').value='state';
drawChanges();renderOverview();selectView(['overview','sequence','changes'].includes(initial)?initial:'overview',false);
const initialSelected=params.get('selected');
if(initialSelected) {
  const selection=resolveSelection(initialSelected,params.get('selection-kind'));
  if(selection) {
    if(selection.kind==='impact-preview') showImpact(selection.item);
    else inspection(selection.item,selection.kind);
  } else feedback('The saved selection is unavailable in this export.');
}
stateURL();
