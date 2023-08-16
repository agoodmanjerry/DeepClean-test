#!/bin/bash

# Run exporter in a tmux session
tmux new-session -s pilot-test -d "CUDA_VISIBLE_DEVICES=0 PROJECT=exporter pinto -p exporter run -e ../.env flask --app=exporter run"

# tmux attach -t pilot-test
# C-b %

CUDA_VISIBLE_DEVICES=0 PROJECT=trainer && PROJECT_DIR=$HOME/deepclean/$PROJECT DATA_DIR=${HOME}/kagra-o4-data IFO=K1 pinto -p trainer run -e ../.env train --typeo pyproject.toml script=train architecture=autoencoder