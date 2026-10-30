"""Chapter 08: fixed-panel, reference-stratified whole-item bootstrap.

Requires the existing Anaconda NumPy runtime; source/model remain read-only.
"""

from qa_metrics import read_sql
from reproducibility import snapshot,check_snapshot,verify_sources
import ast
import csv
import gzip
import hashlib
import json
import math
import re
import sqlite3
from collections import Counter,defaultdict
from pathlib import Path
from datetime import datetime,timezone
from build_qa_model import PANELS,load_csv,raw_ratings
from quality_audit import Q1_OK
from prepare_sources import file_hash
from qa_metrics import scorecard,macro_item_metrics
from run_quality_baseline import assert_close,write_csv
from reliability_metrics import (CELLS,point_metrics,nominal_alpha,coincidence_alpha,
                                 interval_summary,official_nominal_fixture)
ROOT=Path(__file__).resolve().parents[1]
PLAN=ROOT/'docs/08_reliability_plan.json'
SQL=ROOT/'analysis.sql'


def raw_item_counts(rows):
    groups=defaultdict(list)
    for r in rows:groups[r['item_id']].append(r)
    pairs={'tp':('Yes','Yes'),'fn':('Yes','No'),'uy':('Yes','Unsure'),
           'fp':('No','Yes'),'tn':('No','No'),'un':('No','Unsure')}
    result=[]
    for item,rs in sorted(groups.items()):
        c=Counter((r['safety_gold'],r['Q_overall']) for r in rs)
        result.append({'item_id':item,'reference':rs[0]['safety_gold'],'n':len(rs),'raters':len({r['rater_id'] for r in rs}),
                       **{key:c[pair] for key,pair in pairs.items()}})
    return result


def draw_indices(references,repeats,seed):
    import numpy as np
    if repeats<1 or not references:raise ValueError('Positive repeat count and items required')
    if any(r not in {'Yes','No'} for r in references):raise ValueError('Unknown reference stratum')
    rng=np.random.default_rng(seed);columns=[]
    for ref in ['Yes','No']:
        positions=np.array([i for i,r in enumerate(references) if r==ref],dtype=np.int64)
        if len(positions):columns.append(rng.choice(positions,size=(repeats,len(positions)),replace=True))
    return np.concatenate(columns,axis=1)


def vector_estimates(items,indices,contract,batch_size=250):
    """Reuse whole-item sufficient statistics. Never average rating ratios."""
    import numpy as np
    cells=np.asarray([[r[c] for c in CELLS] for r in items],dtype=np.int64)
    if indices.ndim!=2 or indices.shape[1]!=len(items):raise ValueError('Expected one full item draw per replicate')
    if indices.size and (indices.min()<0 or indices.max()>=len(items)):raise ValueError('Invalid item position')
    yes=cells[:,0]+cells[:,3];no=cells[:,1]+cells[:,4];u=cells[:,2]+cells[:,5];n=yes+no+u;d=yes+no
    categories=np.column_stack([yes,no,u]);pairable=n>=2
    def safe_divide(num,den):
        num,den=np.broadcast_arrays(np.asarray(num,dtype=float),np.asarray(den,dtype=float))
        return np.divide(num,den,out=np.full(num.shape,np.nan),where=den!=0)
    i01=safe_divide(u,n);i02=safe_divide(2*yes*no,d*(d-1));i02[d<2]=np.nan
    p01=safe_divide(np.sum(categories*(categories-1),axis=1),n*(n-1));p01[n<2]=np.nan
    off=safe_divide(n*n-np.sum(categories*categories,axis=1),n-1);off[~pairable]=0
    pair_n=np.where(pairable,n,0);pair_cat=np.where(pairable[:,None],categories,0)
    output={key:np.empty(len(indices)) for key in [m['id'] for m in contract['metrics']]+['I01','I02','P01','K01']}
    for start in range(0,len(indices),batch_size):
        end=min(start+batch_size,len(indices));ix=indices[start:end]
        summed=cells[ix].sum(axis=1);c={key:summed[:,i] for i,key in enumerate(CELLS)}
        c.update(n=summed.sum(axis=1),d=c['tp']+c['fn']+c['fp']+c['tn'],u=c['uy']+c['un'],
          matches=c['tp']+c['tn'],ref_unsafe=c['tp']+c['fn']+c['uy'],ref_safe=c['fp']+c['tn']+c['un'])
        for metric in contract['metrics']:
            num=sum(c[k] for k in metric['numerator'].split('+'));den=sum(c[k] for k in metric['denominator'].split('+'))
            output[metric['id']][start:end]=safe_divide(num,den)
        for key,values in [('I01',i01),('I02',i02),('P01',p01)]:
            selected=values[ix];defined=np.isfinite(selected)
            output[key][start:end]=safe_divide(np.where(defined,selected,0).sum(axis=1),defined.sum(axis=1))
        total_n=pair_n[ix].sum(axis=1);marginal=pair_cat[ix].sum(axis=1)
        do=safe_divide(off[ix].sum(axis=1),total_n)
        de=safe_divide(total_n*total_n-np.sum(marginal*marginal,axis=1),total_n*(total_n-1))
        alpha=1-safe_divide(do,de);alpha[de<=0]=np.nan
        output['K01'][start:end]=alpha
    return output


def check_engine(items,indices,contract,computed):
    """Independent scalar stdlib calculation on declared actual resamples."""
    import numpy as np
    checked=sorted(set([0,1,2,len(indices)//2,len(indices)-1]+list(range(min(20,len(indices))))))
    for i in checked:
        expected=point_metrics([items[int(k)] for k in indices[i]],contract)['values']
        for key,value in expected.items():
            actual=float(computed[key][i])
            if value is None:assert not np.isfinite(actual),(i,key)
            else:assert math.isclose(actual,value,rel_tol=1e-12,abs_tol=1e-12),(i,key,actual,value)
    return len(checked)


def collect_inputs():
    protected=snapshot(['reports/07_diagnostic_results.json','reports/06_baseline_results.json'])
    check_snapshot(protected)
    plan=json.loads(PLAN.read_text());contract=json.loads((ROOT/'docs/04_qa_metric_contract.json').read_text())
    baseline=json.loads((ROOT/'reports/06_baseline_results.json').read_text())
    fields,raw=load_csv(ROOT/'data/raw/dices350/diverse_safety_adversarial_dialog_350.csv')
    readme=(ROOT/'references/google_dices/350_README.md').read_text()
    excluded=set(ast.literal_eval(re.search(r'removed_raters_350 = (\[[^\n]+\])',readme).group(1)))
    independent={'all_released':raw,'published_104_panel':[r for r in raw if r['rater_id'] not in excluded],
                 'q1_comprehension_only':[r for r in raw if r['Q1_whole_conversation_evaluation']==Q1_OK]}
    receipt=json.loads((ROOT/'reports/05_model_validation.json').read_text());model=ROOT/receipt['model_path']
    conn=sqlite3.connect(model.as_uri()+'?mode=ro',uri=True);conn.row_factory=sqlite3.Row
    panels={};all_items={}
    try:
        for name,view in PANELS.items():
            items=[dict(r) for r in conn.execute(read_sql('08_item_panel_counts.sql').replace('PANEL_VIEW',view))]
            assert_close(items,raw_item_counts(independent[name]))
            point=point_metrics(items,contract);raw_score=scorecard(raw_ratings(independent[name]))
            for m in contract['metrics']:
                key=m['id'];assert_close(point['values'][key],raw_score['metrics'][key]['value'])
                assert_close(point['details'][key]['numerator'],raw_score['metrics'][key]['numerator'])
                assert_close(point['details'][key]['denominator'],raw_score['metrics'][key]['denominator'])
                assert_close(point['values'][key],baseline['panels'][name]['metrics'][key]['value'])
            macro=macro_item_metrics(raw_ratings(independent[name]))
            for key in ['I01','I02']:assert_close(point['values'][key],macro[key]['value'])
            categories=[[x['tp']+x['fp'],x['fn']+x['tn'],x['uy']+x['un']] for x in items]
            alpha_matrix=coincidence_alpha(categories)
            assert_close(point['values']['K01'],alpha_matrix['value'])
            panels[name]={'records':len(independent[name]),'items':len(items),
              'raters':len({r['rater_id'] for r in independent[name]}),
              'per_item_records_min':min(x['n'] for x in items),'per_item_records_max':max(x['n'] for x in items),
              'point':point,'alpha_coincidence_check':alpha_matrix}
            all_items[name]=items
    finally:conn.close()
    ref_order=[(x['item_id'],x['reference']) for x in all_items['all_released']]
    for name,items in all_items.items():assert [(x['item_id'],x['reference']) for x in items]==ref_order,name
    fixture=nominal_alpha(official_nominal_fixture())
    assert_close(fixture['value'],.7434210526315789)
    assert fixture['pairable_values']==40 and fixture['excluded_items']==1
    for p,h in protected.items():assert file_hash(p)==h,str(p)
    return plan,contract,panels,all_items,protected,fixture


def run():
    import numpy as np
    plan,contract,panels,all_items,protected,fixture=collect_inputs()
    indices=draw_indices([x['reference'] for x in all_items['all_released']],plan['bootstrap']['repeats'],plan['bootstrap']['seed'])
    references=np.array([x['reference'] for x in all_items['all_released']])
    for ref in ['Yes','No']:assert np.all((references[indices]==ref).sum(axis=1)==(references==ref).sum())
    work=ROOT/'data/work';work.mkdir(exist_ok=True);index_path=work/'08_item_bootstrap_indices.npy';np.save(index_path,indices)
    samples={};engine_checks={};summaries=[];contrasts=[];stability=[]
    half=plan['bootstrap']['repeats']//2
    for name,items in all_items.items():
        samples[name]=vector_estimates(items,indices,contract)
        engine_checks[name]=check_engine(items,indices,contract,samples[name])
        intervals={}
        for key,values in samples[name].items():
            interval=interval_summary(values.tolist());intervals[key]=interval
            short=interval_summary(values[:half].tolist())
            shifts={f'{endpoint}_absolute_shift':abs(interval[endpoint]-short[endpoint]) if interval[endpoint] is not None and short[endpoint] is not None else None for endpoint in ['low','high']}
            stability.append({'kind':'panel','panel':name,'metric':key,'first_replicates':half,'all_replicates':len(indices),**shifts})
            detail=panels[name]['point']['details'][key]
            summaries.append({'panel':name,'metric':key,'point':panels[name]['point']['values'][key],
              'low':interval['low'],'high':interval['high'],'finite_replicates':interval['finite_replicates'],
              'undefined_replicates':interval['undefined_replicates'],'bootstrap_se':interval['bootstrap_se'],
              'numerator':detail.get('numerator'),'denominator':detail.get('denominator'),
              'unit':detail.get('unit','nominal coefficient'),'items':panels[name]['items'],'raters':panels[name]['raters']})
        panels[name]['intervals']=intervals
    for alternative in ['published_104_panel','q1_comprehension_only']:
        for key in samples[alternative]:
            diff=samples[alternative][key]-samples['all_released'][key];interval=interval_summary(diff.tolist())
            a=panels[alternative]['point']['values'][key];b=panels['all_released']['point']['values'][key]
            point=a-b if a is not None and b is not None else None
            contrasts.append({'comparison':alternative+' minus all_released','metric':key,'point_difference':point,**interval})
            short=interval_summary(diff[:half].tolist())
            stability.append({'kind':'paired_difference','panel':alternative,'metric':key,'first_replicates':half,'all_replicates':len(indices),
              **{f'{e}_absolute_shift':abs(interval[e]-short[e]) if interval[e] is not None and short[e] is not None else None for e in ['low','high']}})
    for p,h in protected.items():assert file_hash(p)==h,str(p)
    base_items=all_items['all_released'];first=indices[0]
    result={'chapter':8,'computed_at_utc':datetime.now(timezone.utc).isoformat(),
      'plan_sha256':file_hash(PLAN),'contract_version':contract['version'],'extension_version':plan['extension_version'],
      'numpy_version':np.__version__,'bootstrap':plan['bootstrap'],'panels':panels,'paired_contrasts':contrasts,
      'monte_carlo_endpoint_stability':stability,'official_nominal_fixture_validation':fixture,
      'reconciliation':{'raw_python_sql_item_rows_per_panel':350,'rating_metric_points':42,'item_macro_points':6,
        'independent_nominal_coincidence_checks':3,'actual_resamples_scalar_checked_per_panel':engine_checks},
      'protected_inputs_unchanged':True,'protected_input_sha256':{str(p.relative_to(ROOT)):h for p,h in protected.items()},
      'index_file':str(index_path.relative_to(ROOT)),'index_sha256':file_hash(index_path),
      'first_replicate_example':{'drawn_item_slots':len(first),'unique_original_items':len(set(first.tolist())),
        'reference_yes_slots':int((references[first]=='Yes').sum()),'reference_no_slots':int((references[first]=='No').sum()),
        'retained_records_by_panel':{name:sum(items[int(i)]['n'] for i in first) for name,items in all_items.items()}},
      'inferential_limits':[plan['inference_target'],plan['panel_interpretation'],
        'Pointwise approximate percentile intervals; not simultaneous coverage, guaranteed coverage or confidence that the fixed observed score is unknown.',
        'Unsure-Unsure consistency is not a safe/unsafe decision; alpha is not accuracy and has no worker pass/fail cutoff.',
        'Reference-stratum proportions fixed to175/175 research items; no production prevalence, rater-population generalization, causal improvement or audit simulation.']}
    out=ROOT/'reports';(out/'08_agreement_results.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    write_csv(out/'08_panel_intervals.csv',summaries);write_csv(out/'08_paired_differences.csv',contrasts)
    write_csv(out/'08_endpoint_stability.csv',stability)
    write_csv(out/'08_item_panel_counts.csv',[{'panel':name,**x} for name,items in all_items.items() for x in items])
    # Deterministic gzip timestamp; distributions contain only aggregate values.
    with (out/'08_bootstrap_replicates.csv.gz').open('wb') as raw:
        with gzip.GzipFile(fileobj=raw,mode='wb',mtime=0,filename='') as gz:
            import io
            with io.TextIOWrapper(gz,encoding='utf-8',newline='') as text:
                writer=csv.writer(text);keys=list(samples['all_released']);writer.writerow(['replicate','panel']+keys)
                for name,metrics in samples.items():
                    for i in range(len(indices)):writer.writerow([i+1,name]+[float(metrics[k][i]) if np.isfinite(metrics[k][i]) else '' for k in keys])
    render_report(result)
    print(json.dumps({'status':'complete_conditional_item_resampling','panel_interval_rows':len(summaries),
      'paired_difference_rows':len(contrasts),'replicates_per_panel':len(indices),'points':{name:{k:v['point']['values'][k] for k in ['R03','R09','I02','P01','K01']} for name,v in panels.items()},
      'first_replicate':result['first_replicate_example'],'protected_inputs_unchanged':True},indent=2))
    return result


def render_report(r):
    lines=['# Chapter 08 — Agreement, reliability and conditional uncertainty','',
      'Primary unit: 350 conversations with repeated judgments from the fixed observed panels. Existing metric contract v1.0.0 is unchanged. Supplementary response-consistency extension v1.0.0 is specified separately.',
      '', '## Three different questions','',
      '- R03/R04: agreement with a fallible expert reference, with explicit rating denominators.',
      '- I02: equal-item decisive pair disagreement; report alongside coverage and Unsure.',
      '- P01/K01: consistency across Yes/No/Unsure responses; shared abstention counts as matching response categories, not a correct safety judgment.',
      '', 'Nominal Krippendorff alpha is the single supplementary chance-adjusted coefficient. It supports multiple raters and varying item coverage. Unsure is a distinct nominal response, not missing or an ordinal midpoint. Alpha=1-Do/De uses pairable-only marginals; it is not an accuracy percentage. No worker pass/fail threshold or competing-coefficient ranking is assigned.',
      '', '## Prespecified conditional resampling design','',
      'Resample whole items with replacement within expert-reference Yes/No strata, preserving 175 items in each class. Keep all available ratings of each sampled item and the same sampled positions across all 3 panels. Raters and their observed judgments are conditioned on. This preserves within-item dependence and paired panel comparisons; it does not estimate new-rater variability or remove source-selection bias.',
      '', '10,000 replicates; seed 20261028; NumPy default_rng/PCG64. Recompute rating ratios from summed cell counts, item means over defined items and alpha marginals in every replicate. Pointwise 95% percentile intervals use linear 0.025/0.975 quantiles. These are approximate conditional item-selection variability intervals, not BCa or simultaneous guarantees. A class-stratified exchangeability approximation is assumed; production sampling is not established.',
      '', 'Undefined replicates are counted; intervals are withheld unless every replicate is finite. First 5000 vs full 10000 endpoint differences are recorded as Monte Carlo diagnostics, not proof of interval coverage or a seed-selection rule.',
      '', '## Observed scores and conditional 95% intervals','',
      '| Panel | Records /items /raters | R03 reference agreement | I02 decisive disagreement | P01 three-response agreement | K01 nominal alpha |','|---|---|---:|---:|---:|---:|']
    def display(p,key,scale):
        value=p['point']['values'][key];ci=p['intervals'][key]
        if value is None:return 'Undefined'
        digits=3 if key=='K01' else 2
        return f"{value*scale:.{digits}f} [{ci['low']*scale:.{digits}f}, {ci['high']*scale:.{digits}f}]" if ci['low'] is not None else f'{value*scale:.{digits}f} [withheld]'
    for name,p in r['panels'].items():
        lines.append(f"| {name} | {p['records']:,}/{p['items']}/{p['raters']} | {display(p,'R03',100)}% | {display(p,'I02',100)}% | {display(p,'P01',100)}% | {display(p,'K01',1)} |")
    lines += ['', 'Exact numerators/denominators and all 18 metric intervals per panel are in 08_panel_intervals.csv (54 rows). Nominal alpha details include observed/expected disagreement, pairable values and category marginals. Equal-item metrics include defined/undefined item counts. Q1 has nonuniform item coverage, so rating-ratio and equal-item weighting remain distinct.',
      '', 'Required companion metrics are retained: R01/R02 coverage/Unsure, R03/R04 reference agreement, R06/R09/R11 conditional recall/full-reference detection/unsafe Unsure, and R08/R12/R13 conditional FPR/full-reference opposite judgment/safe Unsure. Full per-metric denominators are not silently replaced by an item-average ratio.',
      '', '## Paired panel sensitivity','',
      '| Comparison (alternative minus all 123) | Metric | Observed difference | Conditional95% interval |','|---|---|---:|---:|']
    for x in r['paired_contrasts']:
        if x['metric'] not in ['R03','I02','K01']:continue
        factor=1 if x['metric']=='K01' else 100;unit='alpha units' if factor==1 else 'percentage points'
        digits=5 if x['metric']=='K01' else 3
        lines.append(f"| {x['comparison']} | {x['metric']} | {x['point_difference']*factor:.{digits}f} {unit} | [{x['low']*factor:.{digits}f}, {x['high']*factor:.{digits}f}] |")
    lines += ['', 'The same item draws are used on both sides. The fixed published exclusion list and Q1 comprehension filter are observed nonrandom subsets. Even an interval excluding 0 does not establish that exclusions, coaching or policy changes cause improvement. Overlap of two standalone intervals is not the comparison procedure.',
      '', '## Computational checks','',
      'Normalized SQL item counts reconcile to fresh raw-CSV Python independently for all 350 items in each panel. All 42 frozen rating points and 6 item macro points match prior results. Alpha matches an independently constructed coincidence matrix for each panel. Krippendorff Section C numeric fixture yields 0.7434210526 with 40 pairable values and one excluded singleton item.',
      '',f"Actual bootstrap vector calculations were checked against independent scalar standard-library calculations on {r['reconciliation']['actual_resamples_scalar_checked_per_panel']} replicate indices per panel. First-draw evidence: {r['first_replicate_example']}.",
      '', 'Saved integer item draws: data/work/08_item_bootstrap_indices.npy; aggregate distributions: reports/08_bootstrap_replicates.csv.gz (30,000 rows across panels). Repeated item positions are intentional resampling weights, not source key duplicates. Earlier source/model/audit/baseline/diagnostic hashes remain unchanged.',
      '', '## Scope and limits','']+['- '+s for s in r['inferential_limits']]+[
      '', '## Methods references','',
      '[Krippendorff, Computing Alpha-Reliability](https://www.asc.upenn.edu/sites/default/files/2021-03/Computing%20Krippendorff%27s%20Alpha-Reliability.pdf): nominal coincidence formula and Section C validation fixture.',
      '', '[SciPy1.11.4 bootstrap documentation](https://docs.scipy.org/doc/scipy-1.11.4/reference/generated/scipy.stats.bootstrap.html): percentile interval and paired resampling concepts. Our explicit indices implement reference-stratified whole-item sampling, rather than an iid-row bootstrap.',
      '', 'Code: src/reliability_metrics.py, src/run_agreement_analysis.py. SQL: sql/08_item_panel_counts.sql. Study notebook: notebooks/08_agreement_reliability.ipynb.',
      '', 'Next: Chapter 09 retrospective audit sampling and calibration simulation. No real employee coaching or operational improvement has been measured.','']
    (ROOT/'reports/08_agreement.md').write_text('\n'.join(lines))


if __name__=='__main__':run()
