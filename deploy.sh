#!/bin/bash
set -e
cd /home/ubuntu/projects/Plain-facts/PlainFacts-OnlineTool
docker compose down
docker compose build
docker compose up -d
docker compose ps
