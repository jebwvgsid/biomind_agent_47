"""New synthetic projects only. Real filesystem/SQLite, no provider or science."""
import json
import multiprocessing
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from memory.wiki_memory import WikiMemoryManager


def writer(root, name, ready, start, results):
    w=WikiMemoryManager(None, root)
    ready.put(True);start.wait(10)
    results.put(w.apply_patch_result({'operations':[{'op':'append_section','path':'methods/shared.md',
        'content':name,'section':'Observed','sources':['task:'+name]}]}, 'A'))


class ConsultMemoryStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.w=WikiMemoryManager(None,Path(self.tmp.name)/'memory')
    def create(self, scope='A', **extras):
        op={'op':'create_page','path':'methods/shared.md','title':'samtools indexing','type':'method',
            'content':'samtools 索引失败 E42 /project/input.tsv','sources':['task:'+scope],**extras}
        return self.w.apply_patch_result({'operations':[op]},scope)
    def test_namespaces_scopes_global_and_read_only_search(self):
        a=self.create();b=self.create('B');g=self.create('global')
        self.assertEqual(len({r['written'][0]['path'] for r in (a,b,g)}),3)
        with patch.object(self.w,'_all_pages',side_effect=AssertionError('full scan')):
            hits=self.w.search('索引失败','B');self.assertEqual({h['project_id'] for h in hits},{'B','global'})
        self.assertIsNone(self.w.page_record(a['written'][0]['path'],'B'))
    def test_empty_navigation_no_purpose(self):
        self.assertEqual(self.w.navigation('A'),'')
        self.assertEqual(self.w.build_prompt_context('new','A','missing'),'')
    def test_launcher_metadata_reads_empty_projects_without_wiki_initialization(self):
        with self.w._connect() as conn:
            conn.executemany('INSERT INTO memory_project_roots VALUES (?,?)',[
                ('/synthetic/empty 项目','legacy-project'),('/synthetic/empty 项目','ambiguous-project')])
            conn.commit()
        before={str(p.relative_to(self.w.root)):(p.read_bytes(),p.stat().st_mtime_ns)
            for p in self.w.root.rglob('*') if p.is_file()}
        with patch.object(WikiMemoryManager,'__init__',side_effect=AssertionError('constructor writes')):
            for _ in range(2):
                result=WikiMemoryManager.read_project_roots(self.w.root)
                self.assertEqual(result['status'],'ok')
                self.assertEqual(result['roots'],[
                    {'root':'/synthetic/empty 项目','project_id':'ambiguous-project'},
                    {'root':'/synthetic/empty 项目','project_id':'legacy-project'}])
        after={str(p.relative_to(self.w.root)):(p.read_bytes(),p.stat().st_mtime_ns)
            for p in self.w.root.rglob('*') if p.is_file()}
        self.assertEqual(before,after)
        self.assertEqual(self.w.discover_projects()['hits'],[])
    def test_launcher_metadata_missing_corrupt_and_custom_database(self):
        missing=Path(self.tmp.name)/'not-created'
        self.assertEqual(WikiMemoryManager.read_project_roots(missing)['status'],'missing')
        self.assertFalse(missing.exists())
        custom=Path(self.tmp.name)/'custom wiki';custom.mkdir()
        db=custom/'wiki_index.sqlite3';db.write_bytes(b'not a SQLite database')
        self.assertEqual(WikiMemoryManager.read_project_roots(missing,wiki_dir=custom)['status'],'unavailable')
        db.unlink()
        with sqlite3.connect(db) as conn:conn.execute('CREATE TABLE unrelated(value TEXT)')
        before=db.read_bytes()
        self.assertEqual(WikiMemoryManager.read_project_roots(missing,wiki_dir=custom)['reason'],'registry_table_missing')
        self.assertEqual(db.read_bytes(),before)
        self.assertFalse(missing.exists())
    def test_launcher_metadata_wal_is_unavailable_without_coordination_writes(self):
        root=Path(self.tmp.name)/'wal-memory';folder=root/'wiki';folder.mkdir(parents=True)
        db=folder/'wiki_index.sqlite3'
        writer=sqlite3.connect(db)
        self.addCleanup(writer.close)
        self.assertEqual(writer.execute('PRAGMA journal_mode=WAL').fetchone()[0],'wal')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('CREATE TABLE memory_project_roots (root TEXT, project_id TEXT)')
        writer.commit();writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        writer.execute('INSERT INTO memory_project_roots VALUES (?,?)',('/synthetic/wal-project','committed-in-wal'))
        writer.commit()
        def check(memory):
            wiki=memory/'wiki'
            before={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in wiki.iterdir()}
            with patch.object(WikiMemoryManager,'_project_roots_connection',side_effect=AssertionError('WAL connection may write sidecars')):
                result=WikiMemoryManager.read_project_roots(memory)
                self.assertEqual(result,{'status':'unavailable','roots':[], 'reason':'wal_metadata_requires_coordination'})
                self.assertEqual(WikiMemoryManager.register_project_root(memory,self.tmp.name),result)
            self.assertEqual(before,{p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in wiki.iterdir()})
        with self.subTest(state='writer open with committed WAL'):check(root)
        copied=Path(self.tmp.name)/'synthetic-wal-only-copy';(copied/'wiki').mkdir(parents=True)
        for suffix in ('','-wal'):
            shutil.copy2(Path(str(db)+suffix),copied/'wiki'/(db.name+suffix))
        with self.subTest(state='closed WAL-only synthetic snapshot'):check(copied)
        writer.close()
        self.assertFalse(Path(str(db)+'-wal').exists())
        with self.subTest(state='writer cleanly closed'):check(root)
    def test_launcher_metadata_unreadable_header_is_unavailable(self):
        before=self.w.db_path.read_bytes();mode=self.w.db_path.stat().st_mode
        self.w.db_path.chmod(0)
        try:
            with patch.object(WikiMemoryManager,'_project_roots_connection',side_effect=AssertionError('unreadable header must not connect')):
                self.assertEqual(WikiMemoryManager.read_project_roots(self.w.root),
                    {'status':'unavailable','roots':[],'reason':'PermissionError'})
        finally:
            self.w.db_path.chmod(mode)
        self.assertEqual(before,self.w.db_path.read_bytes())
    def test_launcher_registration_preserves_mapping_and_changes_metadata_only(self):
        project=Path(self.tmp.name)/'empty project';project.mkdir()
        with self.w._connect() as conn:
            conn.execute('INSERT INTO memory_project_roots VALUES (?,?)',(str(project),'preexisting-id'))
            conn.commit()
        result=WikiMemoryManager.register_project_root(self.w.root,project)
        self.assertEqual(result['status'],'existing')
        self.assertEqual(result['roots'][0]['project_id'],'preexisting-id')
        other=Path(self.tmp.name)/'新 project';other.mkdir()
        before={str(p.relative_to(self.w.root)):p.read_bytes()
            for p in self.w.root.rglob('*') if p.is_file() and p!=self.w.db_path}
        self.assertEqual(WikiMemoryManager.register_project_root(self.w.root,other)['status'],'registered')
        self.assertEqual(WikiMemoryManager.register_project_root(self.w.root,other)['status'],'existing')
        self.assertEqual(before,{str(p.relative_to(self.w.root)):p.read_bytes()
            for p in self.w.root.rglob('*') if p.is_file() and p!=self.w.db_path})
        self.assertEqual(self.w.discover_projects()['hits'],[])
        missing=Path(self.tmp.name)/'missing-registry'
        self.assertEqual(WikiMemoryManager.register_project_root(missing,other)['status'],'missing')
        self.assertFalse(missing.exists())
    def test_invalidation_forget_tombstone_and_original_record_retained(self):
        a=self.create()['written'][0]
        result=self.w.apply_patch_result({'operations':[{'op':'update_frontmatter','path':a['path'],
            'expected_hash':a['content_hash'],'values':{'status':'forgotten'}}]},'A')
        self.assertEqual(result['status'],'written');self.assertEqual(self.w.search('samtools','A'),[])
        self.assertIsNone(self.w.page_record(a['path'],'A'))
        self.assertEqual(self.w.page_record(a['path'],'A',include_inactive=True)['status'],'forgotten')
        self.assertEqual(self.create()['status'],'noop')
        self.assertIn('samtools',Path(a['absolute_path']).read_text())
        self.assertIn('task:A',self.w.excluded_sources('A'))
    def test_superseded_and_deleted_never_ghost(self):
        a=self.create()['written'][0]
        self.w.apply_patch_result({'operations':[{'op':'update_frontmatter','path':a['path'],'values':{'status':'superseded'}}]},'A')
        self.assertEqual(self.w.search('samtools','A'),[])
        b=self.create('B')['written'][0];Path(b['absolute_path']).unlink()
        self.assertEqual(self.w.search('samtools','B'),[])
    def test_external_edit_conflict_and_refresh(self):
        a=self.create()['written'][0];p=Path(a['absolute_path']);p.write_text(p.read_text()+'\nUSER_EDIT\n')
        r=self.w.apply_patch_result({'operations':[{'op':'append_section','path':a['path'],'expected_hash':a['content_hash'],'content':'BAD'}]},'A')
        self.assertEqual(r['status'],'failed');self.assertNotIn('BAD',p.read_text())
        self.assertEqual(self.w.search('USER_EDIT','A'),[])
        self.w.refresh_pages([a['path']]);self.assertTrue(self.w.search('USER_EDIT','A'))
    def test_publication_survives_index_failure_retry_no_append(self):
        with patch.object(self.w,'refresh_pages',side_effect=RuntimeError('synthetic index failure')):r=self.create()
        self.assertEqual(r['status'],'written');self.assertTrue(r['index_pending'])
        p=Path(r['written'][0]['absolute_path']);before=p.read_bytes()
        again=self.create();self.assertEqual(again['status'],'noop');self.assertEqual(p.read_bytes(),before)
        self.assertTrue(self.w.search('samtools','A'))
    def test_partial_and_noop(self):
        r=self.w.apply_patch_result({'operations':[{'op':'create_page','path':'methods/a.md','content':'ok'},
            {'op':'does_not_exist'}]},'A')
        self.assertEqual(r['status'],'partial');self.assertEqual(len(r['written']),1)
        self.assertEqual(self.w.apply_patch_result({'operations':[]},'A')['status'],'noop')
    def test_process_lock_covers_reread_and_publish_on_workspace_filesystem(self):
        self.create();ctx=multiprocessing.get_context('fork');ready=ctx.Queue();results=ctx.Queue();start=ctx.Event()
        workers=[ctx.Process(target=writer,args=(str(self.w.root),n,ready,start,results)) for n in ['UPDATE_A','UPDATE_B']]
        for p in workers:p.start()
        for p in workers:ready.get(timeout=10)
        start.set()
        for p in workers:p.join(15);self.assertEqual(p.exitcode,0)
        for p in workers:self.assertEqual(results.get(timeout=5)['status'],'written')
        text=self.w.read_page('methods/shared.md','A');self.assertIn('UPDATE_A',text);self.assertIn('UPDATE_B',text)
    def test_low_confidence_is_note_not_new_approval(self):
        r=self.create(confidence='low',status='unverified');self.assertEqual(r['status'],'written')
        self.assertEqual(self.w.search('samtools','A')[0]['status'],'unverified')
    def test_repeat_append_different_call_is_true_noop_and_short_digest(self):
        a=self.create()['written'][0]
        op={'op':'append_section','path':a['path'],'content':'OBSERVED_FIX','section':'Notes','sources':['task:A'],'operation_id':'one'}
        self.assertEqual(self.w.apply_patch_result({'operations':[op]},'A')['status'],'written')
        path=Path(a['absolute_path']);before=path.read_bytes();op['operation_id']='different-call'
        self.assertEqual(self.w.apply_patch_result({'operations':[op]},'A')['status'],'noop')
        self.assertEqual(path.read_bytes(),before)
        digest=json.loads(self.w.digest_path('A').read_text());self.assertNotIn('OBSERVED_FIX',json.dumps(digest))
        self.w.apply_patch_result({'operations':[{'op':'update_frontmatter','path':a['path'],'values':{'status':'forgotten'}}]},'A')
        self.assertEqual(json.loads(self.w.digest_path('A').read_text())['pages'],[])
        self.assertEqual(self.w.read_claims_index('A'),[])
    def test_disk_failure_is_failed_not_success_and_short_query_rewrite(self):
        with patch.object(self.w,'_write_page',side_effect=OSError('synthetic disk full')):
            self.assertEqual(self.create()['status'],'failed')
        self.create()
        self.assertEqual(self.w.search('a completely unrelated purpose','A'),[])
        self.assertTrue(self.w.search('E42','A'))
        self.assertTrue(self.w.search('/project/input.tsv','A'))
        self.assertTrue(self.w.search('索引失败','A'))
