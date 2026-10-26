/** Test display preferences against real UI code, without private records or network. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const appSource=fs.readFileSync(new URL('../web/app.js',import.meta.url),'utf8')
  .replace(/^import .*;\r?\n/gm,'').replace(/\binit\(\);\s*$/,'');
const languageSource=fs.readFileSync(new URL('../web/i18n.js',import.meta.url),'utf8')
  .replaceAll('export function','function');

function harness({storage=new Map(),blocked=false}={}){
  const events=new Map(),controls=[{value:''},{value:''}],nodes=new Map();
  for(const key of ['#app','#workspace','#page-body','#dialog','#toast'])nodes.set(key,{innerHTML:'UNSAVED DOM',close(){}});
  const document={documentElement:{lang:'',dataset:{}},querySelector:s=>nodes.get(s)||null,
    querySelectorAll:s=>s==='[data-text-size-control]'?controls:[],addEventListener:(name,callback)=>events.set(name,callback)};
  let requests=0;
  const context=vm.createContext({document,location:{hostname:'localhost',search:'',hash:''},
    window:{addEventListener(){}},URL,URLSearchParams,Intl,Date,Math,Map,Set,setTimeout:()=>0,clearTimeout(){},
    crypto:{randomUUID:()=>''},Image:class{},cloudConfig:{},
    localStorage:{getItem:key=>{if(blocked)throw Error('Storage blocked');return storage.get(key)||null;},
      setItem:(key,value)=>{if(blocked)throw Error('Storage blocked');storage.set(key,value);}},
    fetch:async()=>{requests++;throw Error('No network allowed');}});
  vm.runInContext(languageSource,context);
  vm.runInContext(appSource+'\nglobalThis.ui={state,textSizeControl,setTextSize,chapterHome,shell,renderAuth,t,setLanguage};',context);
  const ui=context.ui;ui.state.mode='local';ui.state.data={patients:[],visits:[],reports:[],attachments:[]};
  const change=value=>events.get('change')({target:{value,hasAttribute:name=>name==='data-text-size-control'}});
  return {ui,document,controls,nodes,storage,change,requests:()=>requests};
}

test('each allowed size survives a fresh page load; corrupt preferences use standard',()=>{
  for(const value of ['standard','large','extra-large']){
    const h=harness({storage:new Map([['health_text_size',value]])});
    assert.equal(h.document.documentElement.dataset.textSize,value);
    assert.match(h.ui.textSizeControl(),new RegExp(`value="${value}" selected`));
  }
  for(const value of ['',null,'200','giant','<script>']){
    const h=harness({storage:new Map([['health_text_size',value]])});
    assert.equal(h.document.documentElement.dataset.textSize,'standard');
    assert.doesNotMatch(h.ui.textSizeControl(),/<script>/);
  }
});

test('changing size updates all controls and persists without replacing unsaved UI or busy state',async()=>{
  const h=harness();Object.assign(h.ui.state,{busy:true,person:'grandpa',route:'reports',q:'SYNTHETIC SEARCH',
    editor:{title:'SYNTHETIC UNSAVED TITLE'},detail:'synthetic-detail'});
  const editor=h.ui.state.editor;
  await h.change('extra-large');
  assert.equal(h.document.documentElement.dataset.textSize,'extra-large');
  assert.equal(h.storage.get('health_text_size'),'extra-large');
  assert.deepEqual(h.controls.map(control=>control.value),['extra-large','extra-large']);
  assert.equal(h.ui.state.editor,editor);assert.equal(h.ui.state.busy,true);
  assert.equal(h.ui.state.person,'grandpa');assert.equal(h.ui.state.route,'reports');
  assert.equal(h.ui.state.q,'SYNTHETIC SEARCH');assert.equal(h.ui.state.detail,'synthetic-detail');
  for(const node of h.nodes.values())assert.equal(node.innerHTML,'UNSAVED DOM');
  assert.equal(h.requests(),0);
  assert.equal(harness({storage:h.storage}).document.documentElement.dataset.textSize,'extra-large');
  await h.change('standard');assert.equal(h.document.documentElement.dataset.textSize,'standard');
});

test('blocked storage still permits both size and language changes for this page',async()=>{
  const h=harness({blocked:true});assert.equal(h.document.documentElement.dataset.textSize,'standard');
  await h.change('large');assert.equal(h.document.documentElement.dataset.textSize,'large');
  assert.match(h.ui.textSizeControl(),/字體大小/);
  h.ui.setLanguage('zh-Hans');assert.match(h.ui.textSizeControl(),/字体大小/);
  assert.match(h.ui.textSizeControl(),/>标准<|>特大字</);
  h.ui.setLanguage('invalid');assert.match(h.ui.textSizeControl(),/字體大小/);
  await h.change('unsafe');assert.equal(h.document.documentElement.dataset.textSize,'standard');
  assert.equal(h.requests(),0);
});

test('explicit bilingual controls remain available at chapter home, in both versions and before login',()=>{
  const h=harness();
  for(const language of ['zh-Hant','zh-Hans']){
    h.ui.setLanguage(language);
    for(const view of ['family','manage']){
      h.ui.state.view=view;h.ui.state.person='grandpa';
      for(const html of [h.ui.chapterHome(),h.ui.shell()]){
        assert.match(html,/data-text-size-control/);assert.ok(html.includes(h.ui.t('textSize')));
        assert.equal((html.match(/data-text-size-control/g)||[]).length,1);
        assert.ok(html.includes(h.ui.t('textSize_extra-large')));
      }
    }
    for(const mode of ['login','setup','share-denied']){
      h.ui.state.mode=mode;h.ui.renderAuth();
      const html=h.nodes.get('#app').innerHTML;
      assert.match(html,/data-text-size-control/);assert.ok(html.includes(h.ui.t('textSize')));
    }
  }
});
