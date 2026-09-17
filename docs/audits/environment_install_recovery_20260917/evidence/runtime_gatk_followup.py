"""Distinguish the installed GATK Python launcher from its JVM runtime."""
import asyncio
import json
import tempfile
import runtime_probe as p

async def main():
    p.LIMIT = 10
    with tempfile.TemporaryDirectory(prefix='biomind-gatk-help-') as tmp:
        env={'PATH':str(p.ROOT/'envs/bio_gatk/bin')+':/usr/bin:/bin',
             'LC_ALL':'C','TMPDIR':tmp,'CONDA_PREFIX':str(p.ROOT/'envs/bio_gatk'),
             'CONDA_DEFAULT_ENV':'bio_gatk'}
        wrapper=str(p.ROOT/'envs/bio_gatk/bin/gatk')
        rows=[]
        for arguments in [['--version'],*[ [command,'--help'] for command in
            ('HaplotypeCaller','CombineGVCFs','GenotypeGVCFs','SelectVariants',
             'VariantFiltration','ValidateVariants')]]:
            rows.append(await p.probe('gatk_explicit_system_python_'+'_'.join(arguments),
                ['/usr/bin/python3',wrapper,*arguments],env,tmp))
        rows.append(await p.probe('gatk_env_bcftools_stats_help',
            ['bcftools','stats','--help'],env,tmp))
        wgs={**env,'PATH':str(p.ROOT/'envs/bio_wgs/bin')+':/usr/bin:/bin',
             'CONDA_PREFIX':str(p.ROOT/'envs/bio_wgs'),'CONDA_DEFAULT_ENV':'bio_wgs'}
        for command in ('quickcheck','view','coverage'):
            rows.append(await p.probe('wgs_samtools_'+command+'_help',
                ['samtools',command,'--help'],wgs,tmp))
    (p.BASE/'runtime_gatk_followup_results.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
    for row in rows:
        print(row['label'],row['returncode'],row['outcome'],repr((row['stdout']+row['stderr'])[:300]))

asyncio.run(main())
