"""Standard-library selection/evaluation split and synthetic workflow routing."""
from qa_metrics import divide
SIGNAL_FIELDS={'item_id','source_severity','n','yes','no','unsure'}


def validate_budget(n,budget):
    if not isinstance(budget,int) or isinstance(budget,bool) or not 0<=budget<=n:
        raise ValueError('Budget must be an integer between0 and universe size')


def signal_priority_order(signals):
    """Only supplied selection metadata; no expert final labels or FN allowed."""
    ids=[]
    for row in signals:
        if set(row)!=SIGNAL_FIELDS:raise ValueError('Selection must receive only the six approved signal fields')
        if not isinstance(row['item_id'],str) or not row['item_id']:raise ValueError('String item ID required')
        if any(not isinstance(row[k],int) or isinstance(row[k],bool) or row[k]<0 for k in ['n','yes','no','unsure']):
            raise ValueError('Nonnegative integer signal counts required')
        if row['n']<=0 or row['yes']+row['no']+row['unsure']!=row['n']:raise ValueError('Inconsistent signal counts')
        if row['source_severity'] not in {'Extreme','Moderate','Debatable','Benign'}:raise ValueError('Unknown source severity')
        ids.append(row['item_id'])
    if len(set(ids))!=len(ids):raise ValueError('Duplicate item ID')
    return sorted(range(len(signals)),key=lambda i:(-int(signals[i]['source_severity']=='Extreme'),
        -signals[i]['no']/signals[i]['n'],-signals[i]['unsure']/signals[i]['n'],signals[i]['item_id']))


def choose(strategy,order,permutation,budget):
    n=len(order);validate_budget(n,budget)
    if sorted(order)!=list(range(n)) or sorted(permutation)!=list(range(n)):
        raise ValueError('Order and draw must be unique full-universe permutations')
    if strategy=='random':return list(permutation[:budget])
    if strategy in {'signal_priority','oracle_ceiling'}:return list(order[:budget])
    if strategy=='mixed':
        k=budget//2;fixed=list(order[:k]);excluded=set(fixed)
        return fixed+[i for i in permutation if i not in excluded][:budget-k]
    raise ValueError('Unknown selection strategy')


def inclusion_probabilities(strategy,order,budget):
    n=len(order);validate_budget(n,budget)
    if sorted(order)!=list(range(n)):raise ValueError('Invalid priority order')
    if strategy=='random':return [budget/n]*n if n else []
    if strategy in {'signal_priority','oracle_ceiling'}:
        selected=set(order[:budget]);return [float(i in selected) for i in range(n)]
    if strategy=='mixed':
        k=budget//2;fixed=set(order[:k]);p=(budget-k)/(n-k) if n>k else 0
        return [1. if i in fixed else p for i in range(n)]
    raise ValueError('Unknown strategy')


def evaluate_counts(items,selected,probabilities):
    selected=set(selected)
    if not selected.issubset(range(len(items))):raise ValueError('Selection outside universe')
    if len(probabilities)!=len(items) or any(not 0<=p<=1 for p in probabilities):raise ValueError('Invalid inclusion probabilities')
    if any(probabilities[i]<=0 for i in selected):raise ValueError('Selected item has zero inclusion probability')
    total={k:sum(x[k] for x in items) for k in ['n','fn','fp','unsure']}
    observed={k:sum(items[i][k] for i in selected) for k in total}
    ht_total=sum(items[i]['fn']/probabilities[i] for i in selected) if items and all(p>0 for p in probabilities) else None
    return {'reviewed_items':len(selected),'eligible_items':len(items),'selected_records':observed['n'],
      'eligible_records':total['n'],'selected_fn':observed['fn'],'eligible_fn':total['fn'],
      'selected_fp':observed['fp'],'eligible_fp':total['fp'],'selected_unsure':observed['unsure'],
      'eligible_unsure':total['unsure'],'A01':divide(len(selected),len(items)),
      'A02':divide(observed['fn'],total['fn']),'A03':divide(observed['n'],total['n']),
      'fp_capture':divide(observed['fp'],total['fp']),'unsure_capture':divide(observed['unsure'],total['unsure']),
      'sample_fn_cell_share':divide(observed['fn'],observed['n']),
      'population_fn_cell_share':divide(total['fn'],total['n']),
      'ht_fn_cell_share':divide(ht_total,total['n']) if ht_total is not None else None}


def synthetic_route(reference,review_a,review_b):
    if reference not in {'Yes','No'} or review_a not in {'Yes','No','Unsure'} or review_b not in {'Yes','No','Unsure'}:
        raise ValueError('Invalid synthetic response')
    if 'Unsure' in [review_a,review_b]:return 'clarification_needed'
    if review_a!=review_b:return 'adjudication_needed'
    if review_a!=reference:return 'reference_recheck_needed'
    return 'agreement_recorded_validation_pending'


def synthetic_calibration_cases():
    inputs=[('SYN01','Yes','No','Yes'),('SYN02','No','Yes','Yes'),('SYN03','Yes','Unsure','No'),
            ('SYN04','No','No','No'),('SYN05','Yes','Yes','Yes')]
    return [{'case_id':case,'data_kind':'neutral_synthetic_workflow_only','reference':ref,
      'hypothetical_review_a':a,'hypothetical_review_b':b,'proposed_route':synthetic_route(ref,a,b),
      'new_real_review_performed':False,'adjudication_performed':False,
      'new_label':None,'measured_pre_post_gain':None} for case,ref,a,b in inputs]
