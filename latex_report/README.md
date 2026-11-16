# ACA project report: throughput-safe coordination of prefetch throttling and LLC partitioning

## Build
    latexmk -pdf main.tex          # or: pdflatex main; bibtex main; pdflatex main; pdflatex main
On Overleaf: upload the folder (or the zip) and compile `main.tex`. The template uses `IEEEtran` when it is
available (Overleaf has it) and falls back to a two-column `article` otherwise.

## Layout
    main.tex                      preamble, title, abstract, \input of the sections, bibliography
    sections/01_introduction.tex
    sections/02_related_work.tex
    sections/03_problem.tex       research gap: setup, findings 1-4 (tables + figures), problem statement
    sections/04_methodology.tex   coordinator: diagram, inputs/actuators, algorithm, partition control, cost
    sections/05_experiments.tex   v3.3 results, ablation, 8-core (pending), sensitivity, limitations
    sections/06_conclusion.tex
    figures/controller.tex        TikZ block diagram of the coordinator
    figures/*.pdf                 data figures (generated)
    figures/fig*.png              8-core scale-up figures (scripts/plot_8core_report.py in the repo)
    tables/*.tex                  data tables (generated)
    data/*.xlsx                   the result workbooks the figures/tables are made from
    scripts/make_figures.py       regenerates figures/*.pdf and tables/*.tex from data/
    refs.bib

## Filling in the 8-core v3.3 results
1. On the server, when `eval_v33_8.log` shows all 40 runs OK, write the report (variables from the run set):

       python3 scripts/eval_coord.py --setups 8core --mixes-8core 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20 --base-variants vpt_part --bin-tag coord7 --tag final_8core --report-only --variant v33=$V33

2. Copy `eval_results/eval_report_final_8core.xlsx` to `data/v33_8core.xlsx` and run
   `python3 scripts/make_figures.py` -- `tables/eightcore.tex` then contains the v3.3 rows.
3. Replace the red `\pending{...}` notes (abstract, Sec. 5.3, conclusion, author block); `grep -rn pending sections main.tex`.

Please double-check the bibliography entries (page numbers/DOIs were not verified online).
