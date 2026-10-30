"""Retrospective Chapter 09 audit selection; no new human review or label change."""

from qa_metrics import read_sql
from reproducibility import snapshot,check_snapshot,verify_sources
import ast
import csv
import gzip
import io
import json
import math
import re
import sqlite3
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
from prepare_sources import file_hash
from build_qa_model import PANELS,load_csv,raw_ratings
from quality_audit import Q1_OK
from qa_metrics import audit_coverage
from run_agreement_analysis import raw_item_counts
from run_quality_baseline import assert_close,write_csv
from audit_sampling import (signal_priority_order,choose,inclusion_probabilities,
                            evaluate_counts,synthetic_calibration_cases)
ROOT=Path(__file__).resolve().parents[1]
PLAN=ROOT/'docs/09_audit_simulation_plan.json'
STRATEGIES=['random','signal_priority','mixed','oracle_ceiling']
METRICS=['A01','A02','A03','selected_records','selected_fn','selected_fp','selected_unsure',
         'fp_capture','unsure_capture','sample_fn_cell_share','ht_fn_cell_share']


def collect_inputs():
    protected=snapshot(['reports/08_agreement_results.json'])
    check_snapshot(protected)
    fields,raw=load_csv(ROOT/'data/raw/dices350/diverse_safety_adversarial_dialog_350.csv')
    text=(ROOT/'references/google_dices/350_README.md').read_text()
    removed=set(ast.literal_eval(re.search(r'removed_raters_350 = (\[[^\n]+\])',text).group(1)))
    source={'all_released':raw,'published_104_panel':[r for r in raw if r['rater_id'] not in removed],
            'q1_comprehension_only':[r for r in raw if r['Q1_whole_conversation_evaluation']==Q1_OK]}
    model_receipt=json.loads((ROOT/'reports/05_model_validation.json').read_text());model=ROOT/model_receipt['model_path']
    conn=sqlite3.connect(model.as_uri()+'?mode=ro',uri=True);conn.row_factory=sqlite3.Row
    evaluation={};panel_metadata={}
    try:
        signals=[dict(x) for x in conn.execute(read_sql('09_selection_signals.sql'))]
        metadata={r['item_id']:r['degree_of_harm'] for r in raw}
        for name,view in PANELS.items():
            counts=[dict(x) for x in conn.execute(read_sql('08_item_panel_counts.sql').replace('PANEL_VIEW',view))]
            assert_close(counts,raw_item_counts(source[name]))
            evaluation[name]=[{'item_id':x['item_id'],'reference':x['reference'],'n':x['n'],'fn':x['fn'],'fp':x['fp'],'unsure':x['uy']+x['un']} for x in counts]
            if name=='all_released':
                independent=[{'item_id':x['item_id'],'source_severity':metadata[x['item_id']],'n':x['n'],
                   'yes':x['tp']+x['fp'],'no':x['fn']+x['tn'],'unsure':x['uy']+x['un']} for x in raw_item_counts(raw)]
                assert_close(signals,independent)
            panel_metadata[name]={'items':len(counts),'records':sum(x['n'] for x in counts),
                'raters':len({r['rater_id'] for r in source[name]}),'fn':sum(x['fn'] for x in counts),
                'fp':sum(x['fp'] for x in counts),'unsure':sum(x['uy']+x['un'] for x in counts)}
    finally:conn.close()
    order=[x['item_id'] for x in signals]
    for rows in evaluation.values():assert [x['item_id'] for x in rows]==order
    return signals,evaluation,panel_metadata,source,protected,model


def generate_permutations(n,repeats,seed):
    import numpy as np
    rng=np.random.default_rng(seed)
    return np.array([rng.permutation(n) for _ in range(repeats)],dtype=np.int64)


def selection_arrays(permutations,priority,budget):
    import numpy as np
    n=permutations.shape[1];k=budget//2
    fixed=np.asarray(priority[:k],dtype=np.int64)
    complement=permutations[~np.isin(permutations,fixed)].reshape(len(permutations),n-k)
    return {'random':permutations[:,:budget],
      'signal_priority':np.asarray(priority[:budget],dtype=np.int64)[None,:],
      'mixed':np.concatenate([np.tile(fixed,(len(permutations),1)),complement[:,:budget-k]],axis=1)}


def evaluate_array(items,selected,probabilities):
    import numpy as np
    n=len(items)
    if selected.ndim!=2 or selected.shape[1]>n:raise ValueError('Invalid selected array')
    if selected.size and (selected.min()<0 or selected.max()>=n):raise ValueError('Item outside universe')
    if selected.shape[1]>1 and np.any(np.diff(np.sort(selected,axis=1),axis=1)==0):raise ValueError('Duplicated selected items')
    totals={k:sum(x[k] for x in items) for k in ['n','fn','fp','unsure']}
    obs={k:np.asarray([x[k] for x in items])[selected].sum(axis=1) for k in totals}
    def div(a,b):
        a,b=np.broadcast_arrays(np.asarray(a,dtype=float),np.asarray(b,dtype=float))
        return np.divide(a,b,out=np.full(a.shape,np.nan),where=b!=0)
    pi=np.asarray(probabilities);repeats=len(selected);budget=selected.shape[1]
    ht=div((np.asarray([x['fn'] for x in items])/pi)[selected].sum(axis=1),totals['n']) if np.all(pi>0) else np.full(repeats,np.nan)
    return {'A01':np.full(repeats,budget/n),'A02':div(obs['fn'],totals['fn']),'A03':div(obs['n'],totals['n']),
      'selected_records':obs['n'],'selected_fn':obs['fn'],'selected_fp':obs['fp'],'selected_unsure':obs['unsure'],
      'fp_capture':div(obs['fp'],totals['fp']),'unsure_capture':div(obs['unsure'],totals['unsure']),
      'sample_fn_cell_share':div(obs['fn'],obs['n']),'ht_fn_cell_share':ht}


def outcome_summary(values,stochastic):
    import numpy as np
    valid=np.asarray(values)[np.isfinite(values)]
    complete=len(valid)==len(values) and len(valid)>0
    return {'mean':float(valid.mean()) if complete else None,
      'selection_p025':float(np.quantile(valid,.025)) if complete and stochastic else None,
      'selection_p975':float(np.quantile(valid,.975)) if complete and stochastic else None,
      'finite_runs':len(valid),'undefined_runs':len(values)-len(valid),
      'monte_carlo_se_of_mean':float(valid.std(ddof=1)/math.sqrt(len(valid))) if complete and stochastic and len(valid)>1 else None}


def evaluate_sql(conn,view,ids):
    sql=read_sql('09_evaluate_selected.sql').replace('PANEL_VIEW',view)
    sql=sql.replace('ITEM_PARAMS',','.join('?' for _ in ids) if ids else 'NULL')
    return dict(conn.execute(sql,ids).fetchone())


def simulate():
    import numpy as np
    plan=json.loads(PLAN.read_text());signals,evaluation,panel_metadata,source,protected,model=collect_inputs()
    priority=signal_priority_order(signals);n=len(signals);repeats=plan['randomization']['repeats']
    permutations=generate_permutations(n,repeats,plan['randomization']['seed'])
    assert np.all(np.sort(permutations,axis=1)==np.arange(n))
    work=ROOT/'data/work';work.mkdir(exist_ok=True);perm_path=work/'09_audit_permutations.npy';np.save(perm_path,permutations)
    samples={};summary=[];examples=[];ledger=[];probability_rows=[];checks=[]
    scalar_checks=0;sql_checks=0;frozen_checks=0
    conn=sqlite3.connect(model.as_uri()+'?mode=ro',uri=True);conn.row_factory=sqlite3.Row
    try:
        for budget in plan['budgets']:
            common=selection_arrays(permutations,priority,budget)
            for panel,items in evaluation.items():
                oracle=sorted(range(n),key=lambda i:(-items[i]['fn'],items[i]['item_id']))
                ceiling=sum(items[i]['fn'] for i in oracle[:budget])
                for strategy in STRATEGIES:
                    rank=oracle if strategy=='oracle_ceiling' else priority
                    selected=np.array(oracle[:budget],dtype=np.int64)[None,:] if strategy=='oracle_ceiling' else common[strategy]
                    pi=inclusion_probabilities(strategy,rank,budget)
                    values=evaluate_array(items,selected,pi);samples[(budget,panel,strategy)]=values
                    stochastic=strategy in {'random','mixed'}
                    expected_fn=sum(p*x['fn'] for p,x in zip(pi,items))
                    expected_ht=panel_metadata[panel]['fn']/panel_metadata[panel]['records'] if all(p>0 for p in pi) else None
                    outcomes={k:outcome_summary(v,stochastic) for k,v in values.items()}
                    assert np.all(values['selected_fn']<=ceiling)
                    assert np.allclose(values['A01'],budget/n)
                    fn_mc_se=outcomes['selected_fn']['monte_carlo_se_of_mean']
                    assert abs(outcomes['selected_fn']['mean']-expected_fn)<=6*(fn_mc_se or 0)+1e-10
                    if expected_ht is not None:
                        se=outcomes['ht_fn_cell_share']['monte_carlo_se_of_mean']
                        assert abs(outcomes['ht_fn_cell_share']['mean']-expected_ht)<=6*(se or 0)+1e-12
                    for run in sorted(set([0,min(1,len(selected)-1),len(selected)-1])):
                        expected=evaluate_counts(items,selected[run].tolist(),pi)
                        for key in METRICS:
                            if expected[key] is None:assert not np.isfinite(values[key][run])
                            else:assert math.isclose(float(values[key][run]),expected[key],rel_tol=1e-12,abs_tol=1e-12)
                        scalar_checks+=1
                    first=selected[0].tolist();chosen=choose(strategy,rank,permutations[0].tolist(),budget)
                    assert first==chosen
                    ids=[items[i]['item_id'] for i in first];sql=evaluate_sql(conn,PANELS[panel],ids)
                    expected=evaluate_counts(items,first,pi)
                    for key in ['selected_records','selected_fn','selected_fp','selected_unsure']:
                        assert sql[key]==expected[key]
                    assert sql['selected_items']==budget;sql_checks+=1
                    audit=audit_coverage(raw_ratings(source[panel]),ids)
                    for key in ['A01','A02','A03']:assert_close(expected[key],audit[key])
                    assert audit['captured_fn']==expected['selected_fn'];frozen_checks+=1
                    summary.append({'budget':budget,'panel':panel,'strategy':strategy,'stochastic':stochastic,
                      'runs':len(selected),'population_records':panel_metadata[panel]['records'],
                      'population_fn':panel_metadata[panel]['fn'],'population_fn_cell_share':panel_metadata[panel]['fn']/panel_metadata[panel]['records'],
                      'outcomes':outcomes,'analytic_expected_selected_fn':expected_fn,
                      'analytic_expected_A02':expected_fn/panel_metadata[panel]['fn'],
                      'analytic_expected_ht_fn_cell_share':expected_ht,
                      'first_run_reference_items':dict(Counter(items[i]['reference'] for i in first))})
                    examples.append({'budget':budget,'panel':panel,'strategy':strategy,'run':1,**expected,
                      'selected_reference_yes_items':sum(items[i]['reference']=='Yes' for i in first),
                      'selected_reference_no_items':sum(items[i]['reference']=='No' for i in first)})
                    for position,i in enumerate(first,1):
                        ledger.append({'budget':budget,'panel':panel,'strategy':strategy,'run':1,'selected_position':position,
                          'item_id':items[i]['item_id'],'inclusion_probability':pi[i],
                          'selection_uses_final_reference':strategy=='oracle_ceiling'})
                    rank_positions={i:position+1 for position,i in enumerate(rank)}
                    for i,x in enumerate(items):
                        probability_rows.append({'budget':budget,'panel':panel,'strategy':strategy,'item_id':x['item_id'],
                           'inclusion_probability':pi[i],'ordering_rank':rank_positions[i] if strategy!='random' else None,
                           'selection_uses_final_reference':strategy=='oracle_ceiling'})
                    checks.append({'budget':budget,'panel':panel,'strategy':strategy,
                      'analytic_expected_fn':expected_fn,'simulated_mean_fn':outcomes['selected_fn']['mean'],
                      'mean_fn_monte_carlo_se':fn_mc_se,'within_prespecified_six_mc_se':True,
                      'all_runs_below_panel_specific_oracle_ceiling':True})
    finally:conn.close()
    for p,h in protected.items():assert file_hash(p)==h,str(p)
    r={'chapter':9,'computed_at_utc':datetime.now(timezone.utc).isoformat(),'plan_sha256':file_hash(PLAN),
      'contract_version':'1.0.0','numpy_version':np.__version__,'plan':plan,'panel_metadata':panel_metadata,
      'summary':summary,'analytic_checks':checks,'synthetic_calibration_cases':synthetic_calibration_cases(),
      'permutation_file':str(perm_path.relative_to(ROOT)),'permutation_sha256':file_hash(perm_path),
      'protected_inputs_unchanged':True,'protected_input_sha256':{str(p.relative_to(ROOT)):h for p,h in protected.items()},
      'validation':{'raw_sql_items_per_panel':350,'signal_fields_exclude_final_reference_and_fn':True,
        'independent_scalar_cases':scalar_checks,'sql_example_selections':sql_checks,'frozen_audit_metric_cases':frozen_checks,
        'oracle_recomputed_per_evaluation_panel':True,'selection_sizes_unique_and_exact':True,
        'analytic_random_and_mixed_expectations_checked':True},
      'decision_note':'For this fixed-data FN-capture objective, retain random as the baseline; the prespecified proxy is not validated and its observed failure is retained without retuning. Any new proxy needs independent validation and signal-cost/availability evidence.',
      'new_real_reviews_performed':0,'labels_changed':0,'actual_coaching_performed':False,
      'measured_post_calibration_gain':None,'production_error_rate_estimated':False}
    return r,samples,examples,ledger,probability_rows


def render_report(r):
    plan=r['plan'];main=plan['main_budget'];p=r['panel_metadata']['all_released']
    lines=['# Chapter 09 — Retrospective audit sampling and proposed calibration','',
      'All results are simulations over existing research judgments. No new human review, adjudication, employee coaching, label change or measured post-calibration gain occurred.',
      '', '## Prespecified design and selection information','',
      '350 existing conversations; item budgets 35/70/105 (10/20/30%); main scenario 35. Budget counts unique selected items and assumes equal item-selection cost. All 123 existing judgments per item and expert-supplied source severity are already available; acquisition cost is excluded. No minute/SLA or cold-start deployment claim. Source severity discrepancy remains unresolved.',
      '', '| Strategy | Information used | Selection |','|---|---|---|',
      '| random | Item frame only | Uniform without replacement |',
      '| signal_priority | Source-Extreme flag, No/all share, Unsure share, lexical ID | Fixed priority prefix; no final expert label, FN, FP or expert reason |',
      '| mixed | Same fixed proxy order and random item frame | floor(budget/2) fixed priority items plus uniform complement sample |',
      '| oracle_ceiling | Full final reference/FN evaluation labels | Panel-specific largest FN counts; retrospective ceiling only, never a deployable policy |',
      '', 'Selection signal dictionaries are restricted to six approved fields; evaluator labels/counts are separate. The three non-oracle strategies select identical IDs across panels. Oracle IDs may differ because each panel has its own retrospective upper bound.',
      '', '10,000 common random permutations, seed 20261029, NumPy default_rng/PCG64. Each audit is without replacement. Random uses permutation prefixes; mixed samples its complement. Deterministic strategies have one result, without invented error bars. Ranges below are central 95% selection-distribution ranges on this fixed dataset, not Chapter 08 bootstrap CIs or production intervals.',
      '', '## Main 35-item scenario: all 123 panel','',
      f"Eligible data: {p['items']} items, {p['records']:,} judgments, {p['fn']:,} existing FN judgments. A01=35/350=10%; A03=4,305/43,050=10% for every base-panel sample.",
      '', '| Strategy | Mean existing FN judgments included | A02 mean capture | Central95% selection range for A02 | Mean FP capture | Mean Unsure capture |',
      '|---|---:|---:|---:|---:|---:|']
    for x in r['summary']:
        if x['budget']!=main or x['panel']!='all_released':continue
        o=x['outcomes'];a=o['A02'];interval=f"[{a['selection_p025']*100:.2f}%, {a['selection_p975']*100:.2f}%]" if x['stochastic'] else 'Deterministic: no selection range'
        lines.append(f"| {x['strategy']} | {o['selected_fn']['mean']:.2f} | {a['mean']*100:.2f}% | {interval} | {o['fp_capture']['mean']*100:.2f}% | {o['unsure_capture']['mean']*100:.2f}% |")
    proxy=next(x for x in r['summary'] if x['budget']==main and x['panel']=='all_released' and x['strategy']=='signal_priority')
    lines += ['', f"The prespecified proxy underperformed random for this FN-capture objective. Its 35 selected items contain {proxy['first_run_reference_items'].get('Yes',0)} expert-Yes and {proxy['first_run_reference_items'].get('No',0)} expert-No items. This post-simulation composition check explains the selected set without changing the rule: source-Extreme plus a high No share is not equivalent to an unsafe-reference miss. No favourable rule/seed was substituted after observing the outcome.",'',r['decision_note']]
    lines += ['', 'A01=unique selected items/350. A02=existing FN judgments on selected items/all existing FN judgments. A03=existing selected judgments/all judgments. A02 is judgment-mass coverage, not new confirmed error discoveries, corrected items or quality improvement. FP and Unsure capture are separate objectives.',
      '', 'All budget/panel/strategy results and exact expected counts are in 09_audit_results.json and 09_strategy_summary.csv. Q1 selected-record coverage can vary because its item clusters have different sizes; same selected IDs do not imply the same A03.',
      '', '## Selection bias and inclusion weighting','',
      f"The known full-data FN-cell share is {p['fn']:,}/{p['records']:,}={p['fn']/p['records']*100:.2f}%. It divides FN by all judgments, not unsafe-reference judgments or decisive unsafe-reference judgments. A selected sample's unweighted FN/all-selected-record share is not automatically representative.",
      '', '| Strategy | Mean unweighted sample FN-cell share | Mean HT finite-data FN-cell-share estimate |','|---|---:|---:|']
    for x in r['summary']:
        if x['budget']!=main or x['panel']!='all_released':continue
        o=x['outcomes'];weighted=o['ht_fn_cell_share']['mean'];text=f'{weighted*100:.2f}%' if weighted is not None else 'Withheld: some population inclusion probabilities are zero'
        lines.append(f"| {x['strategy']} | {o['sample_fn_cell_share']['mean']*100:.2f}% | {text} |")
    lines += ['', 'Uniform inclusion probability is budget/350. For mixed 35, the 17 fixed items have pi=1 and each remaining item pi=18/333. Horvitz-Thompson totals sum selected FN_i/pi_i and divide by the known full-panel judgment count. Random/mixed retain positive inclusion probability for every item; deterministic partial coverage does not. Weights target only this fixed finite dataset under the declared design, not production representativeness. Small random samples still vary; no population-parameter CI is inferred from selection ranges.',
      '', '[Statistics Canada: Horvitz-Thompson design-based estimation](https://www150.statcan.gc.ca/n1/pub/12-001-x/2013001/article/11831/section2-eng.htm) supports the positive-inclusion-probability total estimator. Analytical expectations and exact small synthetic enumerations validate the implementation; simulation means are Monte Carlo approximations.',
      '', '## Proposed calibration workflow (not performed)','',
      'Selected candidate -> two independent blinded reviews -> compare evidence -> clarification / independent adjudication / reference recheck -> documented rationale -> separate validation on future or held-out cases. Reviewers see the rubric and content but are proposed to be blinded to existing labels/other answers. This protocol is a proposal, not a completed review.',
      '', 'Neutral synthetic label-only cases demonstrate routing. Unsure routes to clarification; reviewer disagreement to adjudication; reviewer agreement conflicting with the original reference to reference recheck. Matching the reference merely records agreement with validation pending. No case invents a new final label or pre/post quality result.',
      '', 'See docs/09_calibration_workflow.md and reports/09_calibration_simulation.csv. They contain no offensive text, real reviewer identities, employee SLA or fabricated review completion.',
      '', '## Verification and limits','',
      f"Validation: {r['validation']}. All sample sizes are exact/unique; oracle caps hold for each evaluation panel. Raw CSV/SQL item counts reconcile, first-run SQL outcomes and frozen Chapter 04 audit arithmetic agree, and vector outcomes match independent scalar calculations. Protected source/model/earlier analytical outputs remain unchanged.",
      '', '- Selection uses all pre-existing research judgments; signal collection costs and prospective availability are unmeasured.',
      '- Source severity is unvalidated metadata; reference-label-blind is not expert-metadata-free.',
      '- Oracle ceilings use outcome labels and cannot establish operational strategy efficacy.',
      '- Outcomes are retrospective inclusion of existing disagreement records, not confirmed harm, review discoveries or intervention effects.',
      '- No strategy is declared universally optimal; budgets, objectives and proxy assumptions remain explicit.',
      '', 'Code: src/audit_sampling.py,src/run_audit_simulation.py. SQL: sql/09_selection_signals.sql,sql/09_evaluate_selected.sql. Study: notebooks/09_audit_sampling_calibration.ipynb.',
      '', 'Next: Chapter 10 Power BI web QA dashboard.','']
    (ROOT/'reports/09_audit_plan.md').write_text('\n'.join(lines))


def run():
    import numpy as np
    r,samples,examples,ledger,probabilities=simulate();out=ROOT/'reports'
    (out/'09_audit_results.json').write_text(json.dumps(r,indent=2,allow_nan=False)+'\n')
    flat=[]
    for x in r['summary']:
        row={k:x[k] for k in ['budget','panel','strategy','runs','population_records','population_fn','population_fn_cell_share']}
        row.update({f'{metric}_{field}':value for metric,summary in x['outcomes'].items() for field,value in summary.items()})
        row.update({k:x[k] for k in ['analytic_expected_selected_fn','analytic_expected_A02','analytic_expected_ht_fn_cell_share']});flat.append(row)
    for name,rows in [('strategy_summary',flat),('example_scorecards',examples),('selection_ledger',ledger),
                       ('inclusion_probabilities',probabilities),('calibration_simulation',r['synthetic_calibration_cases'])]:
        write_csv(out/f'09_{name}.csv',rows)
    with (out/'09_selection_runs.csv.gz').open('wb') as raw:
        with gzip.GzipFile(fileobj=raw,mode='wb',mtime=0,filename='') as gz:
            with io.TextIOWrapper(gz,encoding='utf-8',newline='') as text:
                writer=csv.writer(text);writer.writerow(['budget','panel','strategy','run']+METRICS)
                for (budget,panel,strategy),values in samples.items():
                    for i in range(len(values['A01'])):
                        writer.writerow([budget,panel,strategy,i+1]+[float(values[k][i]) if np.isfinite(values[k][i]) else '' for k in METRICS])
    render_report(r)
    print(json.dumps({'status':'complete_retrospective_audit_simulation','summary_rows':len(r['summary']),
      'selection_runs':sum(len(v['A01']) for v in samples.values()),'main_budget':r['plan']['main_budget'],
      'primary_main_results':[x for x in r['summary'] if x['budget']==35 and x['panel']=='all_released'],
      'validation':r['validation'],'new_real_reviews':0},indent=2))


if __name__=='__main__':run()
