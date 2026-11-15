"""Split existing researched themes into individual classified components."""
from pathlib import Path
from collections import Counter
import ast
import hashlib
import json
import math
import zipfile
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.chart import BarChart, Reference

BASE=Path(__file__).resolve().parent
SOURCE=BASE/'NIFTY_164_Event_Analysis_Bucketed.xlsx'
OUTPUT=BASE/'NIFTY_164_Event_Analysis_Bucketed_Geography.xlsx'
# Read only the taxonomy literal; importing the old builder would regenerate files.
tree=ast.parse((BASE/'bucket_triggers.py').read_text(encoding='utf-8'))
taxonomy=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='TAXONOMY' for t in n.targets))
roles={
 'C':'Candidate / contemporaneous cue',
 'E':'Expectation / scheduled-event anticipation',
 'O':'Opposing or offsetting cue',
 'B':'Older background',
 'H':'Positioning / transmission hypothesis',
 'U':'Cutoff timing not established',
 'X':'After cutoff / excluded from eligible attribution',
}
scope_names={'D':'Domestic','I':'INTL'}
components={}
for line in (BASE/'trigger_components.txt').read_text(encoding='utf-8').splitlines():
    if not line.strip():continue
    date,body=line.split('|',1)
    assert date not in components,date
    components[date]=[]
    for item in body.split(' || '):
        scope,bucket,role,trigger=item.split(':',3)
        assert scope in scope_names and bucket in taxonomy and role in roles,(date,item)
        components[date].append(dict(scope=scope_names[scope],bucket=taxonomy[bucket][0],role=roles[role],role_code=role,trigger=trigger))

# India-index futures are India-specific market cues irrespective of the market
# venue; a cue does not become international just because it trades overnight.
for date,items in components.items():
    for item in items:
        if 'Indian futures' in item['trigger'] or 'GIFT Nifty indication' in item['trigger']:
            item['scope']='Domestic'
        if date=='2025-01-06' and item['trigger']=='Positive premarket indications':item['scope']='Domestic'
        if date=='2026-02-02' and 'STT increase' in item['trigger']:
            item['role_code']='O';item['role']=roles['O']

original_hash=hashlib.sha256(SOURCE.read_bytes()).hexdigest()
w=openpyxl.load_workbook(SOURCE)
s=next(sh for sh in w if sh.cell(1,1).value=='Date' and sh.cell(1,5).value=='Event trigger(s)')
original_cols=s.max_column
dates={s.cell(r,1).value.strftime('%Y-%m-%d') for r in range(2,s.max_row+1)}
assert set(components)==dates and len(dates)==164,(dates-set(components),set(components)-dates)
assert original_cols==18
new_headers=['Individual triggers: bucket and geography','Pre-cutoff candidate/cue geographies','Recorded component count','Candidate / anticipation / opposing cue count','Background / hypothesis / uncertain / excluded count']
for c,name in enumerate(new_headers,19):s.cell(1,c,name)

detail=w.create_sheet('Individual triggers')
detail.append(['Date','NIFTY daily change (date-level)','Trigger ID','Individual trigger with geography','Geography','Trigger bucket','Evidence role','Treatment in trigger summary','Causal confidence (date-level)','Research status (date-level)','Magnitude / surprise (date-level context)','Reasoning (date-level context)','Timing evidence (date-level context)','Sources (date-level; shared across components)'])
all_items=[]
for row in range(2,s.max_row+1):
    date_value=s.cell(row,1).value
    date=date_value.strftime('%Y-%m-%d')
    items=components[date]
    display=[]
    for n,item in enumerate(items,1):
        item=dict(item,date=date,row=row,id=f'{date}-T{n:02d}')
        all_items.append(item)
        labelled=f"{item['trigger']} ({item['scope']})"
        display.append(f"{n}. {labelled} | {item['bucket']} | {item['role']}")
        included=item['role_code'] in 'CEO'
        detail.append([date_value,s.cell(row,3).value,item['id'],labelled,item['scope'],item['bucket'],item['role'],
                       'Candidate / anticipation / opposing cue' if included else 'Context / uncertainty / excluded',
                       *[s.cell(row,c).value for c in [8,9,6,7,12,10]]])
    active=[i for i in items if i['role_code'] in 'CEO']
    scopes='; '.join(x for x in ['Domestic','INTL'] if any(i['scope']==x for i in active)) or 'None established'
    values=['\n'.join(display),scopes,len(items),len(active),len(items)-len(active)]
    for c,v in enumerate(values,19):s.cell(row,c,v).alignment=Alignment(wrap_text=True,vertical='top')
    s.row_dimensions[row].height=max(s.row_dimensions[row].height or 105,min(330,len(items)*45))
s.column_dimensions['S'].width=115
s.column_dimensions['T'].width=32
for col in ['U','V','W']:s.column_dimensions[col].width=22
s.auto_filter.ref=f'A1:W{s.max_row}'

summary=w.create_sheet('Trigger geography summary',0)
summary.append(['Individual-trigger buckets and geography'])
notes=[
 ('Scope','164 dates, split into individual components from existing research. No new web research or new causal verification was performed for this classification.'),
 ('Counting','Each component has one bucket and one geography. A date may appear in several buckets and both geographies. These are component counts; do not add repeated NIFTY returns across components.'),
 ('Candidate/cue counts','Candidate / contemporaneous cue, Expectation / scheduled-event anticipation, and Opposing or offsetting cue are shown separately. None is proof of causality. Older background, hypotheses, unverified timing and after-cutoff events are excluded from candidate/cue totals.'),
 ('Confidence and sources','Confidence, magnitude, reasoning, timing and source links in Individual triggers are inherited date-level context. They are not independently assigned per component. Component role reflects the existing research caveats.'),
 ('Legacy summary','Bucket summary retains the previous one-primary-bucket-per-date analysis. Use this sheet and Individual triggers for the new multi-trigger geography analysis.'),
]
for k,v in notes:summary.append([k,v])
summary.append([])
summary.append(['Bucket','Domestic: candidate/cue','INTL: candidate/cue','All candidate/cues','Candidate / cue','Anticipation','Opposing cue','Older background','Hypothesis','Timing uncertain','Excluded after cutoff','All recorded components','Distinct dates with candidate/cues'])
ordered=sorted([v[0] for v in taxonomy.values()],key=lambda b:sum(i['bucket']==b and i['role_code'] in 'CEO' for i in all_items),reverse=True)
def stat(items,label):
    active=[i for i in items if i['role_code'] in 'CEO']
    counts=Counter(i['role_code'] for i in items)
    return [label,sum(i['scope']=='Domestic' for i in active),sum(i['scope']=='INTL' for i in active),len(active),*[counts[k] for k in 'CEOBHUX'],len(items),len({i['date'] for i in active})]
for bucket in ordered:summary.append(stat([i for i in all_items if i['bucket']==bucket],bucket))
summary.append(stat(all_items,'TOTAL COMPONENTS; distinct-date total deduplicated'))
summary.append([])
summary.append(['Geography','Candidate / cue','Anticipation','Opposing cue','Older background','Hypothesis','Timing uncertain','Excluded after cutoff','All components','Distinct candidate/cue dates'])
for scope in ['Domestic','INTL']:
    items=[i for i in all_items if i['scope']==scope]
    counts=Counter(i['role_code'] for i in items)
    summary.append([scope,*[counts[k] for k in 'CEOBHUX'],len(items),len({i['date'] for i in items if i['role_code'] in 'CEO'})])
summary.append(['Date coverage','Domestic and INTL distinct-date counts overlap; neither sum represents 164 unique dates.'])
for r in range(2,7):
    summary.merge_cells(start_row=r,start_column=2,end_row=r,end_column=13)
    summary.cell(r,2).alignment=Alignment(wrap_text=True,vertical='top')
    summary.row_dimensions[r].height=45
summary.column_dimensions['A'].width=51
for c in 'BCDEFGHIJKLM':summary.column_dimensions[c].width=20
summary.freeze_panes='B9'
summary.auto_filter.ref='A8:M18'
chart=BarChart()
chart.type='bar';chart.grouping='stacked';chart.overlap=100
chart.title='Candidate / anticipation / opposing cues by geography'
chart.x_axis.title='Components, including opposing cues; not proven causes'
chart.add_data(Reference(summary,min_col=2,max_col=3,min_row=8,max_row=18),titles_from_data=True)
chart.set_categories(Reference(summary,min_col=1,min_row=9,max_row=18))
chart.width=29;chart.height=15
summary.add_chart(chart,'A28')

rules=w.create_sheet('Geography and role rules')
rules.append(['Classification','Rule / interpretation','Examples / boundaries'])
rule_rows=[
 ('Domestic','India-specific economic data, domestic policy, Indian-company operating news, Indian-market flows, rupee moves and India-index futures cues.','India CPI/GDP/PMI; RBI; Budget/GST; Maruti/TCS/Axis earnings; FII selling in India; rupee; GIFT Nifty.'),
 ('INTL','Foreign economic/policy/market developments and cross-border trade, sanctions or military/diplomatic developments.','Fed/US CPI; China/Japan data; Wall Street; crude/dollar; India-US trade agreement; India-Pakistan conflict.'),
 ('Cross-border classification','Cross-border trade/conflict and foreign legal actions are INTL even when Indian firms or India are affected. Publisher location alone never decides geography.','US Adani indictment = INTL; TCS results = Domestic; TCS NHS contract = Domestic company business update; India sovereign-rating change = Domestic subject.'),
 ('Market venue','India-index futures are Domestic market cues; foreign index futures are INTL. These are sentiment proxies, not independent external events.','GIFT Nifty / Indian futures = Domestic; Nasdaq futures = INTL.'),
 ('Event versus response','A single development may have a separately recorded policy/news component and market/commodity response. Counts reflect these annotations, not independent shocks.','Iran attack (INTL / Geopolitics) plus oil jump (INTL / Oil & energy) can appear on the same date.'),
 ('Component splitting','Distinct themes/events are split where supported by the existing trigger, reasoning or timing notes. Closely related claims may remain combined when they describe one event.','One date can contain Domestic earnings, Domestic FII flows and INTL equity cues.'),
 ('Date-level context','Existing confidence, magnitude, reasoning, timing and URLs remain at date level. Repetition in Individual triggers is labelled as shared context, not independent component verification.','No trigger-specific causal probability or magnitude has been invented.'),
]
for row in rule_rows:rules.append(row)
for code,label in roles.items():rules.append([label,{'C':'Contemporaneous candidate event or observed market cue in existing research; causality remains provisional.','E':'Anticipated or scheduled event; the later actual outcome is not assumed known.','O':'Cue counter to the observed close direction or offsetting a leading theme. This can coexist with an accepted candidate.','B':'Older information or carryover outside the strict fresh-event window.','H':'Possible positioning or transmission explanation; not a verified external event.','U':'Public availability before cutoff not established; omitted from candidate/cue totals.','X':'After-cutoff or otherwise excluded event, retained visibly to prevent look-ahead attribution.'}[code],'Included in candidate/cue counts' if code in 'CEO' else 'Excluded from candidate/cue counts'])
for c,width in {'A':51,'B':110,'C':100}.items():rules.column_dimensions[c].width=width
for row in rules:
    rules.row_dimensions[row[0].row].height=70
    for cell in row:cell.alignment=Alignment(wrap_text=True,vertical='top')
rules.freeze_panes='B2'
rules.auto_filter.ref=f'A1:C{rules.max_row}'

detail.freeze_panes='E2';detail.auto_filter.ref=detail.dimensions
widths={'A':14,'B':18,'C':24,'D':85,'E':15,'F':43,'G':51,'H':45,'I':24,'J':50,'K':65,'L':95,'M':100,'N':85}
for c,width in widths.items():detail.column_dimensions[c].width=width
for r in range(2,detail.max_row+1):
    detail.cell(r,1).number_format='dd-mmm-yyyy';detail.cell(r,2).number_format='0.000%'
    detail.row_dimensions[r].height=108
    for cell in detail[r]:cell.alignment=Alignment(wrap_text=True,vertical='top')
    scope=detail.cell(r,5)
    scope.fill=PatternFill('solid',fgColor='E2F0D9' if scope.value=='Domestic' else 'DDEBF7')
    if detail.cell(r,7).value==roles['X']:detail.cell(r,7).fill=PatternFill('solid',fgColor='F4CCCC')
    if detail.cell(r,7).value==roles['U']:detail.cell(r,7).fill=PatternFill('solid',fgColor='FFF2CC')
for sheet,header_rows in [(s,[1]),(detail,[1]),(summary,[1,8,21]),(rules,[1])]:
    for r in header_rows:
        for cell in sheet[r]:
            cell.fill=PatternFill('solid',fgColor='17365D');cell.font=Font(color='FFFFFF',bold=True)
            cell.alignment=Alignment(wrap_text=True,vertical='top')
        sheet.row_dimensions[r].height=45
w.save(OUTPUT)

check=openpyxl.load_workbook(OUTPUT)
source=openpyxl.load_workbook(SOURCE)
for old in source:
    new=check[old.title]
    for row in old:
        for cell in row:
            x,y=cell.value,new.cell(cell.row,cell.column).value
            if isinstance(x,(int,float)) and isinstance(y,(int,float)):assert math.isclose(x,y,rel_tol=0,abs_tol=1e-12),(old.title,cell.coordinate)
            else:assert x==y,(old.title,cell.coordinate)
assert check['Individual triggers'].max_row-1==len(all_items)
assert len({i['id'] for i in all_items})==len(all_items)
assert len({i['date'] for i in all_items})==164
assert all(i['scope'] in ['Domestic','INTL'] for i in all_items)
assert all(i['bucket'] in ordered for i in all_items)
assert sum(check[s.title].cell(r,21).value for r in range(2,166))==len(all_items)
assert sum(check['Trigger geography summary'].cell(r,12).value for r in range(9,19))==len(all_items)
assert all(check['Trigger geography summary'].cell(r,4).value==sum(check['Trigger geography summary'].cell(r,c).value for c in [2,3])==sum(check['Trigger geography summary'].cell(r,c).value for c in [5,6,7]) for r in range(9,19))
assert hashlib.sha256(SOURCE.read_bytes()).hexdigest()==original_hash
with zipfile.ZipFile(OUTPUT) as z:assert z.testzip() is None
report={'output':str(OUTPUT),'dates':164,'individual_components':len(all_items),'scope_counts_all_components':dict(Counter(i['scope'] for i in all_items)),'role_counts':dict(Counter(i['role'] for i in all_items)),'candidate_anticipation_opposing_scope_counts':dict(Counter(i['scope'] for i in all_items if i['role_code'] in 'CEO')),'source_workbook_unchanged':True,'existing_values_preserved':True,'validation':'passed'}
(BASE/'geography_validation.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
(BASE/'classified_trigger_components.json').write_text(json.dumps(all_items,indent=2,ensure_ascii=False),encoding='utf-8')
print(json.dumps(report,indent=2))

if __name__=='__main__':pass
