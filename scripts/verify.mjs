import {FileBlob,SpreadsheetFile} from '@oai/artifact-tool';
const p='/Users/moc/workspace/Tender_Evaluation_Report/skills/tender-clearance/tests/fixtures/project-alpha/output/清标底稿.xlsx'; const wb=await SpreadsheetFile.importXlsx(await FileBlob.load(p));
console.log((await wb.inspect({kind:'sheet',include:'id,name'})).ndjson);
console.log((await wb.inspect({kind:'table',range:'项目封面!A1:C8',include:'values,formulas',tableMaxRows:10,tableMaxCols:5})).ndjson);
console.log((await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!',options:{useRegex:true,maxResults:100}})).ndjson);
const b=await wb.render({sheetName:'项目封面',range:'A1:C8',scale:1,format:'png'}); await (await import('node:fs/promises')).writeFile('/tmp/project-cover.png',new Uint8Array(await b.arrayBuffer()));
