#!/bin/zsh

# python reduction.py --config config_new.toml --obs rubies_uds_p11 --stage2a --processes 12
# python reduction.py --config config_new.toml --obs rubies_uds_p12 --stage2a --processes 12
# python reduction.py --config config_new.toml --obs rubies_uds_p13 --stage2a --processes 12
# python reduction.py --config config_new.toml --obs rubies_uds_p21 --stage2a --processes 12
# python reduction.py --config config_new.toml --obs rubies_uds_p22 --stage2a --processes 12
# python reduction.py --config config_new.toml --obs rubies_uds_p23 --stage2a --processes 12

python reduction.py --config config.toml --obs capers_cosmos_p1 --stage1 --processes 6 --overwrite
python reduction.py --config config.toml --obs capers_cosmos_p2 --stage1 --processes 6 --overwrite
python reduction.py --config config.toml --obs capers_cosmos_p3 --stage1 --processes 6 --overwrite
python reduction.py --config config.toml --obs capers_cosmos_p4 --stage1 --processes 6 --overwrite
python reduction.py --config config.toml --obs capers_cosmos_p5 --stage1 --processes 6 --overwrite
python reduction.py --config config.toml --obs capers_cosmos_p6 --stage1 --processes 6 --overwrite
python reduction.py --config config.toml --obs capers_cosmos_p7 --stage1 --processes 6 --overwrite

python reduction.py --config config.toml --obs capers_cosmos_p1 --stage2a --processes 18 --overwrite
python reduction.py --config config.toml --obs capers_cosmos_p2 --stage2a --processes 18 --overwrite
python reduction.py --config config.toml --obs capers_cosmos_p3 --stage2a --processes 18 --overwrite
python reduction.py --config config.toml --obs capers_cosmos_p4 --stage2a --processes 18 --overwrite
python reduction.py --config config.toml --obs capers_cosmos_p5 --stage2a --processes 18 --overwrite
python reduction.py --config config.toml --obs capers_cosmos_p6 --stage2a --processes 18 --overwrite
python reduction.py --config config.toml --obs capers_cosmos_p7 --stage2a --processes 18 --overwrite

