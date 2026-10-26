/** Execute real UI behavior with synthetic in-memory records; never opens a browser. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';
import {readShareToken} from '../web/cloud.js';
import {fileURLToPath} from 'node:url';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const appSource=fs.readFileSync(path.join(root,'web/app.js'),'utf8')
  .replace(/^import .*;\r?\n/gm,'').replace(/\binit\(\);\s*$/,'');
const languageSource=fs.readFileSync(path.join(root,'web/i18n.js'),'utf8')
  .replaceAll('export function','function');

function harness({hostname='localhost',search='',hash=''}={}){
  const events=new Map(),windowEvents=new Map(),nodes=new Map(),storage=new Map(),requests=[];
  const makeNode=(extra={})=>({innerHTML:'',textContent:'',className:'',isConnected:true,
    open:false,disabled:false,close(){this.open=false;},showModal(){this.open=true;},
    addEventListener(){},querySelector:selector=>nodes.get(selector)||null,...extra});
  for(const key of ['#app','#workspace','#page-body','#medicine-categories','#toast','#dialog','.dialog-body'])nodes.set(key,makeNode());
  const document={documentElement:{lang:''},title:'',querySelector:selector=>nodes.get(selector)||null,
    querySelectorAll:()=>[],addEventListener:(name,callback)=>events.set(name,callback)};
  let respond=async()=>{throw Error('Unexpected network access in a synthetic test');};
  const client={getSession:async()=>null,onAuthChange:()=>()=>{},destroy(){},getData:async()=>fixture()};
  const context=vm.createContext({document,location:{hostname,search,hash,pathname:'/',href:'/'+search+hash},
    window:{addEventListener:(name,callback)=>windowEvents.set(name,callback),scrollTo(){},open(){}},URL,URLSearchParams,Intl,Date,Math,Map,Set,
    localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},
    crypto:webcrypto,structuredClone,setTimeout:()=>0,clearTimeout(){},Image:class {},
    FormData:class {constructor(form){this.values=form.values||{};}get(key){return this.values[key]??null;}
      getAll(key){const value=this.values[key];return Array.isArray(value)?value:value?[value]:[];}},
    cloudConfig:{supabaseUrl:'https://test.supabase.co',supabaseAnonKey:'synthetic-public-key'},
    createCloudClient:()=>client,readShareToken,
    createShareClient:(config,token,options)=>{client.shareToken=token;client.onDenied=options.onDenied;return client;},
    fetch:async(url,options)=>{requests.push({url,options});return respond(url,options);},
  });
  vm.runInContext(languageSource,context,{filename:'i18n.js'});
  vm.runInContext(appSource+`\nglobalThis.ui={state,t,init,render,chapterHome,shell,chapterTabs,heading,
    filteredVisits,appointments,medicineList,medicinePhotos,filesPage,renderDetail,empty,
    act,loadData,fileUrl,sourceIds,isLocal,gatherEditor,renderEditor,sampleData,
    medicineRows,medicineCategoryKey,categoryNav,sharePanel,loadShareStatus};`,context,{filename:'app.js'});
  const ui=context.ui;
  ui.state.mode='local';ui.state.view??='manage';ui.state.boot={csrf_token:'synthetic-csrf'};
  ui.state.data=fixture();
  const action=(name,dataset={})=>ui.act(name,{target:{closest:()=>({dataset,isConnected:true})}});
  return {ui,context,nodes,events,windowEvents,requests,client,action,
    setResponse:fn=>{respond=fn;},json:payload=>({ok:true,json:async()=>payload})};
}

function deferred(){
  let resolve,reject;
  const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});
  return {promise,resolve,reject};
}

const nextTurn=()=>new Promise(resolve=>setImmediate(resolve));

function fixture(){
  const attachment=(id,patient_id,privacy_checked=true)=>({id,patient_id,privacy_checked,
    archive_name:id+'.png',mime:'image/png',size:100});
  const visit=(id,patient_id,doctor,medicine,source)=>({id,patient_id,doctor,
    date:'2026-10-25',clinic:'SYNTHETIC CLINIC',diagnosis:doctor+'_DIAGNOSIS',
    status:'confirmed',privacy_reviewed:true,reviewed_at:'2026-10-25T00:00:00Z',
    source_file_ids:source,medicines:[{id:id+'-med',name:medicine,status:'unknown',source_file_ids:[]}],
    appointment:{date:'2026-11-02',status:'scheduled',time:'09:00',location:'SYNTHETIC CLINIC'}});
  return {schema_version:1,
    patients:[{id:'grandpa',name:'爺爺',age:80,sex:'male'},{id:'grandma',name:'嬷嬷',age:79,sex:'female'}],
    visits:[visit('visit-pa','grandpa','GRANDPA_DOCTOR','GRANDPA_MED',['pa-visit-a','pa-visit-b']),
      visit('visit-ma','grandma','GRANDMA_DOCTOR','GRANDMA_MED',['ma-photo'])],
    attachments:[attachment('pa-visit-a','grandpa'),attachment('pa-visit-b','grandpa'),
      attachment('pa-med','grandpa'),attachment('pa-raw','grandpa',false),attachment('ma-photo','grandma')]};
}

test('chapter home starts without a merged record list and selects either person',async()=>{
  const h=harness();
  assert.equal(h.ui.state.person,null);
  h.ui.render();
  const home=h.nodes.get('#app').innerHTML;
  assert.equal((home.match(/data-action="chapter"/g)||[]).length,2);
  assert.match(home,/data-id="grandpa"/);assert.match(home,/data-id="grandma"/);
  assert.doesNotMatch(home,/data-id="all"|GRANDPA_DOCTOR|GRANDMA_DOCTOR/);
  await h.action('chapter',{id:'grandma'});
  assert.equal(h.ui.state.person,'grandma');
  assert.equal(h.ui.filteredVisits().map(v=>v.id).join(','),'visit-ma');
});

test('switching chapter clears previous detail, draft and filters',async()=>{
  const h=harness();Object.assign(h.ui.state,{person:'grandpa',detail:'visit-pa',editor:{id:'draft-pa'},
    route:'medicines',q:'GRANDPA',from:'2026-01-01',to:'2026-02-01',status:'draft',category:'SYNTHETIC_CATEGORY'});
  await h.action('chapter',{id:'grandma'});
  assert.equal(h.ui.state.detail,null);assert.equal(h.ui.state.editor,null);
  assert.equal(h.ui.state.q,'');assert.equal(h.ui.state.from,'');assert.equal(h.ui.state.to,'');
  assert.equal(h.ui.state.status,'all');assert.equal(h.ui.state.route,'overview');
  assert.ok([null,'','all'].includes(h.ui.state.category),'Chapter change must reset the medicine category');
  await h.action('new');assert.equal(h.ui.state.editor.patient_id,'grandma');
  await h.action('chapters');assert.equal(h.ui.state.person,null);assert.equal(h.ui.state.editor,null);
});

test('family chapters open visits and expose visits, medicines and reports',async()=>{
  const h=harness();h.ui.state.view='family';
  await h.action('chapter',{id:'grandpa'});
  assert.equal(h.ui.state.route,'visits');
  const tabs=h.ui.chapterTabs();
  assert.match(tabs,/data-route="visits"/);assert.match(tabs,/data-route="medicines"/);assert.match(tabs,/data-route="reports"/);
  assert.doesNotMatch(tabs,/data-route="(?:overview|files|settings)"/);
  await h.action('nav',{route:'overview'});assert.equal(h.ui.state.route,'visits');
});

test('patient lists, reminders and search never include the other chapter',()=>{
  const h=harness();h.ui.state.person='grandpa';
  assert.equal(h.ui.filteredVisits().map(v=>v.id).join(','),'visit-pa');
  for(const html of [h.ui.appointments(),h.ui.medicineList(),h.ui.filesPage()]){
    assert.doesNotMatch(html,/GRANDMA_DOCTOR|GRANDMA_MED|ma-photo/);
  }
  h.ui.state.q='GRANDMA';assert.equal(h.ui.filteredVisits().length,0);
  h.ui.state.person=null;h.ui.state.q='';assert.equal(h.ui.filteredVisits().length,0);
});

test('medicines share one date section per day in descending order with unknown dates last',()=>{
  const h=harness();h.ui.state.person='grandpa';h.ui.state.route='medicines';
  const base=h.ui.state.data.visits[0];
  const dated=(id,date,name)=>({...structuredClone(base),id,date,
    medicines:[{id:id+'-med',name,status:'unknown',source_file_ids:[]}]});
  h.ui.state.data.visits=[dated('old','2026-10-01','MED_OLD'),dated('new-one','2026-10-25','MED_NEW_ONE'),
    dated('undated',null,'MED_UNDATED'),dated('middle','2026-10-10','MED_MIDDLE'),
    dated('new-two','2026-10-25','MED_NEW_TWO'),h.ui.state.data.visits[1]];
  const groups=[...h.ui.medicineList().matchAll(/<section class="medicine-date-group" data-date="([^"]*)">([\s\S]*?)<\/section>/g)];
  assert.deepEqual(groups.map(group=>group[1]),['2026-10-25','2026-10-10','2026-10-01','']);
  assert.match(groups[0][2],/MED_NEW_ONE/);assert.match(groups[0][2],/MED_NEW_TWO/);
  assert.doesNotMatch(groups[0][2],/MED_OLD|MED_MIDDLE|GRANDMA_MED/);
  assert.match(groups[3][2],/MED_UNDATED/);
});

test('medicine category selection isolates its type and patient and updates both navigation locations',async()=>{
  const h=harness();Object.assign(h.ui.state,{person:'grandpa',route:'medicines',category:'all'});
  h.ui.state.data.visits[0].medicines=[
    {id:'alpha-one',name:'MED_ALPHA_ONE',category:' Alpha ',status:'unknown'},
    {id:'alpha-two',name:'MED_ALPHA_TWO',category:'alpha',status:'unknown'},
    {id:'beta',name:'MED_BETA',category:'Beta',status:'unknown'},
    {id:'unknown',name:'MED_UNCLASSIFIED',status:'unknown'},
  ];
  h.ui.state.data.visits[1].medicines=[{id:'ma-alpha',name:'GRANDMA_ALPHA',category:'Alpha',status:'unknown'}];
  assert.equal(h.ui.medicineCategoryKey({category:' Alpha '}),'type:alpha');
  assert.equal(h.ui.medicineCategoryKey({category:''}),'uncategorized');
  assert.equal(h.ui.medicineCategoryKey({}),'uncategorized');
  await h.action('category',{value:'type:alpha'});
  let rendered=h.nodes.get('#page-body').innerHTML;
  assert.match(rendered,/MED_ALPHA_ONE/);assert.match(rendered,/MED_ALPHA_TWO/);
  assert.doesNotMatch(rendered,/MED_BETA|MED_UNCLASSIFIED|GRANDMA_ALPHA/);
  assert.match(h.nodes.get('#medicine-categories').innerHTML,/data-value="type:alpha" class="active" aria-pressed="true"/);
  assert.match(h.ui.categoryNav(),/data-value="type:beta"/);
  await h.action('category',{value:'uncategorized'});
  rendered=h.nodes.get('#page-body').innerHTML;
  assert.match(rendered,/MED_UNCLASSIFIED/);assert.doesNotMatch(rendered,/MED_ALPHA_ONE|MED_ALPHA_TWO|MED_BETA|GRANDMA_ALPHA/);
  await h.action('clear-filters');assert.equal(h.ui.state.category,'all');
});

test('detail and editing actions reject records from a different chapter',async()=>{
  const h=harness();h.ui.state.person='grandpa';
  await h.action('detail',{id:'visit-ma'});assert.equal(h.ui.state.detail,null);
  await h.action('edit',{id:'visit-ma'});assert.equal(h.ui.state.editor,null);
  await h.action('detail',{id:'visit-pa'});assert.equal(h.ui.state.detail,'visit-pa');
  h.ui.state.detail='visit-ma';assert.equal(h.ui.renderDetail(),'');assert.equal(h.ui.state.detail,null);
});

test('medicine source checkboxes remain independent from visit sources and reject other patients',()=>{
  const h=harness();h.ui.state.person='grandpa';h.ui.state.editor=structuredClone(h.ui.state.data.visits[0]);
  h.nodes.set('#visit-form',{values:{patient_id:'grandpa',date:'2026-10-25',doctor:'GRANDPA_DOCTOR',
    source:['pa-visit-a'],med_0_name:'GRANDPA_MED',med_0_category:'SOURCE_CATEGORY',med_0_status:'unknown',med_0_source:['pa-med','ma-photo']}});
  let gathered=h.ui.gatherEditor();
  assert.equal(gathered.source_file_ids.join(','),'pa-visit-a');
  assert.equal(gathered.medicines[0].source_file_ids.join(','),'pa-med');
  assert.equal(gathered.medicines[0].category,'SOURCE_CATEGORY');
  h.nodes.get('#visit-form').values.source=[];
  gathered=h.ui.gatherEditor();assert.equal(gathered.source_file_ids.length,0);
  assert.equal(gathered.medicines[0].source_file_ids.join(','),'pa-med');
  h.nodes.get('#visit-form').values.source=['pa-visit-a'];
  h.nodes.get('#visit-form').values.med_0_source=[];
  gathered=h.ui.gatherEditor();assert.equal(gathered.medicines[0].source_file_ids.length,0);
  assert.equal(gathered.source_file_ids.join(','),'pa-visit-a');
  assert.doesNotMatch(h.ui.renderEditor(),/value="ma-photo"/);
});

test('changing a draft patient clears both visit and per-medicine source assignments',async()=>{
  const h=harness();h.ui.state.person='grandpa';h.ui.state.editor=structuredClone(h.ui.state.data.visits[0]);
  h.nodes.set('#visit-form',{values:{patient_id:'grandma',date:'2026-10-25',doctor:'TEST DOCTOR',
    source:['pa-visit-a'],med_0_name:'TEST MED',med_0_status:'unknown',med_0_source:['pa-med']}});
  await h.events.get('change')({target:{name:'patient_id'}});
  assert.equal(h.ui.state.editor.patient_id,'grandma');
  assert.equal(h.ui.state.editor.source_file_ids.length,0);
  assert.equal(h.ui.state.editor.medicines[0].source_file_ids.length,0);
});

test('medicine photos prefer explicit links and exclude cross-person attachments',()=>{
  const h=harness();h.ui.state.person='grandpa';const v=h.ui.state.data.visits[0];
  const html=h.ui.medicinePhotos({...v.medicines[0],source_file_ids:['pa-med','ma-photo']},v);
  assert.match(html,/data-id="pa-med"/);
  assert.doesNotMatch(html,/data-id="ma-photo"|data-id="pa-visit-a"|data-id="pa-visit-b"/);
  assert.ok(html.includes(h.ui.t('medicinePhoto')));
});

test('visit-photo fallback retains every source and labels its scope honestly',()=>{
  const h=harness();h.ui.state.person='grandpa';const v=h.ui.state.data.visits[0];
  const html=h.ui.medicinePhotos(v.medicines[0],v);
  assert.match(html,/data-id="pa-visit-a"/);assert.match(html,/data-id="pa-visit-b"/);
  assert.ok(html.includes(h.ui.t('visitPhoto')));
  assert.doesNotMatch(h.ui.medicinePhotos(v.medicines[0],{...v,source_file_ids:[]}),/<img/);
});

test('raw medicine photos stay hidden and cannot open from a family view',async()=>{
  const h=harness();h.ui.state.person='grandpa';const v=h.ui.state.data.visits[0];
  const medicine={...v.medicines[0],source_file_ids:['pa-raw']};
  const manage=h.ui.medicinePhotos(medicine,v);
  assert.doesNotMatch(manage,/<img/);assert.match(manage,/data-action="redact"/);
  h.ui.state.view='family';
  const family=h.ui.medicinePhotos(medicine,v);
  assert.doesNotMatch(family,/<img|data-action="redact"|data-action="view-file"/);
  await h.action('view-file',{id:'pa-raw'});
  assert.doesNotMatch(h.nodes.get('.dialog-body').innerHTML,/pa-raw/);
});

test('family preview reads the safe endpoint and management reads its own endpoint',async()=>{
  const h=harness();h.setResponse(()=>h.json(fixture()));
  h.ui.state.view='family';await h.ui.loadData();assert.equal(h.requests.at(-1).url,'/api/family-data');
  h.ui.state.view='manage';await h.ui.loadData();assert.equal(h.requests.at(-1).url,'/api/data');
});

test('family views render no upload, settings, editing or confirmation controls',()=>{
  for(const mode of ['local','cloud','share']){
    const h=harness();Object.assign(h.ui.state,{mode,view:'family',person:'grandpa',detail:'visit-pa'});
    const html=[h.ui.chapterHome(),h.ui.shell(),h.ui.chapterTabs(),h.ui.heading(),h.ui.renderDetail(),h.ui.empty()].join('');
    assert.doesNotMatch(html,/data-action="(?:new|scan|upload|edit|confirm|redact|backup|restore|export)"/);
    assert.doesNotMatch(html,/data-route="(?:overview|files|settings)"/);
    assert.equal(h.ui.isLocal(),false);
  }
});

test('family actions cannot enter operation routes or issue mutation requests',async()=>{
  const h=harness();Object.assign(h.ui.state,{view:'family',person:'grandpa',route:'visits'});
  for(const [name,dataset] of [['nav',{route:'overview'}],['nav',{route:'settings'}],['nav',{route:'files'}],['new',{}],
    ['edit',{id:'visit-pa'}],['scan',{}],['backup',{}]]){
    try{await h.action(name,dataset);}catch(error){assert.match(error.message,/只讀|只读|查閱|查阅|read.?only/i);}
    assert.notEqual(h.ui.state.route,'overview');assert.notEqual(h.ui.state.route,'settings');assert.notEqual(h.ui.state.route,'files');
    assert.equal(h.ui.state.editor,null);
  }
  assert.equal(h.requests.filter(r=>r.options?.method==='POST').length,0);
});

test('local preview query and remote hosting select the family view',async()=>{
  const local=harness({search:'?view=family'});local.ui.state.mode='loading';
  local.setResponse(url=>local.json(url==='/api/bootstrap'?{csrf_token:'test'}:fixture()));
  await local.ui.init();assert.equal(local.ui.state.view,'family');
  assert.ok(local.requests.some(r=>r.url==='/api/family-data'));
  const remote=harness({hostname:'family.example',search:'?view=manage'});remote.ui.state.mode='loading';
  await remote.ui.init();assert.equal(remote.ui.state.view,'family');assert.equal(remote.ui.state.mode,'login');
});

test('family demo omits drafts while operation demo retains review examples without writes',async()=>{
  const h=harness();h.ui.state.demo=true;h.ui.state.view='family';
  await h.ui.loadData();assert.ok(h.ui.state.data.visits.length>0);
  assert.ok(h.ui.state.data.visits.every(v=>v.status==='confirmed'));
  h.ui.state.view='manage';await h.ui.loadData();
  assert.ok(h.ui.state.data.visits.some(v=>v.status==='draft'));
  await assert.rejects(h.action('scan'),error=>error.message===h.ui.t('sampleOnly'));
  assert.equal(h.requests.length,0);
});

test('thumbnail and original routes remain distinct, and chapter images retain full sources',async()=>{
  const h=harness();h.ui.state.person='grandpa';const a=h.ui.state.data.attachments[0];
  assert.equal(await h.ui.fileUrl(a,true),'/api/files/pa-visit-a/thumbnail');
  assert.equal(await h.ui.fileUrl(a,false),'/api/files/pa-visit-a');
  h.ui.state.mode='cloud';h.ui.state.urls.clear();
  let thumbnailCalls=0,originalCalls=0;
  h.ui.state.client={thumbnailUrl:async()=>{thumbnailCalls++;return 'blob:preview';},
    fileUrl:async()=>{originalCalls++;return 'blob:original';}};
  assert.equal(await h.ui.fileUrl(a,true),'blob:preview');
  assert.equal(await h.ui.fileUrl(a,false),'blob:original');
  assert.equal(thumbnailCalls,1);assert.equal(originalCalls,1);
});


test('remote share links bypass login and force family-only view even with manage/demo queries',async()=>{
  const h=harness({hostname:'family.example',search:'?view=manage&demo=1',hash:'#share='+'A'.repeat(43)});
  await h.ui.init();
  assert.equal(h.ui.state.view,'family');assert.equal(h.ui.state.mode,'share');
  assert.equal(h.ui.state.demo,false);assert.equal(h.client.shareToken,'A'.repeat(43));
  assert.doesNotMatch(h.nodes.get('#app').innerHTML,/login-form|data-action="logout"/);
  for(const action of ['share-create','share-rotate-apply','share-revoke-apply','share-copy','new','backup'])
    await assert.rejects(h.action(action));
  assert.equal(h.requests.length,0);
});

test('malformed share link fails closed instead of falling back to an existing account',async()=>{
  const h=harness({hostname:'family.example',hash:'#share=broken'});
  h.client.getSession=async()=>({access_token:'synthetic-session'});
  await h.ui.init();assert.equal(h.ui.state.mode,'share-denied');
  assert.equal(h.ui.state.data.visits.length,0);
  assert.doesNotMatch(h.nodes.get('#app').innerHTML,/login-form|GRANDPA_DOCTOR/);
});

test('access denial clears records, open detail and image URLs',async()=>{
  const h=harness({hostname:'family.example',hash:'#share='+'B'.repeat(43)});
  await h.ui.init();h.ui.state.person='grandpa';h.ui.state.detail='visit-pa';
  h.client.onDenied();
  assert.equal(h.ui.state.mode,'share-denied');assert.equal(h.ui.state.data.visits.length,0);
  assert.equal(h.ui.state.person,null);assert.equal(h.ui.state.detail,null);
  assert.doesNotMatch(h.nodes.get('#app').innerHTML,/GRANDPA_DOCTOR|login-form/);
});

test('local share status uses CSRF on GET and only displays a verified active URL',async()=>{
  const h=harness();h.ui.state.boot.cloud={configured:true};
  h.setResponse(()=>h.json({configured:true,status:'active',verified:true,url:'https://family.example/#share=SYNTHETIC'}));
  await h.ui.loadShareStatus();
  assert.equal(h.requests.at(-1).url,'/api/share');
  assert.equal(h.requests.at(-1).options.headers['X-CSRF-Token'],'synthetic-csrf');
  assert.match(h.ui.sharePanel(),/share-copy|share-rotate|share-revoke/);
  h.ui.state.share.verified=false;assert.doesNotMatch(h.ui.sharePanel(),/SYNTHETIC|share-copy/);
});

for(const olderFailsFirst of [true,false]){
  test(`stale share init rejection cannot override a newer ${olderFailsFirst?'pending':'completed'} share`,async()=>{
    const oldToken='A'.repeat(43),newToken='B'.repeat(43);
    const h=harness({hostname:'family.example',hash:'#share='+oldToken});
    const oldData=deferred(),newData=deferred();
    const oldClient={getData:()=>oldData.promise,destroy(){this.destroyed=true;}};
    const newClient={getData:()=>newData.promise,destroy(){this.destroyed=true;}};
    h.context.createShareClient=(config,token,options)=>{
      const client=token===oldToken?oldClient:newClient;
      client.onDenied=options.onDenied;
      return client;
    };
    const firstInit=h.ui.init();
    assert.equal(h.ui.state.client,oldClient);
    h.context.location.hash='#share='+newToken;
    const secondInit=h.ui.init();
    assert.equal(oldClient.destroyed,true);
    assert.equal(h.ui.state.client,newClient);
    const currentData=fixture();currentData.visits[0].doctor='NEW_SHARE_DOCTOR';
    const rejectOld=async()=>{
      // A late access-denied callback and a late request rejection must both be ignored.
      oldClient.onDenied();
      oldData.reject(new Error('OLD_SHARE_REQUEST_FAILED'));
      await firstInit;
      assert.equal(h.ui.state.mode,'share');
      assert.equal(h.ui.state.client,newClient);
      assert.equal(h.ui.state.error,null);
    };
    if(olderFailsFirst)await rejectOld();
    newData.resolve(currentData);await secondInit;
    if(!olderFailsFirst)await rejectOld();
    assert.equal(h.ui.state.data.visits[0].doctor,'NEW_SHARE_DOCTOR');
    assert.doesNotMatch(h.nodes.get('#app').innerHTML,/OLD_SHARE_REQUEST_FAILED|login-form/);
    assert.equal(h.requests.length,0);
  });
}

test('hashchange immediately clears rendered medical data, editor and dialogs before the new share resolves',async()=>{
  const oldToken='C'.repeat(43),newToken='D'.repeat(43);
  const h=harness({hostname:'family.example',hash:'#share='+oldToken});
  const newData=deferred(),oldImage=deferred();
  const oldClient={getData:async()=>fixture(),fileUrl:()=>oldImage.promise,destroy(){this.destroyed=true;}};
  const newClient={getData:()=>newData.promise,destroy(){this.destroyed=true;}};
  h.context.createShareClient=(config,token)=>token===oldToken?oldClient:newClient;
  await h.ui.init();
  Object.assign(h.ui.state,{person:'grandpa',detail:'visit-pa',route:'visits'});
  // Use the real detail renderer as the currently visible medical DOM.
  h.nodes.get('#app').innerHTML='<main>'+h.ui.renderDetail()+'</main>';
  assert.match(h.nodes.get('#app').innerHTML,/GRANDPA_DOCTOR/);
  const priorImage=h.action('view-file',{id:'pa-visit-a'});
  const discardedImage=assert.rejects(priorImage);
  assert.equal(h.nodes.get('#dialog').open,true);
  h.ui.state.editor={id:'SYNTHETIC_OLD_EDITOR'};
  h.ui.state.urls.set('old-preview','blob:synthetic-prior-share');
  h.context.location.hash='#share='+newToken;
  h.windowEvents.get('hashchange')();

  // These assertions run synchronously, while the replacement fetch is pending.
  assert.equal(oldClient.destroyed,true);
  assert.equal(h.ui.state.client,newClient);
  assert.equal(h.ui.state.data.visits.length,0);
  assert.equal(h.ui.state.data.attachments.length,0);
  assert.equal(h.ui.state.person,null);
  assert.equal(h.ui.state.detail,null);
  assert.equal(h.ui.state.editor,null);
  assert.equal(h.ui.state.urls.size,0);
  assert.equal(h.nodes.get('#dialog').open,false);
  assert.doesNotMatch(h.nodes.get('#app').innerHTML,/GRANDPA_DOCTOR|GRANDPA_MED|SYNTHETIC_OLD_EDITOR/);
  assert.ok(h.nodes.get('#app').innerHTML.includes(h.ui.t('loading')));

  oldImage.resolve('blob:synthetic-late-old-image');await discardedImage;
  assert.equal(h.nodes.get('#dialog').open,false);
  assert.doesNotMatch(h.nodes.get('.dialog-body').innerHTML,/synthetic-late-old-image/);
  const replacement=fixture();replacement.visits[0].doctor='REPLACEMENT_SHARE_DOCTOR';
  newData.resolve(replacement);await nextTurn();
  assert.equal(h.ui.state.mode,'share');
  assert.equal(h.ui.state.data.visits[0].doctor,'REPLACEMENT_SHARE_DOCTOR');
  assert.equal(h.ui.state.client,newClient);
  assert.equal(h.requests.length,0);
});
