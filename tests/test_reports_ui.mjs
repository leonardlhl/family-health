/** Behavioral report/PDF UI checks using synthetic data and controlled async loads. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';
import {fileURLToPath} from 'node:url';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const appSource=fs.readFileSync(path.join(root,'web/app.js'),'utf8')
  .replace(/^import .*;\r?\n/gm,'').replace(/\binit\(\);\s*$/,'');
const languageSource=fs.readFileSync(path.join(root,'web/i18n.js'),'utf8')
  .replaceAll('export function','function');
const HASH='a'.repeat(64);
const plain=value=>JSON.parse(JSON.stringify(value));
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};

function fixture(){
  const report=(id,patient_id,title,date,status='confirmed',source_file_ids=[])=>({
    id,patient_id,title,date,status,privacy_reviewed:status==='confirmed',category:'SYNTHETIC CATEGORY',
    facility:'SYNTHETIC FACILITY',doctor:'SYNTHETIC DOCTOR',summary:title+'_SUMMARY',notes:'',
    review_notes:status==='draft'?'OPERATOR_ONLY_REVIEW':'',source_file_ids,reviewed_at:status==='confirmed'?'2026-10-26T00:00:00Z':null});
  const attachment=(id,patient_id,privacy_checked=true,mime='application/pdf')=>({
    id,patient_id,privacy_checked,mime,size:100,sha256:HASH,pages:2,archive_name:id+(mime==='application/pdf'?'.pdf':'.png')});
  return {schema_version:1,patients:[{id:'grandpa',name:'爺爺'},{id:'grandma',name:'嬷嬷'}],visits:[],
    reports:[report('r-pa-old','grandpa','PA_OLD','2026-09-01','confirmed',['pa-pdf']),
      report('r-pa-new','grandpa','PA_NEW','2026-10-25','confirmed',['pa-photo']),
      report('r-pa-draft','grandpa','PA_DRAFT','2026-10-26','draft',['pa-raw']),
      report('r-ma','grandma','MA_REPORT','2026-10-26','confirmed',['ma-pdf'])],
    attachments:[attachment('pa-pdf','grandpa'),attachment('pa-photo','grandpa',true,'image/png'),
      attachment('pa-raw','grandpa',false),attachment('ma-pdf','grandma')]};
}

function harness(){
  const events=new Map(),nodes=new Map(),requests=[],images=[],draws=[],storage=new Map();
  const makeNode=(extra={})=>({innerHTML:'',textContent:'',className:'',isConnected:true,open:false,
    disabled:false,checked:false,value:'',hidden:false,handlers:new Map(),
    close(){this.open=false;},showModal(){this.open=true;},
    querySelector:selector=>nodes.get(selector)||null,querySelectorAll:()=>[],
    addEventListener(name,callback){this.handlers.set(name,callback);},...extra});
  for(const key of ['#app','#workspace','#page-body','#medicine-categories','#toast','#dialog','.dialog-body',
    '#pdf-page','#pdf-reviewed','#pdf-progress','[data-action="save-pdf"]',
    '[data-action="pdf-prev"]','[data-action="pdf-next"]'])nodes.set(key,makeNode());
  const canvas=makeNode({width:1000,height:2000,setPointerCapture(){},
    getBoundingClientRect:()=>({left:0,top:0,width:1000,height:2000}),
    getContext:()=>({drawImage:img=>draws.push(img.src),fillRect(){},fillStyle:''})});
  nodes.set('#redact-canvas',canvas);
  let decode=()=>Promise.resolve();
  class FakeImage{
    constructor(){this.naturalWidth=1000;this.naturalHeight=2000;images.push(this);}
    decode(){return decode(this);}
  }
  const json=(payload,ok=true)=>({ok,json:async()=>payload});
  let respond=async url=>{
    if(url.endsWith('/pdf-info'))return json({pages:2,page_sizes:[{width:1000,height:2000},{width:1000,height:2000}],sha256:HASH});
    if(url==='/api/data'||url==='/api/family-data')return json(fixture());
    throw Error('Unexpected network request in a synthetic test');
  };
  const document={documentElement:{lang:''},title:'',querySelector:s=>nodes.get(s)||null,
    querySelectorAll:()=>[],addEventListener:(name,callback)=>events.set(name,callback)};
  const context=vm.createContext({document,location:{hostname:'localhost',search:'',hash:'',pathname:'/'},
    window:{addEventListener(){},scrollTo(){},open(){}},URL,URLSearchParams,Intl,Date,Math,Map,Set,
    localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},
    crypto:webcrypto,structuredClone,setTimeout:()=>0,clearTimeout(){},Image:FakeImage,
    FormData:class{constructor(form){this.values=form.values||{};}get(key){return this.values[key]??null;}
      getAll(key){const value=this.values[key];return Array.isArray(value)?value:value?[value]:[];}},
    cloudConfig:{},fetch:async(url,options)=>{requests.push({url,options});return respond(url,options);}});
  vm.runInContext(languageSource,context,{filename:'i18n.js'});
  vm.runInContext(appSource+`\nglobalThis.ui={state,t,render,renderWorkspace,chapterTabs,filteredReports,
    reportFiles,reportList,renderReportDetail,renderReportEditor,gatherReportEditor,fileSummary,
    act,showPdfReview,loadPdfPage,savePdfReview,loadStorageStatus,storagePanel,clearSharedView,
    get redaction(){return redaction;}};`,context,{filename:'app.js'});
  const ui=context.ui;Object.assign(ui.state,{mode:'local',view:'manage',person:'grandpa',route:'reports',
    data:fixture(),boot:{csrf_token:'synthetic-csrf'}});
  const action=(name,dataset={})=>ui.act(name,{target:{closest:()=>({dataset,isConnected:true})}});
  const review=checked=>events.get('change')({target:{id:'pdf-reviewed',checked}});
  const form=values=>makeNode({id:'report-form',values,matches:()=>false});
  const submit=async values=>{const f=form(values);nodes.set('#report-form',f);await events.get('submit')({target:f,preventDefault(){}});};
  return {ui,context,nodes,events,requests,images,draws,canvas,action,review,submit,form,json,
    setResponse:fn=>{respond=fn;},setDecode:fn=>{decode=fn;}};
}

test('legacy snapshots without reports remain readable and family navigation includes reports',async()=>{
  const h=harness();delete h.ui.state.data.reports;h.ui.state.view='family';
  assert.equal(h.ui.filteredReports().length,0);assert.match(h.ui.reportList(),/class="empty"/);
  assert.match(h.ui.chapterTabs(),/data-route="reports"/);
  assert.doesNotMatch(h.ui.chapterTabs(),/data-route="(?:overview|files|settings)"/);
  await h.action('nav',{route:'reports'});assert.equal(h.ui.state.route,'reports');
  await h.action('nav',{route:'settings'});assert.equal(h.ui.state.route,'reports');
});

test('reports filter by selected person, confirmation, search and dates in descending date order',()=>{
  const h=harness();h.ui.state.view='family';
  assert.deepEqual(plain(h.ui.filteredReports().map(r=>r.id)),['r-pa-new','r-pa-old']);
  assert.doesNotMatch(h.ui.reportList(),/PA_DRAFT|MA_REPORT|OPERATOR_ONLY_REVIEW/);
  h.ui.state.q='MA_REPORT';assert.equal(h.ui.filteredReports().length,0);
  h.ui.state.q='SYNTHETIC DOCTOR';h.ui.state.from='2026-10-01';
  assert.deepEqual(plain(h.ui.filteredReports().map(r=>r.id)),['r-pa-new']);
  h.ui.state.to='2026-10-20';assert.equal(h.ui.filteredReports().length,0);
  Object.assign(h.ui.state,{q:'',from:'',to:'',view:'manage'});
  assert.equal(h.ui.filteredReports()[0].id,'r-pa-draft');
});

test('report detail and linked sources reject other people and hide raw sources for family',async()=>{
  const h=harness();h.ui.state.view='family';
  const r=h.ui.state.data.reports[0];r.source_file_ids=['pa-pdf','ma-pdf','pa-raw'];
  assert.deepEqual(plain(h.ui.reportFiles(r).map(a=>a.id)),['pa-pdf']);
  await h.action('report-detail',{id:'r-ma'});assert.equal(h.ui.state.detail,null);
  await h.action('report-detail',{id:r.id});assert.equal(h.ui.state.detail,r.id);
  assert.doesNotMatch(h.ui.renderReportDetail(),/ma-pdf|pa-raw|data-action="(?:edit-report|confirm-report|redact)"/);
  await h.action('report-detail',{id:'r-pa-draft'});
  assert.equal(h.ui.renderReportDetail(),'');assert.equal(h.ui.state.detail,null);
});

test('report text and file labels are escaped before rendering',()=>{
  const h=harness();const attack='<img src=x onerror="steal()">';
  const r=h.ui.state.data.reports[0];r.title=attack;r.summary=attack;r.facility=attack;r.notes=attack;
  h.ui.state.detail=r.id;
  const source=h.ui.state.data.attachments[0];source.archive_name=attack;
  for(const html of [h.ui.reportList(),h.ui.renderReportDetail()]){
    assert.doesNotMatch(html,/<img src=x|onerror="steal\(\)"/);
    assert.match(html,/&lt;img src=x onerror=&quot;steal\(\)&quot;&gt;/);
  }
});

test('family modes cannot mutate reports or enter PDF redaction even through direct actions',async()=>{
  for(const mode of ['local','cloud','share']){
    const h=harness();Object.assign(h.ui.state,{mode,view:'family'});
    for(const action of ['new-report','edit-report','confirm-report','redact','save-pdf'])
      await assert.rejects(h.action(action,{id:'r-pa-old'}));
    assert.equal(h.requests.length,0);assert.equal(h.ui.state.editor,null);
    h.ui.state.detail='r-pa-old';
    assert.doesNotMatch(h.ui.renderReportDetail(),/data-action="(?:edit-report|confirm-report|redact)"/);
  }
});

test('report editor sources and patient change cannot carry files across chapters',async()=>{
  const h=harness();await h.action('edit-report',{id:'r-pa-old'});
  assert.doesNotMatch(h.ui.renderReportEditor(),/value="ma-pdf"/);
  const f=h.form({patient_id:'grandpa',date:'2026-10-26',title:'SYNTHETIC EDIT',source:['pa-pdf','ma-pdf']});
  h.nodes.set('#report-form',f);
  assert.deepEqual(plain(h.ui.gatherReportEditor().source_file_ids),['pa-pdf']);
  f.values.patient_id='grandma';f.values.source=['pa-pdf','ma-pdf'];
  await h.events.get('change')({target:{name:'patient_id'}});
  assert.equal(h.ui.state.editor.patient_id,'grandma');assert.equal(h.ui.state.editor.source_file_ids.length,0);
  assert.doesNotMatch(h.ui.renderReportEditor(),/value="pa-pdf"|value="pa-raw"/);
});

test('report submit checks local management permission before networking',async()=>{
  for(const mode of ['local','cloud','share']){
    const h=harness();Object.assign(h.ui.state,{mode,view:'family',editorKind:'report',editor:{patient_id:'grandpa'}});
    await h.submit({patient_id:'grandpa',title:'SYNTHETIC',date:'2026-10-26',source:['pa-pdf']});
    assert.equal(h.requests.length,0);assert.match(h.nodes.get('#toast').className,/error/);
  }
});

test('local report submit retains stable ID, source ownership and CSRF then opens returned report',async()=>{
  const h=harness();await h.action('edit-report',{id:'r-pa-old'});
  h.setResponse((url)=>h.json(url==='/api/reports'?{id:'r-pa-old'}:fixture()));
  await h.submit({patient_id:'grandpa',title:'UPDATED SYNTHETIC',date:'2026-10-26',source:['pa-pdf','ma-pdf']});
  const request=h.requests.find(r=>r.url==='/api/reports'),body=JSON.parse(request.options.body);
  assert.equal(body.id,'r-pa-old');assert.deepEqual(body.source_file_ids,['pa-pdf']);
  assert.equal(request.options.headers['X-CSRF-Token'],'synthetic-csrf');
  assert.equal(h.ui.state.detailKind,'report');assert.equal(h.ui.state.detail,'r-pa-old');assert.equal(h.ui.state.editor,null);
});

test('PDF review requires every loaded page and sends source hash plus normalized reverse-drag masks',async()=>{
  const h=harness();await h.ui.showPdfReview(h.ui.state.data.attachments[0]);
  assert.equal(h.ui.redaction.info.sha256,HASH);
  assert.match(h.images[0].src,new RegExp('/page/0\\?source='+HASH+'$'));
  await assert.rejects(h.ui.savePdfReview('pa-pdf'));
  h.canvas.handlers.get('pointerdown')({pointerId:1,clientX:800,clientY:1600});
  h.canvas.handlers.get('pointermove')({clientX:200,clientY:400});
  h.canvas.handlers.get('pointerup')();await h.review(true);
  await assert.rejects(h.ui.savePdfReview('pa-pdf'));
  await h.ui.loadPdfPage(1);await h.review(true);
  assert.equal(h.nodes.get('[data-action="save-pdf"]').disabled,false);
  h.setResponse(url=>h.json(url.endsWith('/sanitize-pdf')?{id:'pa-pdf'}:fixture()));
  await h.action('save-pdf',{id:'pa-pdf'});
  const request=h.requests.find(r=>r.url.endsWith('/sanitize-pdf')),body=JSON.parse(request.options.body);
  assert.equal(body.source_sha256,HASH);assert.equal(body.privacy_checked,true);
  assert.deepEqual(body.reviewed_pages,[0,1]);
  assert.deepEqual(body.pages,[{page:0,boxes:[{x:.2,y:.2,w:.6,h:.6}]},{page:1,boxes:[]}]);
  assert.equal(request.options.headers['X-CSRF-Token'],'synthetic-csrf');
  assert.equal(h.nodes.get('#dialog').open,false);assert.equal(h.ui.redaction,null);
});

test('editing or undoing a mask invalidates that page review and blocks save',async()=>{
  const h=harness();await h.ui.showPdfReview(h.ui.state.data.attachments[0]);await h.review(true);
  await h.ui.loadPdfPage(1);await h.review(true);await h.ui.loadPdfPage(0);
  h.canvas.handlers.get('pointerdown')({pointerId:2,clientX:100,clientY:100});
  assert.equal(h.ui.redaction.reviewed.has(0),false);assert.equal(h.nodes.get('[data-action="save-pdf"]').disabled,true);
  h.canvas.handlers.get('pointermove')({clientX:300,clientY:300});h.canvas.handlers.get('pointerup')();
  await h.review(true);assert.equal(h.ui.redaction.reviewed.size,2);
  await h.action('undo-mask');assert.equal(h.ui.redaction.reviewed.has(0),false);
  assert.equal(h.ui.redaction.pageBoxes.get(0).length,0);await assert.rejects(h.ui.savePdfReview('pa-pdf'));
});

test('out-of-order page decode cannot replace the latest page image or mask array',async()=>{
  const h=harness();await h.ui.showPdfReview(h.ui.state.data.attachments[0]);
  const old=deferred(),latest=deferred();h.setDecode(img=>img.src.includes('/page/1?')?old.promise:latest.promise);
  const older=h.ui.loadPdfPage(1),newer=h.ui.loadPdfPage(0);
  latest.resolve();await newer;const active=h.ui.redaction.img;
  old.resolve();await older;
  assert.equal(h.ui.redaction.page,0);assert.equal(h.ui.redaction.img,active);
  assert.match(h.draws.at(-1),/\/page\/0\?/);
  assert.equal(h.ui.redaction.boxes,h.ui.redaction.pageBoxes.get(0));
});

test('closing PDF dialog while a page decodes prevents stale image and approval state returning',async()=>{
  const h=harness();await h.ui.showPdfReview(h.ui.state.data.attachments[0]);
  const wait=deferred();h.setDecode(()=>wait.promise);const loading=h.ui.loadPdfPage(1);
  const draws=h.draws.length;await h.action('close-dialog');wait.resolve();await loading;
  assert.equal(h.ui.redaction,null);assert.equal(h.nodes.get('#dialog').open,false);assert.equal(h.draws.length,draws);
});

test('failed page decoding cannot be marked reviewed or saved',async()=>{
  const h=harness();await h.ui.showPdfReview(h.ui.state.data.attachments[0]);await h.review(true);
  h.setDecode(()=>Promise.reject(Error('SYNTHETIC DECODE FAILURE')));
  await assert.rejects(h.ui.loadPdfPage(1));await h.review(true);
  assert.equal(h.ui.redaction.reviewed.has(1),false);assert.equal(h.nodes.get('#pdf-reviewed').disabled,true);
  await assert.rejects(h.ui.savePdfReview('pa-pdf'));
});

test('source hash rejection keeps the review open and does not claim a saved PDF',async()=>{
  const h=harness();await h.ui.showPdfReview(h.ui.state.data.attachments[0]);await h.review(true);
  await h.ui.loadPdfPage(1);await h.review(true);
  h.setResponse(()=>h.json({error:'SYNTHETIC STALE SOURCE'},false));
  await assert.rejects(h.ui.savePdfReview('pa-pdf'),/SYNTHETIC STALE SOURCE/);
  assert.equal(h.nodes.get('#dialog').open,true);assert.notEqual(h.ui.redaction,null);
  assert.equal(h.requests.filter(r=>r.url==='/api/data').length,0);
});

test('storage response cannot populate family view after leaving management',async()=>{
  const h=harness(),wait=deferred();h.setResponse(()=>wait.promise);
  const pending=h.ui.loadStorageStatus();h.ui.state.view='family';
  wait.resolve(h.json({cloud:{available:true,bucket_bytes:123},local:{archive_bytes:100}}));await pending;
  assert.equal(h.ui.state.storage,null);
});

test('revoked sharing clears report data, selected details and any pending PDF review',async()=>{
  const h=harness();await h.ui.showPdfReview(h.ui.state.data.attachments[0]);
  h.ui.state.detail='r-pa-old';h.ui.state.editor={id:'r-pa-draft'};h.ui.clearSharedView();
  assert.equal(h.ui.state.data.reports.length,0);assert.equal(h.ui.state.detail,null);
  assert.equal(h.ui.state.editor,null);assert.equal(h.ui.redaction,null);assert.equal(h.nodes.get('#dialog').open,false);
});

test('PDF file summary uses the backend pages property',()=>{
  const h=harness();assert.match(h.ui.fileSummary({...h.ui.state.data.attachments[0],pages:7}),/7/);
});
