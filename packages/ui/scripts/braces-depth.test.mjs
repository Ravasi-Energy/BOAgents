import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
const root=process.env.BO01_UI_APP||path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const require=createRequire(path.join(root,'package.json'));
const braces=require('braces'),micromatch=require('micromatch');
// 9983 characters: below this distribution's 10000-character input ceiling.
const pattern='{'.repeat(4990)+'a,b'+'}'.repeat(4990);
const controlled=error=>error instanceof RangeError&&error.code==='ERR_BRACES_DEPTH';
function ast(depth=6000){
 const root={type:'root',nodes:[]};let node=root;
 for(let i=0;i<depth;i++){const next={type:'brace',commas:1,ranges:0,nodes:[],parent:node};node.nodes.push(next);node=next;}
 node.nodes.push({type:'text',value:'safe',parent:node});return root;
}
for(const method of ['main','create','parse','compile','expand','stringify'])test('braces '+method+': deep input is refused with a controlled code before native stack overflow',()=>{
 const fn=method==='main'?braces:braces[method];
 assert.throws(()=>fn(pattern),controlled);
});
for(const method of ['compile','expand','stringify'])test('braces direct lib/'+method+': supplied AST cannot bypass depth fencing',()=>{
 const fn=require('braces/lib/'+method);
 assert.throws(()=>fn(ast()),controlled);
});
test('braces cycles and malformed children are refused, not traversed recursively',()=>{
 const cyclic={type:'root',nodes:[]};cyclic.nodes.push(cyclic);
 assert.throws(()=>braces.compile(cyclic),error=>error.code==='ERR_BRACES_CYCLE');
 assert.throws(()=>braces.stringify({type:'root',nodes:'not-an-array'}),error=>error.code==='ERR_BRACES_AST');
});
test('braces options can tighten bounds but cannot disable or raise ceilings',()=>{
 for(const maxDepth of [0,-1,129,Infinity,NaN,'128'])assert.throws(()=>braces.compile('{a,b}',{maxDepth}),error=>error.code==='ERR_BRACES_LIMIT');
 assert.throws(()=>braces.compile('{a,{b,c}}',{maxDepth:2}),controlled);
 assert.equal(braces.compile('safe',{maxDepth:2}),'safe');
 assert.throws(()=>braces.compile('{a,b}',{maxNodes:1}),error=>error.code==='ERR_BRACES_NODES');
});
test('braces rejects a supplied parent cycle and leaves an oversized AST unmodified',()=>{
 const cycle={type:'root',nodes:[]};cycle.parent=cycle;
 assert.throws(()=>braces.expand(cycle),error=>error.code==='ERR_BRACES_CYCLE');
 const value=ast();assert.throws(()=>braces.expand(value),controlled);
 assert.equal(Object.hasOwn(value,'queue'),false);
 assert.equal(value.nodes.length,1);
});
test('braces ordinary compile/expand/parse/stringify and escaping remain available',()=>{
 assert.deepEqual(braces.expand('a/{b,c}/d'),['a/b/d','a/c/d']);
 assert.equal(braces.compile('a/{b,c}/d'),'a/(b|c)/d');
 assert.equal(braces.stringify(braces.parse('a/{b,c}/d')),'a/{b,c}/d');
 assert.deepEqual(braces.expand('{01..03}'),['01','02','03']);
 assert.deepEqual(braces.expand('\\{a,b\\}'),['{a,b}']);
 assert.deepEqual(braces(['{a,b}','{c,d}'],{expand:true}),['a','b','c','d']);
 assert.deepEqual(braces.expand('{a,b'),['{a,b']);
 assert.equal(braces.stringify(braces.parse('a/{b,c')),'a/{b,c');
});
test('micromatch live consumer keeps ordinary matches and fences public brace entrypoints',()=>{
 assert.deepEqual(micromatch(['a.js','b.ts','c.md'],'*.{js,ts}'),['a.js','b.ts']);
 assert.deepEqual(micromatch.braceExpand('*.{js,ts}'),['*.js','*.ts']);
 assert.throws(()=>micromatch.braces(pattern),controlled);
 assert.throws(()=>micromatch.braceExpand(pattern),controlled);
});
