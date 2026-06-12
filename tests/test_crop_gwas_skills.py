import csv
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CropGwasScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.common = cls.repo_root / "skills" / "crop-breeding" / "_gwas_common" / "scripts"

    def run_python(self, *args):
        return subprocess.run(
            [sys.executable, *map(str, args)],
            cwd=self.repo_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

    def test_python_wrappers_expose_help(self):
        scripts = [
            self.repo_root / "skills" / "crop-breeding" / "crop-gwas-qc-plink2-vcftools" / "scripts" / "run_crop_gwas_qc.py",
            self.repo_root / "skills" / "crop-breeding" / "crop-gwas-gemma-lmm" / "scripts" / "run_gemma_lmm.py",
            self.common / "standardize_gwas_summary.py",
            self.common / "map_snp_to_gene.py",
        ]
        for script in scripts:
            result = self.run_python(script, "--help")
            self.assertIn("usage:", result.stdout.lower())

    def test_standardize_summary_keeps_arbitrary_chromosome_and_trait_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gwas = root / "assoc.tsv"
            gwas.write_text(
                "Marker\tChromosome\tPosition\tP\tEffect\n"
                "m1\tScaffold-Alpha\t101\t0.001\t0.4\n"
                "m2\tScaffold-Alpha\t500\t0.8\t-0.1\n",
                encoding="utf-8",
            )
            outdir = root / "out"
            self.run_python(
                self.common / "standardize_gwas_summary.py",
                "--gwas",
                gwas,
                "--outdir",
                outdir,
                "--trait",
                "fresh_weight_kg",
                "--method",
                "unit-test",
            )

            rows = list(csv.DictReader((outdir / "summary_stats.tsv").open(encoding="utf-8"), delimiter="\t"))
            self.assertEqual(rows[0]["chr"], "Scaffold-Alpha")
            self.assertEqual(rows[0]["trait"], "fresh_weight_kg")
            self.assertEqual(rows[0]["marker_id"], "m1")
            self.assertTrue((outdir / "lead_snps.tsv").is_file())
            self.assertTrue((outdir / "lambda_gc.tsv").is_file())
            self.assertTrue((outdir / "result.json").is_file())

    def test_candidate_gene_mapping_uses_generic_gff_coordinates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snps = root / "lead.tsv"
            snps.write_text(
                "marker_id\tchr\tpos\tp_value\n"
                "leadA\tScaffold-Alpha\t150\t0.001\n",
                encoding="utf-8",
            )
            gff = root / "genes.gff3"
            gff.write_text(
                "##gff-version 3\n"
                "Scaffold-Alpha\tBioMind\tgene\t100\t220\t.\t+\t.\tID=GeneA;Name=generic_gene\n"
                "Scaffold-Alpha\tBioMind\tgene\t1000\t1200\t.\t+\t.\tID=GeneB\n",
                encoding="utf-8",
            )
            outdir = root / "mapped"
            self.run_python(self.common / "map_snp_to_gene.py", "--snps", snps, "--gff", gff, "--outdir", outdir, "--window", "200")

            nearest = list(csv.DictReader((outdir / "lead_snp_nearest_genes.tsv").open(encoding="utf-8"), delimiter="\t"))
            candidates = list(csv.DictReader((outdir / "candidate_genes.tsv").open(encoding="utf-8"), delimiter="\t"))
            self.assertEqual(nearest[0]["gene_id"], "GeneA")
            self.assertEqual(candidates[0]["gene_id"], "GeneA")

    def test_qc_wrapper_dry_run_writes_sample_overlap_without_external_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vcf = root / "input.vcf"
            vcf.write_text(
                "##fileformat=VCFv4.2\n"
                "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1\tS2\n",
                encoding="utf-8",
            )
            phenotype = root / "pheno.tsv"
            phenotype.write_text("sample_id\ttrait\nS1\t1.2\nS3\t2.4\n", encoding="utf-8")
            outdir = root / "qc"
            self.run_python(
                self.repo_root / "skills" / "crop-breeding" / "crop-gwas-qc-plink2-vcftools" / "scripts" / "run_crop_gwas_qc.py",
                "--vcf",
                vcf,
                "--phenotype",
                phenotype,
                "--outdir",
                outdir,
                "--dry-run",
            )

            overlap = list(csv.DictReader((outdir / "qc" / "sample_overlap.tsv").open(encoding="utf-8"), delimiter="\t"))
            by_sample = {row["sample_id"]: row for row in overlap}
            self.assertEqual(by_sample["S1"]["in_both"], "1")
            self.assertEqual(by_sample["S2"]["in_phenotype"], "0")
            self.assertEqual(by_sample["S3"]["in_genotype"], "0")


if __name__ == "__main__":
    unittest.main()
