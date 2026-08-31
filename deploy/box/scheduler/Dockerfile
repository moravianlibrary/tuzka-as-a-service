# Up-hours scheduler for a box: starts/stops the engine + tunnel on a weekly schedule.
# Needs the docker CLI (it drives the host daemon through the mounted socket) and tzdata
# so TZ=Europe/Prague resolves inside the container.
FROM python:3.13-alpine

RUN apk add --no-cache docker-cli tzdata

WORKDIR /app
COPY schedule.py /app/schedule.py

# Root: the mounted docker socket is root-owned on most hosts. The process only ever
# runs `docker ps|start|stop` against its own compose project.
ENTRYPOINT ["python", "-u", "/app/schedule.py"]
