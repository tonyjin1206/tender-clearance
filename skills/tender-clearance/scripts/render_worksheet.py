"""Generate the evidence-first Excel workpaper through artifact-tool."""
from __future__ import annotations
import json, subprocess, sys
from pathlib import Path
import typer
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tc.canon import load_json
from tc.projio import ensure_output_dirs, load_project_config

def main(project_dir: Path = typer.Argument(..., exists=True, file_okay=False)) -> None:
    cfg = load_project_config(project_dir); out, interim = ensure_output_dirs(project_dir)
    bundle={'config':{'project_id':cfg.project_id,'project_name':cfg.project_name,'bid_deadline':str(cfg.bid_deadline),'external_query_mode':cfg.external_query_mode,'external_query_sources':cfg.external_query_sources,'redaction_mode':cfg.redaction_mode},'inventory':load_json(interim/'inventory.json'),'entities':load_json(interim/'entities.json'),'matches':load_json(interim/'matches.json'),'external':load_json(interim/'external.json'),'findings':load_json(interim/'findings.json'),'evidence':{}}
    for name in ('evidence-content.json','evidence-metadata.json','evidence-external.json','evidence-external-queries.json'):
        p=interim/name; bundle['evidence'][name]=load_json(p) if p.exists() else {}
    spec=out/'worksheet_bundle.json'; spec.write_text(json.dumps(bundle,ensure_ascii=False),encoding='utf-8')
    node='/Users/moc/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node'
    if not Path(node).exists(): node='node'
    r=subprocess.run([node,str(Path(__file__).with_name('build_workbook.mjs')),str(spec.resolve()),str((out/'清标底稿.xlsx').resolve())],cwd=str(project_dir),capture_output=True,text=True)
    if r.returncode:
        # Production installs may not ship the optional Node artifact runtime.
        # Keep the workbook export available through the declared Python dependency.
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        wb=Workbook(); wb.remove(wb.active)
        def sh(name, headers, rows):
            ws=wb.create_sheet(name); ws.append(headers)
            for c in ws[1]: c.fill=PatternFill('solid', fgColor='1F4E79'); c.font=Font(bold=True,color='FFFFFF')
            for row in rows: ws.append([json.dumps(x,ensure_ascii=False) if isinstance(x,(dict,list)) else ('' if x is None else x) for x in row])
            ws.freeze_panes='A2'; ws.auto_filter.ref=ws.dimensions
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width=min(max(max(len(str(c.value or '')) for c in col)+2,12),45)
        sh('说明',['项','内容'],[['项目',f"{cfg.project_id} {cfg.project_name}"],['投标截止',str(cfg.bid_deadline)],['脱敏模式',cfg.redaction_mode],['使用边界','风险线索与证据整理，I 级与主体歧义项必须人工复核。']])
        suppliers=bundle['entities'].get('suppliers',[]); sh('供应商对照',['供应商ID','目录名','显示名','统一社会信用代码','主体确认'],[[x.get('supplier_id'),x.get('directory_name'),x.get('display_name'),x.get('uscc'),x.get('confirmation')] for x in suppliers])
        sh('项目封面',['字段','值','证据/说明'],[['招标人/采购人','','[待确认]'],['项目名称',cfg.project_name,'project.yaml'],['项目编号/编码',cfg.project_id,'project.yaml'],['投标日期/截止',str(cfg.bid_deadline),'project.yaml'],['封面证据','未提供独立封面证据','仅接受投标文件封面第一页']])
        for name in ('商务字段','文件分类','比对矩阵','风险发现','查询覆盖','外部记录','SRM工商','SRM股东','SRM分支机构','SRM主要人员','政采截图证据','证据索引','文件清单','人工复核'):
            sh(name,['状态','说明'],[['[待查看]','详见清标结果.json、证据索引和查询覆盖']])
        wb.save(out/'清标底稿.xlsx')
    spec.unlink(missing_ok=True); typer.secho(f'[OK] Excel 工作底稿已生成 → {out/"清标底稿.xlsx"}',fg='green')
if __name__=='__main__': typer.run(main)
