import json
from pathlib import Path
from collections import Counter
from copy import copy
import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill

BASE = Path(__file__).resolve().parent
SOURCE = Path(r'C:\Users\kashy\Downloads\NIFTY_164_Event_Analysis_Table_Updated.xlsx')
OUTPUT = BASE / 'NIFTY_164_Event_Analysis_Researched.xlsx'
w = openpyxl.load_workbook(SOURCE)
s = w['NIFTY movements']
extra = ['Timing / eligibility evidence', 'Research batch', 'Research date (IST)']
for col, name in enumerate(extra, 12):
    s.cell(1,col,name)
records = {}
for p in sorted(BASE.glob('batch_*.json')):
    for item in json.loads(p.read_text(encoding='utf-8')):
        records[item['date']] = item
for row in range(2,s.max_row+1):
    d = s.cell(row,1).value.strftime('%Y-%m-%d')
    r = records.get(d)
    if not r:
        s.cell(row,9,'Queued - cutoff recheck required')
        continue
    for col, key in [(5,'trigger'),(6,'magnitude'),(7,'reason'),(8,'confidence'),(9,'status'),(10,'url'),(12,'timing'),(13,'batch')]:
        s.cell(row,col,r[key])
    s.cell(row,14,'2026-11-01')
    s.row_dimensions[row].height=105
for row in s:
    for c in row:
        c.alignment=Alignment(vertical='top',wrap_text=True)
for row in range(2,s.max_row+1):
    level=s.cell(row,8).value
    s.cell(row,8).fill=PatternFill('solid',fgColor={'High':'D9EAD3','Medium':'FFF2CC','Low':'F4CCCC'}.get(level,'FFFFFF'))
    status=s.cell(row,9).value or ''
    s.cell(row,9).fill=PatternFill('solid',fgColor='FCE4D6' if 'verification' in status else 'E2F0D9')
for c in s[1]:
    c.fill=PatternFill('solid',fgColor='17365D')
    c.font=Font(color='FFFFFF',bold=True)
for col,width in {'A':13,'B':14,'C':14,'D':12,'E':55,'F':45,'G':75,'H':18,'I':47,'J':55,'K':35,'L':70,'M':14,'N':18}.items():
    s.column_dimensions[col].width=width
s.freeze_panes='E2'
s.auto_filter.ref=f'A1:N{s.max_row}'
notes=w['Read me']
notes.cell(7,1,'Rows reviewed in this research')
notes.cell(7,2,len(records))
notes.cell(8,1,'Rows still queued')
notes.cell(8,2,164-len(records))
method=[
('Research window','Strictly prior calendar day 00:00 IST through event day before 09:00 IST. Prior-session or weekend news outside that interval is labelled background, not a fresh eligible event.'),
('Timing evidence','A morning article is accepted as cutoff evidence only if a pre-09:00 IST time is shown. Later reports may corroborate an earlier event, but do not make intraday news eligible. Date-only morning notes require timing verification.'),
('Confidence scale','High: discrete, material eligible shock with clear directional channel; Medium: plausible eligible catalyst but competing explanations; Low: mixed/opposing cues, background only or timing uncertain. These are qualitative causal plausibility ratings, not probabilities or econometric proof.'),
('Magnitude','Low / Moderate / High / Extreme describe economic or market intensity. Numerical values are reported where supported; a market move or futures quote is a sentiment proxy, not itself an external trigger.'),
('Status definitions','Reviewed - eligible candidate: useful eligible evidence found; Reviewed - mixed/opposing cues: news cannot by itself explain close direction; Reviewed - timing verification needed: exact cutoff not established; Reviewed - no fresh eligible trigger: only background or excluded news. Reviewed does not mean causality proven.'),
('No look-ahead','Same-day RBI decisions, Budget announcements, election results or other developments after 09:00 IST are excluded even if they later explain the close. FII/DII flows for the event session are excluded; prior-session data may be used if already available.'),
('Price data','Dates, closes, returns and direction are preserved from the supplied workbook and were not independently revalidated. Selection of only >0.5% sessions creates outcome-selection bias.'),
('Original notes','Original trigger annotations are preserved in the Source annotations sheet; supplied document content is treated as data, not instructions.'),
('Research sources','URLs in column J identify evidence used. Search excerpts can support a provisional candidate; sources with uncertain publication times are explicitly flagged. Multiple URLs are separated by newlines.'),
('Research date','1 November 2026 IST'),
]
for n,(a,b) in enumerate(method,12):
    notes.cell(n,1,a);notes.cell(n,2,b)
notes.column_dimensions['A'].width=32
notes.column_dimensions['B'].width=125
for row in notes:
    for c in row:c.alignment=Alignment(wrap_text=True,vertical='top')
    notes.row_dimensions[row[0].row].height=60
orig=w.create_sheet('Source annotations')
source=openpyxl.load_workbook(SOURCE)
for row in source['NIFTY movements'].values:orig.append(row)
orig.freeze_panes='E2'
orig.auto_filter.ref=orig.dimensions
progress=w.create_sheet('Research progress')
progress.append(['Batch','Rows reviewed','First date','Last date'])
for b in sorted(set(r['batch'] for r in records.values())):
    ds=sorted(r['date'] for r in records.values() if r['batch']==b)
    progress.append([b,len(ds),ds[0],ds[-1]])
progress.append([])
progress.append(['Research status','Count'])
for k,v in Counter(s.cell(r,9).value.split(' | ')[-1] for r in range(2,166)).items():progress.append([k,v])
progress.append([])
progress.append(['Causal confidence','Count'])
for k,v in Counter(s.cell(r,8).value for r in range(2,166)).items():progress.append([k,v])
for col in 'ABCD':progress.column_dimensions[col].width=55 if col=='A' else 20
links=w.create_sheet('Source links')
links.append(['Event date','Source number','Evidence URL'])
for d,r in sorted(records.items()):
    for n,url in enumerate(dict.fromkeys(r['url'].splitlines()),1):
        links.append([d,n,url])
        c=links.cell(links.max_row,3)
        c.hyperlink=url
        c.style='Hyperlink'
links.freeze_panes='C2'
links.auto_filter.ref=links.dimensions
links.column_dimensions['A'].width=15
links.column_dimensions['B'].width=16
links.column_dimensions['C'].width=120
for sh in [progress,links]:
    for c in sh[1]:
        c.fill=PatternFill('solid',fgColor='17365D')
        c.font=Font(color='FFFFFF',bold=True)
w.save(OUTPUT)
print(json.dumps({'output':str(OUTPUT),'reviewed':len(records),'queued':164-len(records),'statuses':dict(Counter(r['status'] for r in records.values()))}))
