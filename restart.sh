#!/bin/bash
set -e
cd /home/ubuntu/projects/Plain-facts/PlainFacts-OnlineTool
docker compose up -d --build
docker compose ps
