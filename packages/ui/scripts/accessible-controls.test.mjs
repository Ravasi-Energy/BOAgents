// Semantic source audit includes conditional editor branches absent from static route fixtures.
// Browser axe and keyboard evidence remain separate and mandatory.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
const root = new URL('../src/', import.meta.url);
function parse(file) {return ts.createSourceFile(file,fs.readFileSync(new URL(file,root),'utf8'),ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);}
function attr(node,name){return node.attributes.properties.find(p=>ts.isJsxAttribute(p)&&p.name.getText()===name);}
for(const file of ['app/jobs/new/page.tsx','app/audit/page.tsx','app/clients/page.tsx','app/council/page.tsx']) {
 test(file+' form controls expose an accessible name, including conditional branches',()=>{
  const sf=parse(file),bad=[];let checked=0;
  function visit(n){if(ts.isJsxOpeningElement(n)||ts.isJsxSelfClosingElement(n)){
   if(['input','textarea','select'].includes(n.tagName.getText(sf))){checked++;
    let wrapped=false;for(let p=n.parent;p;p=p.parent)if(ts.isJsxElement(p)&&p.openingElement.tagName.getText(sf)==='label')wrapped=true;
    const id=attr(n,'id');const associated=id&&sf.text.includes('htmlFor='+id.initializer?.getText(sf));
    if(!wrapped&&!associated&&!['aria-label','aria-labelledby','placeholder','title'].some(x=>attr(n,x)))bad.push(sf.getLineAndCharacterOfPosition(n.getStart(sf)).line+1);
   }
  }ts.forEachChild(n,visit)}visit(sf);assert(checked>0);assert.deepEqual(bad,[],'unnamed controls at lines '+bad.join(','));
 });
}
test('heatmap day names have an image role',()=>{const sf=parse('components/memories/PulseHeader.tsx');let named=0,bad=0;function visit(n){if((ts.isJsxOpeningElement(n)||ts.isJsxSelfClosingElement(n))&&n.tagName.getText(sf)==='div'&&attr(n,'aria-label')?.getText(sf).includes('cell.date')){named++;if(attr(n,'role')?.initializer?.getText(sf)!=='"img"')bad++;}ts.forEachChild(n,visit)}visit(sf);assert(named>0);assert.equal(bad,0);});
test('Cadence overflow regions are keyboard reachable',()=>{const sf=parse('components/memories/CadenceSection.tsx');let count=0,bad=0;function visit(n){if((ts.isJsxOpeningElement(n)||ts.isJsxSelfClosingElement(n))&&attr(n,'className')?.getText(sf).includes('overflow-y-auto')){count++;if(attr(n,'tabIndex')?.initializer?.getText(sf)!=='{0}')bad++;}ts.forEachChild(n,visit)}visit(sf);assert(count>0);assert.equal(bad,0);});

for(const file of ['app/architecture/page.tsx','app/guide/page.tsx'])test(file+' scroll landmark is keyboard reachable',()=>{const sf=parse(file);let count=0,bad=0;function visit(n){if(ts.isJsxOpeningElement(n)&&n.tagName.getText(sf)==='main'&&attr(n,'className')?.getText(sf).includes('overflow-y-auto')){count++;if(attr(n,'tabIndex')?.initializer?.getText(sf)!=='{0}')bad++;}ts.forEachChild(n,visit)}visit(sf);assert(count>0);assert.equal(bad,0);});
