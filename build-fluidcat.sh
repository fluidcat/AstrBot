#! /bin/bash

docker pull soulter/astrbot:latest

docker build -f Dockerfile_fluidcat -t soulter/astrbot:fluidcat .