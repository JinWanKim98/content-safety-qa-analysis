"""Versioned reference-relative QA metrics; supports abstention explicitly.

This module does not read raw DICES or delete source records. Chapter 04 uses
labelled synthetic fixtures; full-source model/scorecard remain Chapters 05–06.
"""
from collections import Counter,defaultdict
from dataclasses import dataclass
from pathlib import Path
import json
import sqlite3

ROOT=Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Rating:
    record_id:str
    item_id:str
    rater_id:str
    reference:str
    judgment:str


def validate(rows):
    ids=set();pairs=set();reference={}
    for row in rows:
        if any(not isinstance(x,str) or not x.strip() for x in [row.record_id,row.item_id,row.rater_id]):
            raise ValueError('Nonempty string identifiers required')
        if row.reference not in {'Yes','No'} or row.judgment not in {'Yes','No','Unsure'}:
            raise ValueError('Invalid reference/judgment label; no imputation')
        if row.record_id in ids or (row.item_id,row.rater_id) in pairs:
            raise ValueError('Duplicate record or rater-item key')
        if row.item_id in reference and reference[row.item_id]!=row.reference:
            raise ValueError('Inconsistent reference within item')
        ids.add(row.record_id);pairs.add((row.item_id,row.rater_id));reference[row.item_id]=row.reference


def divide(num,den):
    return num/den if den else None


def expression(text,counts):
    # Contract formulas are explicit additions of count keys, never executable code.
    return sum(counts[name] for name in text.split('+'))


def scorecard(rows):
    rows=list(rows);validate(rows)
    matrix=Counter((r.reference,r.judgment) for r in rows)
    c={name:matrix[pair] for name,pair in {'tp':('Yes','Yes'),'fn':('Yes','No'),
       'uy':('Yes','Unsure'),'fp':('No','Yes'),'tn':('No','No'),'un':('No','Unsure')}.items()}
    c.update(n=len(rows),items=len({r.item_id for r in rows}),raters=len({r.rater_id for r in rows}))
    c.update(d=c['tp']+c['fn']+c['fp']+c['tn'],u=c['uy']+c['un'],matches=c['tp']+c['tn'],
             ref_unsafe=c['tp']+c['fn']+c['uy'],ref_safe=c['fp']+c['tn']+c['un'])
    contract=json.loads((ROOT/'docs/04_qa_metric_contract.json').read_text())
    metrics={}
    for metric in contract['metrics']:
        numerator=expression(metric['numerator'],c);denominator=expression(metric['denominator'],c)
        metrics[metric['id']]={'numerator':numerator,'denominator':denominator,'value':divide(numerator,denominator)}
    bounds={'B01':[divide(c['matches'],c['n']),divide(c['matches']+c['u'],c['n'])],
            'B02':[divide(c['tp'],c['ref_unsafe']),divide(c['tp']+c['uy'],c['ref_unsafe'])]}
    return {'counts':c,'metrics':metrics,'logical_resolution_bounds':bounds}


def item_metrics(rows):
    rows=list(rows);validate(rows);groups=defaultdict(list)
    for row in rows:groups[row.item_id].append(row.judgment)
    result=[]
    for item,answers in sorted(groups.items()):
        counts=Counter(answers);yes,no,u=counts['Yes'],counts['No'],counts['Unsure'];d=yes+no
        result.append({'item_id':item,'n':len(answers),'yes':yes,'no':no,'unsure':u,'d':d,
                       'I01':u/len(answers),'I02':divide(2*yes*no,d*(d-1)) if d>=2 else None,
                       'all_unsure':int(d==0),'insufficient_decisive':int(d<2),
                       'decisive_tie':int(d>0 and yes==no)})
    return result


def macro_item_metrics(rows):
    items=item_metrics(rows);result={'total_items':len(items)}
    for key in ['I01','I02']:
        values=[x[key] for x in items if x[key] is not None]
        result[key]={'value':sum(values)/len(values) if values else None,
                     'defined_items':len(values),'undefined_items':len(items)-len(values)}
    return result


def audit_coverage(rows,reviewed_items):
    rows=list(rows);validate(rows);universe={r.item_id for r in rows};reviewed=set(reviewed_items)
    if not reviewed.issubset(universe):raise ValueError('Reviewed item is outside the eligible universe')
    selected=[r for r in rows if r.item_id in reviewed]
    all_fn=sum(r.reference=='Yes' and r.judgment=='No' for r in rows)
    selected_fn=sum(r.reference=='Yes' and r.judgment=='No' for r in selected)
    return {'A01':divide(len(reviewed),len(universe)),'A02':divide(selected_fn,all_fn),
            'A03':divide(len(selected),len(rows)), 'reviewed_items':len(reviewed),
            'eligible_items':len(universe),'reviewed_records':len(selected),'eligible_records':len(rows),
            'captured_fn':selected_fn,'eligible_fn':all_fn}


def sql_reference(rows):
    # Independent SQL arithmetic. Python validation supplies the same key/domain
    # preconditions; SQL never turns uncertain answers into No.
    rows=list(rows);validate(rows)
    with sqlite3.connect(':memory:') as conn:
        conn.row_factory=sqlite3.Row
        conn.execute('CREATE TABLE ratings(record_id TEXT PRIMARY KEY,item_id TEXT,rater_id TEXT,'
                     'reference TEXT,judgment TEXT,UNIQUE(item_id,rater_id))')
        conn.executemany('INSERT INTO ratings VALUES(?,?,?,?,?)',
                         [(r.record_id,r.item_id,r.rater_id,r.reference,r.judgment) for r in rows])
        counts=dict(conn.execute(read_sql('04_reference_scorecard.sql')).fetchone())
        items=[dict(r) for r in conn.execute(read_sql('04_item_metrics.sql'))]
    return counts,items


def teaching_fixture():
    """Neutral synthetic example: exactly one observation in each of six cells."""
    return [Rating(f'{prefix}{index}',prefix,f'r{index}',gold,answer)
            for prefix,gold in [('unsafe_item','Yes'),('safe_item','No')]
            for index,answer in enumerate(['Yes','No','Unsure'],1)]


def read_sql(name):
    """Read one named SQL section from analysis.sql."""
    from pathlib import Path
    import re
    path = Path(__file__).resolve().parents[1] / 'analysis.sql'
    sections = re.split(r'^-- BEGIN FILE: (.+)$', path.read_text(), flags=re.MULTILINE)
    queries = {sections[i].strip(): sections[i + 1] for i in range(1, len(sections), 2)}
    if name not in queries:
        raise KeyError('Unknown SQL section: ' + name)
    return queries[name]
