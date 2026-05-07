import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CONDA_ENV = "bio_scrna_core"


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _matrix_market_from_dense(matrix):
    n_rows = len(matrix)
    n_cols = len(matrix[0]) if n_rows else 0
    entries = []
    for row_idx, row in enumerate(matrix, start=1):
        for col_idx, value in enumerate(row, start=1):
            if value:
                entries.append(f"{row_idx} {col_idx} {value}")
    header = [
        "%%MatrixMarket matrix coordinate integer general",
        "%",
        f"{n_rows} {n_cols} {len(entries)}",
    ]
    return "\n".join(header + entries) + "\n"


def _write_10x_folder(folder: Path, genes, barcodes, matrix):
    folder.mkdir(parents=True, exist_ok=True)
    _write_text(folder / "barcodes.tsv", "\n".join(barcodes) + "\n")
    feature_lines = [f"gene_{i}\t{gene}\tGene Expression" for i, gene in enumerate(genes, start=1)]
    _write_text(folder / "features.tsv", "\n".join(feature_lines) + "\n")
    _write_text(folder / "matrix.mtx", _matrix_market_from_dense(matrix))


class ScrnaSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conda = shutil.which("conda")
        if not cls.conda:
            raise unittest.SkipTest("conda is not available in PATH")
        env = os.environ.copy()
        env["CONDA_NO_PLUGINS"] = "true"
        probe = subprocess.run(
            [cls.conda, "run", "-n", CONDA_ENV, "Rscript", "-e", "cat('ready')"],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if probe.returncode != 0:
            raise unittest.SkipTest(f"{CONDA_ENV} is not runnable in this environment: {probe.stderr.strip()}")

    def _run_r_script(self, script_path: Path, *args: str):
        env = os.environ.copy()
        env["CONDA_NO_PLUGINS"] = "true"
        result = subprocess.run(
            [self.conda, "run", "-n", CONDA_ENV, "Rscript", str(script_path), *args],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=1200,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"Command failed: {script_path}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )
        return result

    def _run_r_expr(self, expr: str):
        env = os.environ.copy()
        env["CONDA_NO_PLUGINS"] = "true"
        result = subprocess.run(
            [self.conda, "run", "-n", CONDA_ENV, "Rscript", "-e", expr],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertEqual(result.returncode, 0, msg=f"R expression failed:\n{expr}\nSTDERR:\n{result.stderr}")
        return result.stdout.strip()

    def test_single_sample_10x_smoke(self):
        genes = ["MT-ND1", "MT-CO1", "CD3D", "IL7R", "LTB", "MS4A1", "CD79A", "CD74", "MALAT1", "LYZ"]
        barcodes = [f"cell_{i}" for i in range(1, 9)]
        matrix = [
            [1, 1, 1, 1, 1, 1, 1, 1],
            [1, 1, 1, 1, 1, 1, 1, 1],
            [7, 6, 8, 7, 1, 1, 0, 1],
            [6, 7, 6, 7, 1, 1, 0, 1],
            [5, 6, 5, 6, 1, 1, 0, 0],
            [0, 1, 1, 0, 7, 6, 8, 7],
            [0, 1, 0, 1, 6, 7, 6, 7],
            [1, 0, 1, 0, 5, 6, 5, 6],
            [4, 4, 4, 4, 4, 4, 4, 4],
            [2, 2, 2, 2, 2, 2, 2, 2],
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            tenx_dir = tmp / "data" / "single"
            _write_10x_folder(tenx_dir, genes, barcodes, matrix)
            output_dir = tmp / "outputs" / "single_sample"
            manifest = tmp / "single_sample.yaml"
            _write_text(
                manifest,
                "\n".join(
                    [
                        "project_name: single_sample_smoke",
                        "species: human",
                        "input_mode: 10x_mtx",
                        "sample_id: sample1",
                        f"input_path: {tenx_dir.as_posix()}",
                        f"output_dir: {output_dir.as_posix()}",
                        "qc:",
                        "  min_features: 0",
                        "  max_features: 5000",
                        "  min_counts: 0",
                        "  max_counts: 50000",
                        "  max_percent_mt: 100",
                        "preprocess:",
                        "  method: lognorm",
                        "  nfeatures: 8",
                        "cluster:",
                        "  pca_npcs: 5",
                        "  cluster_dims: '1:3'",
                        "  resolution: 0.2",
                        "  run_tsne: false",
                        "markers:",
                        "  top_n: 3",
                        "annotation:",
                        "  method: marker-first",
                    ]
                )
                + "\n",
            )

            self._run_r_script(REPO_ROOT / "skills/scrna/ingest/data-download/scripts/ingest_scrna_seurat.R", "--manifest", str(manifest))
            self._run_r_script(REPO_ROOT / "skills/scrna/qc/filter/scripts/qc_filter_seurat.R", "--manifest", str(manifest))
            self._run_r_script(REPO_ROOT / "skills/scrna/preprocess/basic/scripts/preprocess_scrna_seurat.R", "--manifest", str(manifest))
            self._run_r_script(REPO_ROOT / "skills/scrna/cluster/basic/scripts/cluster_scrna_seurat.R", "--manifest", str(manifest))
            self._run_r_script(REPO_ROOT / "skills/scrna/markers/find/scripts/find_markers_seurat.R", "--manifest", str(manifest))
            self._run_r_script(REPO_ROOT / "skills/scrna/annotate/basic/scripts/annotate_scrna_seurat.R", "--manifest", str(manifest))

            self.assertTrue((output_dir / "objects/seurat_raw.rds").exists())
            self.assertTrue((output_dir / "objects/seurat_qc.rds").exists())
            self.assertTrue((output_dir / "objects/seurat_norm.rds").exists())
            self.assertTrue((output_dir / "objects/seurat_clustered.rds").exists())
            self.assertTrue((output_dir / "objects/seurat_annotated.rds").exists())
            self.assertTrue((output_dir / "tables/cluster_markers.tsv").exists())
            self.assertTrue((output_dir / "tables/celltype_annotation.tsv").exists())

    def test_dual_sample_ctrl_stim_smoke(self):
        genes = ["MT-ND1", "MT-CO1", "IFIT1", "ISG15", "MX1", "MALAT1", "IL7R", "LTB", "MS4A1", "CD79A"]
        barcodes = [f"cell_{i}" for i in range(1, 7)]
        ctrl_matrix = [
            [1, 1, 1, 1, 1, 1],
            [1, 1, 1, 1, 1, 1],
            [0, 1, 0, 1, 0, 1],
            [0, 1, 0, 1, 0, 1],
            [1, 1, 1, 1, 1, 1],
            [5, 5, 5, 5, 5, 5],
            [5, 6, 5, 6, 1, 1],
            [4, 5, 4, 5, 1, 1],
            [1, 1, 1, 1, 6, 6],
            [1, 1, 1, 1, 5, 5],
        ]
        stim_matrix = [
            [1, 1, 1, 1, 1, 1],
            [1, 1, 1, 1, 1, 1],
            [7, 8, 7, 8, 7, 8],
            [6, 7, 6, 7, 6, 7],
            [5, 6, 5, 6, 5, 6],
            [5, 5, 5, 5, 5, 5],
            [5, 6, 5, 6, 1, 1],
            [4, 5, 4, 5, 1, 1],
            [1, 1, 1, 1, 6, 6],
            [1, 1, 1, 1, 5, 5],
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            ctrl_dir = tmp / "data" / "ctrl"
            stim_dir = tmp / "data" / "stim"
            _write_10x_folder(ctrl_dir, genes, barcodes, ctrl_matrix)
            _write_10x_folder(stim_dir, genes, barcodes, stim_matrix)
            output_dir = tmp / "outputs" / "ctrl_stim"
            manifest = tmp / "ctrl_stim.yaml"
            _write_text(
                manifest,
                "\n".join(
                    [
                        "project_name: ctrl_stim_smoke",
                        "species: human",
                        "input_mode: 10x_mtx",
                        f"output_dir: {output_dir.as_posix()}",
                        "samples:",
                        f"  - sample_id: ctrl\n    path: {ctrl_dir.as_posix()}",
                        f"  - sample_id: stim\n    path: {stim_dir.as_posix()}",
                        "qc:",
                        "  min_features: 0",
                        "  max_features: 5000",
                        "  min_counts: 0",
                        "  max_counts: 50000",
                        "  max_percent_mt: 100",
                        "preprocess:",
                        "  method: lognorm",
                        "  nfeatures: 8",
                        "cluster:",
                        "  pca_npcs: 5",
                        "  cluster_dims: '1:3'",
                        "  resolution: 0.2",
                        "  run_tsne: false",
                        "markers:",
                        "  top_n: 3",
                        "annotation:",
                        "  method: marker-first",
                        "de:",
                        "  group_by: sample_id",
                    ]
                )
                + "\n",
            )

            self._run_r_script(REPO_ROOT / "skills/scrna/ingest/data-download/scripts/ingest_scrna_seurat.R", "--manifest", str(manifest))
            self._run_r_script(REPO_ROOT / "skills/scrna/qc/filter/scripts/qc_filter_seurat.R", "--manifest", str(manifest))
            self._run_r_script(REPO_ROOT / "skills/scrna/preprocess/basic/scripts/preprocess_scrna_seurat.R", "--manifest", str(manifest))
            self._run_r_script(REPO_ROOT / "skills/scrna/cluster/basic/scripts/cluster_scrna_seurat.R", "--manifest", str(manifest))
            self._run_r_script(REPO_ROOT / "skills/scrna/markers/find/scripts/find_markers_seurat.R", "--manifest", str(manifest))
            self._run_r_script(REPO_ROOT / "skills/scrna/annotate/basic/scripts/annotate_scrna_seurat.R", "--manifest", str(manifest))
            self._run_r_script(REPO_ROOT / "skills/scrna/de/contrast/scripts/de_contrast_seurat.R", "--manifest", str(manifest))

            self.assertTrue((output_dir / "tables/de_results.tsv").exists())
            self.assertTrue((output_dir / "tables/de_summary.tsv").exists())
            summary_text = (output_dir / "tables/de_summary.tsv").read_text(encoding="utf-8")
            self.assertIn("sample_id", summary_text)

    def test_seurat_rds_smoke_preserves_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source_output = tmp / "outputs" / "source_demo"
            source_manifest = tmp / "source_demo.yaml"
            _write_text(
                source_manifest,
                "\n".join(
                    [
                        "project_name: source_demo",
                        "species: human",
                        "input_mode: public_demo",
                        "demo_name: pbmc_small_builtin",
                        "sample_id: demo_source",
                        f"output_dir: {source_output.as_posix()}",
                    ]
                )
                + "\n",
            )
            self._run_r_script(REPO_ROOT / "skills/scrna/ingest/data-download/scripts/ingest_scrna_seurat.R", "--manifest", str(source_manifest))

            rds_output = tmp / "outputs" / "rds_roundtrip"
            rds_manifest = tmp / "rds_roundtrip.yaml"
            _write_text(
                rds_manifest,
                "\n".join(
                    [
                        "project_name: rds_roundtrip",
                        "species: human",
                        "input_mode: seurat_rds",
                        "sample_id: override_attempt",
                        f"input_path: {(source_output / 'objects/seurat_raw.rds').as_posix()}",
                        f"output_dir: {rds_output.as_posix()}",
                    ]
                )
                + "\n",
            )
            self._run_r_script(REPO_ROOT / "skills/scrna/ingest/data-download/scripts/ingest_scrna_seurat.R", "--manifest", str(rds_manifest))

            summary = self._run_r_expr(
                "obj <- readRDS('{path}'); "
                "cat(paste(sort(unique(as.character(obj$sample_id))), collapse=',')); "
                "cat('\\n'); "
                "cat(paste(sort(unique(as.character(obj$orig.ident))), collapse=','))".format(
                    path=(rds_output / "objects/seurat_raw.rds").as_posix()
                )
            )
            lines = summary.splitlines()
            self.assertEqual(lines[0], "demo_source")
            self.assertEqual(lines[1], "demo_source")


if __name__ == "__main__":
    unittest.main()
