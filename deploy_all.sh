#!/bin/zsh

python scripts/deploy.py --obs rubies_uds_p11 --no-rgb --no-sed --auto-approve
python scripts/deploy.py --obs rubies_uds_p12 --no-rgb --no-sed --auto-approve
python scripts/deploy.py --obs rubies_uds_p13 --no-rgb --no-sed --auto-approve
