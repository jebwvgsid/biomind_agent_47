import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('review',Path(__file__).resolve().parents[1]/'scripts/collect-maintenance-review.py')
review=importlib.util.module_from_spec(spec);spec.loader.exec_module(review)


class ReviewTests(unittest.TestCase):
    def audit(self):
        return {'reports':[{'slurmJobs':[{'jobId':'123','state':'RUNNING','nodeList':'c[01-02]'}]},
                           {'slurmJobs':[{'jobId':'123','state':'RUNNING','nodeList':'c[01-02]'}]}]}

    def test_job_paths_selected_without_environment_tokens_or_job_name(self):
        with tempfile.TemporaryDirectory() as directory:
            script=Path(directory)/'build job.sbatch';script.touch()
            fields=review.slurm_fields('JobId=123 JobName=PRIVATE_NAME UserId=test(42) JobState=RUNNING '
                'NodeList=c01 BatchHost=c01 Command='+str(script)+' WorkDir=/tmp/my project Env=PRIVATE_TOKEN')
        self.assertEqual(fields['Command'],str(script))
        self.assertEqual(fields['WorkDir'],'/tmp/my project')
        self.assertNotIn('PRIVATE',str(fields))

    def test_same_job_deduplicated_and_all_assigned_hosts_discovered(self):
        with patch.object(review,'control',side_effect=['JobId=123 NodeList=c[01-02]','c01\nc02\n']) as command:
            result=review.job_review(self.audit(),'/usr/bin/scontrol')
        self.assertEqual(result['hosts'],['c01','c02'])
        self.assertEqual(len(result['jobs']),1)
        self.assertEqual(result['failures'],[])
        self.assertEqual(command.call_count,2)

    def test_job_metadata_failure_keeps_node_discovery_and_not_vacancy(self):
        with patch.object(review,'control',side_effect=[OSError('PRIVATE_FAILURE'),'c01\nc02\n']):
            result=review.job_review(self.audit(),'/usr/bin/scontrol')
        self.assertEqual(result['hosts'],['c01','c02'])
        self.assertEqual(result['jobs'][0]['metadataStatus'],'BLOCKED')
        self.assertNotIn('PRIVATE_FAILURE',str(result))

    def test_invalid_host_or_wrong_job_cannot_be_silently_accepted(self):
        with patch.object(review,'control',side_effect=['JobId=999','-oProxyCommand=PRIVATE\n']):
            result=review.job_review(self.audit(),'/usr/bin/scontrol')
        self.assertEqual(result['hosts'],[])
        self.assertEqual(len(result['failures']),2)
        self.assertNotIn('PRIVATE',str(result))

    def test_discovery_never_approves_plan_or_modifies_original(self):
        original={'hosts':[{'name':'ln01','ssh':'ln01'}],'hostInventoryConfirmed':True,
                  'entryInventoryConfirmed':True,'maintenanceApproved':True,'_sha256':'old'}
        plan=review.review_plan(original,['ln01','c01'])
        self.assertEqual([h['name'] for h in plan['hosts']],['ln01','c01'])
        self.assertFalse(plan['hostInventoryConfirmed'])
        self.assertFalse(plan['entryInventoryConfirmed'])
        self.assertFalse(plan['maintenanceApproved'])
        self.assertTrue(original['maintenanceApproved'])
        self.assertEqual(len(original['hosts']),1)
        self.assertNotIn('_sha256',plan)

    def test_unreachable_host_has_unknown_counts_and_incomplete_coverage(self):
        row={'host':'c142','status':'BLOCKED','stage':'ssh','code':'ADOPTION_SSH_HOST_KEY_UNVERIFIED'}
        summary=review.host_summary(row)
        for key in ('legacyReferences','unknownProcesses','unreadableProcesses','activeWriters'):
            self.assertIsNone(summary[key])
        coverage=review.job_host_coverage(['c142'],['ln01','c142'],{'reports':[row]})
        self.assertEqual(coverage['jobHostsAttempted'],['c142'])
        self.assertEqual(coverage['jobHostsNotDeclared'],[])
        self.assertEqual(coverage['jobHostsNotYetAudited'],['c142'])
        self.assertFalse(coverage['jobHostCoverageComplete'])

    def test_full_busy_inventory_is_covered_without_claiming_idle(self):
        row={'host':'c01','status':'BUSY','checks':{k:'PASS' for k in ('hostNamespace','processes','writers','scheduler')},
             'legacyProcesses':[],'unreviewedProcesses':[],'unreadablePids':[],'activeWriterClaims':[]}
        self.assertEqual(review.host_summary(row)['activeWriters'],0)
        coverage=review.job_host_coverage(['c01'],['c01'],{'reports':[row]})
        self.assertEqual(coverage['jobHostsVerified'],['c01'])
        self.assertTrue(coverage['jobHostCoverageComplete'])
        self.assertEqual(row['status'],'BUSY')

    def test_partial_visibility_or_new_node_cannot_satisfy_coverage(self):
        row={'host':'c01','status':'BLOCKED','checks':{'hostNamespace':'PASS','processes':'BLOCKED','writers':'PASS','scheduler':'PASS'},
             'legacyProcesses':[],'unreviewedProcesses':[],'unreadablePids':[1],'activeWriterClaims':[]}
        self.assertEqual(review.host_summary(row)['unreadableProcesses'],1)
        coverage=review.job_host_coverage(['c01','c02'],['c01'],{'reports':[row]})
        self.assertEqual(coverage['jobHostsNotDeclared'],['c02'])
        self.assertEqual(coverage['jobHostsNotYetAudited'],['c01','c02'])
        self.assertFalse(coverage['jobHostCoverageComplete'])


if __name__=='__main__':unittest.main()
