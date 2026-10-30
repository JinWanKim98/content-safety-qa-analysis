"""Standard-library reference calculations for the Chapter08 extension.

Existing rating/I01/I02 metrics remain frozen. Nominal consistency includes
Unsure as its own response category; this never converts it to safe/unsafe.
"""
import math
from qa_metrics import divide,expression

CELLS=['tp','fn','uy','fp','tn','un']


def valid_counts(counts):
    counts=[list(x) for x in counts]
    if counts and (not counts[0] or any(len(x)!=len(counts[0]) for x in counts)):
        raise ValueError('Consistent nonempty category dimensions required')
    if any(not isinstance(v,int) or isinstance(v,bool) or v<0 for row in counts for v in row):
        raise ValueError('Nonnegative integer category counts required')
    return counts


def nominal_alpha(counts):
    counts=valid_counts(counts);eligible=[x for x in counts if sum(x)>=2]
    n=sum(map(sum,eligible));categories=[sum(x[k] for x in eligible) for k in range(len(counts[0]))] if counts else []
    disagreement=sum((sum(x)**2-sum(v*v for v in x))/(sum(x)-1) for x in eligible)
    do=divide(disagreement,n)
    de=divide(n*n-sum(v*v for v in categories),n*(n-1)) if n>=2 else None
    value=1-do/de if de is not None and de>0 else None
    return {'value':value,'observed_disagreement':do,'expected_disagreement':de,
      'pairable_values':n,'category_marginals':categories,'eligible_items':len(eligible),
      'excluded_items':len(counts)-len(eligible)}


def coincidence_alpha(counts):
    """Independent explicit coincidence-matrix formula for reconciliation."""
    counts=valid_counts(counts);k=len(counts[0]) if counts else 0
    coincidence=[[0.]*k for _ in range(k)]
    for row in counts:
        n=sum(row)
        if n<2:continue
        for a in range(k):
            for b in range(k):coincidence[a][b]+=row[a]*(row[b]-int(a==b))/(n-1)
    total=sum(map(sum,coincidence));marginals=[sum(row) for row in coincidence]
    off=sum(coincidence[a][b] for a in range(k) for b in range(k) if a!=b)
    expected=sum(marginals[a]*marginals[b] for a in range(k) for b in range(k) if a!=b)
    alpha=1-(total-1)*off/expected if total>=2 and expected>0 else None
    return {'value':alpha,'coincidences':coincidence,'pairable_values':total}


def point_metrics(items,contract):
    counts={cell:sum(x[cell] for x in items) for cell in CELLS}
    counts.update(n=sum(counts.values()),items=len(items))
    c=counts;c.update(d=c['tp']+c['fn']+c['fp']+c['tn'],u=c['uy']+c['un'],matches=c['tp']+c['tn'],
                     ref_unsafe=c['tp']+c['fn']+c['uy'],ref_safe=c['fp']+c['tn']+c['un'])
    values={};details={}
    for m in contract['metrics']:
        num=expression(m['numerator'],c);den=expression(m['denominator'],c)
        values[m['id']]=divide(num,den);details[m['id']]={'numerator':num,'denominator':den,'unit':'rating records'}
    rates={'I01':[],'I02':[],'P01':[]};categories=[]
    for row in items:
        yes=row['tp']+row['fp'];no=row['fn']+row['tn'];u=row['uy']+row['un'];n=yes+no+u;d=yes+no
        categories.append([yes,no,u])
        if n:rates['I01'].append(u/n)
        if d>=2:rates['I02'].append(2*yes*no/(d*(d-1)))
        if n>=2:rates['P01'].append(sum(x*(x-1) for x in [yes,no,u])/(n*(n-1)))
    for key,defined in rates.items():
        values[key]=divide(sum(defined),len(defined))
        details[key]={'numerator':sum(defined),'denominator':len(defined),'unit':'equal-weight defined items',
                      'defined_items':len(defined),'undefined_items':len(items)-len(defined)}
    alpha=nominal_alpha(categories);values['K01']=alpha['value'];details['K01']=alpha
    return {'values':values,'details':details,'counts':counts}


def linear_quantile(values,q):
    if not 0<=q<=1:raise ValueError('Quantile must be between0 and1')
    values=sorted(values)
    if not values:return None
    pos=(len(values)-1)*q;lo=math.floor(pos);hi=math.ceil(pos)
    return values[lo]+(values[hi]-values[lo])*(pos-lo)


def interval_summary(values):
    values=list(values);valid=[v for v in values if v is not None and math.isfinite(v)]
    complete=bool(values) and len(valid)==len(values)
    mean=sum(valid)/len(valid) if valid else None
    return {'low':linear_quantile(valid,.025) if complete else None,
      'high':linear_quantile(valid,.975) if complete else None,
      'finite_replicates':len(valid),'undefined_replicates':len(values)-len(valid),
      'interval_status':'defined' if complete else 'withheld_nonfinite_or_empty',
      'bootstrap_se':math.sqrt(sum((x-mean)**2 for x in valid)/(len(valid)-1)) if len(valid)>1 else None}


def official_nominal_fixture():
    """SectionC of Krippendorff's primary computational note, printed p4.

    Numerical example data, not DICES. Unit12 has a lone value and is excluded.
    """
    raters=[[1,2,3,3,2,1,4,1,2,None,None,None],
      [1,2,3,3,2,2,4,1,2,5,None,3],
      [None,3,3,3,2,3,4,2,2,5,1,None],
      [1,2,3,3,2,4,4,1,2,5,1,None]]
    return [[sum(r[i]==category for r in raters) for category in range(1,6)] for i in range(12)]
