"""Explicit, reviewable semantic correction of recorded 21-step candidate.
No production normalizer heuristics; original raw and source state remain intact.
"""
import copy,json,hashlib
from pathlib import Path
R=Path('/tmp/biomind-planner-audit-20260908'); O=Path(__file__).parent/'fixtures'
raw=json.loads((R/'evidence/context_trace/planner_initial-0006-cbffb9ef7a7f/response.json').read_text())['content']
c=json.loads((R/'replay/01_parsed.json').read_text());c['outcome_type']='SUBMIT_PLAN_FOR_REVIEW'
original=copy.deepcopy(c)
c['metadata']['resource_contract_version']=2
for a in c['proposed_artifacts']:a.pop('resource_binding',None)
added={
 'plan.reference_indexes':('reference_indexes','directory','An immutable bundle of reference indexes created in the managed output area, without modifying input.reference_fasta'),
 'plan.s1_sorted_bam':('s1_sorted_bam','bam','S1 coordinate-sorted BAM with read groups'),
 'plan.s2_sorted_bam':('s2_sorted_bam','bam','S2 coordinate-sorted BAM with read groups'),
 'plan.s1_markdup_bam':('s1_markdup_bam','bam','S1 duplicate-marked BAM'),
 'plan.s2_markdup_bam':('s2_markdup_bam','bam','S2 duplicate-marked BAM'),
 'plan.s1_bam_index':('s1_bam_index','bai','S1 duplicate-marked BAM index'),
 'plan.s2_bam_index':('s2_bam_index','bai','S2 duplicate-marked BAM index'),
 'plan.s1_duplicate_metrics':('s1_duplicate_metrics','tsv','S1 duplicate marking metrics'),
 'plan.s2_duplicate_metrics':('s2_duplicate_metrics','tsv','S2 duplicate marking metrics'),
 'plan.s1_gvcf':('s1_gvcf','vcf_gz','S1 GVCF'),
 'plan.s2_gvcf':('s2_gvcf','vcf_gz','S2 GVCF'),
 'plan.s1_gvcf_index':('s1_gvcf_index','tbi','S1 GVCF index'),
 'plan.s2_gvcf_index':('s2_gvcf_index','tbi','S2 GVCF index'),
 'plan.raw_cohort_vcf':('raw_cohort_vcf','vcf_gz','Joint-genotyped cohort VCF containing S1 and S2'),
 'plan.snp_stats':('snp_stats','txt','bcftools statistics for filtered SNP VCF'),
 'plan.indel_stats':('indel_stats','txt','bcftools statistics for filtered InDel VCF'),
}
for ref,(role,fmt,desc) in added.items():
 c['proposed_artifacts'].append({'local_ref':ref,'semantic_view':{'role':role,'format':fmt,'description':desc},'artifact_contract':{'object_kind':'directory' if fmt=='directory' else 'file','lifecycle':'plan_local','materialization_policy_id':'managed_intermediate_v1'}})
changes={
 'prepare_reference':(['input.reference_fasta'],['plan.reference_indexes']),
 'map_sample_s1':(['input.reference_fasta','plan.reference_indexes','plan.sample_table'],['plan.s1_sorted_bam']),
 'map_sample_s2':(['input.reference_fasta','plan.reference_indexes','plan.sample_table'],['plan.s2_sorted_bam']),
 'mark_duplicates_s1':(['plan.s1_sorted_bam'],['plan.s1_markdup_bam','plan.s1_bam_index','plan.s1_duplicate_metrics']),
 'mark_duplicates_s2':(['plan.s2_sorted_bam'],['plan.s2_markdup_bam','plan.s2_bam_index','plan.s2_duplicate_metrics']),
 'qc_mapped_bams':(['plan.s1_markdup_bam','plan.s2_markdup_bam','plan.s1_bam_index','plan.s2_bam_index'],[]),
 'create_bam_list':(['plan.s1_markdup_bam','plan.s2_markdup_bam'],['plan.bam_list']),
 'haplotypecaller_s1':(['input.reference_fasta','plan.reference_indexes','plan.bam_list','plan.s1_markdup_bam','plan.s1_bam_index'],['plan.s1_gvcf','plan.s1_gvcf_index']),
 'haplotypecaller_s2':(['input.reference_fasta','plan.reference_indexes','plan.bam_list','plan.s2_markdup_bam','plan.s2_bam_index'],['plan.s2_gvcf','plan.s2_gvcf_index']),
 'combine_gvcfs':(['input.reference_fasta','plan.s1_gvcf','plan.s2_gvcf','plan.s1_gvcf_index','plan.s2_gvcf_index'],['plan.combined_gvcf']),
 'joint_genotyping':(['input.reference_fasta','plan.combined_gvcf'],['plan.raw_cohort_vcf']),
 'split_snp_indel':(['input.reference_fasta','plan.raw_cohort_vcf'],['plan.raw_snp_vcf','plan.raw_indel_vcf']),
 'bcftools_stats':(['plan.filtered_snp_vcf','plan.filtered_indel_vcf'],['plan.snp_stats','plan.indel_stats']),
 'write_report':(['plan.snp_stats','plan.indel_stats'],['output.report']),
}
for step in c['steps']:
 step['workspace_mutations']=[];step['explicit_side_effects']=[]
 if step['step_id'] in changes:step['inputs'],step['outputs']=changes[step['step_id']]
 # The declared operation is a mechanical contract operation; the original
 # scientific intent remains explicit in the unchanged title and criteria.
 if step['purpose']=='validation':
  step['execution_capability_id']=step['execution_capability_id'].removesuffix('.execute')+'.inspect'
  step['operation']='inspect';step['produces_observations']=['validation_observation']
 else:
  step['operation']='produce_files';step['produces_observations']=[]
assert len(c['steps'])==21
assert [s['step_id'] for s in c['steps']]==[s['step_id'] for s in original['steps']]
assert all(s['dependencies']==o['dependencies'] for s,o in zip(c['steps'],original['steps']))
(O/'original_response.txt').write_text(raw)
(O/'original_candidate.json').write_text(json.dumps(original,ensure_ascii=False,indent=2))
(O/'corrected_candidate.json').write_text(json.dumps(c,ensure_ascii=False,indent=2))
(O/'recorded_state.json').write_text((R/'evidence/plan_state.json').read_text())
receipt={'source_session':'session_1788855155','historical_runtime_source_revision':'NOT_PERSISTED','raw_sha256':hashlib.sha256(raw.encode()).hexdigest(),'correction':'Explicit test-authored declaration; not normalizer auto-repair','retained':{'steps':21,'step_ids':True,'scientific_success_criteria':True,'dependencies':True},'known_input_boundary':'This PLAN fixture retains recorded paired_fastq defect; INPUT-FIX acceptance is independent','added_resources':added,'explicit_resource_flow_changes':changes}
(O/'correction_receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2))
