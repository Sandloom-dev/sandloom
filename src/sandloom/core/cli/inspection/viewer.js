'use strict';
const byId = id => document.getElementById(id);
const envelope = JSON.parse(byId('graph-data').textContent);
const data = envelope.workspace || envelope.data;
const allNodes = data.nodes || [], allEdges = data.edges || [], relations = data.module_relations || [];
const nodes = new Map(allNodes.map(n => [n.id, n])), edges = new Map(allEdges.map(e => [e.id, e]));
const modules = allNodes.filter(n => n.kind === 'module');
const cards = new Map((data.modules || []).map(m => [m.id, m]));
const processes = envelope.sequences || [];
const processMap = new Map(processes.map(p => [p.id, p]));
const labels = {required_contract:'Calls contract · required', optional_contract:'Calls contract · optional', event_impact:'May deliver event', ordering:'Declares load ordering', provides_contract:'Provides contract', requires_contract:'Requires contract', produces_event:'Publishes event', consumes_event:'Delivers to consumer', owns_consumer:'Owns consumer', projects_event:'Feeds projection', owns_projection:'Owns projection', requires_capability:'Requires infrastructure', delivers_to_module:'Delivered to module'};
const kinds = {module:'Business module', contract:'Public contract', event:'Versioned event', consumer:'Event consumer', projection:'Projection', capability:'Infrastructure requirement'};
const ns = 'http://www.w3.org/2000/svg';
let selectedId = '', selectedKind = 'entity', currentView = 'overview', visibleNodes = [], visibleLinks = [], listedLinks = [], showAllEntities = false;
const pairs = new Map();
let feedbackTimer;
function el(tag, text, className) { const e = document.createElement(tag); if(text !== undefined) e.textContent = text; if(className) e.className = className; return e; }
function documentationLink(label, value) {
  const link=el('a',label);
  try {
    if(typeof value!=='string')throw new TypeError('Missing documentation URL');
    const url=new URL(value,'https://sandloom.dev/docs/');
    if(!['https:','http:'].includes(url.protocol))throw new TypeError('Unsafe documentation URL');
    link.href=url.href;link.rel='noopener noreferrer';
  } catch {
    link.removeAttribute('href');link.setAttribute('aria-disabled','true');
    link.title='Documentation URL is unavailable or uses an unsupported protocol';
  }
  return link;
}
function svgEl(tag, attrs = {}, text) { const e = document.createElementNS(ns,tag); for(const [k,v] of Object.entries(attrs)) e.setAttribute(k,String(v)); if(text !== undefined) e.textContent = text; return e; }
function name(id) { return nodes.get(id)?.name || id; }
function human(kind) { return labels[kind] || kind.replaceAll('_',' '); }
function feedback(text) { byId('feedback').textContent=text; clearTimeout(feedbackTimer); feedbackTimer=setTimeout(()=>{byId('feedback').textContent='';},4500); }
function json(value) { return JSON.stringify(value,null,2); }
function shell(value) { return /^[a-zA-Z0-9_./:@=-]+$/.test(value) ? value : "'"+value.replaceAll("'", "'\"'\"'")+"'"; }
async function copy(text) {
  try { await navigator.clipboard.writeText(text); feedback('Copied to clipboard.'); }
  catch { const field=el('textarea'); field.value=text; byId('inspector-content').append(field); field.focus(); field.select(); feedback('Clipboard unavailable. Selected text is ready to copy.'); }
}
function button(text, action) { const b=el('button',text); b.type='button'; b.onclick=action; return b; }
function fact(list,key,value) { if(value === undefined || value === null || value === '') return; list.append(el('dt',key),el('dd',typeof value === 'object' ? json(value) : String(value))); }
function stateURL() {
  const params=new URLSearchParams();
  for(const id of ['query','layer','relation','focus','direction','hops','process','process-view']) if(byId(id).value) params.set(id,byId(id).value);
  params.set('view',currentView); if(selectedId) {params.set('selected',selectedId);params.set('selection-kind',selectedKind);}
  try { history.replaceState(null,'','#'+params.toString()); } catch { /* file: exports can still be inspected */ }
}
function selectView(view, save=true) {
  currentView=view;
  for(const v of ['overview','sequence','changes']) {
    byId('panel-'+v).hidden=v!==view; byId('tab-'+v).setAttribute('aria-selected',String(v===view)); byId('tab-'+v).tabIndex=v===view?0:-1;
  }
  if(view==='sequence') drawProcess();
  if(save) stateURL();
}
for(const v of ['overview','sequence','changes']) {
  byId('tab-'+v).onclick=()=>selectView(v);
  byId('tab-'+v).onkeydown=e=>{
    const order=['overview','sequence','changes']; let next=order.indexOf(v);
    if(e.key==='ArrowRight') next=(next+1)%3; else if(e.key==='ArrowLeft') next=(next+2)%3; else if(e.key==='Home') next=0; else if(e.key==='End') next=2; else return;
    e.preventDefault(); selectView(order[next]); byId('tab-'+order[next]).focus();
  };
}
function owners(item) {
  if(item.witnesses) return [...new Set(item.witnesses.map(id=>edges.get(id)?.owner).filter(Boolean))].sort();
  if(item.members) return [item.source,item.target];
  if(item.owner) return [item.owner];
  if(item.kind==='module') return [item.id];
  return [...new Set(allEdges.filter(e=>[e.source,e.target].includes(item.id)).map(e=>e.owner))].sort();
}
function interfaceNames(link) {
  const witness=(link.witnesses || [link.id]).map(id=>edges.get(id)).filter(Boolean);
  const interfaceIds=[...new Set(witness.flatMap(e=>[e.source,e.target]).filter(id=>nodes.get(id)?.kind==='contract'||nodes.get(id)?.kind==='event'))];
  return interfaceIds.map(name);
}
function witnessDescription(link) { return interfaceNames(link).join(', '); }
function edgesOf(kind) { return allEdges.filter(e=>e.kind===kind); }
function handlers() { return allEdges.filter(e=>e.kind==='consumes_event'||e.kind==='projects_event'); }
function unique(values) { return [...new Set(values)]; }
function moduleNames(ids) { return ids.map(name).join(', '); }
// What the reader asks first: who provides, calls, publishes and listens. Nothing here is inferred beyond declarations.
function connectionGroups(item) {
  const provided=id=>edgesOf('provides_contract').filter(e=>e.target===id), callers=id=>edgesOf('requires_contract').filter(e=>e.target===id);
  const published=id=>edgesOf('produces_event').filter(e=>e.target===id), delivered=id=>handlers().filter(e=>e.source===id);
  if(item.kind==='module') return [
    {title:'Provides',rows:edgesOf('provides_contract').filter(e=>e.source===item.id).map(e=>{const users=unique(callers(e.target).map(c=>c.source));return {id:e.target,meta:users.length?'called by '+moduleNames(users):'no callers in this composition'};})},
    {title:'Calls',rows:edgesOf('requires_contract').filter(e=>e.source===item.id).map(e=>{const from=unique(provided(e.target).map(p=>p.source));return {id:e.target,meta:(from.length?'from '+moduleNames(from):'no provider in this composition')+(e.required?'':' · optional'),warn:!from.length};})},
    {title:'Publishes',rows:edgesOf('produces_event').filter(e=>e.source===item.id).map(e=>{const to=unique(delivered(e.target).map(h=>h.owner));return {id:e.target,meta:to.length?'delivered to '+moduleNames(to):'no listeners in this composition'};})},
    {title:'Listens to',rows:unique(handlers().filter(e=>e.owner===item.id).map(e=>e.source)).map(id=>{const from=unique(published(id).map(p=>p.source));return {id,meta:from.length?'from '+moduleNames(from):'no publisher in this composition',warn:!from.length};})},
  ].filter(group=>group.rows.length);
  if(item.kind==='contract') return [
    {title:'Provided by',rows:provided(item.id).map(e=>({id:e.source,meta:e.methods?.join(', ')||''})),empty:'No provider in this composition.'},
    {title:'Called by',rows:callers(item.id).map(e=>({id:e.source,meta:(e.required?'required':'optional')+(e.methods?.length?' · '+e.methods.join(', '):'')})),empty:'No callers in this composition.'},
  ];
  if(item.kind==='event') return [
    {title:'Published by',rows:published(item.id).map(e=>({id:e.source,meta:''})),empty:'No publisher in this composition.'},
    {title:'Delivered to',rows:unique(delivered(item.id).map(e=>e.owner)).map(id=>({id,meta:moduleNames(delivered(item.id).filter(e=>e.owner===id).map(e=>e.target))})),empty:'No listeners in this composition.'},
  ];
  if(item.kind==='consumer'||item.kind==='projection') return [
    {title:'Listens to',rows:handlers().filter(e=>e.target===item.id).map(e=>({id:e.source,meta:''}))},
    {title:'Owned by',rows:allEdges.filter(e=>e.target===item.id&&e.kind.startsWith('owns_')).map(e=>({id:e.source,meta:''}))},
  ].filter(group=>group.rows.length);
  return [];
}
function homeModules(item) {
  const own=item.kind==='contract'?edgesOf('provides_contract'):item.kind==='event'?edgesOf('produces_event'):[];
  const homes=unique(own.filter(e=>e.target===item.id).map(e=>e.source));
  return homes.length?homes:owners(item);
}
function connectionSections(item) {
  const sections=[];
  for(const group of connectionGroups(item)) {
    const section=el('section',undefined,'connections');section.append(el('h3',group.title));
    for(const row of group.rows) { const b=button(name(row.id),()=>goTo(nodes.get(row.id)));b.className='connection';b.append(el('span',row.meta||(kinds[nodes.get(row.id)?.kind]||''),row.warn?'warn':''));section.append(b); }
    if(!group.rows.length)section.append(el('p',group.empty||'None declared.','muted'));
    sections.push(section);
  }
  return sections;
}
function goTo(item) {
  // A focused view draws consumers and projections as their owner module.
  if(item.kind==='consumer'||item.kind==='projection') { const owner=nodes.get(homeModules(item)[0]); if(owner) { byId('layer').value='interfaces';item=owner; } }
  const layer=byId('layer').value;
  if(item.kind==='module') { if(layer==='interfaces')byId('focus').value=item.id; else {byId('layer').value='modules';if(byId('focus').value&&!moduleScope().has(item.id))byId('focus').value='';} }
  else if(item.kind==='capability') { byId('layer').value='infrastructure';byId('focus').value=''; }
  else { const home=homeModules(item);byId('layer').value='interfaces';byId('focus').value=home.length?home[0]:''; }
  byId('query').value='';relationOptions();byId('relation').value='';renderOverview();choose(item);
  byId('diagram').querySelector('[data-item="'+CSS.escape(item.id)+'"]')?.scrollIntoView({block:'nearest',inline:'nearest'});
}
function showInterfaces(id) { byId('layer').value='interfaces';byId('focus').value=id;relationOptions();byId('relation').value='';renderOverview(); }
function syncSelection() {
  for(const b of document.querySelectorAll('[data-selection]')) {
    const selected=b.dataset.selection===selectedId;
    b.classList.toggle('selected',selected);
    b.setAttribute('aria-pressed',String(selected));
  }
}
function impactRecord(item) {
  return {...item,id:'impact:'+item.module_id,name:name(item.module_id),owner:item.module_id};
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
  const item=nodes.get(id)||edges.get(id)||pairs.get(id)||relations.find(r=>r.id===id)||processMap.get(id)?.declaration;
  return item?{item,kind}:null;
}
function declarationChanges(change) {
  const lines=[],left=change.before,right=change.after;
  const before=left?.interface?.methods||{},after=right?.interface?.methods||{};
  for(const method of Object.keys(after).sort())if(!(method in before))lines.push('Added method: '+method);
  for(const method of Object.keys(before).sort())if(!(method in after))lines.push('Removed method: '+method);
  function walk(a,b,path) {
    if(JSON.stringify(a)===JSON.stringify(b))return;
    if(a&&b&&typeof a==='object'&&typeof b==='object'&&!Array.isArray(a)&&!Array.isArray(b)) {
      for(const key of [...new Set([...Object.keys(a),...Object.keys(b)])].sort()) {
        if(path==='interface.methods'&&(!(key in a)||!(key in b)))continue;
        walk(a[key],b[key],path?path+'.'+key:key);
      }
    } else {
      const value=x=>x===undefined?'absent':JSON.stringify(x);
      lines.push((a===undefined?'Added ':b===undefined?'Removed ':'Changed ')+path+': '+value(a)+' → '+value(b));
    }
  }
  if(left&&right)walk(left,right,'');
  else lines.push(change.change==='added'?'Declaration added.':'Declaration removed.');
  return lines;
}
function impactEvidence(item) {
  const direct=(item.seed_witnesses||[]).map(e=>human(e.kind)+': '+[e.source,e.target].filter(id=>['contract','event'].includes(nodes.get(id)?.kind)).map(name).join(', '));
  const indirect=(item.witness_path||[]).map(r=>'Declared link: '+name(r.source)+' → '+name(r.target)+(witnessDescription(r)?' · '+witnessDescription(r):''));
  return [...new Set([...direct,...indirect])];
}
function routeDetails(module) {
  const details=el('details'),summary=el('summary',(module.routes?.length||0)+' HTTP routes');details.append(summary);
  for(const route of module.routes||[]) {
    const title=route.method+' '+route.path,process=processes.find(p=>p.owner===module.id&&p.declaration?.name===title);
    if(process)details.append(button(title,()=>{byId('process').value=process.id;byId('process-view').value='sequence';selectView('sequence');byId('workspace').scrollIntoView({block:'start'});}));
    else details.append(el('p',title+' · '+(route.auth==='public'?'Public':route.auth==='authenticated'?'Authenticated':'Policy unavailable')));
  }
  return details;
}
function inspection(item, kind='entity') {
  selectedId=item.id || ''; selectedKind=kind; const target=byId('inspector-content'); target.replaceChildren();
  const title=item.members?`${name(item.source)} → ${name(item.target)}`:item.name || item.label || (item.source && item.target ? `${name(item.source)} → ${name(item.target)}` : item.label || item.id || 'Selection');
  target.append(el('span',kind==='step'?'PROCESS STEP':kind==='change'?'ARCHITECTURE CHANGE':item.members?'CONNECTION':kinds[item.kind] || human(item.kind || kind),'eyebrow'),el('h2',title));
  if(cards.get(item.id)?.summary)target.append(el('p',cards.get(item.id).summary.replaceAll('``',''),'module-purpose'));
  const sections=connectionSections(item);target.append(...sections);
  if(item.members) { const section=el('section',undefined,'connections');section.append(el('h3','Why they are linked'));for(const member of item.members) { const via=(member.witnesses||[]).flatMap(id=>{const e=edges.get(id);return e?[e.source,e.target]:[];}).find(id=>['contract','event'].includes(nodes.get(id)?.kind));if(!via){section.append(el('p',human(member.kind)));continue;}const b=button(name(via),()=>goTo(nodes.get(via)));b.className='connection';b.append(el('span',human(member.kind)));section.append(b); }target.append(section); }
  const list=el('dl');
  if(sections.length||item.members)list.classList.add('after-connections');
  if(!item.members)fact(list,'Stable ID',item.id);
  if(item.source && item.target && !item.members) { fact(list,'From',name(item.source)); fact(list,'To',name(item.target)); fact(list,'Meaning',human(item.kind || kind)); }
  const witness=witnessDescription(item); if(witness) fact(list,'Public interface',witness);
  const witnesses=(item.witnesses||[]).map(id=>edges.get(id)).filter(Boolean);
  for(const w of witnesses) { if(w.required!==undefined) fact(list,'Required',w.required?'Yes':'Optional'); if(w.methods?.length) fact(list,'Declared methods',w.methods.join(', ')); if(w.reason) fact(list,'Reason',w.reason); }
  if(item.required !== undefined) fact(list,'Required',item.required ? 'Yes' : 'Optional; caller must handle absence');
  if(item.owner) fact(list,'Owner',name(item.owner));
  for(const [key,label] of [['condition','Condition'],['call','Call'],['transaction','Unit boundary'],['durable','Durable consumer'],['group','Consumer group'],['reason','Declaration reason']]) fact(list,label,item[key]);
  const module=cards.get(item.id);let routes;
  if(module) {
    fact(list,'Package',module.import_path);
    const chain=module.chain&&typeof module.chain==='object'?Object.fromEntries(Object.entries(module.chain).filter(([,v])=>v!==null)):module.chain;
    if(chain&&(typeof chain!=='object'||Object.keys(chain).length))fact(list,'Database chain',chain);
    fact(list,'Tables',module.models?.map(m=>m.table).join(', ')||module.model_count);
    if(module.routes?.length)routes=routeDetails(module);
  }
  const ownerIds=homeModules(item); for(const owner of ownerIds) {
    const evidence=data.evidence?.[owner]; fact(list,'Source · '+name(owner),evidence?.source_root || 'Installed distribution; local implementation unavailable');
    if(evidence?.distributions && Object.keys(evidence.distributions).length) fact(list,'Installed distributions',evidence.distributions);
  }
  if(kind==='step') fact(list,'Step kind',item.kind);
  if(item.seed_witnesses){fact(list,'Impact scope',item.scope);fact(list,'Seed module',name(item.seed_module_id));fact(list,'Direct witnesses',item.seed_witnesses.map(e=>human(e.kind)+': '+name(e.source)+' → '+name(e.target)).join('; '));fact(list,'Transitive path',(item.witness_path||[]).map(r=>name(r.source)+' → '+name(r.target)+' · '+human(r.kind)).join('; ')||'Direct interface / owner');}
  fact(list,'Evidence',item.evidence_kind || (kind==='step'?'Declared sequence / conditional framework phase':'Registered authoring declaration'));
  target.append(list);if(routes)target.append(routes);
  if(kind==='change') { const changes=declarationChanges(item);target.append(el('h3','What changed'));const ul=el('ul',undefined,'change-facts');for(const line of changes.slice(0,50))ul.append(el('li',line));target.append(ul);if(changes.length>50)target.append(el('p',(changes.length-50)+' more differences in declaration evidence.')); const c=item.compatibility || {}; target.append(el('h3','Compatibility'),el('p',c.state || 'Not assessed')); for(const reason of c.reasons || []) target.append(el('p',reason)); }
  if(item.notes) for(const note of item.notes) target.append(el('p',note,'muted'));
  const interfaceId = witnesses.flatMap(w=>[w.source,w.target]).find(id=>['contract','event'].includes(nodes.get(id)?.kind));
  if(item.id && (nodes.has(item.id)||processMap.has(item.id)||item.parent_id||interfaceId||item.members)) {
    target.append(el('h3','Next actions'));
    if(item.kind==='module'&&byId('layer').value!=='interfaces')target.append(button('Show its contracts & events',()=>showInterfaces(item.id)));
    for(const owner of ownerIds.filter(id=>data.evidence?.[id]?.source_root).slice(0,6)) {
      const command=['sandloom','context',name(owner)].map(shell).join(' ');
      target.append(button('Copy context command · '+name(owner),()=>copy(command)));
    }
    if(!item.members){target.append(button('Inspect potential impact',()=>showImpact(nodes.get(interfaceId)||processMap.get(item.parent_id)?.declaration||item)));target.append(button('Copy stable ID',()=>copy(item.id)));}
  }
  if(item.test_argv) target.append(button('Copy suggested module test',()=>copy(item.test_argv.map(shell).join(' '))));
  if(item.source_location) target.append(button('Copy source location',()=>copy(item.source_location.path+':'+item.source_location.line)));
  const details=el('details'); details.append(el('summary','Declaration evidence'),el('pre',json(witnesses.length?{...item,witnesses}:item))); target.append(details);
  const doc=documentationLink('Read the documentation',item.docs||'https://sandloom.dev/docs/concepts/'+(item.kind==='event'?'events/':item.kind==='contract'?'contracts/':item.id?.startsWith('saga:')?'sagas/':item.id?.startsWith('route:')?'lifecycle/':'modules/')); target.append(doc);
  syncSelection();
  byId('inspect-selection').hidden=false;
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
  const target=byId('inspector-content'); target.replaceChildren(el('span','POTENTIAL IMPACT','eyebrow'),el('h2',item.name || item.id),el('p','Direct interface users plus transitive module reachability, including other interfaces. This overapproximates possible impact and is not complete test coverage.','muted'));
  for(const [id,path] of [...found].sort()) {
    const b=button(name(id)+' ',()=>inspection(nodes.get(id))); b.className='change-button'; b.append(el('span',path.length?path.map(r=>witnessDescription(r)||human(r.kind)).join(' → '):'Direct owner / user · '+allEdges.filter(e=>e.owner===id&&[e.source,e.target].includes(item.id)).map(e=>human(e.kind)).join(', '))); target.append(b);
  }
  target.append(button('Copy impact query',()=>copy(['sandloom','graph','impact',item.id].map(shell).join(' '))));
  target.append(el('p','Run normal quality gates; do not skip tests based on this view.','notice'));
  byId('inspect-selection').hidden=false;syncSelection();drawDiagram();
  feedback('Potential impact is available in the inspector.');
}
function choose(item,kind) { inspection(item,kind); }
function moduleScope() {
  if(!byId('focus').value) return new Set(modules.map(m=>m.id));
  const selected=new Set([byId('focus').value]), direction=byId('direction').value;
  for(let i=0;i<Number(byId('hops').value);i++) {
    const next=[];
    for(const r of relations) {
      if(direction!=='incoming'&&selected.has(r.source)) next.push(r.target);
      if(direction!=='outgoing'&&selected.has(r.target)) next.push(r.source);
    }
    next.forEach(id=>selected.add(id));
  }
  return selected;
}
// A module's interfaces with the modules on their far side; consumers and projections collapse into their owner.
function interfaceNeighborhood(id) {
  const links=new Map(), add=e=>links.set(e.id,e), deliver=e=>{const link={id:'delivery:'+e.id,kind:'delivers_to_module',via:e.kind,source:e.source,target:e.owner,owner:e.owner,witnesses:[e.id]};edges.set(link.id,link);add(link);};
  const kind=(e,...names)=>names.includes(e.kind);
  for(const e of allEdges.filter(e=>e.owner===id&&kind(e,'provides_contract','requires_contract','produces_event'))) {
    add(e);
    if(e.kind==='provides_contract')allEdges.filter(o=>o.kind==='requires_contract'&&o.target===e.target).forEach(add);
    if(e.kind==='requires_contract')allEdges.filter(o=>o.kind==='provides_contract'&&o.target===e.target).forEach(add);
    if(e.kind==='produces_event')allEdges.filter(o=>kind(o,'consumes_event','projects_event')&&o.source===e.target).forEach(deliver);
  }
  for(const e of allEdges.filter(e=>e.owner===id&&kind(e,'consumes_event','projects_event'))) {
    deliver(e);allEdges.filter(o=>o.kind==='produces_event'&&o.target===e.source).forEach(add);
  }
  return [...links.values()];
}
function layerModel() {
  const layer=byId('layer').value, selected=layer==='modules'||!byId('focus').value?moduleScope():new Set([byId('focus').value]);
  let links, candidates;
  if(layer==='modules') { candidates=modules.filter(n=>selected.has(n.id)); links=relations.filter(r=>selected.has(r.source)&&selected.has(r.target)); }
  else if(layer==='interfaces'&&byId('focus').value) {
    links=interfaceNeighborhood(byId('focus').value);
    const ids=new Set([byId('focus').value,...links.flatMap(e=>[e.source,e.target])]); candidates=allNodes.filter(n=>ids.has(n.id));
  }
  else {
    const allowed=layer==='interfaces'?new Set(['module','contract','event','consumer','projection']):new Set(['module','capability']);
    links=allEdges.filter(e=>selected.has(e.owner)&&allowed.has(nodes.get(e.source)?.kind)&&allowed.has(nodes.get(e.target)?.kind));
    const ids=new Set([...selected,...links.flatMap(e=>[e.source,e.target])]); candidates=allNodes.filter(n=>ids.has(n.id)&&allowed.has(n.kind));
  }
  const filter=byId('relation').value; if(filter) { links=links.filter(e=>(e.via||e.kind)===filter); const ids=new Set(links.flatMap(e=>[e.source,e.target])); candidates=candidates.filter(n=>ids.has(n.id)); }
  const query=byId('query').value.trim().toLocaleLowerCase(); let matches=candidates.length;
  if(query) {
    const matched=new Set(candidates.filter(n=>(n.name+' '+n.id+' '+(cards.get(n.id)?.summary||'')).toLocaleLowerCase().includes(query)).map(n=>n.id)); matches=matched.size;
    links=links.filter(e=>matched.has(e.source)||matched.has(e.target));
    const ids=new Set([...matched,...links.flatMap(e=>[e.source,e.target])]); candidates=candidates.filter(n=>ids.has(n.id));
  }
  return {nodes:candidates,links,drawn:layer==='modules'?pairLinks(links,interfaceNames):links,matches};
}
function searchHints(model) {
  const target=byId('search-hints');target.replaceChildren();target.hidden=true;
  const query=byId('query').value.trim().toLocaleLowerCase();if(!query)return;
  const matches=allNodes.filter(n=>(n.name+' '+n.id+' '+(cards.get(n.id)?.summary||'')).toLocaleLowerCase().includes(query)).map(item=>({text:(kinds[item.kind]||item.kind)+' · '+item.name,open:()=>goTo(item)}));
  for(const card of cards.values()) {
    for(const route of card.routes||[]) if((route.method+' '+route.path).toLocaleLowerCase().includes(query)) { const title=route.method+' '+route.path,process=processes.find(p=>p.owner===card.id&&p.declaration?.name===title);matches.push({text:'Route · '+title+' · '+card.name,open:()=>{if(process){byId('process').value=process.id;byId('process-view').value='sequence';selectView('sequence');}else goTo(nodes.get(card.id));}}); }
    for(const model of card.models||[]) if(String(model.table).toLocaleLowerCase().includes(query)) matches.push({text:'Table · '+model.table+' · '+card.name,open:()=>goTo(nodes.get(card.id))});
  }
  target.hidden=false;
  if(!matches.length){target.append(el('p','No modules, contracts, events, routes or tables match.'));return;}
  target.append(el('p',matches.length+(matches.length===1?' match':' matches')+' across everything'+(model.matches?'':'; none in the current view')+'.'));
  for(const item of matches.slice(0,8))target.append(button(item.text,item.open));
  if(matches.length>8)target.append(el('p','Showing the first 8 results. Refine search to narrow the list.'));
}
function relationOptions() {
  const previous=byId('relation').value, layer=byId('layer').value;
  const kinds=layer==='modules'?new Set(relations.map(r=>r.kind)):new Set(allEdges.filter(e=>layer==='infrastructure'?e.kind==='requires_capability':e.kind!=='requires_capability'&&e.kind!=='declares_ordering').map(e=>e.kind));
  byId('relation').replaceChildren(el('option','All relation types')); byId('relation').firstChild.value='';
  for(const kind of [...kinds].sort()) { const o=el('option',human(kind)); o.value=kind; byId('relation').append(o); }
  if(kinds.has(previous)) byId('relation').value=previous;
}
function renderOverview(fit=true) {
  for(const id of ['direction','hops'])byId(id).disabled=byId('layer').value!=='modules';
  const model=layerModel(); visibleNodes=model.nodes; visibleLinks=model.drawn; listedLinks=model.links;
  pairs.clear();for(const pair of visibleLinks)if(pair.members)pairs.set(pair.id,pair);
  byId('canvas-title').textContent=byId('layer').selectedOptions[0].textContent;
  const count=(n,word)=>n+' '+word+(n===1?'':'s'), modulesLayer=byId('layer').value==='modules';
  byId('counts').textContent=[count(visibleNodes.length,modulesLayer?'module':'entity').replace('entitys','entities'),count(visibleLinks.length,modulesLayer?'connection':'link'),modulesLayer&&listedLinks.length!==visibleLinks.length?count(listedLinks.length,'declaration')+' in the list below':'',byId('query').value.trim()?count(model.matches,'match').replace('matchs','matches'):''].filter(Boolean).join(' · ');
  byId('entity-count').textContent=visibleNodes.length; byId('relation-count').textContent=listedLinks.length;
  byId('nodes').replaceChildren();
  for(const n of visibleNodes) { const b=button(n.name,()=>choose(n)); b.dataset.selection=n.id; b.classList.toggle('selected',selectedId===n.id); b.append(el('span',kinds[n.kind]||n.kind)); if(cards.get(n.id)?.summary)b.append(el('span',cards.get(n.id).summary,'module-purpose')); byId('nodes').append(b); }
  if(!visibleNodes.length) byId('nodes').append(el('p','No entities match these filters.','empty'));
  byId('rows').replaceChildren();
  for(const r of listedLinks) { const b=button(`${name(r.source)} → ${name(r.target)}`,()=>choose(r,'relationship')); b.dataset.selection=r.id; b.classList.toggle('selected',selectedId===r.id); b.append(el('span',human(r.kind)+(witnessDescription(r)?' · '+witnessDescription(r):''))); byId('rows').append(b); }
  if(!listedLinks.length) byId('rows').append(el('p','No relationships in this scope. An isolated module can still be inspected.','empty'));
  const outside=selectedId&&!visibleNodes.some(n=>n.id===selectedId)&&!listedLinks.some(r=>r.id===selectedId)&&!visibleLinks.some(r=>r.id===selectedId);
  byId('selection-state').hidden=!outside; byId('selection-state').textContent='Selected item is outside these overview filters. Clear selection or reset filters to return.';
  searchHints(model);syncSelection();drawChooser();drawDiagram(fit); stateURL();
}
// Too many entities to read at once: start from the question "which module?".
function drawChooser() {
  const target=byId('canvas-choose'), layer=byId('layer').value;
  const show=layer!=='modules'&&!byId('focus').value&&!byId('query').value.trim()&&!byId('relation').value&&!showAllEntities&&visibleNodes.length>20;
  choosing=show;target.hidden=!show;target.replaceChildren();byId('diagram').toggleAttribute('hidden',show||!visibleNodes.length);byId('save-svg').disabled=show;if(!show)return;
  target.append(el('h2','Choose a module'),el('p',visibleNodes.length+' entities are too many to read at once. Pick a module to see its interfaces and who uses them.','muted'));
  const grid=el('div',undefined,'choose-grid');
  for(const module of modules) { const facts=[['provides_contract','contract'],['produces_event','event'],['requires_contract','call']].map(([kind,word])=>{const n=allEdges.filter(e=>e.kind===kind&&e.source===module.id).length;return n?n+' '+word+(n===1?'':'s'):'';}).filter(Boolean).join(' · ');const b=button(module.name,()=>showInterfaces(module.id));b.append(el('span',facts||'no interfaces'));if(cards.get(module.id)?.summary)b.append(el('span',cards.get(module.id).summary,'module-purpose'));grid.append(b); }
  target.append(grid,button('Show all '+visibleNodes.length+' entities anyway',()=>{showAllEntities=true;renderOverview();}));
}
function marker(svg,id='arrow') { const defs=svgEl('defs'), mark=svgEl('marker',{id,viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:7,markerHeight:7,orient:'auto-start-reverse'}); mark.append(svgEl('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:'currentColor'})); defs.append(mark); svg.append(defs); }
let diagramBounds=[0,0,900,460],diagramScale=1,choosing=false;
function applyBox() { const svg=byId('diagram');svg.setAttribute('viewBox',diagramBounds.join(' '));svg.style.width=diagramBounds[2]*diagramScale+'px';svg.style.height=diagramBounds[3]*diagramScale+'px'; }
function drawDiagram(fit=false) {
  if(choosing) { byId('diagram').replaceChildren();byId('canvas-empty').hidden=true;return; }
  const svg=byId('diagram'); const restoreFocus = svg.contains(document.activeElement) ? document.activeElement.getAttribute('data-item') : null; svg.replaceChildren(); marker(svg); const at=positions(visibleNodes,visibleLinks,cards);
  byId('canvas-empty').hidden=visibleNodes.length>0; svg.hidden=!visibleNodes.length;
  const routes=routedLinks(at,visibleLinks);
  const selectedLink=visibleLinks.find(r=>r.id===selectedId||r.members?.some(m=>m.id===selectedId));
  const near=new Set(selectedLink?[selectedLink.source,selectedLink.target]:at.has(selectedId)?[selectedId,...visibleLinks.filter(r=>r.source===selectedId||r.target===selectedId).flatMap(r=>[r.source,r.target])]:[]);
  const dimming=near.size>0;
  for(const {link:r,points,d} of routes) {
    const [ax,ay]=points[0],[bx,by]=points.at(-1);
    const type=r.kind.includes('event')||r.kind==='delivers_to_module'?' event':r.kind.includes('optional')||r.kind==='ordering'||r.required===false?' optional':'';
    const picked=selectedId===r.id||r.members?.some(m=>m.id===selectedId), tied=picked||r.source===selectedId||r.target===selectedId;
    const path=svgEl('path',{d,class:'edge'+type+(picked?' selected':tied?' active':dimming?' dim':''),'marker-end':'url(#arrow)'});
    svg.append(svgEl('path',{d,class:'edge-halo'}));
    path.append(svgEl('title',{},`${name(r.source)} → ${name(r.target)}: ${human(r.kind)}`)); svg.append(path);
    const hit=svgEl('path',{d,class:'edge-hit',tabindex:0,role:'button','data-item':r.id,'aria-label':`${name(r.source)} to ${name(r.target)}: ${human(r.kind)}`}); hit.onclick=()=>choose(r,'relationship'); hit.onkeydown=e=>{if(['Enter',' '].includes(e.key)){e.preventDefault();choose(r,'relationship');}}; svg.append(hit);

  }
  for(const n of visibleNodes) {
    const p=at.get(n.id), group=svgEl('g',{class:'node'+(selectedId===n.id?' selected':dimming&&!near.has(n.id)?' dim':''),tabindex:0,role:'button','data-item':n.id,'aria-label':`${kinds[n.kind]||n.kind}: ${n.name}`});
    group.append(svgEl('title',{},n.name),svgEl('rect',{x:p.x,y:p.y,width:p.width,height:p.height,rx:8}));
    group.append(svgEl('text',{x:p.x+14,y:p.y+22,class:'kind'},(kinds[n.kind]||n.kind)+(p.isolated?' · no links in view':'')));
    p.title.forEach((line,i)=>group.append(svgEl('text',{x:p.x+14,y:p.y+45+i*18},line)));
    p.purpose.forEach((line,i)=>group.append(svgEl('text',{x:p.x+14,y:p.y+48+p.title.length*18+i*15,class:'purpose'},line)));
    group.onclick=()=>choose(n); group.onkeydown=e=>{if(['Enter',' '].includes(e.key)){e.preventDefault();choose(n);}}; svg.append(group);
  }
  for(const {link,text,box} of placeLabels(routes,at)) {
    const tied=link.id===selectedId||link.members?.some(m=>m.id===selectedId)||link.source===selectedId||link.target===selectedId;
    svg.append(svgEl('text',{x:box.x+box.width/2,y:box.y+box.height-4,'text-anchor':'middle',class:'edge-label'+(dimming&&!tied?' dim':'')},text));
  }
  const allPoints=[...at.values()].flatMap(p=>[[p.x,p.y],[p.x+p.width,p.y+p.height]]).concat(routes.flatMap(r=>r.points));
  const xs=allPoints.map(p=>p[0]),ys=allPoints.map(p=>p[1]);
  diagramBounds=[Math.min(0,...xs)-20,Math.min(0,...ys)-20,Math.max(500,...xs)+40,Math.max(250,...ys)+40];
  diagramBounds[2]-=diagramBounds[0];diagramBounds[3]-=diagramBounds[1];
  if(fit) {const canvas=byId('canvas');diagramScale=Math.max(.9,Math.min(1,(canvas.clientWidth||900)/diagramBounds[2],(canvas.clientHeight||460)/diagramBounds[3]));canvas.scrollLeft=0;canvas.scrollTop=0;}
  applyBox();
  if(restoreFocus) [...svg.querySelectorAll('[data-item]')].find(n=>n.getAttribute('data-item')===restoreFocus)?.focus({preventScroll:true});
}
function zoom(factor) {diagramScale=Math.max(.15,Math.min(3,diagramScale/factor));applyBox();}
byId('zoom-in').onclick=()=>zoom(.8);byId('zoom-out').onclick=()=>zoom(1.25);
byId('fit').onclick=()=>{const canvas=byId('canvas');diagramScale=Math.min(1,canvas.clientWidth/diagramBounds[2],canvas.clientHeight/diagramBounds[3]);applyBox();canvas.scrollLeft=0;canvas.scrollTop=0;if(diagramScale<.75)feedback('Entire graph fitted. Zoom in or select a module to read labels.');};
for(const [id,dx,dy] of [['pan-left',-.2,0],['pan-right',.2,0],['pan-up',0,-.2],['pan-down',0,.2]]) byId(id).onclick=()=>{const canvas=byId('canvas');canvas.scrollLeft+=dx*canvas.clientWidth;canvas.scrollTop+=dy*canvas.clientHeight;};
let drag;
byId('diagram').onpointerdown=e=>{if(e.target.closest('[role=button]'))return;const c=byId('canvas');drag={x:e.clientX,y:e.clientY,left:c.scrollLeft,top:c.scrollTop};byId('diagram').setPointerCapture(e.pointerId);byId('diagram').classList.add('dragging');};
byId('diagram').onpointermove=e=>{if(!drag)return;const c=byId('canvas');c.scrollLeft=drag.left+drag.x-e.clientX;c.scrollTop=drag.top+drag.y-e.clientY;};
for(const event of ['pointerup','pointercancel','lostpointercapture'])byId('diagram').addEventListener(event,()=>{drag=null;byId('diagram').classList.remove('dragging');});
function drawProcess() {
  const process=processMap.get(byId('process').value), svg=byId('sequence-diagram'); svg.classList.remove('route-flow');svg.replaceChildren(); byId('steps').replaceChildren(); byId('process-notes').replaceChildren();
  if(!process) { byId('process-summary').textContent=processes.length?'Choose a process. Each sequence has an equivalent numbered step list.':'No recorded processes. Regenerate the authoring index to collect saga and route declarations.'; byId('process-notes').textContent='Module edges cannot supply business chronology. No observed attempt is recorded in this export.'; return; }
  const state=byId('process-view').value==='state';
  if(state&&!process.id.startsWith('saga:')) { byId('process-summary').textContent='State view applies to sagas. Choose a saga or switch to Sequence.'; return; }
  byId('process-summary').textContent=process.evidence;
  const transitions=envelope.saga_states?.transitions || [];
  const compensation=byId('process-view').value==='compensation';
  if(compensation&&!process.id.startsWith('saga:')) { byId('process-summary').textContent='Compensation applies to declared sagas. Choose a saga or Forward sequence.'; return; }
  byId('process-summary').textContent=(state?'Conditional saga states':compensation?'Alternative: compensation before pivot':'Forward path')+' · '+process.evidence;
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
    if(undo&&!compensationHeading) { byId('steps').append(el('h3','Alternative: compensation before pivot')); compensationHeading=true; }
    const item={...message,id:process.id+'#'+byId('process-view').value+'-step-'+i,owner:process.owner,parent_id:process.id};
    const bttn=button(`${i+1}. ${message.label}`,()=>inspection(item,'step')); bttn.className='step-button'; bttn.dataset.selection=item.id; bttn.append(el('span',`${name(message.source)} → ${name(message.target)} · ${message.kind?message.kind+' · ':''}${message.condition||'declared step'}`)); byId('steps').append(bttn);
  });
  if(state)drawStates(svg,transitions);else if(!process.id.startsWith('saga:'))drawRoute(svg,messages,process);
  if(!messages.length) byId('steps').append(el('p','No declared compensation callbacks. This does not imply rollback across provider commits.','notice'));
  syncSelection();
  byId('steps-title').textContent=state?'Conditional state transitions':compensation?'Compensation alternatives · reverse completed steps':'Forward steps';
  for(const note of process.notes) byId('process-notes').append(el('p',note));
  const a=documentationLink('Lifecycle documentation',process.docs); byId('process-notes').append(a,button('Inspect process declaration',()=>inspection({...process.declaration,name:process.name})),button('Copy Mermaid command',()=>copy(['sandloom','graph',state?'state':'sequence',process.id,'--format','mermaid'].map(shell).join(' '))));
}
function drawRoute(svg,messages,process) {
  svg.replaceChildren();marker(svg,'route-arrow');svg.classList.add('route-flow');
  const width=Math.max(280,Math.min(900,svg.parentElement.clientWidth||700)),limit=Math.max(20,Math.floor((width-64)/7));
  let y=20;svg.append(svgEl('title',{},process.name));
  messages.forEach((message,i)=>{
    const title=wrapText(`${i+1}. ${name(message.source)} → ${name(message.target)}`,limit),label=wrapText(message.label,limit),condition=wrapText(message.condition||'',limit);
    const height=28+title.length*17+label.length*18+condition.length*16;
    const group=svgEl('g');group.append(svgEl('rect',{x:16,y,width:width-32,height,rx:8}));
    let lineY=y+22;for(const line of title){group.append(svgEl('text',{x:30,y:lineY,class:'route-condition'},line));lineY+=17;}
    for(const line of label){group.append(svgEl('text',{x:30,y:lineY+3,class:'route-label'},line));lineY+=18;}
    for(const line of condition){group.append(svgEl('text',{x:30,y:lineY+6,class:'route-condition'},line));lineY+=16;}
    svg.append(group);y+=height;
    if(i<messages.length-1)svg.append(svgEl('path',{d:`M ${width/2} ${y} V ${y+26}`,class:'message','marker-end':'url(#route-arrow)'}));
    y+=30;
  });
  svg.setAttribute('viewBox',`0 0 ${width} ${y}`);svg.style.width='100%';svg.style.height=y+'px';
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
  byId('changes').replaceChildren();
  if(report) {
    byId('changes-summary').textContent=`${report.counts.added} added · ${report.counts.removed} removed · ${report.counts.changed} changed. ${report.meaning}. Before: ${report.basis.before}`;
    for(const c of report.changes) { const b=button(`${c.change}: ${c.id}`,()=>inspection(c,'change')); b.className='change-button'; b.dataset.selection=c.id;b.append(el('span','Compatibility: '+c.compatibility.state));const changes=declarationChanges(c);for(const line of changes.slice(0,3))b.append(el('span',line));if(changes.length>3)b.append(el('span',(changes.length-3)+' more differences · select to inspect'));  byId('changes').append(b); }
    if(!report.changes.length) byId('changes').append(el('p','No declared semantic changes. Implementation changes are outside this comparison.','notice'));
  } else if(impact) {
    byId('changes-title').textContent='Potential impact · '+(impact.item.name||impact.item.id); byId('changes-summary').textContent=impact.meaning.replace(/^Potential impact: /,'');
    for(const item of impact.affected) { const b=button(name(item.module_id),()=>inspection(impactRecord(item),'impact')); b.className='change-button'; b.dataset.selection='impact:'+item.module_id;for(const evidence of impactEvidence(item))b.append(el('span',evidence));b.append(el('span',item.witness_path.length+' relationship hops · '+item.test_argv.map(shell).join(' '))); byId('changes').append(b); }
  } else {
    byId('changes-summary').textContent='This page holds one snapshot. To see what a branch changed, build a comparison with its base. Source bodies are excluded; missing interface evidence stays unknown.';
    byId('changes').append(el('pre','sandloom graph diff main --format html > architecture-diff.html'),el('p','For a single interface, choose “Inspect potential impact” in its inspector.','notice'));
  }
}
function save(text,type,filename) { const url=URL.createObjectURL(new Blob([text],{type})), a=el('a'); a.href=url;a.download=filename;a.click();setTimeout(()=>URL.revokeObjectURL(url),0); }
byId('download').onclick=()=>save(json(envelope),'application/json','sandloom-architecture.json');
byId('save-svg').onclick=()=>{ const svg=byId('diagram').cloneNode(true); svg.setAttribute('xmlns',ns); svg.style.width='';svg.style.height='';svg.setAttribute('width',diagramBounds[2]);svg.setAttribute('height',diagramBounds[3]); const css=getComputedStyle(document.documentElement); const style=svgEl('style',{},document.querySelector('style').textContent); svg.prepend(style); svg.style.color=css.getPropertyValue('--text');for(const key of ['--text','--raised','--border','--accent','--muted','--event','--panel']) svg.style.setProperty(key,css.getPropertyValue(key)); save(new XMLSerializer().serializeToString(svg),'image/svg+xml','sandloom-architecture.svg'); };
byId('theme').onclick=()=>{document.documentElement.dataset.theme=document.documentElement.dataset.theme==='light'?'dark':'light';};
byId('inspect-selection').onclick=()=>{byId('inspector').scrollIntoView({block:'start'});const h=byId('inspector-content').querySelector('h2');if(h){h.tabIndex=-1;h.focus({preventScroll:true});}};
byId('clear-selection').onclick=()=>{byId('inspect-selection').hidden=true;selectedId='';selectedKind='entity';byId('inspector-content').replaceChildren(el('h2','Select something.'),el('p','Choose an entity, relationship or process step.','muted'));renderOverview(false);};
for(const module of modules) { const o=el('option',module.name);o.value=module.id;byId('focus').append(o); }
const projects=unique([...cards.values()].map(c=>c.import_path?.split('.')[0]).filter(p=>p&&!p.startsWith('sandloom')));
if(projects.length===1){byId('project').textContent=projects[0];document.title=projects[0]+' · Architecture explorer';}
for(const process of processes) { const o=el('option',(process.id.startsWith('saga:')?'Saga · ':'Route · ')+process.name+' · '+name(process.owner));o.value=process.id;byId('process').append(o); }
byId('status').textContent='Freshness: '+(envelope.presentation?.freshness||envelope.freshness?.state||'unavailable');
byId('scope').textContent=(data.scope?.kind==='authoring_composition'?'Authoring model':'Saved model')+' · '+modules.length+' modules'+(data.hidden?.nodes?' · '+data.hidden.nodes+' entities outside export':'');
const limits=[...(data.cycles||[]).map(c=>({text:'Declared '+human(c.kind)+' cycle: '+c.module_ids.map(name).join(' ↔ ')+'. Not proof of deadlock.',module:c.module_ids[0]})),...(data.unresolved||[]).map(u=>({text:`${name(u.module_id)} needs ${name(u.related_id)}, but nothing here provides it (${u.required?'required':'optional'}).`,module:u.module_id,required:u.required}))];
byId('uncertainty-title').textContent='Model limits';
byId('uncertainty').append(el('p','This graph records an authoring composition. Deployment bindings, runtime availability, method call order and delivery outcomes are not inferred.'));
if(limits.length) {
  byId('problems').hidden=false;byId('problems').append(el('strong',limits.length+(limits.length===1?' declaration':' declarations')+' to check'));
  for(const limit of limits) { const b=button(limit.text,()=>{selectView('overview');if(nodes.has(limit.module))goTo(nodes.get(limit.module));});if(limit.required)b.className='required';byId('problems').append(b); }
}
for(const id of ['query','focus','direction','hops','relation']) byId(id).addEventListener('input',()=>renderOverview());
byId('layer').onchange=()=>{showAllEntities=false;relationOptions();renderOverview();};
for(const id of ['process','process-view']) byId(id).onchange=()=>{if(selectedId.includes('#')) byId('clear-selection').onclick();drawProcess();stateURL();};
byId('reset').onclick=()=>{for(const [id,value] of [['query',''],['layer','modules'],['focus',''],['direction','both'],['hops','1']])byId(id).value=value;relationOptions();byId('relation').value='';renderOverview();feedback('Filters reset.');};
const params=new URLSearchParams(location.hash.slice(1));
for(const id of ['layer','focus','direction','hops','process','process-view','query']) if(params.has(id)) { const value=params.get(id);if(byId(id).tagName!=='SELECT'||[...byId(id).options].some(o=>o.value===value)) byId(id).value=value; }
relationOptions();if([...byId('relation').options].some(o=>o.value===params.get('relation'))) byId('relation').value=params.get('relation');
const initial=params.get('view') || (['sequence','state'].includes(envelope.kind)?'sequence':['impact','diff'].includes(envelope.kind)?'changes':'overview');
if(envelope.selection?.process_id&&!params.has('process')) byId('process').value=envelope.selection.process_id;
if(envelope.kind==='state'&&!params.has('process-view')) byId('process-view').value='state';
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

window.addEventListener('resize',()=>{if(currentView==='sequence'&&!byId('process').value.startsWith('saga:'))drawProcess();});
