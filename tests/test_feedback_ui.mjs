/** Run real medication-feedback UI code using synthetic records and fake services. */
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
const languageSource=fs.readFileSync(path.join(root,'web/i18n.js'),'utf8').replaceAll('export function','function');
const plain=value=>JSON.parse(JSON.stringify(value));
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const sampleItem=(extra={})=>({id:'12345678-1234-4234-8234-123456789abc',patient_id:'grandpa',visit_id:'v-pa',medicine_id:'m-pa',
  rating:'effective',notes:'SYNTHETIC NOTE',revision:1,created_at:'2026-10-26T10:00:00Z',...extra});
const fixture=()=>({schema_version:1,patients:[{id:'grandpa',name:'爺爺'},{id:'grandma',name:'嬷嬷'}],reports:[],attachments:[],
  visits:[{id:'v-pa',patient_id:'grandpa',date:'2026-10-25',status:'confirmed',privacy_reviewed:true,doctor:'SYNTHETIC DOCTOR',source_file_ids:[],
    medicines:[{id:'m-pa',name:'SYNTHETIC PA MED',strength:'SYNTHETIC',dose:'',frequency:'',source_file_ids:[]}]},
    {id:'v-ma',patient_id:'grandma',date:'2026-10-25',status:'confirmed',privacy_reviewed:true,doctor:'SYNTHETIC DOCTOR',source_file_ids:[],
      medicines:[{id:'m-ma',name:'SYNTHETIC MA MED',source_file_ids:[]}]},
    {id:'v-draft',patient_id:'grandpa',status:'draft',medicines:[{id:'m-draft',name:'DRAFT MED'}]}]});

function harness({mode='share',view='family',demo=false}={}){
  const nodes=new Map(),events=new Map(),windowEvents=new Map(),requests=[],saved=[],storage=new Map(),slots=[];
  const makeNode=(extra={})=>({innerHTML:'',textContent:'',className:'',isConnected:true,open:false,disabled:false,
    close(){this.open=false;},showModal(){this.open=true;},querySelector:s=>nodes.get(s)||null,
    addEventListener(){},matches:()=>false,...extra});
  for(const key of ['#app','#workspace','#page-body','#medicine-categories','#toast','#dialog','.dialog-body',
    '#feedback-count','#feedback-form [type="submit"]','#backup-result'])nodes.set(key,makeNode());
  let remoteItems=[];
  let save=async payload=>({item:sampleItem({patient_id:payload.patient_id,visit_id:payload.visit_id,
    medicine_id:payload.medicine_id,rating:payload.rating,notes:payload.notes,revision:payload.expected_revision+1})});
  let getFeedback=async()=>({items:remoteItems});
  let getData=async()=>fixture();
  const client={getFeedback:()=>getFeedback(),getData:()=>getData(),saveFeedback:async payload=>{
    saved.push(plain(payload));return save(payload);},destroy(){},getSession:async()=>null};
  const json=payload=>({ok:true,json:async()=>payload});
  let respond=async url=>url==='/api/medicine-feedback'?json({items:remoteItems,stale:false}):
    ['/api/data','/api/family-data'].includes(url)?json(fixture()):Promise.reject(Error('Unexpected synthetic request'));
  const document={documentElement:{lang:'',dataset:{}},title:'',querySelector:s=>nodes.get(s)||null,
    querySelectorAll:s=>s==='.feedback-slot'?slots:[],addEventListener:(name,callback)=>events.set(name,callback)};
  const context=vm.createContext({document,location:{hostname:'localhost',search:'',hash:'',pathname:'/'},
    window:{addEventListener:(name,callback)=>windowEvents.set(name,callback),scrollTo(){}},URL,URLSearchParams,Intl,Date,Math,Map,Set,
    localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},
    crypto:webcrypto,structuredClone,setTimeout:()=>0,clearTimeout(){},Image:class {},cloudConfig:{},
    FormData:class{constructor(form){this.values=form.values||{};}get(key){return this.values[key]??null;}getAll(key){return this.values[key]||[];}},
    fetch:async(url,options)=>{requests.push({url,options});return respond(url,options);}});
  vm.runInContext(languageSource,context,{filename:'i18n.js'});
  vm.runInContext(appSource+`\nglobalThis.ui={state,t,loadData,act,feedbackCanWrite,feedbackTarget,feedbackCard,
    feedbackSlot,feedbackFor,loadFeedback,showFeedback,saveFeedback,feedbackDraft,feedbackFormHtml,
    acknowledgeFeedbackConflict,refreshFeedbackSource,clearFeedback,clearSharedView,renderFeedbackSlots,
    get editor(){return feedbackEditor;}};`,context,{filename:'app.js'});
  const ui=context.ui;Object.assign(ui.state,{mode,view,demo,person:'grandpa',route:'medicines',data:fixture(),
    client,boot:{csrf_token:'synthetic-csrf'},feedback:{items:[],status:demo?'demo':'ready',stale:false,last_sync:null}});
  const action=(name,dataset={})=>ui.act(name,{target:{closest:()=>({dataset,isConnected:true})}});
  const form=(rating='effective',notes='SYNTHETIC DRAFT')=>{
    const f=makeNode({id:'feedback-form',values:{'feedback-note':notes,...(rating===null?{}:{'feedback-rating':rating})}});
    nodes.set('#feedback-form',f);return f;
  };
  return {ui,nodes,events,windowEvents,requests,saved,client,slots,action,form,json,
    setSave:fn=>{save=fn;},setFeedback:fn=>{getFeedback=fn;},setData:fn=>{getData=fn;},
    setResponse:fn=>{respond=fn;},setItems:items=>{remoteItems=items;}};
}

test('feedback targets require the selected person, confirmed visit and exact medicine',async()=>{
  const h=harness();assert.ok(h.ui.feedbackTarget('v-pa','m-pa'));
  for(const ids of [['v-ma','m-ma'],['v-pa','m-ma'],['v-draft','m-draft']]){
    assert.equal(h.ui.feedbackTarget(...ids),null);await h.ui.showFeedback(...ids);assert.equal(h.ui.editor,null);
  }
  const draft=h.ui.state.data.visits[2];assert.equal(h.ui.feedbackSlot(draft,draft.medicines[0]),'');
});

test('unset and explicitly undecided feedback display as distinct states without naming an author',()=>{
  const h=harness(),v=h.ui.state.data.visits[0],m=v.medicines[0];
  const unset=h.ui.feedbackCard(v,m);assert.ok(unset.includes(h.ui.t('feedbackUnset')));
  h.ui.state.feedback.items=[sampleItem({rating:'undecided',notes:''})];
  const decided=h.ui.feedbackCard(v,m);assert.ok(decided.includes(h.ui.t('feedback_undecided')));
  assert.ok(decided.includes(h.ui.t('feedbackFamily')));assert.ok(!decided.includes(h.ui.t('feedbackUnset')));
  assert.doesNotMatch(decided,/actor_scope|member:|share:|@/);
});

test('local management and local family preview read cache but cannot submit feedback',async()=>{
  for(const view of ['manage','family']){
    const h=harness({mode:'local',view});h.setItems([sampleItem()]);await h.ui.loadFeedback();
    assert.equal(h.requests[0].url,'/api/medicine-feedback');assert.equal(h.requests[0].options.headers['X-CSRF-Token'],'synthetic-csrf');
    assert.equal(h.ui.feedbackCanWrite(),false);await assert.rejects(h.ui.showFeedback('v-pa','m-pa'));
    const v=h.ui.state.data.visits[0];assert.doesNotMatch(h.ui.feedbackCard(v,v.medicines[0]),/data-action="feedback-open"/);
    assert.equal(h.saved.length,0);
  }
});

test('demo allows previewing a form but never submits or contacts remote feedback',async()=>{
  const h=harness({mode:'local',view:'manage',demo:true});await h.ui.loadFeedback();await h.ui.showFeedback('v-pa','m-pa');
  assert.ok(h.ui.editor);assert.match(h.nodes.get('#dialog').innerHTML,/<button type="submit"[^>]*disabled/);
  await assert.rejects(h.ui.saveFeedback(h.form()));assert.equal(h.saved.length,0);assert.equal(h.requests.length,0);
});

test('feedback notes and conflict text are escaped and include a privacy reminder',async()=>{
  const h=harness(),attack='<img src=x onerror="steal()">';h.ui.state.feedback.items=[sampleItem({notes:attack})];
  const v=h.ui.state.data.visits[0];assert.match(h.ui.feedbackCard(v,v.medicines[0]),/&lt;img/);
  await h.ui.showFeedback('v-pa','m-pa');let html=h.nodes.get('#dialog').innerHTML;
  assert.ok(html.includes(h.ui.t('feedbackPrivacy')));assert.doesNotMatch(html,/<img src=x/);
  h.setSave(async()=>{throw Object.assign(Error('conflict'),{code:'feedback_conflict',item:sampleItem({notes:attack,revision:2})});});
  await h.ui.saveFeedback(h.form());html=h.nodes.get('#dialog').innerHTML;assert.match(html,/&lt;img/);assert.doesNotMatch(html,/<img src=x/);
});

test('500 Unicode code points save successfully and 501 are rejected before networking',async()=>{
  const h=harness();await h.ui.showFeedback('v-pa','m-pa');
  await assert.rejects(h.ui.saveFeedback(h.form('effective','🙂'.repeat(501))));assert.equal(h.saved.length,0);
  h.setFeedback(async()=>({items:[sampleItem({notes:'🙂'.repeat(500)})]}));
  await h.ui.saveFeedback(h.form('effective','🙂'.repeat(500)));
  assert.equal(h.saved.length,1);assert.equal(Array.from(h.saved[0].notes).length,500);
  assert.equal(h.saved[0].expected_revision,0);assert.match(h.saved[0].request_id,/^[a-f0-9-]{36}$/);
  assert.deepEqual(Object.keys(h.saved[0]).sort(),['expected_revision','medicine_id','notes','patient_id','rating','request_id','visit_id']);
});

test('unknown network outcome retains original draft and identical retry request UUID/body',async()=>{
  const h=harness();await h.ui.showFeedback('v-pa','m-pa');h.setSave(async()=>{throw Error('SYNTHETIC OFFLINE');});
  await h.ui.saveFeedback(h.form('ineffective','KEEP THIS NOTE'));
  const first=plain(h.ui.editor.pending);assert.equal(h.ui.editor.draft.rating,'ineffective');
  assert.match(h.nodes.get('#dialog').innerHTML,/<fieldset[^>]*disabled/);assert.match(h.nodes.get('#dialog').innerHTML,/<textarea[^>]*readonly/);
  // Native FormData omits a disabled radio fieldset, but includes readonly textareas.
  await h.ui.saveFeedback(h.form(null,'KEEP THIS NOTE'));
  assert.deepEqual(h.saved,[first,first]);assert.equal(h.ui.editor.draft.rating,'ineffective');
  assert.equal(h.ui.editor.draft.notes,'KEEP THIS NOTE');assert.equal(h.nodes.get('#dialog').open,true);
});

test('conflict preserves user draft and requires explicit keep or use-latest before a new write',async()=>{
  for(const useLatest of [false,true]){
    const h=harness();await h.ui.showFeedback('v-pa','m-pa');
    const latest=sampleItem({rating:'ineffective',notes:'LATEST FAMILY NOTE',revision:4});
    h.setSave(async()=>{throw Object.assign(Error('conflict'),{code:'feedback_conflict',item:latest});});
    await h.ui.saveFeedback(h.form('effective','MY DRAFT'));assert.equal(h.ui.editor.draft.notes,'MY DRAFT');
    assert.ok(h.ui.editor.conflict);await assert.rejects(h.ui.saveFeedback(h.form()));assert.equal(h.saved.length,1);
    h.form('effective','MY DRAFT');h.ui.acknowledgeFeedbackConflict(useLatest);
    assert.equal(h.ui.editor.expected_revision,4);assert.equal(h.ui.editor.conflict,null);
    assert.equal(h.ui.editor.draft.notes,useLatest?'LATEST FAMILY NOTE':'MY DRAFT');assert.equal(h.saved.length,1);
    h.setSave(async payload=>({item:sampleItem({...payload,id:latest.id,revision:5})}));
    const draft=plain(h.ui.editor.draft);await h.ui.saveFeedback(h.form(draft.rating,draft.notes));
    assert.equal(h.saved[1].expected_revision,4);assert.notEqual(h.saved[1].request_id,h.saved[0].request_id);
  }
});

test('foreign conflict items cannot be applied or expose the other patient note',async()=>{
  const h=harness();await h.ui.showFeedback('v-pa','m-pa');
  h.setSave(async()=>{throw Object.assign(Error('conflict'),{code:'feedback_conflict',item:sampleItem({patient_id:'grandma',visit_id:'v-ma',medicine_id:'m-ma',notes:'PRIVATE MA NOTE'})});});
  await h.ui.saveFeedback(h.form());assert.equal(h.ui.editor.stale,true);assert.equal(h.ui.editor.conflict,null);
  assert.doesNotMatch(h.nodes.get('#dialog').innerHTML,/PRIVATE MA NOTE/);assert.equal(h.ui.state.feedback.items.length,0);
});

test('stale source refresh preserves draft, updates revision and never automatically resubmits',async()=>{
  const h=harness();await h.ui.showFeedback('v-pa','m-pa');
  h.setSave(async()=>{throw Object.assign(Error('stale'),{code:'feedback_stale'});});
  await h.ui.saveFeedback(h.form('ineffective','RETAIN DURING REFRESH'));
  assert.equal(h.ui.editor.stale,true);h.form(null,'RETAIN DURING REFRESH');
  h.setFeedback(async()=>({items:[sampleItem({revision:3})]}));await h.ui.refreshFeedbackSource();
  assert.equal(h.ui.editor.stale,false);assert.equal(h.ui.editor.expected_revision,3);
  assert.equal(h.ui.editor.draft.rating,'ineffective');assert.equal(h.ui.editor.draft.notes,'RETAIN DURING REFRESH');
  assert.equal(h.saved.length,1);
});

test('removed medicine refresh leaves a visible explanation and retained draft without save',async()=>{
  const h=harness();await h.ui.showFeedback('v-pa','m-pa');
  h.setSave(async()=>{throw Object.assign(Error('stale'),{code:'feedback_stale'});});
  await h.ui.saveFeedback(h.form('effective','KEEP REMOVED MED NOTE'));
  const removed=fixture();removed.visits[0].medicines=[];h.setData(async()=>removed);h.form(null,'KEEP REMOVED MED NOTE');
  await h.ui.refreshFeedbackSource();
  assert.ok(h.nodes.get('#dialog').innerHTML.includes(h.ui.t('feedbackMedicineRemoved')));
  assert.match(h.nodes.get('#dialog').innerHTML,/KEEP REMOVED MED NOTE/);assert.equal(h.ui.editor.draft.notes,'KEEP REMOVED MED NOTE');
  assert.equal(h.ui.editor.stale,true);assert.equal(h.saved.length,1);
});

test('successful idempotent replay refreshes latest revision instead of leaving old feedback visible',async()=>{
  const h=harness();await h.ui.showFeedback('v-pa','m-pa');
  h.setSave(async()=>({item:sampleItem({revision:1,notes:'EARLIER SAVED'})}));
  h.setFeedback(async()=>({items:[sampleItem({revision:3,notes:'NEWER FAMILY UPDATE'})]}));
  await h.ui.saveFeedback(h.form());assert.equal(h.ui.state.feedback.items[0].revision,3);
  assert.equal(h.ui.state.feedback.items[0].notes,'NEWER FAMILY UPDATE');assert.equal(h.nodes.get('#dialog').open,false);
});

test('closing an in-flight save never reopens the dialog on late failure',async()=>{
  const h=harness(),wait=deferred();await h.ui.showFeedback('v-pa','m-pa');h.setSave(()=>wait.promise);
  const pending=h.ui.saveFeedback(h.form());await h.action('close-dialog');wait.reject(Error('LATE OFFLINE'));await pending;
  assert.equal(h.ui.editor,null);assert.equal(h.nodes.get('#dialog').open,false);
});

test('late successful save for a closed editor does not close a newer editor',async()=>{
  const h=harness(),wait=deferred();await h.ui.showFeedback('v-pa','m-pa');h.setSave(()=>wait.promise);
  const pending=h.ui.saveFeedback(h.form());await h.action('close-dialog');
  await h.ui.showFeedback('v-pa','m-pa');const current=h.ui.editor;
  wait.resolve({item:sampleItem()});await pending;
  assert.equal(h.ui.editor,current);assert.equal(h.nodes.get('#dialog').open,true);
});

test('revocation clears feedback and a late successful write cannot repopulate it',async()=>{
  const h=harness(),wait=deferred();h.ui.state.feedback.items=[sampleItem()];await h.ui.showFeedback('v-pa','m-pa');h.setSave(()=>wait.promise);
  const pending=h.ui.saveFeedback(h.form());h.ui.clearSharedView();wait.resolve({item:sampleItem({revision:2})});await pending;
  assert.equal(h.ui.state.mode,'share-denied');assert.equal(h.ui.state.feedback.items.length,0);
  assert.equal(h.ui.editor,null);assert.equal(h.nodes.get('#dialog').open,false);
});

test('out-of-order feedback loads cannot replace a newer result',async()=>{
  const h=harness(),old=deferred(),newest=deferred();let call=0;h.setFeedback(()=>++call===1?old.promise:newest.promise);
  const first=h.ui.loadFeedback(),second=h.ui.loadFeedback();newest.resolve({items:[sampleItem({revision:2})]});await second;
  old.resolve({items:[sampleItem({revision:1})]});await first;assert.equal(h.ui.state.feedback.items[0].revision,2);
});

test('invalid feedback load fails softly, preserves known cache and marks it stale',async()=>{
  const h=harness();h.ui.state.feedback.items=[sampleItem()];
  h.setFeedback(async()=>({items:[sampleItem({patient_id:'grandma',visit_id:'v-pa'})]}));await h.ui.loadFeedback();
  assert.equal(h.ui.state.feedback.status,'error');assert.equal(h.ui.state.feedback.stale,true);
  assert.equal(h.ui.state.feedback.items[0].notes,'SYNTHETIC NOTE');
});

test('local stale cache and backup warning never claim feedback is freshly synced',async()=>{
  const h=harness({mode:'local',view:'manage'});h.setResponse(url=>h.json(url==='/api/medicine-feedback'
    ?{items:[sampleItem()],stale:true,last_sync:'2026-10-25T00:00:00Z'}
    :url==='/api/backup'?{name:'synthetic.zip',feedback_stale:true}:{csrf_token:'synthetic-csrf'}));
  await h.ui.loadFeedback();const v=h.ui.state.data.visits[0];
  assert.ok(h.ui.feedbackCard(v,v.medicines[0]).includes(h.ui.t('feedbackCached')));
  await h.action('backup');assert.equal(h.nodes.get('#toast').textContent,h.ui.t('feedbackBackupStale'));
  assert.match(h.nodes.get('#toast').className,/error/);
});

test('feedback-slot rendering cannot inject the other person into the active chapter',()=>{
  const h=harness();const pa={dataset:{feedbackVisit:'v-pa',feedbackMedicine:'m-pa',feedbackPatient:'grandpa'},innerHTML:''};
  const ma={dataset:{feedbackVisit:'v-ma',feedbackMedicine:'m-ma',feedbackPatient:'grandma'},innerHTML:'old content'};
  h.slots.push(pa,ma);h.ui.renderFeedbackSlots();assert.match(pa.innerHTML,/medicine-feedback/);assert.equal(ma.innerHTML,'');
});
