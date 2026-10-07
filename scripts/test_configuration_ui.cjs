// Pure contract checks. No browser automation or live publication.
const assert = require("node:assert/strict"), vm = require("node:vm"), fs = require("node:fs");
const nodes = new Map();
function element(){return {value:"",textContent:"",checked:false,disabled:false,children:[],addEventListener(){},append(...x){this.children.push(...x)},replaceChildren(...x){this.children=x}}}
const $ = id => {if(!nodes.has(id))nodes.set(id,element());return nodes.get(id)};
const calls=[];let release;
const context=vm.createContext({$,node:(tag,cls,value)=>Object.assign(element(),{textContent:value}),notify(){},date:x=>x,api:async(path,options)=>{calls.push([path,options]);return {proof:"proof",diff:[],warnings:[],effect:"test",expires_at:100}}});
vm.runInContext(fs.readFileSync("src/vey/dashboard_assets/configuration.js","utf8"),context);
(async()=>{
  await vm.runInContext("publishConfiguration()",context);assert.equal(calls.length,0);
  vm.runInContext('configurationState.current={revision:"a".repeat(32)};configurationRows=()=>[{key:"fixture/web",aliases:[],protected:false,health_url:null}]',context);
  $("config-reason").value="test";
  await vm.runInContext("previewConfiguration()",context);assert.equal(calls[0][0],"configuration/preview");
  await vm.runInContext("publishConfiguration()",context);assert.equal(calls.length,1,"Unchecked confirmation must never publish");
  vm.runInContext("editedConfiguration()",context);$("config-confirm").checked=true;
  await vm.runInContext("publishConfiguration()",context);assert.equal(calls.length,1,"Editing must invalidate preview");
  context.api=()=>new Promise(resolve=>{release=resolve});
  const pending=vm.runInContext("previewConfiguration()",context);
  vm.runInContext("clearConfiguration()",context);release({proof:"stale-proof",diff:[],warnings:[],effect:"test",expires_at:100});await pending;
  assert.equal(vm.runInContext("configurationState.proof",context),null);
  assert.equal($("config-publish").disabled,true);
  console.log("Configuration UI contracts passed: preview/confirmation required, edits and logout invalidate pending publication");
})().catch(error=>{console.error(error);process.exitCode=1});
