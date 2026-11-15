"""Classify existing research; no new event or causal claims are added."""
from pathlib import Path
from collections import Counter
from statistics import mean
import json
import math
import re
import zipfile
import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.chart import BarChart, Reference

BASE = Path(__file__).resolve().parent
SOURCE = BASE / 'NIFTY_164_Event_Analysis_Researched.xlsx'
OUTPUT = BASE / 'NIFTY_164_Event_Analysis_Bucketed.xlsx'

# Primary classification reflects the leading theme in the existing annotation,
# independent of whether that theme successfully explains the observed return.
TAXONOMY = {
 'TRADE': ('Trade policy & tariffs', 'Tariffs, sanctions linked to trade, trade agreements and negotiations.', 'Tariff hike/pause; India-US talks; US-China trade truce.'),
 'GEO': ('Geopolitics & political events', 'War, military action, diplomatic escalation, elections and political stability.', 'Iran attacks; India-Pakistan ceasefire; election results.'),
 'RATES': ('Interest rates & liquidity', 'Central-bank decisions, forward guidance, rate expectations and liquidity operations.', 'Fed guidance; RBI easing expectations; RBI liquidity infusion.'),
 'MACRO': ('Economic data & growth', 'Inflation, GDP, employment, PMI, trade-balance data and growth forecasts.', 'CPI surprise; payrolls; weak manufacturing PMI.'),
 'CORP': ('Company earnings & business updates', 'Company results, guidance, operating sales, contracts and governance/legal events.', 'TCS results; auto sales; Adani indictment; bank earnings.'),
 'FISCAL': ('Fiscal policy & domestic regulation', 'Budget, taxes, domestic regulatory changes and sovereign-rating developments.', 'GST proposals; STT; RBI project-finance rules; sovereign rating.'),
 'FLOWS': ('Capital flows & currency', 'Foreign/domestic institutional flows, rupee movements and currency-related positioning.', 'Prior-session FII selling; FPI buying; rupee pressure.'),
 'TECH': ('Technology & AI repricing', 'AI competition, investment costs, disruption fears and broad technology valuation repricing.', 'DeepSeek; AI-cost concerns; software selloff. Specific company earnings use Company earnings as primary.'),
 'ENERGY': ('Oil & energy prices', 'Oil-price increases/decreases and energy-supply repricing when the price channel leads the annotation.', 'Oil spike; crude retreat; reserve-release relief. A discrete military/diplomatic event leads under Geopolitics.'),
 'GLOBAL': ('Global sentiment & market positioning', 'Overnight equity/futures moves, broad risk appetite, valuation concerns and rebound/profit-taking setups without a more specific leading event.', 'Asian rally; Wall Street weakness; oversold rebound; year-end positioning. Market cues are proxies, not external causal shocks.'),
}

ASSIGNMENTS = '''
2024-10-31 TECH
2024-11-04 GEO
2024-11-05 GEO
2024-11-06 GEO
2024-11-07 GEO
2024-11-12 MACRO
2024-11-13 MACRO
2024-11-21 CORP
2024-11-22 GLOBAL
2024-11-25 GEO
2024-11-28 RATES
2024-11-29 GLOBAL
2024-12-02 MACRO
2024-12-03 MACRO
2024-12-05 RATES
2024-12-13 MACRO
2024-12-17 MACRO
2024-12-18 FLOWS
2024-12-19 RATES
2024-12-20 RATES
2024-12-23 MACRO
2024-12-30 GLOBAL
2025-01-02 CORP
2025-01-03 GLOBAL
2025-01-06 GLOBAL
2025-01-09 FLOWS
2025-01-13 ENERGY
2025-01-20 GEO
2025-01-21 TRADE
2025-01-22 TRADE
2025-01-27 TECH
2025-01-28 RATES
2025-01-29 TECH
2025-01-31 GLOBAL
2025-02-03 TRADE
2025-02-04 TRADE
2025-02-10 TRADE
2025-02-11 TRADE
2025-02-21 GLOBAL
2025-02-24 GLOBAL
2025-02-28 TRADE
2025-03-05 GLOBAL
2025-03-06 TRADE
2025-03-18 GLOBAL
2025-03-20 RATES
2025-03-21 FLOWS
2025-03-24 TRADE
2025-03-26 MACRO
2025-04-01 TRADE
2025-04-02 TRADE
2025-04-04 TRADE
2025-04-07 TRADE
2025-04-08 GLOBAL
2025-04-09 TRADE
2025-04-11 TRADE
2025-04-15 TRADE
2025-04-17 FLOWS
2025-04-21 CORP
2025-04-23 TRADE
2025-04-25 GEO
2025-04-28 TRADE
2025-05-08 GEO
2025-05-09 GEO
2025-05-12 GEO
2025-05-13 TRADE
2025-05-15 GLOBAL
2025-05-20 RATES
2025-05-21 GLOBAL
2025-05-22 RATES
2025-05-23 MACRO
2025-05-26 TRADE
2025-05-27 GLOBAL
2025-06-03 TRADE
2025-06-05 MACRO
2025-06-06 RATES
2025-06-12 GEO
2025-06-13 GEO
2025-06-16 GEO
2025-06-20 FISCAL
2025-06-23 GEO
2025-06-25 GEO
2025-06-26 GEO
2025-07-11 CORP
2025-07-18 CORP
2025-07-23 TRADE
2025-07-24 FLOWS
2025-07-25 GLOBAL
2025-07-28 CORP
2025-07-29 FLOWS
2025-08-01 TRADE
2025-08-04 GLOBAL
2025-08-08 TRADE
2025-08-11 TRADE
2025-08-13 MACRO
2025-08-18 FISCAL
2025-08-22 RATES
2025-08-26 TRADE
2025-08-28 TRADE
2025-09-01 MACRO
2025-09-03 FISCAL
2025-09-16 TRADE
2025-09-25 GLOBAL
2025-09-26 TRADE
2025-10-01 RATES
2025-10-06 CORP
2025-10-09 TECH
2025-10-15 GLOBAL
2025-10-16 TRADE
2025-10-20 GLOBAL
2025-10-27 TRADE
2025-10-30 RATES
2025-10-31 GLOBAL
2025-11-04 GLOBAL
2025-11-12 GEO
2025-11-19 GLOBAL
2025-11-20 CORP
2025-11-26 MACRO
2025-12-02 MACRO
2025-12-05 RATES
2025-12-08 GEO
2025-12-11 RATES
2025-12-12 GLOBAL
2025-12-16 TECH
2025-12-19 MACRO
2025-12-22 GLOBAL
2025-12-31 GLOBAL
2026-01-02 CORP
2026-01-08 TRADE
2026-01-09 FLOWS
2026-01-20 TRADE
2026-01-22 TRADE
2026-01-23 CORP
2026-01-27 TRADE
2026-01-28 TRADE
2026-02-01 FISCAL
2026-02-02 FISCAL
2026-02-03 TRADE
2026-02-05 TECH
2026-02-09 GEO
2026-02-12 MACRO
2026-02-13 TECH
2026-02-16 GLOBAL
2026-02-19 GEO
2026-02-23 TRADE
2026-02-24 TECH
2026-02-27 TECH
2026-03-02 GEO
2026-03-04 GEO
2026-03-05 ENERGY
2026-03-06 ENERGY
2026-03-09 ENERGY
2026-03-10 GEO
2026-03-11 ENERGY
2026-03-12 GEO
2026-03-13 GEO
2026-03-16 ENERGY
2026-03-17 ENERGY
2026-03-18 GLOBAL
2026-03-19 GEO
2026-03-23 GEO
2026-03-24 GEO
2026-03-25 GEO
2026-03-27 GEO
2026-03-30 GEO
'''
pairs = [line.split() for line in ASSIGNMENTS.strip().splitlines()]
primary = dict(pairs)
assert len(primary) == len(pairs) == 164

PATTERNS = {
 'TRADE': r'tariff|trade[ -](?:deal|talk|war|negotiat|truce|agreement|concern)|trade-policy|sanction|US-China|US-Japan|US-EU|India-US|US-India|India-EU|India-UK',
 'GEO': r'election|politic|inauguration|war supply|war scepticism|Iran|Israel|Pakistan|Sindoor|Hormuz|Kharg|Greenland|ceasefire|geopolit|military|attack|shipping|energy-infrastructure|power-plant|shutdown',
 'RATES': r'\bFed\b|\bRBI\b|Powell|rate|liquidity|yield|central.bank|bond|easing',
 'MACRO': r'inflation|CPI|PCE|GDP|PMI|payroll|jobs|employment|consumer data|consumer confidence|manufacturing|trade deficit|growth (?:upgrade|forecast)|growth concerns',
 'CORP': r'earnings|(?:Wipro|Infosys).*guidance|sales|business.update|contract|Nvidia earnings|TCS Q1|Axis Bank Q1|IndiGo|indictment|Adani shock|Wipro',
 'FISCAL': r'Budget|GST|STT|regulat|project.finance|sovereign.rating|fiscal',
 'FLOWS': r'foreign|FII|FPI|outflow|rupee|currency|dollar|domestic buying|institutional',
 'TECH': r'\bAI\b|DeepSeek|tech(?:nology)?|software|Nvidia|IT-stock',
 'ENERGY': r'\boil\b|crude|energy|reserve.release',
 'GLOBAL': r'equit|stock|market|global|Asian|Wall Street|futures|risk.appetite|rebound|rally|selloff|sell.off|valuation|positioning|profit.taking',
}

# Editorial overrides prevent broad words (e.g. "market") from generating
# secondary tags for every entry; preserve only materially discussed themes.
SECONDARY_OVERRIDES = {
 '2024-11-21': [], '2025-01-28': [], '2025-06-20': [],
 '2025-07-11': ['TRADE','TECH'], '2025-11-20': ['TECH','GLOBAL'],
 '2025-08-18': [], '2026-02-09': ['TRADE','GLOBAL'],
 '2026-03-19': ['ENERGY','RATES'], '2025-04-23': ['RATES','GEO'],
 '2025-05-22': ['FISCAL','GLOBAL'],
}

RATIONALES = {
 'TRADE': 'Trade restrictions, sanctions or negotiation expectations lead this annotation; global moves are a transmission channel.',
 'GEO': 'The leading event is political, diplomatic or military; oil and market reactions are separate transmission channels.',
 'RATES': 'The leading theme concerns monetary conditions, policy expectations or global financing costs.',
 'MACRO': 'An economic release or growth outlook leads the annotation, even when rates are the transmission channel.',
 'CORP': 'A company result, operating update, contract or legal event leads the annotation.',
 'FISCAL': 'Domestic fiscal, tax, regulatory or sovereign-rating policy is the leading theme.',
 'FLOWS': 'Investor flows or currency positioning lead the annotation; these may themselves respond to other events.',
 'TECH': 'Broad technology/AI sentiment or valuation repricing leads; company-specific earnings are classified separately.',
 'ENERGY': 'Oil-price/supply repricing leads the annotation; war is the underlying context where noted.',
 'GLOBAL': 'Broad market sentiment or positioning leads because the annotation does not isolate a stronger specific event.',
}

w = openpyxl.load_workbook(SOURCE)
s = next(sh for sh in w if sh.cell(1,1).value == 'Date' and sh.cell(1,5).value == 'Event trigger(s)')
original = list(s.values)
assert s.max_row == 165 and s.max_column == 14
headers = ['Primary trigger bucket','Secondary trigger buckets','Classification basis','Bucket rationale']
for c,v in enumerate(headers,15): s.cell(1,c,v)
rows=[]
for r in range(2,166):
    date=s.cell(r,1).value.strftime('%Y-%m-%d')
    code=primary[date]
    trigger=s.cell(r,5).value
    status=s.cell(r,9).value
    # These tags describe themes in the title, including opposing/background cues;
    # they do not claim that every mentioned theme is an accepted causal trigger.
    secondary=[k for k,pat in PATTERNS.items() if k!=code and re.search(pat,trigger,re.I)]
    if code!='GLOBAL' and len(secondary)>2 and 'GLOBAL' in secondary:secondary.remove('GLOBAL')
    if date in SECONDARY_OVERRIDES:secondary=SECONDARY_OVERRIDES[date]
    assert code not in secondary
    basis=('Eligible candidate; provisional attribution' if 'eligible candidate' in status else
           'Mixed/opposing cues; direction not explained' if 'mixed/opposing' in status else
           'Background theme; no fresh eligible trigger established')
    for c,v in enumerate([TAXONOMY[code][0],'; '.join(TAXONOMY[k][0] for k in secondary) or 'None recorded',basis,RATIONALES[code]],15):
        s.cell(r,c,v).alignment=Alignment(wrap_text=True,vertical='top')
    rows.append(dict(date=date,code=code,secondary=secondary,change=s.cell(r,3).value,confidence=s.cell(r,8).value,eligible='eligible candidate' in status,basis=basis))
for col,width in {'O':39,'P':64,'Q':53,'R':87}.items():s.column_dimensions[col].width=width
s.auto_filter.ref='A1:R165'

summary=w.create_sheet('Bucket summary',0)
summary.append(['Trigger bucket summary'])
summary.append(['Scope','164 selected sessions with absolute close-to-close moves above 0.5%; classification of existing research, not new web research.'])
summary.append(['Interpretation','One primary bucket per date; mixed/background rows stay in their logical theme. Frequency does not mean proven causality or predictive power.'])
summary.append(['Return statistics','Observed whole-session returns within this selected sample; they are not estimated effects of the trigger, forecast returns, or a trading strategy.'])
summary.append([])
summary.append(['Primary bucket','Dates','Share of dates','Positive closes','Negative closes','Mean daily change','Mean absolute change','High confidence','Medium confidence','Low confidence','Eligible-candidate rows','Mixed/opposing rows','Background-only rows'])
ordered=sorted(TAXONOMY,key=lambda k:sum(r['code']==k for r in rows),reverse=True)
for code in ordered:
    selected=[r for r in rows if r['code']==code]
    conf=Counter(r['confidence'] for r in selected)
    summary.append([TAXONOMY[code][0],len(selected),len(selected)/164,sum(r['change']>0 for r in selected),sum(r['change']<0 for r in selected),mean(r['change'] for r in selected),mean(abs(r['change']) for r in selected),conf['High'],conf['Medium'],conf['Low'],sum(r['eligible'] for r in selected),sum('Mixed/opposing' in r['basis'] for r in selected),sum('Background theme' in r['basis'] for r in selected)])
summary.append(['TOTAL',164,1,sum(r['change']>0 for r in rows),sum(r['change']<0 for r in rows),mean(r['change'] for r in rows),mean(abs(r['change']) for r in rows),sum(r['confidence']=='High' for r in rows),sum(r['confidence']=='Medium' for r in rows),sum(r['confidence']=='Low' for r in rows),sum(r['eligible'] for r in rows),sum('Mixed/opposing' in r['basis'] for r in rows),sum('Background theme' in r['basis'] for r in rows)])
summary.append([])
summary.append(['Any mention (primary or secondary)','Dates mentioning theme','Share of dates'])
for code in ordered:
    n=sum(r['code']==code or code in r['secondary'] for r in rows)
    summary.append([TAXONOMY[code][0],n,n/164])
summary.append(['Overlap note','Any-mention counts overlap and must not be summed. Secondary tags include opposing or background cues and are not separately verified causes.'])
for row in range(7,18):
    for c in [3,6,7]:summary.cell(row,c).number_format='0.00%'
for row in range(20,30):summary.cell(row,3).number_format='0.00%'
summary.column_dimensions['A'].width=43
for col in 'BCDEFGHIJKLM':summary.column_dimensions[col].width=20
for r in range(2,5):
    summary.merge_cells(start_row=r,start_column=2,end_row=r,end_column=13)
    summary.cell(r,2).alignment=Alignment(wrap_text=True,vertical='top')
    summary.row_dimensions[r].height=34
summary.merge_cells(start_row=30,start_column=2,end_row=30,end_column=13)
summary.cell(30,2).alignment=Alignment(wrap_text=True)
summary.row_dimensions[30].height=34
summary.freeze_panes='B7'
summary.auto_filter.ref='A6:M16'
chart=BarChart()
chart.type='bar'
chart.title='Dates by primary trigger bucket'
chart.x_axis.title='Number of selected dates'
chart.add_data(Reference(summary,min_col=2,min_row=6,max_row=16),titles_from_data=True)
chart.set_categories(Reference(summary,min_col=1,min_row=7,max_row=16))
chart.height=13;chart.width=24
chart.legend=None
summary.add_chart(chart,'A33')

guide=w.create_sheet('Bucket definitions')
guide.append(['Bucket','Definition / boundary','Examples'])
for code,(label,definition,examples) in TAXONOMY.items():guide.append([label,definition,examples])
guide.append(['Primary versus secondary','Primary is the leading theme from the existing row. Secondary tags are supporting, opposing or background themes mentioned in its trigger title.','A war-driven oil spike can have Geopolitics as primary and Oil & energy prices as secondary.'])
guide.append(['Evidence stays separate','Research status and causal confidence retain their original meaning. A logical bucket does not upgrade weak evidence or make post-cutoff announcements eligible.','Background-only dates remain explicitly labelled in Classification basis.'])
guide.append(['Overlapping decisions','A macro release that changes Fed expectations uses Economic data as primary. An actual Fed/RBI decision uses Interest rates. Tariff-related sanctions use Trade policy.','Company-specific Nvidia earnings use Company earnings; broad AI disruption uses Technology & AI repricing.'])
guide.append(['Energy versus geopolitics','A discrete attack, ceasefire or diplomatic threat uses Geopolitics. A row led by oil-price repricing uses Oil & energy prices.','Global sentiment is used when no more specific leading event is isolated.'])
guide.append(['Selection limitation','All dates were already selected for >0.5% movement; counts and mean returns cannot estimate the probability that a bucket causes such a move.','No control sessions, event-study adjustment or causal regression was added.'])
for col,width in {'A':43,'B':105,'C':95}.items():guide.column_dimensions[col].width=width
for row in guide:
    guide.row_dimensions[row[0].row].height=60
    for cell in row:cell.alignment=Alignment(wrap_text=True,vertical='top')
guide.freeze_panes='B2'
guide.auto_filter.ref='A1:C11'
for sheet,header_rows in [(s,[1]),(summary,[1,6,19]),(guide,[1])]:
    for row in header_rows:
        for cell in sheet[row]:
            cell.fill=PatternFill('solid',fgColor='17365D')
            cell.font=Font(bold=True,color='FFFFFF')
            cell.alignment=Alignment(wrap_text=True,vertical='top')
w.save(OUTPUT)

# Validate the saved workbook rather than only the in-memory object.
check=openpyxl.load_workbook(OUTPUT)
for old_sheet in openpyxl.load_workbook(SOURCE):
    new_sheet=check[old_sheet.title]
    for row in old_sheet:
        for cell in row:
            x,y=cell.value,new_sheet.cell(cell.row,cell.column).value
            if isinstance(x,(int,float)) and isinstance(y,(int,float)):
                assert math.isclose(x,y,rel_tol=0,abs_tol=1e-12),(old_sheet.title,cell.coordinate)
            else:assert x==y,(old_sheet.title,cell.coordinate)
assert len(rows)==sum(check['Bucket summary'].cell(r,2).value for r in range(7,17))==164
assert all(check[s.title].cell(r,15).value in [v[0] for v in TAXONOMY.values()] for r in range(2,166))
assert all(check['Bucket summary'].cell(r,2).value==sum(check['Bucket summary'].cell(r,c).value for c in [4,5]) for r in range(7,17))
with zipfile.ZipFile(OUTPUT) as z:assert z.testzip() is None
report={'output':str(OUTPUT),'classified_rows':len(rows),'primary_counts':{TAXONOMY[c][0]:sum(r['code']==c for r in rows) for c in ordered},'existing_cell_values_preserved':True,'xlsx_integrity':'passed'}
(BASE/'bucket_validation.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report,indent=2))
