// Contracts set the direction first; an event or ordering edge joins only if the graph stays acyclic.
// A caller sits after the contract it requires, so an interface reads provider → contract → caller.
function positions(items,links,cardMap=new Map()) {
  const ids=items.map(n=>n.id).sort(), adjacent=new Map(ids.map(id=>[id,[]])), reverse=new Map(ids.map(id=>[id,[]]));
  for(const r of links) if(adjacent.has(r.source)&&adjacent.has(r.target)) { adjacent.get(r.source).push(r.target); reverse.get(r.target).push(r.source); }
  const forward=new Map(ids.map(id=>[id,new Set()])), weight=r=>r.kind?.includes('contract')?0:r.kind==='ordering'?2:1;
  const reaches=(from,to)=>{const seen=new Set([from]),pending=[from];while(pending.length){const at=pending.pop();if(at===to)return true;for(const next of forward.get(at))if(!seen.has(next)){seen.add(next);pending.push(next);}}return false;};
  for(const r of [...links].sort((a,b)=>weight(a)-weight(b)||String(a.id).localeCompare(String(b.id)))) { const [from,to]=r.kind==='requires_contract'?[r.target,r.source]:[r.source,r.target]; if(forward.has(from)&&forward.has(to)&&from!==to&&!reaches(to,from)) forward.get(from).add(to); }
  const ranks=new Map(ids.map(id=>[id,0])), indegree=new Map(ids.map(id=>[id,0]));
  for(const targets of forward.values()) for(const id of targets) indegree.set(id,indegree.get(id)+1);
  const ready=ids.filter(id=>indegree.get(id)===0);
  while(ready.length) { const at=ready.shift(); for(const next of forward.get(at)) { ranks.set(next,Math.max(ranks.get(next),ranks.get(at)+1)); indegree.set(next,indegree.get(next)-1); if(indegree.get(next)===0) ready.push(next); } }
  // Pack weak components separately. A column is vertical, never a row of intermediaries.
  const unseen=new Set(ids), groups=[];
  while(unseen.size) {const start=unseen.values().next().value,group=[],pending=[start];unseen.delete(start);while(pending.length){const id=pending.pop();group.push(id);for(const next of [...adjacent.get(id),...reverse.get(id)])if(unseen.delete(next))pending.push(next);}groups.push(group.sort());}
  groups.sort((a,b)=>b.length-a.length||a[0].localeCompare(b[0]));
  const result=new Map();let top=64;
  for(const group of groups) {
    let bottom=top;
    const columns=[...new Set(group.map(id=>ranks.get(id)))].sort((a,b)=>a-b), order=new Map(group.map((id,i)=>[id,i]));
    const score=id=>{const near=[...adjacent.get(id),...reverse.get(id)].filter(n=>ranks.get(n)!==ranks.get(id));return near.length?near.reduce((n,x)=>n+order.get(x),0)/near.length:order.get(id);};
    for(let pass=0;pass<4;pass++) for(const rank of pass%2?[...columns].reverse():columns) group.filter(id=>ranks.get(id)===rank).sort((a,b)=>score(a)-score(b)||a.localeCompare(b)).forEach((id,i)=>order.set(id,i));
    for(const rank of columns) {
      let y=top;
      for(const id of group.filter(id=>ranks.get(id)===rank).sort((a,b)=>order.get(a)-order.get(b))) {
        const n=items.find(n=>n.id===id), title=wrapText(n.name,30), purpose=n.kind==='module'?wrapText(cardMap.get(id)?.summary||'',33):[];
        const degree=links.filter(r=>r.source===id||r.target===id).length;
        const height=Math.max(48+title.length*18+purpose.length*15,40+degree*14);
        result.set(id,{x:28+rank*400,y,width:240,height,title,purpose,isolated:group.length===1&&!degree});
        y+=height+72;bottom=Math.max(bottom,y);
      }
    }
    top=bottom+80;
  }
  return result;
}
// One line per direction of a module pair. Ordering stays in the list when a real link already explains the pair.
function pairLinks(relations,interfaceNames) {
  const key=r=>[r.source,r.target].sort().join('|'), explained=new Set(relations.filter(r=>r.kind!=='ordering').map(key)), pairs=new Map();
  for(const r of relations) {
    if(r.kind==='ordering'&&explained.has(key(r)))continue;
    const id='pair:'+r.source+'>'+r.target;if(!pairs.has(id))pairs.set(id,{id,source:r.source,target:r.target,members:[]});
    pairs.get(id).members.push(r);
  }
  const plural=(n,word)=>n+' '+word+(n===1?'':'s');
  return [...pairs.values()].map(pair=>{
    const has=kind=>pair.members.some(m=>m.kind===kind), of=test=>[...new Set(pair.members.filter(test).flatMap(interfaceNames))];
    const contracts=of(m=>m.kind.endsWith('_contract')), events=of(m=>m.kind==='event_impact'), named=[...contracts,...events];
    const counts=[contracts.length?plural(contracts.length,'contract'):'',events.length?plural(events.length,'event'):''].filter(Boolean).join(' · ')||'load order';
    return {...pair,kind:has('required_contract')?'required_contract':has('optional_contract')?'optional_contract':has('event_impact')?'event_impact':'ordering',label:named.length===1?named[0]:counts,shortLabel:counts};
  });
}
// A label must never hide a block or another label; the list below always carries the full text.
function placeLabels(routes,at) {
  const boxes=[...at.values()], placed=[], hits=(a,b,pad=2)=>a.x<b.x+b.width+pad&&a.x+a.width+pad>b.x&&a.y<b.y+b.height+pad&&a.y+a.height+pad>b.y, result=[];
  for(const {link,points} of routes) {
    if(!link.label)continue;
    const segments=points.slice(1).map((q,i)=>[points[i],q]).filter(([p,q])=>p[1]===q[1]).sort((a,b)=>Math.abs(b[0][0]-b[1][0])-Math.abs(a[0][0]-a[1][0]));
    let found=null;
    for(const text of link.shortLabel&&link.shortLabel!==link.label?[link.label,link.shortLabel]:[link.label]) {
      const width=Array.from(text).length*5.6+8,height=14;
      for(const [p,q] of segments) for(const t of [.5,.3,.7,.15,.85]) {
        const box={x:p[0]+(q[0]-p[0])*t-width/2,y:p[1]-height/2,width,height};
        if(!boxes.some(b=>hits(box,b))&&!placed.some(b=>hits(box,b))){found={link,text,box};break;}
      }
      if(found)break;
    }
    if(found){placed.push(found.box);result.push(found);}
  }
  return result;
}
function wrapText(text,length) {
  const width=value=>Array.from(value).reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:/[MW@#%ЖШЩЮФ]/i.test(c)?1.6:1),0);
  const lines=[];let line='';
  for(const word of String(text).replace(/``([^`]+)``/g,'$1').split(/\s+/).filter(Boolean)) {
    const parts=[];let part='';for(const c of Array.from(word)){if(part&&width(part+c)>length){parts.push(part);part='';}part+=c;}if(part)parts.push(part);
    for(const part of parts){if(line&&width(line+' '+part)>length){lines.push(line);line='';}line+=(line?' ':'')+part;}
  }
  if(line)lines.push(line);return lines;
}
function crossesBox(a,b,box,pad=0) {
  const left=box.x-pad,right=box.x+box.width+pad,top=box.y-pad,bottom=box.y+box.height+pad;
  return a[0]===b[0]?a[0]>left&&a[0]<right&&Math.max(a[1],b[1])>top&&Math.min(a[1],b[1])<bottom:a[1]>top&&a[1]<bottom&&Math.max(a[0],b[0])>left&&Math.min(a[0],b[0])<right;
}
function overlap(a,b,c,d) {
  if(a[0]===b[0]&&c[0]===d[0]&&a[0]===c[0])return Math.max(0,Math.min(Math.max(a[1],b[1]),Math.max(c[1],d[1]))-Math.max(Math.min(a[1],b[1]),Math.min(c[1],d[1])));
  if(a[1]===b[1]&&c[1]===d[1]&&a[1]===c[1])return Math.max(0,Math.min(Math.max(a[0],b[0]),Math.max(c[0],d[0]))-Math.max(Math.min(a[0],b[0]),Math.min(c[0],d[0])));
  return 0;
}
function crossing(a,b,c,d) {
  if(a[0]===b[0]&&c[1]===d[1])return a[0]>Math.min(c[0],d[0])&&a[0]<Math.max(c[0],d[0])&&c[1]>Math.min(a[1],b[1])&&c[1]<Math.max(a[1],b[1]);
  if(a[1]===b[1]&&c[0]===d[0])return crossing(c,d,a,b);
  return false;
}
function routedLinks(at,links) {
  const ports=new Map(), specs=links.map((link,index)=>{
    const a=at.get(link.source),b=at.get(link.target),forward=b.x>a.x;
    const sourceSide=forward||a.x===b.x?'right':'left',targetSide=forward?'left':'right';
    const spec={link,index,a,b,sourceSide,targetSide};
    for(const [id,side,end] of [[link.source,sourceSide,'start'],[link.target,targetSide,'end']]){const key=id+':'+side;if(!ports.has(key))ports.set(key,[]);ports.get(key).push({spec,end});}
    return spec;
  });
  for(const entries of ports.values()){entries.sort((a,b)=>{const p=a.end==='start'?a.spec.b:a.spec.a,q=b.end==='start'?b.spec.b:b.spec.a,home=a.end==='start'?a.spec.a:a.spec.b;return (p.y+p.height/2)-(q.y+q.height/2)||Math.abs(q.x-home.x)-Math.abs(p.x-home.x)||a.spec.link.id.localeCompare(b.spec.link.id);});entries.forEach(({spec,end},i)=>{const box=end==='start'?spec.a:spec.b,side=end==='start'?spec.sourceSide:spec.targetSide;spec[end]=[box.x+(side==='right'?box.width:0),box.y+18+(i+1)*(box.height-36)/(entries.length+1)];spec[end+'Offset']=18+(end==='end'?entries.length-i-1:i)%8*7;});}
  const used=[], result=[];
  for(const spec of specs) {
    const {link,index,a,b,start,end}=spec, sx=start[0]+(spec.sourceSide==='right'?1:-1)*spec.startOffset,tx=end[0]+(spec.targetSide==='right'?1:-1)*spec.endOffset;
    const minY=Math.min(...[...at.values()].map(p=>p.y)),maxY=Math.max(...[...at.values()].map(p=>p.y+p.height));
    const lanes=[...new Set([start[1],end[1],...Array.from(at.values()).flatMap(p=>[p.y-24,p.y+p.height+24]),minY-36-index*13,maxY+36+index*13])];
    const channels=[(sx+tx)/2,sx,tx,...Array.from(at.values()).flatMap(p=>[p.x-24,p.x+p.width+24])];
    const candidates=channels.map(x=>[start,[x,start[1]],[x,end[1]],end]);
    candidates.push(...lanes.map(y=>[start,[sx,start[1]],[sx,y],[tx,y],[tx,end[1]],end]));
    let best=null,score=Infinity;
    for(const candidate of candidates) {
      const points=candidate.filter((p,i)=>!i||p[0]!==candidate[i-1][0]||p[1]!==candidate[i-1][1]);
      let cost=points.length*18,valid=true;
      for(let i=1;i<points.length&&valid;i++){
        const p=points[i-1],q=points[i];
        for(const [id,box] of at)if(crossesBox(p,q,box,[link.source,link.target].includes(id)?0:10)){valid=false;break;}
        cost+=Math.abs(p[0]-q[0])+Math.abs(p[1]-q[1]);
        for(const [r,s] of used)cost+=overlap(p,q,r,s)*10000+(crossing(p,q,r,s)?1000:0);
      }
      if(valid&&cost<score){best=points;score=cost;}
    }
    // Never draw a guessed straight shortcut when a route is unavailable.
    if(!best)throw new Error('No obstacle-free route for '+link.id);
    for(let i=1;i<best.length;i++)used.push([best[i-1],best[i]]);
    result.push({link,points:best,d:best.map((p,i)=>(i?'L ':'M ')+p.join(' ')).join(' ')});
  }
  return result;
}
