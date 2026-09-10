import fs from 'node:fs/promises';
import { SpreadsheetFile, Workbook } from '@oai/artifact-tool';
const bundle = JSON.parse(await fs.readFile(process.argv[2], 'utf8'));
const wb = Workbook.create();
const headerFill = '#1F4E79';
const headersFont = {name:'Arial', bold:true, color:'#FFFFFF', size:10};
function plain(v){ if(v===null||v===undefined)return ''; if(typeof v==='object')return JSON.stringify(v); return v; }
function addSheet(name, headers, rows, opts={}){
  const s=wb.worksheets.add(name); s.showGridLines=false;
  const data=[headers,...rows.map(r=>r.map(plain))];
  if(data.length) s.getRangeByIndexes(0,0,data.length,headers.length).values=data;
  const hr=s.getRangeByIndexes(0,0,1,headers.length); hr.format={fill:headerFill,font:headersFont,verticalAlignment:'center',horizontalAlignment:'center',wrapText:true};
  const used=s.getUsedRange(); if(used){used.format.font={name:'Arial',size:10}; used.format.verticalAlignment='center'; used.format.wrapText=true; used.format.borders={insideHorizontal:{style:'thin',color:'#D9E2F3'},bottom:{style:'thin',color:'#A6A6A6'}};}
  s.freezePanes.freezeRows(1);
  if(data.length>1) s.getRangeByIndexes(0,0,data.length,headers.length).format.autofitColumns();
  for(let c=0;c<headers.length;c++) s.getRangeByIndexes(0,c,data.length,1).format.columnWidth=Math.min(Math.max((String(headers[c]).length+8),12),38);
  return s;
}
const inv=bundle.inventory, ent=bundle.entities, mat=bundle.matches, ext=bundle.external, fin=bundle.findings, cfg=bundle.config;
addSheet('说明',['项','内容'],[
 ['项目',`${cfg.project_id||''} ${cfg.project_name||''}`],['投标截止',cfg.bid_deadline||'未提供（禁止有效期结论）'],['运行时间(UTC)',inv.run?.started_at||''],['工具/规则版本',`${inv.run?.tool_version||''} / rules ${inv.run?.rules_version||''}`],['外部查询模式',`${cfg.external_query_mode||''}（渠道：${(cfg.external_query_sources||[]).join(', ')||'无'}）`],['脱敏模式',cfg.redaction_mode||''],['使用边界','本底稿是风险线索与证据整理，不是违法/资格认定；I 级与主体歧义项必须人工复核；空结果不等于无风险。']]);
addSheet('项目封面',['字段','值','证据/说明'],[['招标人/采购人','','[待确认]'],['项目名称',cfg.project_name||'','project.yaml'],['项目编号/编码',cfg.project_id||'','project.yaml'],['投标日期/截止',cfg.bid_deadline||'','project.yaml'],['封面证据','未提供独立封面证据','仅接受投标文件封面第一页']]);
addSheet('供应商对照',['供应商ID','目录名','显示名','声明名称','统一社会信用代码','代码状态','主体确认','备注'],(ent.suppliers||[]).map(s=>[s.supplier_id,s.directory_name,s.display_name,s.declared_name,s.uscc,s.uscc_status,s.confirmation,s.confirmation_note||'']));
addSheet('商务字段',['供应商ID','公司名称','统一社会信用代码','法定代表人','法定代表人证件掩码','授权代表','授权代表证件掩码','证据ID','状态'],(ent.suppliers||[]).map(s=>[s.supplier_id,s.declared_name,s.uscc,'[待确认]','[待确认]','[待确认]','[待确认]',(s.evidence_ids||[]).join('、'),s.confirmation||'']));
addSheet('文件分类',['文档ID','供应商目录','路径','标书子类型','媒体类型','页数','字节数','提取状态','状态说明'],(inv.documents||[]).map(d=>[d.document_id,d.supplier_dir,d.relative_path,d.bid_subtype,d.media_type,d.page_count,d.size_bytes,d.extraction_status,d.status_detail||'']));
const matrix=mat.matrix||mat.matches||[]; addSheet('比对矩阵',matrix.length?Object.keys(matrix[0]):['（无比对记录）'],matrix.map(m=>Object.keys(m).map(k=>m[k])));
const fheaders=['级别','规则','域','预警强度','供应商','事实','状态','采购条款备注','建议','证据','发现ID']; const frows=(fin.findings||[]).map(f=>[f.level,f.rule_id,f.domain,f.evidence_strength,(f.supplier_ids||[]).join('、'),f.fact,f.status,f.procurement_clause_note||'',f.recommendation||'',(f.evidence_ids||[]).join('、'),f.finding_id]); addSheet('风险发现',fheaders,frows);
const qheaders=['查询ID','渠道','供应商','方式','状态','记录数','查询时间','主体键','说明','适配器版本']; addSheet('查询覆盖',qheaders,(ext.queries||[]).map(q=>[q.query_id,q.source_id,q.subject_supplier_id,q.query_mode,q.status,q.record_count,q.queried_at||'',JSON.stringify(q.subject_key||{}),q.detail||'',q.adapter_version||'']));
const records=ext.records||[]; addSheet('外部记录',['渠道','类型','主体确认','供应商','名称','代码','字段','生效起','生效止','证据ID'],records.map(r=>[r.source_id,r.record_kind,r.subject_confirmation,r.supplier_id,r.subject_name,r.subject_uscc,r.fields,r.effective_from,r.effective_to,(r.evidence_ids||[]).join('、')]));
function byKind(kind){return records.filter(r=>r.record_kind===kind);}
addSheet('SRM工商',['供应商','主体确认','记录类型','字段','值','状态','证据ID'],byKind('basic').map(r=>[r.supplier_id,r.subject_confirmation,r.record_kind,r.fields,r.value||'',r.status||'',(r.evidence_ids||[]).join('、')]));
addSheet('SRM股东',['供应商','主体确认','记录类型','字段','值','生效起','生效止','状态','证据ID'],byKind('ownership').map(r=>[r.supplier_id,r.subject_confirmation,r.record_kind,r.fields,r.value||'',r.effective_from||'',r.effective_to||'',r.status||'',(r.evidence_ids||[]).join('、')]));
addSheet('SRM分支机构',['供应商','主体确认','记录类型','字段','值','状态','证据ID'],byKind('branch').map(r=>[r.supplier_id,r.subject_confirmation,r.record_kind,r.fields,r.value||'',r.status||'',(r.evidence_ids||[]).join('、')]));
addSheet('SRM主要人员',['供应商','主体确认','记录类型','字段','值','状态','证据ID'],byKind('person').map(r=>[r.supplier_id,r.subject_confirmation,r.record_kind,r.fields,r.value||'',r.status||'',(r.evidence_ids||[]).join('、')]));
addSheet('政采截图证据',['供应商','查询入口','证据状态','截图/文件证据','查询时间','备注'],(ext.queries||[]).filter(q=>q.source_id==='government_procurement').map(q=>[q.subject_supplier_id,'http://219.143.74.201/search/cr/',q.status,(q.evidence_ids||[]).join('、'),q.queried_at||'',q.detail||'未提供截图；仅保留实际导入证据']));
const ev=[]; for(const fn of ['evidence-content.json','evidence-metadata.json','evidence-external.json','evidence-external-queries.json']){const d=bundle.evidence[fn]||{}; ev.push(...(d.evidence||[]));} addSheet('证据索引',['证据ID','来源类型','文档','字段','原文摘录','定位','方法','强度'],ev.map(e=>[e.evidence_id,e.source_type,e.document_id,e.field,e.raw_value,e.location,e.method,e.strength]));
addSheet('文件清单',['文档ID','路径','分类','子类型','类型','页数','字节','SHA256(前12)','提取状态'],(inv.documents||[]).map(d=>[d.document_id,d.relative_path,d.category,d.bid_subtype,d.media_type,d.page_count,d.size_bytes,String(d.sha256||'').slice(0,12),d.extraction_status]));
addSheet('人工复核',fheaders,frows.filter((_,i)=>{const f=fin.findings[i];return f?.human_review_required||(fin.human_review_queue||[]).includes(f?.finding_id);}));
const out=process.argv[3]; await fs.mkdir(new URL('.',`file://${out}`).pathname,{recursive:true}); const blob=await SpreadsheetFile.exportXlsx(wb); await blob.save(out);
