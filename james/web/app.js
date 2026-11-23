"use strict";
const $ = id => document.getElementById(id);
const el = (tag, text) => Object.assign(document.createElement(tag), {textContent:text});
let state, thread = "main", directory = "", opened = "", fingerprint = "", selection;
let modalAction, imageData = "", imageReady = Promise.resolve(), busy = false;
let live = {}, archiveOpen = false;
function updateGuide() {
  if (!state) return;
  const feed = $("feed"), events = live.events || [];
  const elapsed = Math.max(0,Math.floor((live.ended || Date.now()/1000)-live.started));
  const turn = events.filter(line => line.startsWith("Action ")).at(-1)?.split(" · ")[0] || "Starting";
  const latest = events.at(-1)?.split(" · ").at(-1) || "Opening workspace";
  const guides = {idle:"Create a new build plan to begin.",review:"Review the checklist, then click Approve.",
    ready:"Plan approved. Click Run step to execute the next task.",complete:"Plan complete. Start a new build plan.",
    halted:"Circuit breaker triggered. Copy the packet, then supply a repair hint."};
  const reason = live.result?.reason;
  $("nextAction").textContent = live.running ? "Running step · "+turn+" · "+latest+" · "+elapsed+"s" :
    state.phase==="halted" ? guides.halted : reason ? "Paused · "+reason : guides[state.phase];
  $("stop").hidden = !live.running; $("stop").disabled = !!live.stopping;
  feed.hidden = !events.length || live.thread !== thread;
  if (feed.textContent !== events.join("\n")) {
    feed.textContent = events.join("\n"); feed.scrollTop = feed.scrollHeight;
  }
}
const stream = new EventSource("/api/live");
stream.onmessage = e => {live=JSON.parse(e.data);controls();updateGuide();};
setInterval(updateGuide,500);
$("stop").onclick=async()=>{try {await api("stop/"+live.id,{});}catch(e){note(e.message);}};
const note = text => { $("status").textContent = text; };
async function api(path, body) {
  const options = body === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json","X-James-UI":"1"}, body:JSON.stringify(body)};
  const reply = await fetch("/api/" + path, options);
  const data = await reply.json();
  if (!reply.ok) throw Error(typeof data.detail === "string" ? data.detail : "Invalid request");
  return data;
}
async function perform(label, work) {
  if (busy) return;
  busy = true; note(label); controls();
  try { await work(); note("Ready"); }
  catch (error) { note(error.message); }
  finally { busy = false; controls(); }
}
function controls() {
  if (!state) return;
  $("approve").disabled = busy || !["review","ready"].includes(state.phase);
  $("step").disabled = busy || live.running || state.phase !== "ready";
  $("packet").hidden = state.phase !== "halted";
  $("hint").hidden = !state.current;
  $("confirm").hidden = !state.behavior;
  $("composer").querySelector("button").disabled = busy;
  $("plan").disabled = busy || live.running;
  $("sketch").disabled = busy || !["review","idle","complete"].includes(state.phase);
}
function button(name, click) {
  const b = el("button",name); b.onclick = () => Promise.resolve().then(click).catch(e=>note(e.message)); return b;
}
function renderTree() {
  const nav = $("tree"); nav.replaceChildren();
  const archived = document.createElement("details");
  archived.open = archiveOpen;
  archived.append(el("summary","Archived plans"));
  archived.ontoggle = () => {if(archived.isConnected) archiveOpen=archived.open;};
  function children(parent, depth) {
    for (const [key,n] of Object.entries(state.tree).filter(([,n])=>n.parent === parent)) {
      const b = button((n.kind === "folder" ? "▾  " : "↳  ") + n.name, ()=>switchThread(key));
      b.style.paddingLeft = (7 + depth*13) + "px"; b.classList.toggle("active",key === thread);
      (n.archived ? archived : nav).append(b); children(key,depth+1);
    }
  }
  children(null,0);
  if (archived.children.length > 1) nav.append(archived);
}
async function refresh() {
  const next = await api("state"); state = next;
  $("project").textContent = state.project; $("model").textContent = state.model;
  $("phase").textContent = state.phase; $("todo").replaceChildren();
  for (const line of state.todo.split("\n")) {
    const match = /^- \[([ x])\] (.*)/.exec(line); if (!match) continue;
    const item = el("li",""); item.append(el("span",match[1]==="x"?"☑":"□"),el("span",match[2].split(" | ")[0]));
    item.title=match[2]; item.classList.toggle("done",match[1]==="x"); $("todo").append(item);
  }
  if (!$("todo").children.length) $("todo").append(el("li","Create a build plan to add tasks."));
  const used = state.budget?.input_bound || 0;
  $("used").textContent=used.toLocaleString(); $("meter").value=used;
  $("budget").textContent=used?"Last build prompt · conservative input bound · 2,048 reserved for output":"No active build context";
  $("loaded").textContent=used?["Loaded · rules and memory",state.budget.module,...state.budget.pages,"Future task files unloaded"].filter(Boolean).join("\n"):"";
  controls(); renderTree(); updateGuide();
}
async function switchThread(key) {
  thread=key; selection=null; $("reference").hidden=true;
  $("threadName").textContent=state.tree[key].name; renderTree(); await historyView();
}
async function historyView() {
  const key=thread, entries=await api("history?thread="+encodeURIComponent(key));
  if (key!==thread) return;
  const view=$("history"); view.replaceChildren();
  for (const entry of entries) {
    const card=el("article","");
    if (entry.type==="qa") {
      card.append(el("strong",entry.question),el("small",entry.path ? entry.path+" · lines "+entry.start+"–"+entry.end : ""),el("p",entry.answer));
      const suggestion = entry.answer.trim().split("\n")[0].replaceAll("|"," ").slice(0,200);
      card.append(button("Promote to task",()=>dialog("Promote to task","Review the suggested outcome and target files",suggestion+" | "+(entry.path||""),async val=>build("promote",val))));
    } else {
      const r=entry.result;
      card.append(el("strong",entry.command+" · "+r.event),el("p",[entry.task,r.done,r.changed && Object.keys(r.changed).join(", "),r.spark,r.reason,r.todo].filter(Boolean).join("\n")));
      if (r.activity?.length) {
        const details=el("details","");
        details.append(el("summary","Tool activity"),el("p",r.activity.join("\n")));
        card.append(details);
      }
    }
    view.append(card);
  }
  if (!entries.length) view.append(el("p","Questions and build events appear here."));
  view.scrollTop=view.scrollHeight;
}
async function browse(folder) {
  const entries=await api("files?directory="+encodeURIComponent(folder));
  directory=folder; $("directory").textContent=folder || "/"; $("files").replaceChildren();
  for (const f of entries) $("files").append(button((f.folder?"▸  ":"") + f.name,()=>f.folder?browse(f.path):openFile(f.path)));
}
async function openFile(path) {
  const f=await api("file?path="+encodeURIComponent(path));
  opened=path; fingerprint=f.fingerprint; $("code").value=f.text; $("filename").textContent=path;
  clearSelection();
}
function clearSelection(){ selection=null; $("reference").hidden=$("selection").hidden=true; }
$("up").onclick=()=>browse(directory.split("/").slice(0,-1).join("/")).catch(e=>note(e.message));
const code=$("code");
function detectSelection(){
  $("selection").hidden=code.selectionStart===code.selectionEnd || !opened;
}
code.onselect=code.onmouseup=code.onkeyup=detectSelection;
$("selection").onclick=()=>{
  const start=code.value.slice(0,code.selectionStart).split("\n").length;
  const end=code.value.slice(0,Math.max(code.selectionStart,code.selectionEnd-1)).split("\n").length;
  selection={path:opened,fingerprint,start,end};
  $("reference").textContent=opened+" · lines "+start+"–"+end+"   ×";
  $("reference").hidden=false; $("selection").hidden=true; $("question").focus();
};
$("reference").onclick=clearSelection;
$("composer").onsubmit=e=>{
  e.preventDefault();
  perform("James is reading the question…",async()=>{
    await api("ask",{thread,text:$("question").value,...selection}); $("question").value="";clearSelection();await historyView();
  });
};
function dialog(title,label,value,action,kind) {
  $("title").textContent=title; $("label").textContent=label; $("input").value=value;
  $("imageLabel").hidden=kind!=="sketch"; $("parentLabel").hidden=kind!=="node";
  $("input").required=kind!=="sketch"; $("preview").hidden=true; $("image").value="";
  imageData="";imageReady=Promise.resolve();modalAction=action;$("modal").showModal();$("input").focus();
}
$("close").onclick=()=>$("modal").close();
$("dialogForm").onsubmit=e=>{
  e.preventDefault(); if (busy) return;
  perform("Working…",async()=>{ await imageReady; await modalAction($("input").value.trim()); $("modal").close(); });
};
function nodeDialog(kind,edit=false) {
  const node=state.tree[thread], parent=edit?node.parent:node.kind==="folder"?thread:node.parent;
  dialog(edit?"Rename or move":kind==="folder"?"New milestone":"New follow-up","Name",edit?node.name:"",async name=>{
    const result=await api("node",{kind,name,parent:$("parent").value||null,key:edit?thread:null});
    await refresh(); await switchThread(result.key);
  },"node");
  const choices=$("parent"); choices.replaceChildren();
  if(kind==="folder") choices.add(new Option("Workspace root",""));
  for(const [id,n] of Object.entries(state.tree)){
    if(n.kind==="folder" && !(edit && id===thread)) choices.add(new Option(n.name,id));
  }
  choices.value=parent||"";
}
$("folder").onclick=()=>nodeDialog("folder"); $("subfile").onclick=()=>nodeDialog("qa");
$("manage").onclick=()=>nodeDialog(state.tree[thread].kind,true);
async function build(op,text="",image=""){
  if(op==="step" && state.tree[thread].kind==="qa") await switchThread(state.tree[thread].parent);
  let result;
  try {
    result=await api("build",{op,text,image,thread});
  } finally {
    if(op==="step") await browse(directory);
  }
  await refresh();
  const node=state.tree[thread]; await switchThread(node.kind==="folder"?thread:node.parent);
  if(result.event==="completed" && opened) await openFile(opened);
}
  $("plan").onclick=()=>dialog("New build plan","Build goal · the previous plan will be archived","",v=>build("plan",v));
$("hint").onclick=()=>dialog("Repair hint","What should James try?","",v=>build("hint",v));
$("approve").onclick=()=>perform("Approving plan…",()=>build("approve"));
$("step").onclick=()=>perform("Running one task…",()=>build("step"));
$("confirm").onclick=()=>perform("Recording verification…",()=>build("confirm"));
$("packet").onclick=async()=>{try{await navigator.clipboard.writeText((await api("packet")).packet);note("Consultation packet copied");}catch(e){note(e.message);}};
$("sketch").onclick=()=>dialog("Prototype from Sketch","Describe the layout or paste a wireframe","",v=>build("sketch",v,imageData),"sketch");
async function thumbnail(file){
  const bitmap=await createImageBitmap(file), scale=Math.min(1,512/Math.max(bitmap.width,bitmap.height));
  const canvas=document.createElement("canvas");canvas.width=Math.max(1,Math.round(bitmap.width*scale));canvas.height=Math.max(1,Math.round(bitmap.height*scale));
  canvas.getContext("2d").drawImage(bitmap,0,0,canvas.width,canvas.height);bitmap.close();
  const uri=canvas.toDataURL("image/png");imageData=uri.split(",")[1];$("preview").src=uri;$("preview").hidden=false;
}
function chooseImage(file){imageReady=thumbnail(file);imageReady.catch(e=>note(e.message));}
$("image").onchange=e=>{if(e.target.files[0])chooseImage(e.target.files[0]);};
$("modal").onpaste=e=>{const f=e.clipboardData.files[0];if(f&&!$("imageLabel").hidden){e.preventDefault();chooseImage(f);}};
(async()=>{await refresh();await switchThread(thread);await browse("");note("Ready");setInterval(()=>refresh().catch(e=>note(e.message)),2500);})().catch(e=>note(e.message));
