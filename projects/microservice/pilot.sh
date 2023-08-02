#!/bin/bash

# Run exporter in a tmux session
tmux new-session -s pilot-test -d "CUDA_VISIBLE_DEVICES=0 PROJECT=exporter pinto -p exporter run -e ../.env flask --app=exporter run"

tmux split-window -v "CUDA_VISIBLE_DEVICES=0 PROJECT= trainer PROJECT_DIR=$HOME/deepclean/$PROJECT DATA_DIR=/data/KAGRA_O4a_data pinto -p trainer run -e ../.env train --typeo pyproject.toml script=train architecture=autoencoder"