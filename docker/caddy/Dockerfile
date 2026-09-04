# syntax=docker/dockerfile:1.9
#
# Thin overlay on the pinned official Caddy Alpine image that bakes in the
# Caddyfile (the public-edge reverse-proxy routes). The official image default
# CMD is `caddy run --config /etc/caddy/Caddyfile --adapter caddyfile`, so
# COPYing the Caddyfile to that exact path needs no CMD/ENTRYPOINT override.
#
# No volumes: the Caddyfile is baked, not bind-mounted. A Caddyfile edit now
# requires `docker compose build proxy` before `make start` (the image name no
# longer changes, so up -d without --build reuses the baked config). See
# docker/caddy/Caddyfile for the route table.
#
# Build:  docker compose build proxy
# Base bump: change CADDY_IMAGE_TAG in .env (rides the image: name -> forces
#           a rebuild). Pinned to a specific patch (2.11.4-alpine), not a
#           floating tag -- a floating tag would let the base drift without a
#           rebuild now that make pull skips this service (pull_policy: never).
#           ARG has no default; an empty value fails loud (invalid reference
#           format) -- the compose-level :? guard fires first.
ARG CADDY_IMAGE_TAG
FROM caddy:${CADDY_IMAGE_TAG}

COPY Caddyfile /etc/caddy/Caddyfile