# Deployment Guide

## Requirements

Python 3.12+

## Clone Repository

git clone https://github.com/YOURNAME/sarasvati-signals.git

cd sarasvati-signals

## Install Dependencies

pip install -r requirements.txt

## Run Application

uvicorn app.main:app --host 0.0.0.0 --port 8000

## Access Dashboard

Dashboard:
http://localhost:8000

Propagation:
http://localhost:8000/propagation

Global:
http://localhost:8000/global

MUF:
http://localhost:8000/mufmap

Path:
http://localhost:8000/path

Space Weather:
http://localhost:8000/space-weather

Timelapse:
http://localhost:8000/sdo-timelapse