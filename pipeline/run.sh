#!/bin/zsh

# python reduction.py --config config.toml --obs capers_uds_p1 --stage2a --processes 12 --overwrite --source-ids 151856 14059 14070 14502 15795 16842 16964 109375 154077 14950 17905 20366 100661 112611 117357 119232 119472 119540 149533 150683
# python reduction.py --config config.toml --obs capers_uds_p2 --stage2a --processes 12 --overwrite --source-ids 3975 5645 6673 6806 132209 143579 4275 4679 9160 130679
# python reduction.py --config config.toml --obs capers_uds_p3 --stage2a --processes 12 --overwrite --source-ids 1002 1354 12169 125909 126349 135091 143548 2111 128684 132167
# python reduction.py --config config.toml --obs capers_uds_p4 --stage2a --processes 12 --overwrite --source-ids 33598 34525 34909 38782 44802 70196 33654 78907 33393 33603 36362 36428 39135
# python reduction.py --config config.toml --obs capers_uds_p5 --stage2a --processes 12 --overwrite --source-ids 25083 25410 27778 89837 97710 98316 100314 25296 25414 26544 27754 27779 27780 29938 29939 89009 89048 101239
# python reduction.py --config config.toml --obs capers_uds_p6 --stage2a --processes 12 --overwrite --source-ids 35570 35576 35656 35879 36017 36365 36395 38073 38258 38451 39336 42404 59580 64354 67272 67363 68224 68310 69667 71767 71981 72119 72124 73452 74093 74524 74785 76453 78091 80300
# python reduction.py --config config.toml --obs capers_uds_p7 --stage2a --processes 12 --overwrite --source-ids 18576 19132 19454 20360 20636 22028 23353 27175 27626 107158 108665 108683 108712 108930 109018 109450 109907 114575 114648
source ~/.zshrc

conda activate jwst && cd .. && python scripts/deploy.py --obs capers_uds_p6 && cd pipeline
conda activate base && python plot_slits.py --obs capers_uds_p7 --approve-shifts && conda activate jwst && cd .. && python scripts/deploy.py --obs capers_uds_p7 && cd pipeline
python reduction.py --config config.toml --obs capers_uds_p1 --stage2b --stage3 --processes 18 && python fitting.py --obs capers_uds_p1 && conda activate base && python plot_slits.py --obs capers_uds_p1 --approve-shifts && conda activate jwst && cd .. && python scripts/deploy.py --obs capers_uds_p1 && cd pipeline
python reduction.py --config config.toml --obs capers_uds_p2 --stage2b --stage3 --processes 18 && python fitting.py --obs capers_uds_p2 && conda activate base && python plot_slits.py --obs capers_uds_p2 --approve-shifts && conda activate jwst && cd .. && python scripts/deploy.py --obs capers_uds_p2 && cd pipeline
python reduction.py --config config.toml --obs capers_uds_p3 --stage2b --stage3 --processes 18 && python fitting.py --obs capers_uds_p3 && conda activate base && python plot_slits.py --obs capers_uds_p3 --approve-shifts && conda activate jwst && cd .. && python scripts/deploy.py --obs capers_uds_p3 && cd pipeline
python reduction.py --config config.toml --obs capers_uds_p4 --stage2b --stage3 --processes 18 && python fitting.py --obs capers_uds_p4 && conda activate base && python plot_slits.py --obs capers_uds_p4 --approve-shifts && conda activate jwst && cd .. && python scripts/deploy.py --obs capers_uds_p4 && cd pipeline
python reduction.py --config config.toml --obs capers_uds_p5 --stage2b --stage3 --processes 18 && python fitting.py --obs capers_uds_p5 && conda activate base && python plot_slits.py --obs capers_uds_p5 --approve-shifts && conda activate jwst && cd .. && python scripts/deploy.py --obs capers_uds_p5 && cd pipeline

