# Relay image: python3 (the relay) + node/npx with wrangler preinstalled.
#
# wrangler is installed HERE rather than bind-mounted because the Worker's
# checkout has no node_modules -- `npx wrangler` would otherwise fetch wrangler
# from npm on every start, which needs network at exactly the moment you least
# want it.
#
# The OAuth token is NOT baked into the image. It is bind-mounted from the
# host's wrangler config at run time (see docker-compose.yml), because an image
# layer is a terrible place to keep a credential.
FROM node:22-alpine

# tini reaps the wrangler child and, more importantly, forwards SIGTERM so
# `docker stop` reaches the relay's finally block instead of being swallowed by
# PID 1. python3 is the relay itself; curl is not needed (healthcheck is a
# python script, not an HTTP probe).
RUN apk add --no-cache python3 tini

RUN npm install -g wrangler@4 && wrangler --version

WORKDIR /app
COPY relay.py metrics.py event_log.py relay_lock.py health.py \
     provision_grafana.py ./

# State files live on the host mount at /state (see compose). Redirecting them
# here is what keeps the counter memory alive across container replacement.
# RELAY_WORKER_DIR is the observed Worker's own checkout, mounted read-only at
# /worker -- a different repository entirely, which is why it is a mount and not
# a path relative to this one.
ENV RELAY_STATE=/state/state.json \
    RELAY_LOG=/state/events.log \
    RELAY_LOCK=/state/relay.lock \
    RELAY_WORKER_DIR=/worker \
    CF_WORKER=netrunner-linear-webhook

# tini as PID 1; exec so the relay becomes PID 1's child and receives signals.
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["python3", "-u", "relay.py"]