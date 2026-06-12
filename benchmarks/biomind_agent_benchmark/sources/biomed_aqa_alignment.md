# BioMed-AQA Alignment Notes

Verified on 2026-05-31.

## Reference Benchmark

- Paper: "Empowering AI data scientists using a multi-agent LLM framework with self-evolving capabilities for autonomous, tool-aware biomedical data analyses"
- Article page: https://www.nature.com/articles/s41551-026-01634-6
- Code: https://github.com/BOBQWERA/BioMedAgent
- Open-ended benchmark dataset: https://huggingface.co/datasets/BOBQWERA/biomed-aqa-dataset
- MCQ benchmark dataset: https://huggingface.co/datasets/BOBQWERA/biomed-mcq-dataset
- Input and milestone files: https://doi.org/10.5281/zenodo.17430550

Hugging Face dataset metadata reported 327 open-ended rows with 16 columns for
BioMed-AQA and 172 MCQ rows with 14 columns for BioMed-MCQ.

## Observed BioMed-AQA Fields

The open-ended dataset exposes fields for task category, task abbreviation,
natural-language question, reference milestone count, reference milestones,
reference step count, reference steps, milestone reference files, task subclass,
dataset replicate, question type, input files, order, task, and index.

The MCQ dataset exposes fields for question id, task, original question, data
source, MCQ prompt, option description, five options, correct answer, question
type, and index.

## BioMind Adaptation

BioMind keeps the evaluation shape but rewrites the content for local skills and
crop/omics workflows:

- BioMed-AQA `question` maps to BioMind `prompt`.
- BioMed-AQA `reference_steps` maps to BioMind `reference_steps`.
- BioMed-AQA `reference_milestones` maps to BioMind milestone descriptions.
- BioMed-AQA `files` maps to BioMind `repo_files` or `input_requirements`.
- BioMed-AQA `milestone_reference_files` maps to BioMind `expected_artifacts`.
- BioMed-MCQ `correct_answer` maps to BioMind `answers/expected_answers.yaml`
  `mcq_answers`.
- BioMind v0.3 adds `data_files` for explicit fixture paths and availability.
- BioMind v0.3 adds `result_question` for final result-choice scoring after
  analysis, plus `expected_metrics` and `numeric_tolerances` for metric checks.
- BioMind v0.3 distinguishes `expected_skills`, `optional_skills`, and
  `forbidden_skills` so skill selection can be scored independently.

BioMind does not copy the original benchmark tasks into the repo. The active
BioMind benchmark uses BioMed-AQA's structure with BioMind-specific questions,
skills, fixtures, and hard-failure rules. Active runnable fixtures should come
from professional `msprime` simulation or public downloaded subsets; deletable
`results/` outputs are not valid benchmark source files.
